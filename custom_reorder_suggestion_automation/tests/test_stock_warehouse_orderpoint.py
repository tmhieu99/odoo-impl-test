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

    def test_columns_are_the_pdf_fields(self):
        # PDF 6.a.vi a-k, in order; diagnostic columns are not shown. Read
        # from the view's own arch: the processed one drops group-restricted
        # columns (UoM) and adds a hidden company_id.
        arch = etree.fromstring(self.list_view.arch)
        columns = [field.get('name') for field in arch.xpath('/list/field')]
        self.assertEqual(columns, [
            'product_id', 'reorder_description', 'reorder_on_order', 'qty_on_hand',
            'reorder_forecast', 'reorder_product_route_ids', 'reorder_vendor_id', 'reorder_min',
            'reorder_max', 'reorder_to_order', 'product_uom_name',
        ])

    def test_routes_column_shows_product_routes(self):
        # The product's routes, not the row's Preferred Route.
        cross_dock = self.env['stock.route'].create({'name': 'Test Cross-Dock'})
        product = self._flagged_product('Routes Column Product')
        orderpoint = self.Orderpoint.create({
            'product_id': product.id,
            'warehouse_id': self.warehouse.id,
            'location_id': self.warehouse.lot_stock_id.id,
            'route_id': cross_dock.id,
        })
        self.assertEqual(orderpoint.reorder_product_route_ids, product.route_ids)
        self.assertNotIn(cross_dock, orderpoint.reorder_product_route_ids)
        arch = etree.fromstring(self.list_view.arch)
        column = arch.xpath("/list/field[@name='reorder_product_route_ids']")[0]
        self.assertEqual(column.get('widget'), 'many2many_tags')

    def test_action_hides_snoozed_by_default(self):
        context = literal_eval(self.action.context)
        self.assertTrue(context.get('search_default_filter_not_snoozed'))
        self.assertNotIn('default_trigger', context)

    def test_no_row_action_buttons(self):
        # Ordering goes only through the header Reorder on the selected rows,
        # and Snooze is not offered (user, 2026-10-06). The only row button
        # left is the "No Vendor" badge.
        arch = self._list_arch()
        row_buttons = arch.xpath("/list/button")
        self.assertEqual([button.get('name') for button in row_buttons],
                         ['action_open_product_vendors'])
        snooze_action = self.env.ref('stock.action_orderpoint_snooze')
        self.assertFalse(arch.xpath(f"//button[@name='{snooze_action.id}']"))
        self.assertFalse(arch.xpath("//button[@string='Snooze']"))

    def test_header_reorder_button_for_selected_rows(self):
        # List header buttons act on the selected rows: one click, one native
        # procurement run, one PO per vendor. The custom wrapper stages Final
        # TOQ and runs native action_replenish; the native page's own button
        # still calls action_replenish directly and orders the native quantity.
        buttons = self._list_arch().xpath("/list/header/button")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get('type'), 'object')
        self.assertEqual(buttons[0].get('name'), 'action_reorder_suggestion_replenish')

    def test_automate_button_is_not_offered(self):
        # action_replenish_auto sets trigger='auto', and the scheduler's domain
        # is [('trigger','=','auto'), ...] — one click would turn a suggestion
        # into unattended nightly purchasing.
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

    def test_forecast_counts_only_own_company_confirmed_po(self):
        orderpoint_a = self._orderpoint(self.warehouse_a)
        orderpoint_b = self._orderpoint(self.warehouse_b)
        order = self.env['purchase.order'].with_company(self.company_b).create({
            'partner_id': self.vendor_b.id,
            'company_id': self.company_b.id,
            'picking_type_id': self.warehouse_b.in_type_id.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 5.0})],
        })
        orderpoints = orderpoint_a | orderpoint_b
        # An RFQ counts nowhere, not even in its own company.
        self.assertEqual(orderpoints._reorder_forecast_by_orderpoint(),
                         {orderpoint_a.id: 0.0, orderpoint_b.id: 0.0})
        order.button_confirm()
        self.assertEqual(orderpoints._reorder_forecast_by_orderpoint(),
                         {orderpoint_a.id: 0.0, orderpoint_b.id: 5.0})


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderSuggestionQuantities(TransactionCase):
    """End-to-end Min / Max / To Order: the engine's ROP/TOQ math wired to
    vendor params, template fields and the product's warehouse Forecasted qty."""

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
        orderpoint = self.env['stock.warehouse.orderpoint'].create({
            'product_id': product.id,
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'location_id': self.warehouse.lot_stock_id.id,
            'trigger': 'manual',
        })
        # The suggestion columns are filled by the nightly scheduled action,
        # not by the ORM, so a fresh row is blank until a run touches it.
        orderpoint._update_reorder_suggestions()
        return orderpoint

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
        self.assertAlmostEqual(orderpoint.reorder_forecast, 100.0, places=3)
        self.assertAlmostEqual(orderpoint.reorder_max, orderpoint.reorder_forecast, places=3)

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

    # -------------------------------------------------------------------------
    # Forecast = the product's Forecasted quantity in the warehouse: no open
    # RFQs, no lead-days horizon (change request, 2026-10-07)
    # -------------------------------------------------------------------------
    def _rfq(self, product, qty):
        return self.env['purchase.order'].with_company(self.company).create({
            'partner_id': self.vendor.id,
            'company_id': self.company.id,
            'picking_type_id': self.warehouse.in_type_id.id,
            'order_line': [Command.create({'product_id': product.id, 'product_qty': qty})],
        })

    def _move(self, product, qty, days_ahead, incoming):
        supplier = self.env.ref('stock.stock_location_suppliers')
        customer = self.env.ref('stock.stock_location_customers')
        stock = self.warehouse.lot_stock_id
        source, destination = (supplier, stock) if incoming else (stock, customer)
        move = self.env['stock.move'].create({
            'name': product.name,
            'product_id': product.id,
            'product_uom_qty': qty,
            'product_uom': product.uom_id.id,
            'location_id': source.id,
            'location_dest_id': destination.id,
            'date': fields.Datetime.now() + timedelta(days=days_ahead),
        })
        move._action_confirm()
        return move

    def test_open_rfq_does_not_reduce_suggestion(self):
        self._sell(self.parent, 13, 30)
        orderpoint = self._orderpoint(self.component)
        order = self._rfq(self.component, 24.0)
        # The RFQ comes from the suggestion row, as one made by Reorder does.
        order.order_line.orderpoint_id = orderpoint
        for state in ('draft', 'sent', 'to approve'):
            order.state = state
            orderpoint._update_reorder_suggestions()
            # Native Replenishment Forecast counts the RFQ; the suggestion must not.
            self.assertAlmostEqual(orderpoint.qty_forecast, 24.0, places=3, msg=state)
            self.assertEqual(orderpoint.reorder_forecast, 0.0, state)
            self.assertAlmostEqual(orderpoint.reorder_to_order, 24.0, places=3, msg=state)
            self.assertAlmostEqual(orderpoint.reorder_max, 24.0, places=3, msg=state)

    def test_confirmed_po_reduces_suggestion(self):
        self._sell(self.parent, 13, 30)
        orderpoint = self._orderpoint(self.component)
        order = self._rfq(self.component, 24.0)
        order.button_confirm()
        orderpoint._update_reorder_suggestions()
        self.assertAlmostEqual(orderpoint.reorder_forecast, 24.0, places=3)
        # Forecast 24 is above ROP 6: nothing more to order, Max = Forecast.
        self.assertEqual(orderpoint.reorder_to_order, 0.0)
        self.assertAlmostEqual(orderpoint.reorder_max, 24.0, places=3)

    def test_forecast_includes_moves_beyond_lead_time(self):
        self._sell(self.parent, 13, 30)
        orderpoint = self._orderpoint(self.component)
        # Lead time is 2 weeks; both moves are dated a year out.
        self._move(self.component, 40.0, 365, incoming=True)
        self._move(self.component, 15.0, 365, incoming=False)
        orderpoint._update_reorder_suggestions()
        # Native Replenishment stops at the lead-days date and sees neither.
        self.assertAlmostEqual(orderpoint.qty_forecast, 0.0, places=3)
        self.assertAlmostEqual(orderpoint.reorder_forecast, 25.0, places=3)
        self.assertAlmostEqual(
            orderpoint.reorder_forecast, self.component.with_context(
                warehouse_id=self.warehouse.id).virtual_available, places=3)
        # Forecast 25 >= ROP 6: nothing to order.
        self.assertEqual(orderpoint.reorder_to_order, 0.0)
        self.assertAlmostEqual(orderpoint.reorder_max, 25.0, places=3)

    def test_reorder_again_adds_to_open_draft(self):
        # Accepted behaviour (2026-10-07): the open RFQ does not count, so a
        # second Reorder before confirming orders the suggestion again and
        # native line merging grows the draft line.
        self._sell(self.parent, 13, 30)
        # A row created the nightly cron's way survives native cleanup, as in
        # production; native only merges PO lines coming from the same row.
        Orderpoint = self.env['stock.warehouse.orderpoint']
        Orderpoint._create_missing_reorder_suggestion_orderpoints()
        orderpoint = Orderpoint.search([
            ('product_id', '=', self.component.id),
            ('location_id', '=', self.warehouse.lot_stock_id.id),
        ])
        orderpoint.action_reorder_suggestion_replenish()
        line = self._purchase_lines(self.component)
        self.assertAlmostEqual(line.product_qty, 24.0, places=3)
        self.assertTrue(orderpoint.exists())

        orderpoint.action_reorder_suggestion_replenish()
        self.assertEqual(self._purchase_lines(self.component), line)
        self.assertAlmostEqual(line.product_qty, 48.0, places=3)

    def test_reorder_refuses_when_nothing_is_suggested(self):
        self.env['stock.quant']._update_available_quantity(
            self.component, self.warehouse.lot_stock_id, 100.0)
        orderpoint = self._orderpoint(self.component)
        self.assertEqual(orderpoint.reorder_to_order, 0.0)
        with self.assertRaises(UserError):
            orderpoint.action_reorder_suggestion_replenish()

    # -------------------------------------------------------------------------
    # Bulk Reorder: one PO per vendor (PDF 6.a.v.2)
    # -------------------------------------------------------------------------
    def _bought_component(self, name, vendor):
        component = self.env['product.product'].create({
            'name': name,
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(self.buy_route.ids)],
            'automated_reorder_suggestions': True,
            'inventory_turns_target': 6,
            'safety_factor': 1,
            'seller_ids': [Command.create({
                'partner_id': vendor.id,
                'min_qty': 1.0,
                'lead_time_weeks': 2,
                'standard_case_quantity': 12,
            })],
        })
        self._age_product(component, 200)
        return component

    def _sell_kit_of(self, components):
        # One manufactured parent consuming each component, so a single sale
        # gives them all demand without outgoing moves on the components.
        kit = self.env['product.product'].create({
            'name': 'Bulk Kit',
            'is_storable': True,
            'route_ids': [Command.set(self.manufacture_route.ids)],
        })
        self.env['mrp.bom'].create({
            'product_tmpl_id': kit.product_tmpl_id.id,
            'product_qty': 1.0,
            'bom_line_ids': [Command.create({'product_id': component.id, 'product_qty': 2.0})
                             for component in components],
        })
        self._sell(kit, 13, 30)

    def _purchase_lines(self, products):
        return self.env['purchase.order.line'].search([('product_id', 'in', products.ids)])

    def test_bulk_reorder_groups_lines_per_vendor(self):
        vendor_y = self.env['res.partner'].create({'name': 'Qty Vendor Y'})
        x1 = self._bought_component('Bulk X1', self.vendor)
        x2 = self._bought_component('Bulk X2', self.vendor)
        y1 = self._bought_component('Bulk Y1', vendor_y)
        self._sell_kit_of(x1 | x2 | y1)
        orderpoints = self.env['stock.warehouse.orderpoint']
        for product in (x1, x2, y1):
            orderpoints |= self._orderpoint(product)
        suggested = {op.product_id: op.reorder_to_order for op in orderpoints}
        self.assertTrue(all(qty > 0.0 for qty in suggested.values()))

        toast = orderpoints.action_reorder_suggestion_replenish()

        lines = self._purchase_lines(x1 | x2 | y1)
        self.assertEqual(len(lines), 3)
        orders = lines.order_id
        self.assertEqual(len(orders), 2)
        self.assertEqual(toast['tag'], 'display_notification')
        self.assertEqual(toast['params']['title'], '2 purchase order(s) created')
        self.assertEqual({link['label'] for link in toast['params']['links']},
                         set(orders.mapped('display_name')))
        self.assertEqual(toast['params']['message'].count('%s'), 2)
        self.assertEqual(set(orders.mapped('state')), {'draft'})
        order_x = orders.filtered(lambda order: order.partner_id == self.vendor)
        order_y = orders.filtered(lambda order: order.partner_id == vendor_y)
        self.assertEqual(order_x.order_line.product_id, x1 | x2)
        self.assertEqual(order_y.order_line.product_id, y1)
        for line in lines:
            self.assertAlmostEqual(line.product_qty, suggested[line.product_id], places=3)

    def test_bulk_reorder_skips_rows_with_nothing_to_order(self):
        novendor = self.env['product.product'].create({
            'name': 'Bulk No Vendor',
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(self.buy_route.ids)],
        })
        stocked = self._bought_component('Bulk Stocked', self.vendor)
        self.env['stock.quant']._update_available_quantity(
            stocked, self.warehouse.lot_stock_id, 100.0)
        self._sell(self.parent, 13, 30)
        orderpoints = self._orderpoint(self.component) | self._orderpoint(novendor) \
            | self._orderpoint(stocked)

        toast = orderpoints.action_reorder_suggestion_replenish()

        lines = self._purchase_lines(self.component | novendor | stocked)
        self.assertEqual(lines.product_id, self.component)
        self.assertEqual(lines.order_id.partner_id, self.vendor)
        self.assertEqual(toast['params']['title'], '1 purchase order(s) created')

    def test_bulk_reorder_toast_counts_merge_into_open_draft(self):
        later = self._bought_component('Merge Later', self.vendor)
        self._sell(self.parent, 13, 30)
        self._sell_kit_of(later)
        first = self._orderpoint(self.component)
        second = self._orderpoint(later)
        first.action_reorder_suggestion_replenish()
        draft = self._purchase_lines(self.component).order_id
        # The test user is SUPERUSER, so native action_replenish deleted the
        # satisfied manual row — as it does for rows native Replenishment
        # generates — and the toast must still find its PO lines.
        self.assertFalse(first.exists())
        # Another product from the same vendor, reordered later, is added to
        # that open draft.
        # Each click is its own transaction in real use, but cr.now() is fixed
        # for the whole test transaction: age the first click's records.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE purchase_order SET create_date = create_date - interval '1 hour' WHERE id = %s",
            (draft.id,))
        self.env.cr.execute(
            "UPDATE purchase_order_line SET write_date = write_date - interval '1 hour' "
            "WHERE order_id = %s", (draft.id,))
        self.env.invalidate_all()

        toast = second.action_reorder_suggestion_replenish()

        self.assertEqual(self._purchase_lines(later).order_id, draft)
        self.assertEqual(toast['params']['title'], '1 existing draft(s) updated')
        self.assertEqual(toast['params']['links'][0]['label'], draft.display_name)


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderSuggestionRowCreation(TransactionCase):
    """The nightly cron creates the rows the suggestion page lists, so a flagged
    SKU appears without anyone opening native Replenishment first."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Orderpoint = cls.env['stock.warehouse.orderpoint']
        cls.company_a = cls.env.ref('base.main_company')
        cls.company_b = cls.env['res.company'].create({'name': 'Row Creation Company B'})
        # A company the tests never look at, so every database runs them with
        # more companies than the two under test, as the Odoo.sh one does.
        cls.env['res.company'].create({'name': 'Row Creation Bystander Company'})
        cls.env = cls.env(context=dict(cls.env.context,
                                       allowed_company_ids=[cls.company_a.id, cls.company_b.id]))
        cls.Orderpoint = cls.env['stock.warehouse.orderpoint']
        Warehouse = cls.env['stock.warehouse']
        cls.warehouse_a = Warehouse.search([('company_id', '=', cls.company_a.id)], limit=1)
        cls.warehouse_b = Warehouse.search([('company_id', '=', cls.company_b.id)], limit=1) or \
            Warehouse.with_company(cls.company_b).create({
                'name': 'Row Creation Company B Warehouse',
                'code': 'RCB',
                'company_id': cls.company_b.id,
            })
        cls.buy_route = cls.env.ref('purchase_stock.route_warehouse0_buy')
        cls.vendor = cls.env['res.partner'].create({'name': 'Row Creation Vendor'})
        cls.flagged = cls.env['product.product'].create({
            'name': 'Row Creation Flagged',
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(cls.buy_route.ids)],
            'automated_reorder_suggestions': True,
            'inventory_turns_target': 4,
            'safety_factor': 1,
            'seller_ids': [Command.create({
                'partner_id': cls.vendor.id,
                'company_id': False,
                'min_qty': 1.0,
            })],
        })
        cls.unflagged = cls.env['product.product'].create({
            'name': 'Row Creation Unflagged',
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(cls.buy_route.ids)],
        })
        cls.owner = cls.env.ref(f'{MODULE}.user_reorder_suggestion')

    def _rows(self, product):
        # Scoped to the two test companies: the database may hold more (the
        # Odoo.sh one already has San Francisco and Chicago), and the cron
        # rightly creates a row in each of their warehouses too.
        return self.Orderpoint.with_context(active_test=False).search(
            [('product_id', '=', product.id),
             ('company_id', 'in', (self.company_a | self.company_b).ids)])

    def test_flagged_product_gets_one_row_per_company_warehouse(self):
        self.Orderpoint._create_missing_reorder_suggestion_orderpoints()
        rows = self._rows(self.flagged)
        by_company = {row.company_id: row for row in rows}
        self.assertEqual(set(by_company), {self.company_a, self.company_b})
        for warehouse in (self.warehouse_a, self.warehouse_b):
            row = by_company[warehouse.company_id]
            self.assertEqual(row.warehouse_id, warehouse)
            self.assertEqual(row.location_id, warehouse.lot_stock_id)
            self.assertEqual(row.trigger, 'manual')
            self.assertEqual(row.create_uid, self.owner)
        # Warehouse A's stock is the destination of the Buy route's rule.
        self.assertEqual(by_company[self.company_a].route_id, self.buy_route)

    def test_unflagged_product_gets_no_row(self):
        self.Orderpoint._create_missing_reorder_suggestion_orderpoints()
        self.assertFalse(self._rows(self.unflagged))

    def test_company_specific_product_only_in_its_company(self):
        self.flagged.product_tmpl_id.company_id = self.company_b
        self.Orderpoint._create_missing_reorder_suggestion_orderpoints()
        rows = self.Orderpoint.with_context(active_test=False).search(
            [('product_id', '=', self.flagged.id)])
        self.assertEqual(rows.company_id, self.company_b)

    def test_existing_and_archived_rows_are_reused(self):
        existing = self.Orderpoint.create({
            'product_id': self.flagged.id,
            'company_id': self.company_a.id,
            'warehouse_id': self.warehouse_a.id,
            'location_id': self.warehouse_a.lot_stock_id.id,
        })
        archived = self.Orderpoint.create({
            'product_id': self.flagged.id,
            'company_id': self.company_b.id,
            'warehouse_id': self.warehouse_b.id,
            'location_id': self.warehouse_b.lot_stock_id.id,
            'active': False,
        })
        created = self.Orderpoint._create_missing_reorder_suggestion_orderpoints()
        self.assertFalse(created & self._rows(self.flagged))
        self.assertEqual(self._rows(self.flagged), existing | archived)
        self.assertFalse(archived.active)

    def test_creation_is_idempotent(self):
        self.Orderpoint._create_missing_reorder_suggestion_orderpoints()
        rows = self._rows(self.flagged)
        self.Orderpoint._create_missing_reorder_suggestion_orderpoints()
        self.assertEqual(self._rows(self.flagged), rows)

    def test_rows_survive_native_cleanup(self):
        self.Orderpoint._create_missing_reorder_suggestion_orderpoints()
        rows = self._rows(self.flagged)
        self.assertTrue(all(row.qty_to_order <= 0.0 for row in rows))
        self.Orderpoint._unlink_processed_orderpoints()
        self.assertEqual(self._rows(self.flagged).exists(), rows)

    def test_cron_creates_and_calculates_rows(self):
        self.Orderpoint._cron_update_reorder_suggestions()
        row = self._rows(self.flagged).filtered(
            lambda orderpoint: orderpoint.company_id == self.company_a)
        self.assertTrue(row)
        self.assertEqual(row.reorder_vendor_id, self.vendor)
        # No demand yet: the engine ran and stored its (zero) suggestion.
        self.assertEqual(row.reorder_wad, 0.0)
        self.assertEqual(row.reorder_to_order, 0.0)
