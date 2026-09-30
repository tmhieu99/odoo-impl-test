from ast import literal_eval
from datetime import timedelta

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import Form, TransactionCase, tagged

MODULE = 'custom_reorder_suggestion_automation'


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderSuggestionReplenishmentPage(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Orderpoint = cls.env['stock.warehouse.orderpoint']
        cls.action = cls.env.ref(f'{MODULE}.action_reorder_suggestion_replenishment')
        cls.list_view = cls.env.ref(f'{MODULE}.view_reorder_suggestion_replenishment_list')
        cls.warehouse = cls.env.ref('stock.warehouse0')
        cls.vendor = cls.env['res.partner'].create({'name': 'Replenishment Page Vendor'})
        cls.product = cls.env['product.product'].create({
            'name': 'Replenishment Page Product',
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(cls.env.ref('purchase_stock.route_warehouse0_buy').ids)],
            'seller_ids': [Command.create({'partner_id': cls.vendor.id, 'min_qty': 1.0})],
        })

    def _create_orderpoint(self, **vals):
        values = {
            'product_id': self.product.id,
            'location_id': self.warehouse.lot_stock_id.id,
            'trigger': 'manual',
        }
        values.update(vals)
        return self.Orderpoint.create(values)

    def _list_arch(self):
        return etree.fromstring(self.Orderpoint.get_view(self.list_view.id, 'list')['arch'])

    # -------------------------------------------------------------------------
    # View wiring
    # -------------------------------------------------------------------------
    def test_list_has_no_create_button(self):
        arch = self._list_arch()
        self.assertEqual(arch.get('create'), 'false')
        self.assertEqual(arch.get('editable'), 'bottom')

    def test_action_hides_snoozed_by_default(self):
        context = literal_eval(self.action.context)
        self.assertTrue(context.get('search_default_filter_not_snoozed'))
        self.assertNotIn('default_trigger', context)

    def test_action_buttons_use_native_logic(self):
        arch = self._list_arch()
        snooze_action = self.env.ref('stock.action_orderpoint_snooze')
        # Reorder goes through the custom wrapper, which stages Final TOQ and
        # then runs native action_replenish; the native page's own button still
        # calls action_replenish directly and orders the native quantity.
        expected = {
            'Snooze': ('action', str(snooze_action.id)),
            'Reorder': ('object', 'action_reorder_suggestion_replenish'),
        }
        for label, (btn_type, name) in expected.items():
            with self.subTest(button=label):
                buttons = arch.xpath(f"//button[@string='{label}']")
                self.assertEqual(len(buttons), 1)
                self.assertEqual(buttons[0].get('type'), btn_type)
                self.assertEqual(buttons[0].get('name'), name)

    def test_row_buttons_key_off_the_custom_to_order(self):
        arch = self._list_arch()
        for label in ('Snooze', 'Reorder'):
            with self.subTest(button=label):
                invisible = arch.xpath(f"//button[@string='{label}']")[0].get('invisible')
                self.assertIn('reorder_to_order', invisible)
                # Native qty_to_order must not drive the custom page.
                self.assertNotIn('qty_to_order', invisible)
        # Native write() raises a UserError when an 'auto' row is snoozed, so
        # that guard has to survive alongside the quantity condition.
        snooze = arch.xpath("//button[@string='Snooze']")[0].get('invisible')
        self.assertIn("trigger != 'manual'", snooze)

    def test_automate_button_is_not_offered(self):
        # action_replenish_auto sets trigger='auto', and the scheduler's domain
        # is [('trigger','=','auto'), ...] — one click would turn a suggestion
        # into unattended nightly purchasing. Snooze + manual Reorder only.
        arch = self._list_arch()
        self.assertFalse(arch.xpath("//button[@name='action_replenish_auto']"))
        self.assertFalse(arch.xpath("//button[@string='Automate']"))

    def test_placeholder_methods_removed(self):
        for name in ('action_reorder_suggestion_snooze', 'action_reorder_suggestion_reorder',
                     'action_reorder_suggestion_automate'):
            with self.subTest(method=name):
                self.assertFalse(hasattr(self.Orderpoint, name))

    # -------------------------------------------------------------------------
    # Behavior
    # -------------------------------------------------------------------------
    def test_snooze_wizard(self):
        orderpoint = self._create_orderpoint()
        wizard_form = Form(self.env['stock.orderpoint.snooze'].with_context(
            default_orderpoint_ids=orderpoint.ids))
        wizard_form.predefined_date = 'week'
        wizard = wizard_form.save()
        wizard.action_snooze()
        self.assertEqual(orderpoint.snoozed_until, wizard.snoozed_until)
        self.assertTrue(orderpoint.snoozed_until)

    def test_reorder_creates_purchase_order(self):
        orderpoint = self._create_orderpoint(qty_to_order_manual=5.0)
        orderpoint.action_replenish()
        line = self.env['purchase.order.line'].search([('product_id', '=', self.product.id)])
        self.assertEqual(len(line), 1)
        self.assertEqual(line.product_qty, 5.0)
        self.assertEqual(line.partner_id, self.vendor)

    # -------------------------------------------------------------------------
    # Which rows the page lists
    # -------------------------------------------------------------------------
    def _flagged_product(self, name):
        return self.env['product.product'].create({
            'name': name,
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(self.env.ref('purchase_stock.route_warehouse0_buy').ids)],
            'automated_reorder_suggestions': True,
            'inventory_turns_target': 4.0,
            'safety_factor': 2,
        })

    def _action_rows(self):
        return self.Orderpoint.search(literal_eval(self.action.domain))

    def test_action_lists_only_products_in_the_suggestion_flow(self):
        """A manufactured product with a reordering rule is not a suggestion.

        Without the action's domain the page is a second copy of native
        Reordering Rules: it lists every orderpoint row, and a Manufacture-only
        product shows up with no vendor and no calculated quantities.
        """
        flagged = self._flagged_product('Flagged Suggestion Product')
        manufactured = self.env['product.product'].create({
            'name': 'Manufactured Only Product',
            'is_storable': True,
            'route_ids': [Command.set(
                self.env.ref('mrp.route_warehouse0_manufacture').ids)],
        })
        self.assertFalse(manufactured.automated_reorder_suggestions)

        flagged_orderpoint = self._create_orderpoint(product_id=flagged.id)
        manufactured_orderpoint = self._create_orderpoint(product_id=manufactured.id)

        rows = self._action_rows()
        self.assertIn(flagged_orderpoint, rows)
        self.assertNotIn(manufactured_orderpoint, rows)

    def test_action_drops_rows_when_the_flag_is_cleared(self):
        """Losing a prerequisite clears the flag, so the row leaves the page."""
        flagged = self._flagged_product('Buy Route Removed Product')
        orderpoint = self._create_orderpoint(product_id=flagged.id)
        self.assertIn(orderpoint, self._action_rows())

        flagged.write({'route_ids': [Command.clear()]})

        self.assertFalse(flagged.automated_reorder_suggestions)
        self.assertNotIn(orderpoint, self._action_rows())



@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderSuggestionMultiCompany(TransactionCase):
    """Right Weigh runs two companies, one warehouse each, sharing products."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.ref('base.main_company')
        cls.company_b = cls.env['res.company'].create({'name': 'Reorder Company B'})
        # Both companies selected in the switcher: the case where unscoped
        # quantities / vendor lookups leak across companies.
        cls.env = cls.env(context=dict(cls.env.context,
                                       allowed_company_ids=[cls.company_a.id, cls.company_b.id]))
        Warehouse = cls.env['stock.warehouse']
        cls.warehouse_a = Warehouse.search([('company_id', '=', cls.company_a.id)], limit=1)
        cls.warehouse_b = Warehouse.search([('company_id', '=', cls.company_b.id)], limit=1) or \
            Warehouse.with_company(cls.company_b).create({
                'name': 'Reorder Company B Warehouse',
                'code': 'RWB',
                'company_id': cls.company_b.id,
            })
        Partner = cls.env['res.partner']
        cls.vendor_a = Partner.create({'name': 'Company A Vendor'})
        cls.vendor_b = Partner.create({'name': 'Company B Vendor'})
        cls.vendor_shared = Partner.create({'name': 'Shared Vendor'})
        cls.product = cls.env['product.product'].create({
            'name': 'Multi-Company Reorder Product',
            'is_storable': True,
        })

    def _seller(self, partner, company=None, sequence=10):
        # company_id is passed explicitly: it defaults to env.company otherwise.
        return self.env['product.supplierinfo'].create({
            'partner_id': partner.id,
            'product_tmpl_id': self.product.product_tmpl_id.id,
            'company_id': company.id if company else False,
            'sequence': sequence,
        })

    def _orderpoint(self, warehouse):
        return self.env['stock.warehouse.orderpoint'].create({
            'product_id': self.product.id,
            'company_id': warehouse.company_id.id,
            'warehouse_id': warehouse.id,
            'location_id': warehouse.lot_stock_id.id,
        })

    def test_vendor_is_resolved_per_company(self):
        self._seller(self.vendor_a, self.company_a, sequence=1)
        self._seller(self.vendor_b, self.company_b, sequence=2)
        self.assertEqual(self._orderpoint(self.warehouse_a).reorder_vendor_id, self.vendor_a)
        self.assertEqual(self._orderpoint(self.warehouse_b).reorder_vendor_id, self.vendor_b)

    def test_vendor_falls_back_to_shared_line(self):
        self._seller(self.vendor_a, self.company_a, sequence=1)
        self._seller(self.vendor_shared, sequence=5)
        self.assertEqual(self._orderpoint(self.warehouse_a).reorder_vendor_id, self.vendor_a)
        self.assertEqual(self._orderpoint(self.warehouse_b).reorder_vendor_id, self.vendor_shared)

    def test_vendor_follows_vendor_list_order(self):
        self._seller(self.vendor_a, sequence=1)
        later_seller = self._seller(self.vendor_shared, sequence=2)
        orderpoint = self._orderpoint(self.warehouse_b)
        self.assertEqual(orderpoint.reorder_vendor_id, self.vendor_a)
        later_seller.sequence = 0
        self.assertEqual(orderpoint.reorder_vendor_id, self.vendor_shared)

    def test_vendor_empty_without_supplier_info(self):
        self._seller(self.vendor_a, self.company_a)
        self.assertFalse(self._orderpoint(self.warehouse_b).reorder_vendor_id)

    def test_on_order_scoped_to_own_warehouse(self):
        orderpoint_a = self._orderpoint(self.warehouse_a)
        orderpoint_b = self._orderpoint(self.warehouse_b)
        supplier_location = self.env.ref('stock.stock_location_suppliers')
        receipt = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse_a.in_type_id.id,
            'location_id': supplier_location.id,
            'location_dest_id': self.warehouse_a.lot_stock_id.id,
            'move_ids': [Command.create({
                'name': self.product.name,
                'product_id': self.product.id,
                'product_uom_qty': 7.0,
                'product_uom': self.product.uom_id.id,
                'location_id': supplier_location.id,
                'location_dest_id': self.warehouse_a.lot_stock_id.id,
            })],
        })
        receipt.action_confirm()
        self.assertEqual(orderpoint_a.reorder_on_order, 7.0)
        self.assertEqual(orderpoint_b.reorder_on_order, 0.0)


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderSuggestionQuantities(TransactionCase):
    """End-to-end Min / Max / To Order: the engine's ROP/TOQ math wired to
    vendor params, template fields and the native Forecast on the orderpoint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.ref('base.main_company')
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.buy_route = cls.env.ref('purchase_stock.route_warehouse0_buy')
        cls.manufacture_route = cls.env.ref('mrp.route_warehouse0_manufacture')
        cls.customer = cls.env['res.partner'].create({'name': 'Qty Customer'})
        cls.vendor = cls.env['res.partner'].create({'name': 'Qty Vendor'})
        # Bought component, flagged, 6 turns/year and 1 week of safety stock.
        cls.component = cls.env['product.product'].create({
            'name': 'Qty Component',
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(cls.buy_route.ids)],
            'automated_reorder_suggestions': True,
            'inventory_turns_target': 6,
            'safety_factor': 1,
            'seller_ids': [Command.create({
                'partner_id': cls.vendor.id,
                'min_qty': 1.0,
                'lead_time_weeks': 2,
                'standard_case_quantity': 12,
            })],
        })
        # A manufactured parent consuming 2× the component: selling the parent
        # drives the component's demand without any outgoing move on the
        # component itself, so its Forecast stays controllable.
        cls.parent = cls.env['product.product'].create({
            'name': 'Qty Parent',
            'is_storable': True,
            'route_ids': [Command.set(cls.manufacture_route.ids)],
        })
        cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.parent.product_tmpl_id.id,
            'product_qty': 1.0,
            'bom_line_ids': [Command.create({
                'product_id': cls.component.id,
                'product_qty': 2.0,
            })],
        })
        cls._age_product(cls.component, 200)

    @classmethod
    def _age_product(cls, product, days):
        moment = fields.Datetime.now() - timedelta(days=days)
        product.flush_recordset()
        cls.env.cr.execute(
            'UPDATE product_product SET create_date = %s WHERE id = %s',
            (moment, product.id))
        cls.env.cr.execute(
            'UPDATE product_template SET create_date = %s WHERE id = %s',
            (moment, product.product_tmpl_id.id))
        product.invalidate_recordset(['create_date'])
        product.product_tmpl_id.invalidate_recordset(['create_date'])

    @classmethod
    def _sell(cls, product, qty, days_ago):
        order = cls.env['sale.order'].with_company(cls.company).create({
            'partner_id': cls.customer.id,
            'company_id': cls.company.id,
            'order_line': [Command.create(
                {'product_id': product.id, 'product_uom_qty': qty})],
        })
        order.action_confirm()
        moment = fields.Datetime.now() - timedelta(days=days_ago)
        order.flush_recordset()
        cls.env.cr.execute(
            'UPDATE sale_order SET date_order = %s WHERE id = %s', (moment, order.id))
        order.invalidate_recordset(['date_order'])
        return order

    def _orderpoint(self, product):
        return self.env['stock.warehouse.orderpoint'].create({
            'product_id': product.id,
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'location_id': self.warehouse.lot_stock_id.id,
            'trigger': 'manual',
        })

    def test_min_max_to_order_when_below_rop(self):
        # Parent sold 13 units 30 days ago -> component RGD 26 in the 3-month
        # window -> Greatest WAD = 26/13 = 2.0.
        self._sell(self.parent, 13, 30)
        orderpoint = self._orderpoint(self.component)
        # ROP = 2.0 * (2 lead + 1 safety) = 6.0.
        self.assertAlmostEqual(orderpoint.reorder_min, 6.0, places=3)
        # Base TOQ 2*52/6 = 17.333; + shortfall (6-0) = 23.333; MOQ 1;
        # round up to the 12-unit case -> 24.
        self.assertAlmostEqual(orderpoint.reorder_to_order, 24.0, places=3)
        # Max = Forecast (0) + Final TOQ.
        self.assertAlmostEqual(orderpoint.reorder_max, 24.0, places=3)

    def test_to_order_zero_when_forecast_meets_rop(self):
        self._sell(self.parent, 13, 30)
        self.env['stock.quant']._update_available_quantity(
            self.component, self.warehouse.lot_stock_id, 100.0)
        orderpoint = self._orderpoint(self.component)
        self.assertAlmostEqual(orderpoint.reorder_min, 6.0, places=3)
        self.assertEqual(orderpoint.reorder_to_order, 0.0)
        # With nothing to order, Max collapses to just the Forecast.
        self.assertAlmostEqual(orderpoint.reorder_max, orderpoint.qty_forecast, places=3)

    def test_no_values_without_vendor(self):
        novendor = self.env['product.product'].create({
            'name': 'Qty No Vendor',
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(self.buy_route.ids)],
        })
        orderpoint = self._orderpoint(novendor)
        self.assertEqual(orderpoint.reorder_min, 0.0)
        self.assertEqual(orderpoint.reorder_max, 0.0)
        self.assertEqual(orderpoint.reorder_to_order, 0.0)
        self.assertFalse(orderpoint.reorder_vendor_id)

    def test_reorder_orders_the_suggested_quantity(self):
        self._sell(self.parent, 13, 30)
        orderpoint = self._orderpoint(self.component)
        self.assertAlmostEqual(orderpoint.reorder_to_order, 24.0, places=3)

        orderpoint.action_reorder_suggestion_replenish()

        lines = self.env['purchase.order.line'].search([
            ('product_id', '=', self.component.id),
            ('order_id.partner_id', '=', self.vendor.id),
        ])
        self.assertEqual(len(lines), 1)
        # Final TOQ, not the native computed quantity — the native Min/Max on
        # this orderpoint are both 0, so native alone would order nothing.
        self.assertAlmostEqual(lines.product_qty, 24.0, places=3)

    def test_reorder_refuses_when_nothing_is_suggested(self):
        self.env['stock.quant']._update_available_quantity(
            self.component, self.warehouse.lot_stock_id, 100.0)
        orderpoint = self._orderpoint(self.component)
        self.assertEqual(orderpoint.reorder_to_order, 0.0)
        with self.assertRaises(UserError):
            orderpoint.action_reorder_suggestion_replenish()
