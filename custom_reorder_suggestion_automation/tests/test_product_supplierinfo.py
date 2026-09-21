from lxml import etree

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import Form, TransactionCase, tagged

from ..hooks import backfill_lead_time_weeks

MODULE = 'custom_reorder_suggestion_automation'


@tagged('post_install', '-at_install', 'custom_reorder_suggestion_automation')
class TestProductSupplierinfoLeadTimeFields(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SupplierInfo = cls.env['product.supplierinfo']
        cls.vendor = cls.env['res.partner'].create({'name': 'Lead Time Vendor'})
        cls.product_tmpl = cls.env['product.template'].create({'name': 'Lead Time Product'})

    def _create_seller(self, **vals):
        values = {
            'partner_id': self.vendor.id,
            'product_tmpl_id': self.product_tmpl.id,
        }
        values.update(vals)
        return self.SupplierInfo.create(values)

    def _assert_lead_time(self, seller, delay, weeks):
        self.assertRecordValues(seller, [{'delay': delay, 'lead_time_weeks': weeks}])

    # -------------------------------------------------------------------------
    # Field definitions
    # -------------------------------------------------------------------------

    def test_field_definitions(self):
        expected = {
            'lead_time_weeks': 'Lead-time (Weeks)',
            'standard_case_quantity': 'Standard Case Quantity',
        }
        for name, label in expected.items():
            with self.subTest(field=name):
                field = self.SupplierInfo._fields.get(name)
                self.assertTrue(field, f"product.supplierinfo.{name} is not defined")
                self.assertEqual(field.type, 'integer')
                self.assertEqual(field.string, label)
                self.assertTrue(field.store)
                self.assertTrue(field.required)
                self.assertTrue(self.env['ir.model.fields']._get('product.supplierinfo', name))
                self.assertTrue(self.env.ref(
                    f'{MODULE}.field_product_supplierinfo__{name}', raise_if_not_found=False))

    def test_field_defaults(self):
        self.assertEqual(
            self.SupplierInfo.default_get(['lead_time_weeks', 'standard_case_quantity']),
            {'lead_time_weeks': 1, 'standard_case_quantity': 1},
        )
        seller = self._create_seller()
        self._assert_lead_time(seller, delay=1, weeks=1)
        self.assertEqual(seller.standard_case_quantity, 1)

    def test_views_contain_fields(self):
        views = {
            'purchase.product_supplierinfo_tree_view2': 'list',
            'product.product_supplierinfo_form_view': 'form',
        }
        for xmlid, view_type in views.items():
            with self.subTest(view=xmlid):
                view = self.env.ref(xmlid)
                arch = etree.fromstring(
                    self.SupplierInfo.get_view(view.id, view_type)['arch'])
                field_names = [node.get('name') for node in arch.iter('field')]
                for name in ('delay', 'lead_time_weeks', 'standard_case_quantity'):
                    self.assertIn(name, field_names)
                # Placed after the native lead time, weeks before case quantity
                self.assertLess(field_names.index('delay'), field_names.index('lead_time_weeks'))
                self.assertLess(
                    field_names.index('lead_time_weeks'),
                    field_names.index('standard_case_quantity'),
                )

    # -------------------------------------------------------------------------
    # Day -> week conversion (round up)
    # -------------------------------------------------------------------------

    def test_weeks_from_days(self):
        cases = {False: 0, 0: 0, 1: 1, 6: 1, 7: 1, 8: 2, 14: 2, 15: 3, 20: 3, 21: 3, 22: 4}
        for days, weeks in cases.items():
            with self.subTest(days=days):
                self.assertEqual(self.SupplierInfo._weeks_from_days(days), weeks)

    # -------------------------------------------------------------------------
    # create() / write() sync
    # -------------------------------------------------------------------------

    def test_create_with_delay_derives_weeks(self):
        self._assert_lead_time(self._create_seller(delay=20), delay=20, weeks=3)

    def test_create_with_weeks_derives_delay(self):
        self._assert_lead_time(self._create_seller(lead_time_weeks=4), delay=28, weeks=4)

    def test_create_with_both_delay_wins(self):
        self._assert_lead_time(self._create_seller(delay=20, lead_time_weeks=5), delay=20, weeks=3)

    def test_create_multi_syncs_each_record(self):
        sellers = self.SupplierInfo.create([
            {'partner_id': self.vendor.id, 'product_tmpl_id': self.product_tmpl.id, 'delay': 10},
            {'partner_id': self.vendor.id, 'product_tmpl_id': self.product_tmpl.id, 'lead_time_weeks': 3},
            {'partner_id': self.vendor.id, 'product_tmpl_id': self.product_tmpl.id},
        ])
        self.assertRecordValues(sellers, [
            {'delay': 10, 'lead_time_weeks': 2},
            {'delay': 21, 'lead_time_weeks': 3},
            {'delay': 1, 'lead_time_weeks': 1},
        ])

    def test_write_delay_derives_weeks(self):
        seller = self._create_seller(lead_time_weeks=2)
        for delay, weeks in ((15, 3), (14, 2), (1, 1), (36, 6)):
            with self.subTest(delay=delay):
                seller.delay = delay
                self._assert_lead_time(seller, delay=delay, weeks=weeks)

    def test_write_weeks_derives_delay(self):
        seller = self._create_seller(delay=3)
        seller.lead_time_weeks = 5
        self._assert_lead_time(seller, delay=35, weeks=5)

    def test_write_both_delay_wins(self):
        seller = self._create_seller()
        seller.write({'delay': 9, 'lead_time_weeks': 7})
        self._assert_lead_time(seller, delay=9, weeks=2)

    def test_write_multi_syncs_each_record(self):
        sellers = self._create_seller(delay=1) | self._create_seller(lead_time_weeks=4)
        sellers.write({'delay': 10})
        self.assertRecordValues(sellers, [
            {'delay': 10, 'lead_time_weeks': 2},
            {'delay': 10, 'lead_time_weeks': 2},
        ])

    def test_unrelated_write_does_not_touch_lead_time(self):
        seller = self._create_seller(delay=20)
        seller.write({'price': 12.5, 'standard_case_quantity': 24})
        self._assert_lead_time(seller, delay=20, weeks=3)
        self.assertEqual(seller.standard_case_quantity, 24)

    # -------------------------------------------------------------------------
    # Constraints
    # -------------------------------------------------------------------------

    def test_negative_lead_time_rejected(self):
        for vals in ({'lead_time_weeks': -1}, {'delay': -7}, {'delay': -1}):
            with self.subTest(vals=vals), self.assertRaises(ValidationError):
                self._create_seller(**vals)
        seller = self._create_seller()
        with self.assertRaises(ValidationError):
            seller.lead_time_weeks = -2
        with self.assertRaises(ValidationError):
            seller.delay = -1  # derives 0 weeks, but negative days are still invalid

    def test_zero_lead_time_allowed(self):
        # Zero is allowed pending Right Weigh's confirmation (the PDF says positive)
        self._assert_lead_time(self._create_seller(delay=0), delay=0, weeks=0)
        self._assert_lead_time(self._create_seller(lead_time_weeks=0), delay=0, weeks=0)
        seller = self._create_seller(delay=20)
        seller.delay = 0
        self._assert_lead_time(seller, delay=0, weeks=0)
        seller.lead_time_weeks = 2
        seller.lead_time_weeks = 0
        self._assert_lead_time(seller, delay=0, weeks=0)

    def test_confirm_po_from_new_vendor_creates_seller(self):
        # Regression: purchase adds the vendor with delay=0, whose derived
        # 0 weeks used to be rejected and block the PO confirmation
        product = self.env['product.product'].create({'name': 'No Seller Yet', 'purchase_ok': True})
        order = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({
                'product_id': product.id, 'product_qty': 1, 'price_unit': 10.0,
            })],
        })
        order.button_confirm()
        self.assertEqual(order.state, 'purchase')
        self.assertRecordValues(product.seller_ids, [
            {'partner_id': self.vendor.id, 'delay': 0, 'lead_time_weeks': 0},
        ])

    def test_non_positive_case_quantity_rejected(self):
        for qty in (0, -12):
            with self.subTest(qty=qty), self.assertRaises(ValidationError):
                self._create_seller(standard_case_quantity=qty)
        seller = self._create_seller()
        with self.assertRaises(ValidationError):
            seller.standard_case_quantity = 0

    # -------------------------------------------------------------------------
    # post_init_hook backfill
    # -------------------------------------------------------------------------

    def test_backfill_hook_derives_weeks_from_delay(self):
        seller_20 = self._create_seller(delay=20)
        seller_14 = self._create_seller(delay=14)
        seller_0 = self._create_seller(delay=7)
        sellers = seller_20 | seller_14 | seller_0
        # Simulate rows that predate the module: column filled with the static default
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE product_supplierinfo SET lead_time_weeks = 1 WHERE id IN %s",
            [tuple(sellers.ids)],
        )
        # 0-day row as core purchase creates it; used to crash the install
        self.env.cr.execute(
            "UPDATE product_supplierinfo SET delay = 0 WHERE id = %s", [seller_0.id])
        sellers.invalidate_recordset(['delay', 'lead_time_weeks'])
        self.assertEqual(sellers.mapped('lead_time_weeks'), [1, 1, 1])

        backfill_lead_time_weeks(self.env)

        # delay is authoritative: it must not be rewritten from the stale weeks
        self.assertRecordValues(sellers, [
            {'delay': 20, 'lead_time_weeks': 3},
            {'delay': 14, 'lead_time_weeks': 2},
            {'delay': 0, 'lead_time_weeks': 0},
        ])

    # -------------------------------------------------------------------------
    # Live form editing (onchange cycle)
    # -------------------------------------------------------------------------

    def _seller_form(self):
        model = self.SupplierInfo.with_context(default_product_tmpl_id=self.product_tmpl.id)
        form = Form(model, view='product.product_supplierinfo_form_view')
        form.partner_id = self.vendor
        return form

    def test_form_typing_delay_keeps_typed_value(self):
        # Regression: onchange cascade used to rewrite 20 days -> 3 weeks -> 21 days
        form = self._seller_form()
        form.delay = 20
        self.assertEqual(form.lead_time_weeks, 3)
        self.assertEqual(form.delay, 20)
        self._assert_lead_time(form.save(), delay=20, weeks=3)

    def test_form_typing_weeks_updates_delay(self):
        form = self._seller_form()
        form.lead_time_weeks = 4
        self.assertEqual(form.delay, 28)
        self.assertEqual(form.lead_time_weeks, 4)
        self._assert_lead_time(form.save(), delay=28, weeks=4)

    def test_form_alternating_edits(self):
        form = self._seller_form()
        form.delay = 20
        form.lead_time_weeks = 5
        self.assertEqual(form.delay, 35)
        form.delay = 36
        self.assertEqual(form.lead_time_weeks, 6)
        self.assertEqual(form.delay, 36)
        form.standard_case_quantity = 12
        self.assertEqual(form.delay, 36)
        seller = form.save()
        self._assert_lead_time(seller, delay=36, weeks=6)
        self.assertEqual(seller.standard_case_quantity, 12)

    def test_form_zero_weeks_not_bounced(self):
        # Regression: typed 0 used to be silently bounced back to 1
        form = self._seller_form()
        form.lead_time_weeks = 0
        self.assertEqual(form.lead_time_weeks, 0)
        self.assertEqual(form.delay, 0)
        self._assert_lead_time(form.save(), delay=0, weeks=0)

    def test_form_zero_delay_saved(self):
        form = self._seller_form()
        form.delay = 0
        self.assertEqual(form.lead_time_weeks, 0)
        self._assert_lead_time(form.save(), delay=0, weeks=0)

    def test_form_negative_weeks_rejected_on_save(self):
        form = self._seller_form()
        form.lead_time_weeks = -1
        self.assertEqual(form.delay, -7)
        with self.assertRaises(ValidationError):
            form.save()

    def test_form_zero_case_quantity_rejected_on_save(self):
        form = self._seller_form()
        form.standard_case_quantity = 0
        with self.assertRaises(ValidationError):
            form.save()

    def test_purchase_tab_inline_line_syncs_live(self):
        with Form(self.product_tmpl) as tmpl_form:
            with tmpl_form.seller_ids.new() as line:
                line.partner_id = self.vendor
                line.delay = 20
                self.assertEqual(line.lead_time_weeks, 3)
                self.assertEqual(line.delay, 20)
                line.standard_case_quantity = 6
            with tmpl_form.seller_ids.new() as line:
                line.partner_id = self.vendor
                line.lead_time_weeks = 2
                self.assertEqual(line.delay, 14)
        self.assertRecordValues(self.product_tmpl.seller_ids, [
            {'delay': 20, 'lead_time_weeks': 3, 'standard_case_quantity': 6},
            {'delay': 14, 'lead_time_weeks': 2, 'standard_case_quantity': 1},
        ])
