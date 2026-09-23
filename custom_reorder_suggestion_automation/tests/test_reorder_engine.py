from datetime import date, timedelta

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged

DEMAND_WINDOW_DAYS = 365


class ReorderEngineCommon(TransactionCase):
    """Deterministic demand dataset whose expected RGD is known by hand.

    Sales orders are confirmed through the normal flow and then backdated with
    raw SQL, because confirmation overwrites `date_order`.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['custom.reorder.engine']
        cls.company_a = cls.env.ref('base.main_company')
        cls.buy_route = cls.env.ref('purchase_stock.route_warehouse0_buy')
        cls.manufacture_route = cls.env.ref('mrp.route_warehouse0_manufacture')
        cls.dropship_route = cls.env.ref('stock_dropshipping.route_drop_shipping')
        cls.unit_uom = cls.env.ref('uom.product_uom_unit')
        cls.dozen_uom = cls.env.ref('uom.product_uom_dozen')
        cls.customer = cls.env['res.partner'].create({'name': 'RW Engine Customer'})
        cls.vendor = cls.env['res.partner'].create({'name': 'RW Engine Vendor V1'})

    # -------------------------------------------------------------------------
    # Seed helpers
    # -------------------------------------------------------------------------
    @classmethod
    def _component(cls, name, flagged=True, with_vendor=True):
        """A purchased component, flagged for automated reorder suggestions."""
        values = {
            'name': name,
            'is_storable': True,
            'purchase_ok': True,
            'route_ids': [Command.set(cls.buy_route.ids)],
            'automated_reorder_suggestions': flagged,
        }
        if flagged:
            # Both are required by the model's constraint once the flag is on.
            values.update(inventory_turns_target=6, safety_factor=1)
        if with_vendor:
            values['seller_ids'] = [Command.create({'partner_id': cls.vendor.id})]
        return cls.env['product.product'].create(values)

    @classmethod
    def _parent(cls, name, routes):
        return cls.env['product.product'].create({
            'name': name,
            'is_storable': True,
            'route_ids': [Command.set(routes.ids)],
        })

    @classmethod
    def _bom(cls, product, lines, header_qty=1.0, bom_type='normal'):
        return cls.env['mrp.bom'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'product_qty': header_qty,
            'type': bom_type,
            'bom_line_ids': [
                Command.create({
                    'product_id': component.id,
                    'product_qty': qty,
                    'product_uom_id': (uom or cls.unit_uom).id,
                })
                for component, qty, uom in lines
            ],
        })

    @classmethod
    def _backdate(cls, order, days):
        moment = fields.Datetime.now() - timedelta(days=days)
        # Confirmation leaves a pending `date_order` write in the ORM buffer;
        # without flushing it first, the next flush (the engine's own, for
        # instance) would write today's date straight back over this UPDATE.
        order.flush_recordset()
        cls.env.cr.execute(
            'UPDATE sale_order SET date_order = %s WHERE id = %s', (moment, order.id))
        order.invalidate_recordset(['date_order'])

    @classmethod
    def _backdate_product(cls, product, days):
        """Age a product. Both rows are updated: the variant carries the date
        the engine reads, the template keeps them consistent."""
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
    def _sell(cls, product, qty, days_ago, state='sale', company=None, route=None):
        company = company or cls.company_a
        line = {'product_id': product.id, 'product_uom_qty': qty}
        if route:
            line['route_id'] = route.id
        order = cls.env['sale.order'].with_company(company).create({
            'partner_id': cls.customer.id,
            'company_id': company.id,
            'order_line': [Command.create(line)],
        })
        if state == 'sale':
            order.action_confirm()
        elif state == 'cancel':
            order._action_cancel()
        cls._backdate(order, days_ago)
        return order

    # -------------------------------------------------------------------------
    # Engine helpers
    # -------------------------------------------------------------------------
    def _rgd(self, company=None):
        company = company or self.company_a
        tracked = self.engine._eligible_products(company)
        date_from = fields.Datetime.now() - timedelta(days=DEMAND_WINDOW_DAYS)
        return self.engine._collect_rgd(tracked, company, date_from)

    def _wad(self, product, company=None):
        company = company or self.company_a
        tracked = self.engine._eligible_products(company)
        date_from = fields.Datetime.now() - timedelta(days=DEMAND_WINDOW_DAYS)
        figures = self.engine._collect_wad(tracked, company, date_from)
        return figures.get(product.id, {'wad': 0.0, 'new_product': False})

    def _total(self, rgd, product, window=DEMAND_WINDOW_DAYS):
        today = date.today()
        return sum(
            qty for day, qty in rgd.get(product.id, {}).items()
            if (today - day).days <= window
        )


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderEngineRgd(ReorderEngineCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmp_1 = cls._component('RW-CMP-1')
        cls.cmp_2 = cls._component('RW-CMP-2')
        cls.cmp_3 = cls._component('RW-CMP-3')
        cls.cmp_4 = cls._component('RW-CMP-4')
        cls.cmp_5 = cls._component('RW-CMP-5', with_vendor=False)
        cls.cmp_6 = cls._component('RW-CMP-6')
        cls.cmp_7 = cls._component('RW-CMP-7')
        cls.new_1 = cls._component('RW-NEW-1')

        cls.fg_a = cls._parent('RW-FG-A', cls.manufacture_route)
        cls.fg_b = cls._parent('RW-FG-B', cls.manufacture_route)
        cls.fg_c = cls._parent('RW-FG-C', cls.manufacture_route)
        cls.sub_1 = cls._parent('RW-SUB-1', cls.manufacture_route)
        cls.fg_d = cls._parent('RW-FG-D', cls.manufacture_route)
        cls.kit_1 = cls._parent('RW-KIT-1', cls.manufacture_route)
        # Bought, not manufactured: never exploded.
        cls.buy_parent = cls._parent('RW-BUY-PARENT', cls.buy_route)

        cls._bom(cls.fg_a, [(cls.cmp_1, 2, None)])
        cls._bom(cls.fg_b, [(cls.cmp_2, 3, None)], header_qty=10)
        cls._bom(cls.fg_c, [(cls.sub_1, 2, None)])
        cls._bom(cls.sub_1, [(cls.cmp_3, 4, None)])
        cls._bom(cls.buy_parent, [(cls.cmp_4, 5, None)])
        cls._bom(cls.fg_d, [(cls.cmp_7, 1, cls.dozen_uom)])
        # Kit: the sale line keeps the kit product, so its components are only
        # ever visible through this explosion.
        cls._bom(cls.kit_1, [(cls.cmp_5, 2, None)], bom_type='phantom')

        cls._sell(cls.fg_a, 10, 300)
        cls._sell(cls.fg_a, 10, 200)
        cls._sell(cls.fg_a, 10, 100)
        cls._sell(cls.fg_a, 30, 30)
        cls._sell(cls.cmp_1, 20, 10)
        cls._sell(cls.fg_b, 100, 60)
        cls._sell(cls.fg_c, 5, 20)
        cls._sell(cls.buy_parent, 8, 15)
        cls._sell(cls.fg_d, 5, 10)
        cls._sell(cls.new_1, 50, 14)
        cls._sell(cls.kit_1, 20, 25)

        # Exclusion matrix, all on RW-CMP-6: only the first line counts.
        cls._sell(cls.cmp_6, 20, 20)
        cls._sell(cls.cmp_6, 100, 20, state='draft')
        cls._sell(cls.cmp_6, 100, 20, state='cancel')
        cls._sell(cls.cmp_6, -50, 20)
        cls._sell(cls.cmp_6, 100, 20, route=cls.dropship_route)

    # -------------------------------------------------------------------------
    # Demand windows
    # -------------------------------------------------------------------------
    def test_rgd_windows_bucket_by_order_date(self):
        rgd = self._rgd()
        self.assertAlmostEqual(self._total(rgd, self.cmp_1, 365), 140.0, places=2)
        self.assertAlmostEqual(self._total(rgd, self.cmp_1, 182), 100.0, places=2)
        self.assertAlmostEqual(self._total(rgd, self.cmp_1, 91), 80.0, places=2)

    def test_bom_header_quantity_is_divided(self):
        # 100 x FG-B, header qty 10, line 3 -> 0.3 per unit, not 3.
        self.assertAlmostEqual(self._total(self._rgd(), self.cmp_2), 30.0, places=2)

    def test_explosion_is_multi_level(self):
        # 5 x FG-C -> 10 x SUB-1 -> 40 x CMP-3. Native explode() would stop
        # here, since it only recurses into phantom sub-BOMs.
        self.assertAlmostEqual(self._total(self._rgd(), self.cmp_3), 40.0, places=2)

    def test_newest_bom_wins_when_several_are_active(self):
        """71 products in production carry more than one active BOM."""
        parent = self._parent('RW-MULTI-BOM', self.manufacture_route)
        older_component = self._component('RW-MULTI-OLD')
        newer_component = self._component('RW-MULTI-NEW')
        # The *lower* id gets the newer create_date, so this fails if the
        # selection falls back on id ordering instead of the date.
        newest_bom = self._bom(parent, [(newer_component, 3, None)])
        oldest_bom = self._bom(parent, [(older_component, 3, None)])
        for bom, days in ((newest_bom, 1), (oldest_bom, 100)):
            bom.flush_recordset()
            self.env.cr.execute(
                'UPDATE mrp_bom SET create_date = %s WHERE id = %s',
                (fields.Datetime.now() - timedelta(days=days), bom.id))
        (newest_bom | oldest_bom).invalidate_recordset(['create_date'])
        self._sell(parent, 10, 5)

        rgd = self._rgd()
        self.assertAlmostEqual(self._total(rgd, newer_component), 30.0, places=2)
        self.assertAlmostEqual(self._total(rgd, older_component), 0.0, places=2)

    def test_purchased_parent_is_not_exploded(self):
        self.assertAlmostEqual(self._total(self._rgd(), self.cmp_4), 0.0, places=2)

    def test_kit_components_are_counted_once(self):
        # The sale order line keeps the kit product and only the stock moves
        # explode, so skipping phantom BOMs would drop this demand entirely.
        self.assertAlmostEqual(self._total(self._rgd(), self.cmp_5), 40.0, places=2)

    def test_uom_is_converted(self):
        # 5 x FG-D, one Dozen of CMP-7 each.
        self.assertAlmostEqual(self._total(self._rgd(), self.cmp_7), 60.0, places=2)

    def test_exclusion_rules(self):
        # Quotation, cancelled, negative and dropshipped lines all drop out.
        self.assertAlmostEqual(self._total(self._rgd(), self.cmp_6), 20.0, places=2)

    def test_direct_sales_are_counted(self):
        self.assertAlmostEqual(self._total(self._rgd(), self.new_1), 50.0, places=2)

    # -------------------------------------------------------------------------
    # The flag drives the run
    # -------------------------------------------------------------------------
    def test_unflagged_product_gets_no_row(self):
        rgd = self._rgd()
        # FG-A is sold directly four times but is not flagged for purchasing.
        self.assertNotIn(self.fg_a.id, rgd)
        self.assertIn(self.cmp_1.id, rgd)

    def test_clearing_the_flag_drops_only_that_product(self):
        self.cmp_1.product_tmpl_id.automated_reorder_suggestions = False
        rgd = self._rgd()
        self.assertNotIn(self.cmp_1.id, rgd)
        self.assertAlmostEqual(self._total(rgd, self.cmp_2), 30.0, places=2)

    def test_flagged_component_under_unflagged_parent(self):
        # The whole point of the explosion: demand reaches CMP-1 only through
        # sales of FG-A, which carries no flag of its own.
        self.assertFalse(self.fg_a.product_tmpl_id.automated_reorder_suggestions)
        self.assertGreater(self._total(self._rgd(), self.cmp_1), 0.0)

    def test_dropship_route_on_the_product_is_excluded(self):
        self.new_1.product_tmpl_id.route_ids = [Command.link(self.dropship_route.id)]
        self.assertAlmostEqual(self._total(self._rgd(), self.new_1), 0.0, places=2)

    def test_line_route_overrides_the_product_dropship_route(self):
        # The product dropships by default, but this order was placed against
        # another route, so it is real demand on inventory.
        self.new_1.product_tmpl_id.route_ids = [Command.link(self.dropship_route.id)]
        from_stock = self.env['stock.route'].create({
            'name': 'RW Engine From Stock',
            'sale_selectable': True,
        })
        self._sell(self.new_1, 7, 5, route=from_stock)
        self.assertAlmostEqual(self._total(self._rgd(), self.new_1), 7.0, places=2)


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderEngineRgdMultiCompany(ReorderEngineCommon):
    """Company B's sales must not move company A's numbers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_b = cls.env['res.company'].create({'name': 'RW Engine Company B'})
        cls.env = cls.env(context=dict(
            cls.env.context, allowed_company_ids=[cls.company_a.id, cls.company_b.id]))
        cls.customer.company_id = False
        cls.cmp_1 = cls._component('RW-CMP-1')
        cls.fg_a = cls._parent('RW-FG-A', cls.manufacture_route)
        cls._bom(cls.fg_a, [(cls.cmp_1, 2, None)])
        cls._sell(cls.fg_a, 10, 30)
        cls._sell(cls.cmp_1, 26, 20, company=cls.company_b)

    def test_company_a_demand_excludes_company_b_sales(self):
        self.assertAlmostEqual(self._total(self._rgd(self.company_a), self.cmp_1), 20.0, places=2)

    def test_company_b_gets_its_own_demand(self):
        self.assertAlmostEqual(self._total(self._rgd(self.company_b), self.cmp_1), 26.0, places=2)


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestReorderEngineWad(ReorderEngineCommon):
    """Stage 2a: Greatest WAD and the Total WAD path for new products."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmp_1 = cls._component('RW-CMP-1')
        cls.new_1 = cls._component('RW-NEW-1')
        cls.quiet = cls._component('RW-QUIET')
        cls.fg_a = cls._parent('RW-FG-A', cls.manufacture_route)
        cls._bom(cls.fg_a, [(cls.cmp_1, 2, None)])

        # CMP-1: 140 / 100 / 80 across the three windows.
        cls._sell(cls.fg_a, 10, 300)
        cls._sell(cls.fg_a, 10, 200)
        cls._sell(cls.fg_a, 10, 100)
        cls._sell(cls.fg_a, 30, 30)
        cls._sell(cls.cmp_1, 20, 10)

        # NEW-1: created 35 days ago, one sale of 50 a fortnight ago.
        cls._sell(cls.new_1, 50, 14)
        cls._backdate_product(cls.new_1, 35)

    def test_greatest_window_wins(self):
        # 140/52 = 2.6923, 100/26 = 3.8462, 80/13 = 6.1538 -> the 3-month
        # window, so a recent surge is not averaged away by a quiet year.
        self.assertAlmostEqual(self._wad(self.cmp_1)['wad'], 6.1538, places=3)
        self.assertFalse(self._wad(self.cmp_1)['new_product'])

    def test_total_wad_for_a_new_product(self):
        # 35 days old -> 5 weeks; 50 units / 5 = 10.0 per week.
        figures = self._wad(self.new_1)
        self.assertAlmostEqual(figures['wad'], 10.0, places=3)
        self.assertTrue(figures['new_product'])

    def test_imported_history_does_not_trigger_total_wad(self):
        """A young record with old demand is an import, not a new product."""
        # Same product, but now sold 300 days ago as well: its real age is at
        # least 300 days, so the windowed path must be used. Without the guard
        # this would read (50 + 12) / 5 = 12.4 instead of ~2.38.
        self._sell(self.new_1, 12, 300)
        figures = self._wad(self.new_1)
        self.assertFalse(figures['new_product'])
        self.assertAlmostEqual(figures['wad'], 50 / 13.0, places=3)

    def test_product_created_today_uses_the_one_week_floor(self):
        today_product = self._component('RW-TODAY')
        self._sell(today_product, 7, 0)
        figures = self._wad(today_product)
        self.assertTrue(figures['new_product'])
        # max(1.0, 0/7) -> the whole demand lands in a single week.
        self.assertAlmostEqual(figures['wad'], 7.0, places=3)

    def test_ninety_one_days_is_not_a_new_product(self):
        boundary = self._component('RW-BOUNDARY')
        self._sell(boundary, 26, 30)
        self._backdate_product(boundary, 91)
        figures = self._wad(boundary)
        self.assertFalse(figures['new_product'])
        self.assertAlmostEqual(figures['wad'], 26 / 13.0, places=3)

    def test_no_demand_is_zero_not_a_division_error(self):
        figures = self._wad(self.quiet)
        self.assertEqual(figures['wad'], 0.0)
        self.assertFalse(figures['new_product'])

    def test_wad_is_computed_per_company(self):
        company_b = self.env['res.company'].create({'name': 'RW WAD Company B'})
        self.env['stock.warehouse'].search([('company_id', '=', company_b.id)], limit=1)
        self.assertAlmostEqual(self._wad(self.cmp_1)['wad'], 6.1538, places=3)
        self.assertEqual(self._wad(self.cmp_1, company_b)['wad'], 0.0)
