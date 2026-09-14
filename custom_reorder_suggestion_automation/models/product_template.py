from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

REORDER_PREREQUISITE_FIELDS = {'is_storable', 'purchase_ok', 'route_ids'}


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    automated_reorder_suggestions = fields.Boolean(
        string='Automated Reorder Suggestions',
        default=False,
        help='Include this SKU in the automated purchasing re-order suggestion flow. '
             'Only available when Track Inventory and Can be Purchased are enabled and '
             'the Buy route is selected; automatically cleared if any of those is removed.',
    )
    inventory_turns_target = fields.Float(
        string='Inventory Turns Target',
        help='Target number of times inventory should be sold and replaced per year. '
             'Required when Automated Reorder Suggestions is enabled.',
    )
    safety_factor = fields.Integer(
        string='Safety Factor',
        help='Number of weeks of demand to hold as safety stock. '
             'Required when Automated Reorder Suggestions is enabled.',
    )
    reorder_prerequisites_met = fields.Boolean(
        string='Reorder Prerequisites Met',
        compute='_compute_reorder_prerequisites_met',
        help='Technical field: True when Track Inventory, Can be Purchased, and the '
             'Buy route are all enabled on this product.',
    )

    @api.depends('is_storable', 'purchase_ok', 'route_ids')
    def _compute_reorder_prerequisites_met(self):
        buy_route = self.env.ref('purchase_stock.route_warehouse0_buy', raise_if_not_found=False)
        for product in self:
            product.reorder_prerequisites_met = bool(
                product.is_storable
                and product.purchase_ok
                and buy_route
                and buy_route.id in product.route_ids.ids
            )

    def _reset_automated_reorder_suggestions_if_unmet(self):
        to_reset = self.filtered(
            lambda p: p.automated_reorder_suggestions and not p.reorder_prerequisites_met
        )
        if to_reset:
            to_reset.write({'automated_reorder_suggestions': False})

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._reset_automated_reorder_suggestions_if_unmet()
        return records

    def write(self, vals):
        result = super().write(vals)
        trigger_fields = REORDER_PREREQUISITE_FIELDS | {'automated_reorder_suggestions'}
        if trigger_fields & set(vals.keys()):
            self._reset_automated_reorder_suggestions_if_unmet()
        return result

    @api.constrains('inventory_turns_target', 'safety_factor', 'automated_reorder_suggestions')
    def _check_reorder_suggestion_fields(self):
        for product in self:
            if product.inventory_turns_target < 0:
                raise ValidationError(_('Inventory Turns Target must be a positive value.'))
            if product.safety_factor < 0:
                raise ValidationError(_('Safety Factor must be a positive integer.'))
            if product.automated_reorder_suggestions:
                if product.inventory_turns_target <= 0:
                    raise ValidationError(_(
                        'Inventory Turns Target is required when Automated Reorder '
                        'Suggestions is enabled.'
                    ))
                if product.safety_factor <= 0:
                    raise ValidationError(_(
                        'Safety Factor is required when Automated Reorder Suggestions '
                        'is enabled.'
                    ))
