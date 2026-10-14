import math

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

LEAD_TIME_TRIGGER_FIELDS = {'lead_time_weeks', 'delay'}


class ProductSupplierinfo(models.Model):
    _inherit = 'product.supplierinfo'

    lead_time_weeks = fields.Integer(
        string='Lead-time (Weeks)',
        required=True,
        default=1,
        help='Total duration, in weeks, between the initiation of the purchase order '
             'and delivery of the goods. Kept in sync with the native Delivery Lead '
             'Time (days) field: editing either one recomputes the other.',
    )
    standard_case_quantity = fields.Integer(
        string='Standard Case Quantity',
        required=True,
        default=1,
        help='Rounding factor used to set the To-Order Quantity of goods.',
    )

    @api.model
    def _weeks_from_days(self, days):
        # No floor here: this must be a pure, round-trip-consistent function of
        # `days` (weeks_from_days(0) == 0) so the onchange cascade guards below
        # can correctly recognize "already in sync" at every value, including
        # zero. Rejecting invalid values is @api.constrains's job, not this
        # derivation's
        return math.ceil((days or 0) / 7)

    def _backfill_lead_time_weeks(self):
        """Derive lead_time_weeks from each record's existing delay, without
        touching delay itself. Used by the post_init_hook for rows that
        predate this module's fields; delay is authoritative here, so the
        normal write() sync (which would push a derived delay back from the
        newly-set weeks) must be bypassed.
        """
        for info in self:
            weeks = info._weeks_from_days(info.delay)
            if info.lead_time_weeks != weeks:
                super(ProductSupplierinfo, info).write({'lead_time_weeks': weeks})

    def _sync_lead_time_fields(self, vals):
        self.ensure_one()
        if 'lead_time_weeks' in vals and 'delay' not in vals:
            new_delay = self.lead_time_weeks * 7
            if self.delay != new_delay:
                super(ProductSupplierinfo, self).write({'delay': new_delay})
        else:
            new_weeks = self._weeks_from_days(self.delay)
            if self.lead_time_weeks != new_weeks:
                super(ProductSupplierinfo, self).write({'lead_time_weeks': new_weeks})

    @api.onchange('delay')
    def _onchange_delay_sync_weeks(self):
        for info in self:
            new_weeks = info._weeks_from_days(info.delay)
            if info.lead_time_weeks != new_weeks:
                info.lead_time_weeks = new_weeks

    @api.onchange('lead_time_weeks')
    def _onchange_lead_time_weeks_sync_delay(self):
        # Guard against Odoo's onchange fixpoint cascade: setting
        # lead_time_weeks from _onchange_delay_sync_weeks re-triggers this
        # method, and since delay->weeks is lossy (ceiling rounding), blindly
        # writing delay = weeks * 7 back would silently overwrite whatever
        # delay the user just typed (e.g. 20 -> 3 weeks -> 21). Only rewrite
        # delay when it's not already consistent with the current weeks value.
        for info in self:
            if info._weeks_from_days(info.delay) != info.lead_time_weeks:
                info.delay = info.lead_time_weeks * 7

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for vals, record in zip(vals_list, records):
            record._sync_lead_time_fields(vals)
        return records

    def write(self, vals):
        result = super().write(vals)
        if LEAD_TIME_TRIGGER_FIELDS & set(vals.keys()):
            for record in self:
                record._sync_lead_time_fields(vals)
        return result

    @api.constrains('delay', 'lead_time_weeks', 'standard_case_quantity')
    def _check_lead_time_and_case_quantity(self):
        for info in self:
            # Zero lead time is allowed pending Right Weigh's confirmation (the
            # PDF says positive integers); see docs/implementation-decisions.md
            if info.lead_time_weeks < 0:
                raise ValidationError(_('Lead-time (Weeks) must not be negative.'))
            if info.delay < 0:
                raise ValidationError(_('Delivery Lead Time must not be negative.'))
            if info.standard_case_quantity <= 0:
                raise ValidationError(_('Standard Case Quantity must be a positive integer.'))
