{
    'name': 'Custom Re-Order Suggestion Automation',
    'version': '18.0.1.0.0',
    'summary': 'Automated purchasing re-order suggestion fields on items (Phase 1)',
    'category': 'Inventory/Purchase',
    'author': 'Right Weigh',
    'license': 'LGPL-3',
    'depends': [
        'product', 'stock', 'purchase', 'purchase_stock',
        'sale', 'sale_stock', 'mrp', 'sale_mrp', 'stock_dropshipping',
    ],
    'data': [
        'views/product_template_views.xml',
        'views/product_supplierinfo_views.xml',
        'views/stock_warehouse_orderpoint_views.xml',
    ],
    'installable': True,
    'application': False,
    'post_init_hook': 'backfill_lead_time_weeks',
}
