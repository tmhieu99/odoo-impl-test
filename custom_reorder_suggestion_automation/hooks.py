def backfill_lead_time_weeks(env):
    """post_init_hook: pre-existing product.supplierinfo rows never pass
    through create()/write() when this module's lead_time_weeks column is
    added, so the schema migration would otherwise leave every row at the
    field's static default regardless of its real delay. Derive the correct
    value once here.
    """
    env['product.supplierinfo'].search([])._backfill_lead_time_weeks()
