from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

# Global (not per-company) parameters: the product fields they seed are plain
# product.template fields shared by both companies.
PARAM_DEFAULT_TURNS_TARGET = 'custom_reorder_suggestion_automation.default_inventory_turns_target'
PARAM_DEFAULT_SAFETY_FACTOR = 'custom_reorder_suggestion_automation.default_safety_factor'
FALLBACK_TURNS_TARGET = 4.0
FALLBACK_SAFETY_FACTOR = 13


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    reorder_default_inventory_turns_target = fields.Float(
        string='Default Inventory Turns Target',
        config_parameter=PARAM_DEFAULT_TURNS_TARGET,
        default=FALLBACK_TURNS_TARGET,
        help='Inventory Turns Target pre-filled on new products and when Automated '
             'Reorder Suggestions is enabled on a product without one.',
    )
    reorder_default_safety_factor = fields.Integer(
        string='Default Safety Factor (Weeks)',
        config_parameter=PARAM_DEFAULT_SAFETY_FACTOR,
        default=FALLBACK_SAFETY_FACTOR,
        help='Safety Factor, in weeks, pre-filled on new products and when Automated '
             'Reorder Suggestions is enabled on a product without one.',
    )

    @api.constrains('reorder_default_inventory_turns_target', 'reorder_default_safety_factor')
    def _check_reorder_suggestion_defaults(self):
        for settings in self:
            if settings.reorder_default_inventory_turns_target <= 0:
                raise ValidationError(_('Default Inventory Turns Target must be a positive value.'))
            if settings.reorder_default_safety_factor <= 0:
                raise ValidationError(_('Default Safety Factor must be a positive integer.'))
