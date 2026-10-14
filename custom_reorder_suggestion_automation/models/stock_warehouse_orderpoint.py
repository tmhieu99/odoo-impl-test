from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

BLANK_SUGGESTION = {
    'reorder_min': 0.0,
    'reorder_max': 0.0,
    'reorder_to_order': 0.0,
    'reorder_forecast': 0.0,
    'reorder_rgd': 0.0,
    'reorder_wad': 0.0,
    'reorder_wad_is_total': False,
}


class StockWarehouseOrderpoint(models.Model):
    _inherit = 'stock.warehouse.orderpoint'

    reorder_vendor_id = fields.Many2one(
        'res.partner',
        string='Suggested Vendor',
        compute='_compute_reorder_vendor_id',
        help="Preferred vendor for this orderpoint's company: the first vendor line "
             "(company-specific or shared) in the product's vendor list order.",
        store=True
    )

    # Plain stored columns rather than computed fields: they are produced by
    # the nightly "Re-Order Suggestions: recompute" scheduled action, not by
    # the ORM's dependency graph. See `_cron_update_reorder_suggestions`.
    reorder_min = fields.Float(
        string='Suggested Min',
        readonly=True,
        copy=False,
        help='Auto-calculated reorder point (ROP): Greatest WAD × (vendor '
             'lead-time weeks + safety factor weeks). Refreshed nightly.',
    )
    reorder_max = fields.Float(
        string='Suggested Max',
        readonly=True,
        copy=False,
        help='Auto-calculated order up-to quantity: Forecast + Final '
             'To-Order Quantity. Refreshed nightly.',
    )
    reorder_to_order = fields.Float(
        string='Suggested To Order',
        readonly=True,
        copy=False,
        help='Auto-calculated to-order quantity (Final TOQ), rounded up to the '
             "vendor's standard case quantity. Zero when the product is not "
             'below its reorder point. Refreshed nightly.',
    )
    reorder_forecast = fields.Float(
        string='Forecast (excl. RFQs)',
        readonly=True,
        copy=False,
        digits='Product Unit of Measure',
        help="The product's Forecasted quantity in this warehouse, which the "
             'suggestion is calculated from: on hand plus every open incoming '
             'move minus every open outgoing move, whatever its date. Open RFQs '
             '(draft, sent, to approve) are not counted; a purchase counts once '
             'it is confirmed. Refreshed nightly.',
    )

    reorder_product_route_ids = fields.Many2many(
        'stock.route',
        string='Routes',
        related='product_id.route_ids',
        readonly=True,
    )

    reorder_description = fields.Char(
        string='Description',
        compute='_compute_reorder_display_fields',
    )

    reorder_on_order = fields.Float(
        string='On Order (Warehouse)',
        compute='_compute_reorder_display_fields',
        digits='Product Unit of Measure',
    )

    # --- Diagnostic columns (engine output, not PDF requirements) ---
    # Same nightly run as Min / Max / To Order: they come out of the one
    # `_collect_wad` call, so storing them costs nothing extra and keeps the
    # list view from re-running the engine every time a column is unfolded.
    reorder_rgd = fields.Float(
        string='RGD (12 months)',
        readonly=True,
        copy=False,
        digits='Product Unit of Measure',
        help='Raw-Goods Demand over the last 365 days as the calculation engine '
             'sees it: direct sales of this product plus its share of every '
             'confirmed sale of a parent that consumes it. Diagnostic column '
             'for verifying the engine; the Min / Max / To Order values come '
             'from the later stages.',
    )
    reorder_wad = fields.Float(
        string='WAD (weekly)',
        readonly=True,
        copy=False,
        digits='Product Unit of Measure',
        help='Weekly Average Demand: the greatest of the 12-, 6- and 3-month '
             'averages, so a recent surge is never averaged away by a quiet '
             'year. Products younger than 91 days use their total demand '
             'divided by the weeks they have existed instead.',
    )
    reorder_wad_is_total = fields.Boolean(
        string='New Product WAD',
        readonly=True,
        copy=False,
        help='This product is younger than 91 days, so its weekly demand comes '
             'from its total sales divided by its age rather than from the '
             'three standard windows.',
    )

    @api.depends('product_id', 'company_id', 'product_id.seller_ids.partner_id',
                 'product_id.seller_ids.company_id', 'product_id.seller_ids.sequence',
                 'product_id.seller_ids.product_id')
    def _compute_reorder_vendor_id(self):
        for orderpoint in self:
            product = orderpoint.product_id.with_company(orderpoint.company_id)
            orderpoint.reorder_vendor_id = product._prepare_sellers()[:1].partner_id

    # -------------------------------------------------------------------------
    # Nightly recalculation
    # -------------------------------------------------------------------------
    @api.model
    def _cron_update_reorder_suggestions(self):
        """Entry point of the "Re-Order Suggestions: recompute" scheduled action.

        The engine reads a year of confirmed sale lines and explodes BOMs, so
        hanging it off `qty_forecast` would drag the whole calculation into
        every stock move, purchase confirmation and sale confirmation. Weekly
        Average Demand moves on a weekly scale, so a nightly batch is accurate
        enough while keeping the cost off the transactional paths.
        """
        self._create_missing_reorder_suggestion_orderpoints()
        self.search([])._update_reorder_suggestions()

    @api.model
    def _create_missing_reorder_suggestion_orderpoints(self):
        """Give every flagged SKU a row in each company's warehouse stock.

        The suggestion page only lists existing orderpoints, and native Odoo
        creates them only when the Replenishment page is opened with a negative
        forecast — so a flagged SKU sitting between 0 and its ROP, or one with
        no vendor (which the page must still show with a warning), would never
        appear. The flag alone decides inclusion (Phase 1, item field).

        An orderpoint that already exists for the product/location/company is
        reused, archived ones included: there can be only one
        (`product_location_check`), and an archived one means someone switched
        it off on purpose. Rows are built like native's own
        (`_get_orderpoint_values`, `_set_default_route_id`) but owned by the
        module's technical user, so native clean-up of SUPERUSER-created manual
        rows leaves them alone.
        """
        products = self.env['product.product'].sudo().search(
            [('automated_reorder_suggestions', '=', True)])
        warehouses = self.env['stock.warehouse'].sudo().search([])
        if not products or not warehouses:
            return self.browse()

        existing = self.sudo().with_context(active_test=False).search_read(
            [('product_id', 'in', products.ids),
             ('location_id', 'in', warehouses.lot_stock_id.ids)],
            ['product_id', 'location_id', 'company_id'])
        taken = {
            (row['product_id'][0], row['location_id'][0], row['company_id'][0])
            for row in existing
        }

        vals_list = []
        for warehouse in warehouses:
            company = warehouse.company_id
            location = warehouse.lot_stock_id
            for product in products:
                if product.company_id and product.company_id != company:
                    continue
                if (product.id, location.id, company.id) in taken:
                    continue
                values = self._get_orderpoint_values(product.id, location.id)
                values.update({
                    'name': _('Re-Order Suggestion'),
                    'warehouse_id': warehouse.id,
                    'company_id': company.id,
                })
                vals_list.append(values)
        if not vals_list:
            return self.browse()

        owner = self.env.ref(
            'custom_reorder_suggestion_automation.user_reorder_suggestion')
        orderpoints = self.with_user(owner).sudo().create(vals_list)
        orderpoints._set_default_route_id()
        for orderpoint in orderpoints:
            orderpoint.qty_multiple = orderpoint._get_qty_multiple_to_order()
        return orderpoints.with_env(self.env)

    def _update_reorder_suggestions(self):
        """Recalculate and store Min / Max / To Order and the diagnostics."""
        engine = self.env['custom.reorder.engine']
        date_from = fields.Datetime.now() - timedelta(days=365)
        blank = self.env['stock.warehouse.orderpoint']

        for company, orderpoints in self.grouped('company_id').items():
            suggested = orderpoints.filtered(
                lambda orderpoint: orderpoint.product_id.automated_reorder_suggestions)
            blank |= orderpoints - suggested
            if not company:
                blank |= suggested
                continue

            # One WAD run per company rather than one per row.
            figures = engine._collect_wad(suggested.product_id, company, date_from)
            forecasts = suggested._reorder_forecast_by_orderpoint()
            for orderpoint in suggested:
                product = orderpoint.product_id.with_company(company)
                product_figures = figures.get(orderpoint.product_id.id) or {}
                wad = product_figures.get('wad', 0.0)
                values = {
                    'reorder_rgd': sum(product_figures.get('rgd', {}).values()),
                    'reorder_wad': wad,
                    'reorder_wad_is_total': product_figures.get('new_product', False),
                    'reorder_forecast': forecasts[orderpoint.id],
                }

                params = engine._vendor_reorder_params(product, company)
                if params:
                    template = product.product_tmpl_id
                    forecast = forecasts[orderpoint.id]
                    rop = engine._reorder_point(
                        wad, params['lead_time_weeks'], template.safety_factor)
                    final_toq = engine._final_toq(
                        wad, rop, forecast, template.inventory_turns_target,
                        params['min_quantity'], params['standard_case_quantity'])
                    values.update({
                        'reorder_min': rop,
                        'reorder_max': forecast + final_toq,
                        'reorder_to_order': final_toq,
                    })
                else:
                    # No vendor: stop the calculation and populate nothing; the
                    # page shows the vendor warning instead of values.
                    values.update({
                        'reorder_min': 0.0,
                        'reorder_max': 0.0,
                        'reorder_to_order': 0.0,
                    })
                orderpoint.write(values)

        # Rows that are no longer flagged would otherwise keep the numbers of
        # whichever run last saw them, and the Reorder button would still act
        # on them. Only the ones still holding values are written.
        blank.filtered(lambda orderpoint: any(
            orderpoint[name] != value for name, value in BLANK_SUGGESTION.items()
        )).write(BLANK_SUGGESTION)

    def _reorder_forecast_by_orderpoint(self):
        """The product's native Forecasted quantity per row: ``{orderpoint_id: qty}``.

        This is `virtual_available` in the row's warehouse, the figure on the
        product form's Forecasted button, not the Replenishment page's
        `qty_forecast`. That one differs twice over: it adds
        `_quantity_in_progress` (open RFQ lines — draft / sent / to approve),
        and it only counts moves up to the row's lead-days date. Right Weigh
        wants an RFQ to count only once confirmed (a confirmed PO is already
        here through its receipt moves) and every open move counted whatever
        its date (change request, 2026-10-07).

        One read per warehouse: the warehouse context covers all of its
        locations, and each company has its own warehouse, so this stays per
        company.
        """
        forecasts = {}
        for warehouse, orderpoints in self.grouped('warehouse_id').items():
            products = orderpoints.product_id.with_context(warehouse_id=warehouse.id)
            quantities = {
                product['id']: product['virtual_available']
                for product in products.read(['virtual_available'])
            }
            for orderpoint in orderpoints:
                forecasts[orderpoint.id] = quantities.get(orderpoint.product_id.id, 0.0)
        return forecasts

    @api.depends('product_id', 'location_id')
    def _compute_reorder_display_fields(self):
        # Quantities are scoped to the orderpoint's location like native On Hand /
        # Forecast; without it, product quantities sum every warehouse of all
        # selected companies.
        for location, orderpoints in self.grouped('location_id').items():
            products = orderpoints.product_id.with_context(location=location.id)
            incoming = {product.id: product.incoming_qty for product in products} if location else {}
            for orderpoint in orderpoints:
                orderpoint.reorder_description = orderpoint.product_id.display_name
                orderpoint.reorder_on_order = incoming.get(orderpoint.product_id.id, 0.0)

    def action_open_product_vendors(self):
        """Open the product behind a "No Vendor" row, on its Purchase tab.

        The badge is a list button rather than a warning field so that nothing
        has to be stored to render it: an empty `reorder_vendor_id` already
        says "no vendor", and the button both shows that and takes the
        purchaser where the vendor line is added.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.product_id.display_name,
            'res_model': 'product.template',
            'res_id': self.product_id.product_tmpl_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # -------------------------------------------------------------------------
    # Step 6 — order the suggested quantity, through the native flow
    # -------------------------------------------------------------------------
    def action_reorder_suggestion_replenish(self):
        """Reorder Final TOQ instead of the native computed quantity.

        Native `action_replenish` procures `qty_to_order`, which is
        `qty_to_order_manual if qty_to_order_manual else qty_to_order_computed`
        (see `_compute_qty_to_order`). Staging Final TOQ in
        `qty_to_order_manual` therefore makes the native flow order exactly the
        suggested quantity while every native behaviour is kept: PO line
        merging per vendor and company, the RedirectWarning for a product with
        no vendor/route, and the activity logged on failure. Native clears `qty_to_order_manual` again at the end
        of `action_replenish` (`action_remove_manual_qty_to_order`), so nothing
        lingers on the row.

        `qty_to_order_manual` is written directly rather than through the
        `qty_to_order` inverse, because that inverse throws the value away for
        rows whose trigger is 'auto'.

        A separate method rather than an override of `action_replenish`: the
        native replenishment page must keep ordering the native quantity
        ("Native replenishment page and functionality shall remain intact and
        undisturbed", Phase 1 step 6b), and only this page's button should
        order the suggestion.

        The selected rows are recalculated first: the stored suggestion is only
        as fresh as last night's run, so without this a purchaser could order
        against a To Order that a receipt — or an earlier click of this very
        button — has already satisfied.
        """
        self._update_reorder_suggestions()
        to_replenish = self.filtered(
            lambda orderpoint: orderpoint.reorder_to_order > 0.0
            and orderpoint.reorder_vendor_id)
        if not to_replenish:
            raise UserError(_(
                'Nothing to order: these products are not below their reorder '
                'point, or have no vendor to order from.'))

        for orderpoint in to_replenish:
            orderpoint.qty_to_order_manual = orderpoint.reorder_to_order

        # One native run per company: `action_replenish` procures under
        # `self.env.company`, so a mixed selection would otherwise be pushed
        # through whichever company the user happens to have selected.
        now = self.env.cr.now()
        # Read before the run: native `action_replenish` ends by deleting the
        # SUPERUSER-owned manual rows it has just satisfied, which also empties
        # `orderpoint_id` on their new PO lines.
        products = to_replenish.product_id
        companies = to_replenish.company_id
        for company, orderpoints in to_replenish.grouped('company_id').items():
            orderpoints.with_company(company).action_replenish()
        return self._reorder_suggestion_notification(products, companies, now)

    @api.model
    def _reorder_suggestion_notification(self, products, companies, written_after):
        """Toast listing the POs the bulk Reorder created or added lines to.

        Native only notifies for a single row
        (`_get_replenishment_order_notification`); this covers any selection.
        Lines are matched on product, company and the `written_after` time
        native uses — not on `orderpoint_id`, which native clears when it
        deletes a satisfied row it generated itself. A line merged into an open
        draft RFQ counts as an update of that RFQ, not a new PO.
        """
        lines = self.env['purchase.order.line'].search([
            ('product_id', 'in', products.ids),
            ('company_id', 'in', companies.ids),
            ('write_date', '>=', written_after),
        ])
        orders = lines.order_id.sorted('id')
        created = orders.filtered(lambda order: order.create_date >= written_after)
        updated = orders - created

        parts = []
        if created:
            parts.append(_('%(count)s purchase order(s) created', count=len(created)))
        if updated:
            parts.append(_('%(count)s existing draft(s) updated', count=len(updated)))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': ', '.join(parts) or _('No purchase order generated'),
                # One '%s' per link, as native's notification does.
                'message': ' '.join(['%s'] * len(created | updated)),
                'links': [{
                    'label': order.display_name,
                    'url': f'/odoo/action-purchase.action_rfq_form/{order.id}',
                } for order in created | updated],
                'type': 'success' if orders else 'warning',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }
