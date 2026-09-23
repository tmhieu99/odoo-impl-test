import logging
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Circular BOM data would otherwise recurse forever; any SKU that reaches this
# depth is logged instead of silently truncated.
MAX_BOM_DEPTH = 10

# Longest demand window the WAD calculation needs (12 months).
DEMAND_WINDOW_DAYS = 365

# Demand windows, in days, mapped to the number of weeks each spans. The
# greatest of the three averages wins.
WAD_WINDOWS = {365: 52.0, 182: 26.0, 91: 13.0}

# Below this age a product has no meaningful 3-month average, so its total
# demand is divided by the weeks it has existed instead (Total WAD).
NEW_PRODUCT_DAYS = 91


class ReorderEngine(models.AbstractModel):
    """Re-order suggestion calculation engine.

    Stage 1 turns confirmed sales orders into per-product, per-date raw-goods
    demand in each product's reference UoM; stage 2a averages that into a
    weekly demand figure. Everything here returns plain dicts and never
    writes — the later stages (ROP / TOQ, write-back) consume this output.
    """

    _name = 'custom.reorder.engine'
    _description = 'Reorder suggestion calculation engine'

    # -------------------------------------------------------------------------
    # Eligibility: the run is driven by the Automated Reorder Suggestions flag
    # -------------------------------------------------------------------------
    @api.model
    def _eligible_products(self, company):
        """Variants of every product flagged for automated reorder suggestions.

        The flag lives on product.template and is already gated on Track
        Inventory / Can be Purchased / Buy route by the model's own
        create()/write() reset, so no prerequisite re-check is needed here.
        """
        templates = self.env['product.template'].with_company(company).search([
            ('automated_reorder_suggestions', '=', True),
        ])
        return templates.product_variant_ids

    @api.model
    def _demand_sources(self, tracked, company):
        """The tracked products plus every product whose BOM consumes one of
        them, transitively.

        Flagged products are the *output* set, not the demand filter: a flagged
        component earns most of its demand from sales of unflagged parents (a
        manufactured finished good is never flagged for purchasing). Those
        ancestors are read as demand carriers only — they never get a row of
        their own unless they are themselves flagged.
        """
        BomLine = self.env['mrp.bom.line']
        sources = tracked
        frontier = tracked
        for _depth in range(MAX_BOM_DEPTH):
            if not frontier:
                break
            lines = BomLine.search([
                ('product_id', 'in', frontier.ids),
                ('bom_id.active', '=', True),
                '|', ('company_id', '=', False), ('company_id', '=', company.id),
            ])
            boms = lines.bom_id
            parents = boms.product_id | boms.filtered(
                lambda bom: not bom.product_id
            ).product_tmpl_id.product_variant_ids
            # Subtracting what we already hold also breaks BOM cycles.
            frontier = parents - sources
            sources |= frontier
        return sources

    # -------------------------------------------------------------------------
    # Raw demand rows
    # -------------------------------------------------------------------------
    @api.model
    def _sold_lines(self, sources, company, date_from):
        """Confirmed sale order lines that can contribute demand, as raw rows.

        One query for the whole window; bucketing by date happens in Python.
        """
        if not sources:
            return []
        # Raw SQL bypasses the ORM cache, so pending ORM writes must hit the
        # database first.
        self.env.flush_all()

        dropship_route = self.env.ref(
            'stock_dropshipping.route_drop_shipping', raise_if_not_found=False)
        params = {
            'company_id': company.id,
            'date_from': date_from,
            'source_ids': tuple(sources.ids),
        }
        # Dropship demand is fulfilled by the vendor, not from inventory. The
        # route is a many2one on the line (there is no m2m relation table).
        # This excludes lines that explicitly carry it; `route_id` is selected
        # too, so the caller can apply the product-level route only to lines
        # that name no route of their own.
        dropship_clause = ''
        if dropship_route:
            dropship_clause = 'AND (sol.route_id IS NULL OR sol.route_id != %(dropship_route_id)s)'
            params['dropship_route_id'] = dropship_route.id

        self.env.cr.execute(f"""
            SELECT sol.product_id      AS product_id,
                   sol.product_uom     AS uom_id,
                   sol.product_uom_qty AS qty,
                   sol.route_id        AS route_id,
                   so.date_order       AS demand_date
              FROM sale_order_line sol
              JOIN sale_order so ON so.id = sol.order_id
             WHERE so.state = 'sale'
               AND so.company_id = %(company_id)s
               AND so.date_order >= %(date_from)s
               AND sol.product_uom_qty > 0
               AND sol.product_id IN %(source_ids)s
               {dropship_clause}
        """, params)
        return self.env.cr.dictfetchall()

    # -------------------------------------------------------------------------
    # BOM explosion
    # -------------------------------------------------------------------------
    @api.model
    def _explosion_context(self, company):
        """Per-run state for the BOM walk.

        Built once per company run: the two route ids the leaf test needs, and
        the memo of already-expanded products. Resolving the external ids here
        keeps them out of the recursion, which visits a product once per BOM
        line that mentions it.
        """
        buy_route = self.env.ref(
            'purchase_stock.route_warehouse0_buy', raise_if_not_found=False)
        manufacture_route = self.env.ref(
            'mrp.route_warehouse0_manufacture', raise_if_not_found=False)
        return {
            'company': company,
            'buy_route_id': buy_route.id if buy_route else False,
            'manufacture_route_id': manufacture_route.id if manufacture_route else False,
            'factors': {},
        }

    @api.model
    def _is_purchased_parent(self, product, context=None):
        """A parent that is bought rather than manufactured is not exploded.

        "Enabled" means the route is on the product itself, the same reading
        Step 1 uses for the Buy-route prerequisite (category routes excluded).
        `context` comes from `_explosion_context`; without one the routes are
        resolved on the spot, so this stays usable as a standalone check.
        """
        if context is None:
            context = self._explosion_context(self.env.company)
        route_ids = product.route_ids.ids
        buy_route_id = context['buy_route_id']
        manufacture_route_id = context['manufacture_route_id']
        return bool(
            buy_route_id and buy_route_id in route_ids
            and not (manufacture_route_id and manufacture_route_id in route_ids)
        )

    @api.model
    def _find_bom(self, product, company):
        """The BOM this engine explodes: the **newest active** one.

        Native `_bom_find` picks by `(sequence, product_id, id)`; Right Weigh's
        rule is the most recently created BOM instead (user decision,
        2026-09-18) — 71 products carry more than one active BOM in production.
        The domain is the native one, so variant/template matching, company
        scoping and the active filter behave exactly as Odoo's own lookup;
        only the ordering differs. `id` breaks ties, since every record created
        in one transaction shares a `create_date`.

        No `bom_type` filter: kits (phantom) are explodable too.
        """
        if product.type == 'service':
            return self.env['mrp.bom']
        Bom = self.env['mrp.bom']
        domain = Bom._bom_find_domain(product, company_id=company.id)
        return Bom.search(domain, order='create_date desc, id desc', limit=1)

    @api.model
    def _component_factors(self, product, context, depth=0, path=frozenset()):
        """Raw-goods consumed per 1 unit of `product`, in each component's own
        reference UoM: ``{product_id: qty}``.

        A product with no BOM (or a purchased parent) is its own demand.
        """
        cache = context['factors']
        if product.id in cache:
            return cache[product.id]
        if product.id in path:
            _logger.warning(
                'Reorder engine: circular BOM detected at product %s (id %s); '
                'stopping the explosion here.', product.display_name, product.id)
            return {product.id: 1.0}
        if depth >= MAX_BOM_DEPTH:
            _logger.warning(
                'Reorder engine: BOM depth limit (%s) reached at product %s (id %s); '
                'deeper components are not counted.', MAX_BOM_DEPTH, product.display_name, product.id)
            return {product.id: 1.0}
        # A leaf's factors never depend on the path, so memoise them — otherwise
        # every purchased component is recomputed once per BOM line naming it.
        if self._is_purchased_parent(product, context):
            cache[product.id] = {product.id: 1.0}
            return cache[product.id]

        # Kits (phantom) are exploded too: the sale order line keeps the kit
        # product — only the stock moves explode — so kit components are real
        # demand that would otherwise be missed entirely.
        bom = self._find_bom(product, context['company'])
        if not bom or not bom.bom_line_ids or not bom.product_qty:
            cache[product.id] = {product.id: 1.0}
            return cache[product.id]

        # The header quantity is expressed in the BOM's own UoM: a BOM
        # producing 10 units with a line of 3 contributes 0.3 per parent unit.
        header_factor = product.uom_id._compute_quantity(
            1.0, bom.product_uom_id, round=False) / bom.product_qty

        factors = defaultdict(float)
        for line in bom.bom_line_ids:
            if line._skip_bom_line(product):
                continue
            component = line.product_id
            line_qty = line.product_uom_id._compute_quantity(
                line.product_qty * header_factor, component.uom_id, round=False)
            sub_factors = self._component_factors(
                component, context, depth + 1, path | {product.id})
            for component_id, component_qty in sub_factors.items():
                factors[component_id] += component_qty * line_qty

        factors = dict(factors)
        cache[product.id] = factors
        return factors

    # -------------------------------------------------------------------------
    # Stage 1 entry point
    # -------------------------------------------------------------------------
    @api.model
    def _collect_rgd(self, tracked, company, date_from):
        """Raw-Goods Demand per tracked product: ``{product_id: {date: qty}}``.

        Quantities are in each tracked product's reference UoM; dates are the
        order's `date_order` day.
        """
        if not tracked:
            return {}
        rows = self._sold_lines(self._demand_sources(tracked, company), company, date_from)
        if not rows:
            return {}

        tracked_ids = set(tracked.ids)
        products = self.env['product.product'].browse(
            {row['product_id'] for row in rows}).exists()
        uoms = self.env['uom.uom'].browse({row['uom_id'] for row in rows}).exists()
        products_by_id = {product.id: product for product in products}
        uoms_by_id = {uom.id: uom for uom in uoms}

        # A product carrying the dropship route is fulfilled by the vendor even
        # when the order line itself names no route.
        dropship_route = self.env.ref(
            'stock_dropshipping.route_drop_shipping', raise_if_not_found=False)
        dropshipped_ids = set()
        if dropship_route:
            dropshipped_ids = set(products.filtered(
                lambda product: dropship_route.id in product.route_ids.ids).ids)

        context = self._explosion_context(company)
        demand = defaultdict(lambda: defaultdict(float))
        for row in rows:
            # A line that names its own route has already been vetted by the
            # query; the product's route only decides when the line names none.
            if row['route_id'] is None and row['product_id'] in dropshipped_ids:
                continue
            product = products_by_id.get(row['product_id'])
            uom = uoms_by_id.get(row['uom_id'])
            if not product or not uom:
                continue
            qty = uom._compute_quantity(row['qty'], product.uom_id, round=False)
            demand_date = row['demand_date'].date()
            factors = self._component_factors(
                product.with_company(company), context)
            for component_id, factor in factors.items():
                if component_id in tracked_ids:
                    demand[component_id][demand_date] += qty * factor
        return {product_id: dict(dated) for product_id, dated in demand.items()}

    # -------------------------------------------------------------------------
    # Stage 2a — Weekly Average Demand
    # -------------------------------------------------------------------------
    @api.model
    def _product_age_days(self, product, dated_qty, today=None):
        """How long this product has really existed, in days.

        `create_date` alone is the date the *record* appeared in Odoo, not the
        date the product did. When a catalogue is imported at go-live every
        product carries the import date, so a SKU with a year of imported
        history would look days old — and Total WAD would divide that year of
        demand by a week or two, over-ordering by an order of magnitude.

        Taking the earliest of `create_date` and the product's own oldest
        demand makes imported history self-correcting: a product with sales
        from 300 days ago is at least 300 days old whatever its record says.
        The variant's `create_date` is used, not the template's, because demand
        is counted per variant.
        """
        today = today or fields.Date.today()
        created = product.create_date
        earliest = created.date() if created else today
        if dated_qty:
            earliest = min(earliest, min(dated_qty))
        return max(0, (today - earliest).days)

    @api.model
    def _compute_wad(self, dated_qty, product, today=None):
        """Weekly Average Demand for one product, from its dated demand.

        Two paths, as the specification has it:

        - **Greatest WAD** — the demand inside each of the 365 / 182 / 91-day
          windows divided by 52 / 26 / 13 weeks; the largest of the three wins,
          so a recent surge is never averaged away by a quiet year.
        - **Total WAD** — for a product younger than 91 days, total demand
          divided by the weeks it has existed, since no 3-month average exists
          yet.

        Pure: it reads `product.create_date` and nothing else from the
        database, so it can be checked against hand-computed values.
        """
        if not dated_qty:
            return 0.0
        today = today or fields.Date.today()
        age_days = self._product_age_days(product, dated_qty, today)

        if age_days < NEW_PRODUCT_DAYS:
            # The floor keeps a product created today from dividing by zero; it
            # makes that product's WAD equal its whole demand, which is
            # aggressive but bounded.
            weeks = max(1.0, age_days / 7.0)
            return sum(dated_qty.values()) / weeks

        wads = []
        for window, weeks in WAD_WINDOWS.items():
            total = sum(qty for day, qty in dated_qty.items()
                        if (today - day).days <= window)
            wads.append(total / weeks)
        return max(wads)

    @api.model
    def _collect_wad(self, tracked, company, date_from=None, today=None):
        """``{product_id: {'wad': float, 'rgd': {date: qty}, 'new_product': bool}}``.

        One Stage 1 run, then the average per product — so a caller that needs
        both figures pays for the demand collection once.
        """
        today = today or fields.Date.today()
        if date_from is None:
            date_from = fields.Datetime.now() - timedelta(days=DEMAND_WINDOW_DAYS)
        rgd = self._collect_rgd(tracked, company, date_from)
        result = {}
        for product in tracked:
            dated_qty = rgd.get(product.id, {})
            result[product.id] = {
                'rgd': dated_qty,
                'wad': self._compute_wad(dated_qty, product, today),
                'new_product': bool(dated_qty) and self._product_age_days(
                    product, dated_qty, today) < NEW_PRODUCT_DAYS,
            }
        return result
