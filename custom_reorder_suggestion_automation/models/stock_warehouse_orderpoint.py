from datetime import timedelta

from odoo import _, api, fields, models


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
        help='Auto-calculated reorder point (ROP). Placeholder until the '
             're-order calculation engine (1.3) is implemented.',
        store=True
    )
    reorder_max = fields.Float(
        string='Suggested Max',
        compute='_compute_reorder_quantities',
        help='Auto-calculated order up-to quantity. Placeholder until the '
             're-order calculation engine (1.3) is implemented.',
        store=True
    )
    reorder_to_order = fields.Float(
        string='Suggested To Order',
        compute='_compute_reorder_quantities',
        help='Auto-calculated to-order quantity (Final TOQ). Placeholder until '
             'the re-order calculation engine (1.3) is implemented.',
        store=True
    )
    reorder_no_vendor = fields.Char(
        string='Vendor Warning',
        compute='_compute_reorder_vendor_id',
        help='Shows a "No Vendor" badge when the product has no vendor for this '
             'orderpoint, so no suggested vendor can be picked.',
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
            orderpoint.reorder_no_vendor = (_('No Vendor') if not orderpoint.reorder_vendor_id else False)

    @api.depends('product_id')
    def _compute_reorder_quantities(self):
        # Placeholder until the 1.3 engine produces ROP / Max / Final TOQ.
        for orderpoint in self:
            orderpoint.reorder_min = 0.0
            orderpoint.reorder_max = 0.0
            orderpoint.reorder_to_order = 0.0

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
