from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockWarehouseOrderpoint(models.Model):
    _inherit = 'stock.warehouse.orderpoint'

    # --- 1.2 fields (declared now, real logic deferred to 1.3 calc engine) ---
    reorder_vendor_id = fields.Many2one(
        'res.partner',
        string='Suggested Vendor',
        compute='_compute_reorder_vendor_id',
        help="Preferred vendor for this orderpoint's company: the first vendor line "
             "(company-specific or shared) in the product's vendor list order.",
        store=True
    )
    reorder_min = fields.Float(
        string='Suggested Min',
        compute='_compute_reorder_quantities',
        help='Auto-calculated reorder point (ROP): Greatest WAD × (vendor '
             'lead-time weeks + safety factor weeks).',
        store=True
    )
    reorder_max = fields.Float(
        string='Suggested Max',
        compute='_compute_reorder_quantities',
        help='Auto-calculated order up-to quantity: native Forecast + Final '
             'To-Order Quantity.',
        store=True
    )
    reorder_to_order = fields.Float(
        string='Suggested To Order',
        compute='_compute_reorder_quantities',
        help='Auto-calculated to-order quantity (Final TOQ), rounded up to the '
             "vendor's standard case quantity. Zero when the product is not "
             'below its reorder point.',
        store=True
    )

    # --- Mockup display columns (no native orderpoint equivalent) ---
    reorder_description = fields.Char(
        string='Description',
        compute='_compute_reorder_display_fields',
    )
    reorder_on_order = fields.Float(
        string='On Order',
        compute='_compute_reorder_display_fields',
        digits='Product Unit of Measure',
    )

    # --- Diagnostic columns (engine output, not PDF requirements) ---
    reorder_rgd = fields.Float(
        string='RGD (12 months)',
        compute='_compute_reorder_diagnostics',
        digits='Product Unit of Measure',
        help='Raw-Goods Demand over the last 365 days as the calculation engine '
             'sees it: direct sales of this product plus its share of every '
             'confirmed sale of a parent that consumes it. Diagnostic column '
             'for verifying the engine; the stored Min / Max / To Order values '
             'come from the later stages.',
    )
    reorder_wad = fields.Float(
        string='WAD (weekly)',
        compute='_compute_reorder_diagnostics',
        digits='Product Unit of Measure',
        help='Weekly Average Demand: the greatest of the 12-, 6- and 3-month '
             'averages, so a recent surge is never averaged away by a quiet '
             'year. Products younger than 91 days use their total demand '
             'divided by the weeks they have existed instead.',
    )
    reorder_wad_is_total = fields.Boolean(
        string='New Product WAD',
        compute='_compute_reorder_diagnostics',
        help='This product is younger than 91 days, so its weekly demand comes '
             'from its total sales divided by its age rather than from the '
             'three standard windows.',
    )

    @api.depends('product_id', 'company_id')
    def _compute_reorder_diagnostics(self):
        # One engine run per company rather than one per row, and one run for
        # all three columns rather than one each.
        engine = self.env['custom.reorder.engine']
        date_from = fields.Datetime.now() - timedelta(days=365)
        for company, orderpoints in self.grouped('company_id').items():
            figures = {}
            if company:
                figures = engine._collect_wad(
                    orderpoints.product_id, company, date_from)
            for orderpoint in orderpoints:
                product_figures = figures.get(orderpoint.product_id.id) or {}
                orderpoint.reorder_rgd = sum(product_figures.get('rgd', {}).values())
                orderpoint.reorder_wad = product_figures.get('wad', 0.0)
                orderpoint.reorder_wad_is_total = product_figures.get('new_product', False)

    @api.depends('product_id', 'company_id', 'product_id.seller_ids.partner_id',
                 'product_id.seller_ids.company_id', 'product_id.seller_ids.sequence',
                 'product_id.seller_ids.product_id')
    def _compute_reorder_vendor_id(self):
        for orderpoint in self:
            product = orderpoint.product_id.with_company(orderpoint.company_id)
            orderpoint.reorder_vendor_id = product._prepare_sellers()[:1].partner_id

    @api.depends('product_id', 'company_id', 'qty_forecast',
                 'product_id.inventory_turns_target', 'product_id.safety_factor',
                 'product_id.seller_ids.lead_time_weeks',
                 'product_id.seller_ids.min_qty',
                 'product_id.seller_ids.standard_case_quantity',
                 'product_id.seller_ids.sequence',
                 'product_id.seller_ids.company_id',
                 'product_id.seller_ids.product_id')
    def _compute_reorder_quantities(self):
        # One WAD run per company rather than one per row.
        engine = self.env['custom.reorder.engine']
        date_from = fields.Datetime.now() - timedelta(days=365)
        for company, orderpoints in self.grouped('company_id').items():
            figures = engine._collect_wad(
                orderpoints.product_id, company, date_from) if company else {}

            for orderpoint in orderpoints:
                product = orderpoint.product_id.with_company(company)
                params = engine._vendor_reorder_params(product, company) if company else None

                if not params:
                    # No vendor: stop the calculation and populate nothing; the
                    # page shows the vendor warning and "-" instead of values.
                    orderpoint.reorder_min = 0.0
                    orderpoint.reorder_max = 0.0
                    orderpoint.reorder_to_order = 0.0
                    continue

                template = product.product_tmpl_id
                wad = (figures.get(orderpoint.product_id.id) or {}).get('wad', 0.0)
                forecast = orderpoint.qty_forecast

                rop = engine._reorder_point(
                    wad, params['lead_time_weeks'], template.safety_factor)
                final_toq = engine._final_toq(
                    wad, rop, forecast, template.inventory_turns_target,
                    params['min_quantity'], params['standard_case_quantity'])

                orderpoint.reorder_min = rop
                orderpoint.reorder_max = forecast + final_toq
                orderpoint.reorder_to_order = final_toq

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
        merging per vendor and company, the "PO created" notification, the
        RedirectWarning for a product with no vendor/route, and the activity
        logged on failure. Native clears `qty_to_order_manual` again at the end
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
        """
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
        notification = False
        for company, orderpoints in to_replenish.grouped('company_id').items():
            result = orderpoints.with_company(company).action_replenish()
            if len(to_replenish) == 1:
                notification = result
        return notification
