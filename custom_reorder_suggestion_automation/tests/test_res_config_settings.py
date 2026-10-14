from odoo.exceptions import ValidationError
from odoo.tests import Form, TransactionCase, tagged

from ..models.res_config_settings import PARAM_DEFAULT_SAFETY_FACTOR, PARAM_DEFAULT_TURNS_TARGET


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderSuggestionSettings(TransactionCase):

    def test_settings_default_values(self):
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.reorder_default_inventory_turns_target, 4.0)
        self.assertEqual(settings.reorder_default_safety_factor, 13)

    def test_settings_stored_as_global_parameters(self):
        self.env['res.config.settings'].create({
            'reorder_default_inventory_turns_target': 5.5,
            'reorder_default_safety_factor': 10,
        }).execute()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        self.assertEqual(float(get_param(PARAM_DEFAULT_TURNS_TARGET)), 5.5)
        self.assertEqual(int(get_param(PARAM_DEFAULT_SAFETY_FACTOR)), 10)
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.reorder_default_inventory_turns_target, 5.5)
        self.assertEqual(settings.reorder_default_safety_factor, 10)

    def test_non_positive_settings_rejected(self):
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({'reorder_default_inventory_turns_target': 0.0})
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({'reorder_default_safety_factor': -1})

    def test_non_positive_settings_rejected_in_form(self):
        get_param = self.env['ir.config_parameter'].sudo().get_param
        for field_name, value in (
            ('reorder_default_inventory_turns_target', 0.0),
            ('reorder_default_inventory_turns_target', -2.0),
            ('reorder_default_safety_factor', 0),
            ('reorder_default_safety_factor', -3),
        ):
            with self.subTest(field=field_name, value=value):
                form = Form(self.env['res.config.settings'])
                setattr(form, field_name, value)
                with self.assertRaises(ValidationError):
                    form.save()
        # Nothing was stored, so the built-in defaults still apply
        self.assertFalse(get_param(PARAM_DEFAULT_TURNS_TARGET))
        self.assertFalse(get_param(PARAM_DEFAULT_SAFETY_FACTOR))

    def test_invalid_stored_parameters_fall_back(self):
        # A parameter edited directly under System Parameters skips the settings check
        set_param = self.env['ir.config_parameter'].sudo().set_param
        ProductTemplate = self.env['product.template']
        for turns, safety in (('0', '0'), ('-4', '-13'), ('abc', 'xyz')):
            with self.subTest(turns=turns, safety=safety):
                set_param(PARAM_DEFAULT_TURNS_TARGET, turns)
                set_param(PARAM_DEFAULT_SAFETY_FACTOR, safety)
                self.assertEqual(ProductTemplate._reorder_suggestion_defaults(), {
                    'inventory_turns_target': 4.0,
                    'safety_factor': 13,
                })

    def test_settings_view_contains_fields(self):
        arch = self.env['res.config.settings'].get_view(
            self.env.ref('purchase.res_config_settings_view_form_purchase').id)['arch']
        for name in ('reorder_default_inventory_turns_target', 'reorder_default_safety_factor'):
            self.assertIn(f'name="{name}"', arch)
