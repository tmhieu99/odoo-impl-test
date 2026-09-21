from lxml import etree

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import Form, TransactionCase, tagged

MODULE = 'custom_reorder_suggestion_automation'


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestProductTemplateReorderFields(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ProductTemplate = cls.env['product.template']
        cls.buy_route = cls.env.ref('purchase_stock.route_warehouse0_buy')

    def _create_product(self, **vals):
        """Product with every reorder prerequisite met, unless overridden."""
        values = {
            'name': 'Reorder Test Product',
            'type': 'consu',
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(self.buy_route.ids)],
        }
        values.update(vals)
        return self.ProductTemplate.create(values)

    def _create_enabled_product(self, **vals):
        values = {
            'automated_reorder_suggestions': True,
            'inventory_turns_target': 6.0,
            'safety_factor': 2,
        }
        values.update(vals)
        return self._create_product(**values)

    # -------------------------------------------------------------------------
    # Field definitions
    # -------------------------------------------------------------------------

    def test_field_definitions(self):
        expected = {
            # name: (type, label, stored)
            'automated_reorder_suggestions': ('boolean', 'Automated Reorder Suggestions', True),
            'inventory_turns_target': ('float', 'Inventory Turns Target', True),
            'safety_factor': ('integer', 'Safety Factor', True),
            'reorder_prerequisites_met': ('boolean', 'Reorder Prerequisites Met', False),
        }
        for name, (ftype, label, stored) in expected.items():
            with self.subTest(field=name):
                field = self.ProductTemplate._fields.get(name)
                self.assertTrue(field, f"product.template.{name} is not defined")
                self.assertEqual(field.type, ftype)
                self.assertEqual(field.string, label)
                self.assertEqual(field.store, stored)
                self.assertFalse(field.required)
                # Registered in ir.model.fields and owned by this module
                self.assertTrue(self.env['ir.model.fields']._get('product.template', name))
                self.assertTrue(self.env.ref(
                    f'{MODULE}.field_product_template__{name}', raise_if_not_found=False))
                # Reachable from variants through _inherits
                self.assertIn(name, self.env['product.product']._fields)

        self.assertEqual(
            self.ProductTemplate._fields['reorder_prerequisites_met'].compute,
            '_compute_reorder_prerequisites_met',
        )

    def test_field_defaults(self):
        product = self.ProductTemplate.create({'name': 'Defaults Product'})
        self.assertFalse(product.automated_reorder_suggestions)
        self.assertEqual(product.inventory_turns_target, 0.0)
        self.assertEqual(product.safety_factor, 0)

    def test_form_view_contains_fields(self):
        arch = etree.fromstring(self.ProductTemplate.get_view(view_type='form')['arch'])
        groups = arch.xpath("//group[@name='automated_reorder_suggestions_group']")
        self.assertEqual(len(groups), 1, "Automated Re-Order Suggestions group missing from form")
        group = groups[0]
        self.assertEqual(group.get('invisible'), 'not reorder_prerequisites_met')
        self.assertTrue(arch.xpath("//field[@name='reorder_prerequisites_met']"))
        self.assertTrue(group.xpath("./field[@name='automated_reorder_suggestions']"))
        for name in ('inventory_turns_target', 'safety_factor'):
            with self.subTest(field=name):
                node = group.xpath(f"./field[@name='{name}']")
                self.assertTrue(node, f"{name} missing from the group")
                self.assertEqual(node[0].get('invisible'), 'not automated_reorder_suggestions')
                self.assertEqual(node[0].get('required'), 'automated_reorder_suggestions')

    # -------------------------------------------------------------------------
    # reorder_prerequisites_met
    # -------------------------------------------------------------------------

    def test_prerequisites_met_when_all_enabled(self):
        self.assertTrue(self._create_product().reorder_prerequisites_met)

    def test_prerequisites_not_met_when_one_missing(self):
        cases = {
            'not storable': {'is_storable': False},
            'not purchasable': {'purchase_ok': False},
            'no buy route': {'route_ids': [Command.clear()]},
        }
        for label, vals in cases.items():
            with self.subTest(case=label):
                self.assertFalse(self._create_product(**vals).reorder_prerequisites_met)

    def test_buy_route_on_category_only_does_not_count(self):
        # Decision: only the product's own route_ids count, not category routes.
        category = self.env['product.category'].create({
            'name': 'Buy Category',
            'route_ids': [Command.set(self.buy_route.ids)],
        })
        product = self._create_product(categ_id=category.id, route_ids=[Command.clear()])
        self.assertFalse(product.reorder_prerequisites_met)

    def test_prerequisites_recomputed_on_write(self):
        product = self._create_product(route_ids=[Command.clear()])
        self.assertFalse(product.reorder_prerequisites_met)
        product.route_ids = [Command.link(self.buy_route.id)]
        self.assertTrue(product.reorder_prerequisites_met)

    # -------------------------------------------------------------------------
    # Automatic reset of automated_reorder_suggestions
    # -------------------------------------------------------------------------

    def test_enabled_product_keeps_flag(self):
        product = self._create_enabled_product()
        self.assertTrue(product.automated_reorder_suggestions)
        self.assertTrue(product.product_variant_id.automated_reorder_suggestions)

    def test_create_with_unmet_prerequisites_resets_flag(self):
        product = self._create_enabled_product(route_ids=[Command.clear()])
        self.assertFalse(product.automated_reorder_suggestions)

    def test_removing_prerequisite_resets_flag(self):
        cases = {
            'untrack inventory': {'is_storable': False},
            'uncheck can be purchased': {'purchase_ok': False},
            'remove buy route': {'route_ids': [Command.unlink(self.buy_route.id)]},
        }
        for label, vals in cases.items():
            with self.subTest(case=label):
                product = self._create_enabled_product()
                product.write(vals)
                self.assertFalse(product.automated_reorder_suggestions)
                # Target values are kept; only the flag is cleared
                self.assertEqual(product.inventory_turns_target, 6.0)
                self.assertEqual(product.safety_factor, 2)

    def test_enabling_flag_with_unmet_prerequisites_is_reverted(self):
        product = self._create_product(purchase_ok=False)
        product.write({
            'automated_reorder_suggestions': True,
            'inventory_turns_target': 4.0,
            'safety_factor': 1,
        })
        self.assertFalse(product.automated_reorder_suggestions)

    def test_unrelated_write_keeps_flag(self):
        product = self._create_enabled_product()
        product.write({'name': 'Renamed', 'safety_factor': 3})
        self.assertTrue(product.automated_reorder_suggestions)

    # -------------------------------------------------------------------------
    # Constraints
    # -------------------------------------------------------------------------

    def test_zero_values_allowed_when_disabled(self):
        product = self._create_product(inventory_turns_target=0.0, safety_factor=0)
        self.assertFalse(product.automated_reorder_suggestions)

    def test_negative_values_rejected_even_when_disabled(self):
        product = self._create_product()
        with self.assertRaises(ValidationError):
            product.inventory_turns_target = -1.0
        with self.assertRaises(ValidationError):
            product.safety_factor = -1

    def test_values_required_when_enabled(self):
        with self.assertRaises(ValidationError):
            self._create_enabled_product(inventory_turns_target=0.0)
        with self.assertRaises(ValidationError):
            self._create_enabled_product(safety_factor=0)

        product = self._create_enabled_product()
        with self.assertRaises(ValidationError):
            product.inventory_turns_target = 0.0
        with self.assertRaises(ValidationError):
            product.safety_factor = 0

    # -------------------------------------------------------------------------
    # Live form editing (onchange cycle)
    # -------------------------------------------------------------------------

    def test_form_group_hidden_until_prerequisites_met(self):
        form = Form(self.ProductTemplate)
        form.name = 'Form Product'
        form.is_storable = True
        # purchase_stock pre-selects the Buy route on new products; clear it
        form.route_ids.clear()
        self.assertFalse(form.reorder_prerequisites_met)
        with self.assertRaises(AssertionError):
            form.automated_reorder_suggestions = True  # invisible field

        form.route_ids.add(self.buy_route)
        self.assertTrue(form.reorder_prerequisites_met)
        form.automated_reorder_suggestions = True
        form.inventory_turns_target = 6.0
        form.safety_factor = 2
        product = form.save()

        self.assertTrue(product.automated_reorder_suggestions)
        self.assertEqual(product.inventory_turns_target, 6.0)
        self.assertEqual(product.safety_factor, 2)

    def test_form_unchecking_prerequisite_resets_flag_on_save(self):
        product = self._create_enabled_product()
        with Form(product) as form:
            form.purchase_ok = False
            self.assertFalse(form.reorder_prerequisites_met)
        self.assertFalse(product.automated_reorder_suggestions)

    def test_form_enabled_without_targets_fails_on_save(self):
        form = Form(self.ProductTemplate)
        form.name = 'Form Product'
        form.is_storable = True
        form.route_ids.add(self.buy_route)
        form.automated_reorder_suggestions = True
        with self.assertRaises(ValidationError):
            form.save()
