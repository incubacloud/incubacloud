"""Post-migrate for 1.0.107 — mark hosts that already declared a proxy.

``behind_cdn`` is new, and the render it gates already existed: until
now, declaring trusted proxy ranges was what made the rate limit key on
the forwarded chain. Anyone who set ranges by hand did so because a
proxy answers for that host, so the flag starts on for them and their
configuration comes out of the upgrade unchanged.

Hosts whose ranges arrive from a policy rather than the field are left
alone on purpose: the module that supplies the policy is the one that
knows whether a CDN is really in front, and it sets the flag itself.
"""


def migrate(cr, version):
    """Turn the flag on wherever the host's own range field is filled.

    Also re-runs the cron-bot assignment: the sweep that revokes expired
    upload keys is new, and a cron added by an update would otherwise
    keep the implicit owner, which in Odoo 19 means its server action
    carries none of the groups the execution pre-check needs.
    """
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
    cr.execute("""
        UPDATE cloud_host
           SET behind_cdn = TRUE
         WHERE coalesce(trusted_proxy_ranges, '') <> ''
           AND behind_cdn IS NOT TRUE
    """)
