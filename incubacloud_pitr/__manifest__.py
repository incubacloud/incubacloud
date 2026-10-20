{
    "name": "IncubaCloud Continuous Recovery",
    "version": "1.0.4",
    "summary": "Restore a production instance to any minute of its backup "
               "window (PostgreSQL archiving with wal-g, filestore copied "
               "as it changes)",
    "description": """
Continuous recovery (point-in-time recovery) for IncubaCloud instances.

Off until switched on per production instance. Once on, the instance's
database streams its write-ahead log to the instance's own backup bucket
(wal-g: compressed, encrypted on the host, with a daily base copy) and its
filestore is copied as it changes, so it can be restored to any minute of
its backup window. Stagings never archive. Uninstalling is refused while
an instance still has it on.
""",
    "category": "Tools",
    "website": "https://www.incubacloud.io",
    "author": "IncubaCloud",
    "license": "Other proprietary",
    "depends": ["incubacloud"],
    "data": [
        "data/job_type.xml",
        "data/pitr_cron.xml",
        "data/alert_policy_data.xml",
    ],
    "assets": {
        "incubacloud.assets_prod": [
            "incubacloud_pitr/static/src/components/**/*",
        ],
        "web.assets_unit_tests": [
            "incubacloud_pitr/static/src/components/**/*.js",
            "incubacloud_pitr/static/tests/**/*.test.js",
        ],
    },
    "installable": True,
    "auto_install": False,
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
}
