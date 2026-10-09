"""Every email of this module goes out in the company layout.

The alerts, job notifications and the daily digest were bare HTML — the
digest with a green of one brand written into it — and the mail template
had no layout. They now share ``incubacloud.mail_layout``, which takes
logo, accent, name, website and contact address from the sending company
and names no brand of its own.
"""
from odoo.tests.common import TransactionCase, tagged

from ..mail_layout import MAIL_LAYOUT, wrap_in_mail_layout


@tagged('post_install', '-at_install')
class TestTheCompanyLayout(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write({
            'email_secondary_color': '#13579b',
            'website': 'https://brand.example.com',
            'email': 'help@brand.example.com',
        })
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('incubacloud.mail_logo_url', False)

    def _wrap(self, html='<p>The message itself</p>'):
        """The whole email around ``html``.

        :return: the rendered HTML, as a string.
        """
        return str(wrap_in_mail_layout(self.env, html))

    def test_the_message_is_inside_as_markup(self):
        email = self._wrap()
        self.assertIn('<p>The message itself</p>', email)
        self.assertNotIn('&lt;p&gt;', email)

    def test_harmful_markup_in_the_message_is_dropped(self):
        """Alert messages carry names from outside."""
        email = self._wrap('<p>Disk full</p><script>alert(1)</script>')
        self.assertIn('Disk full', email)
        self.assertNotIn('<script>', email)

    def test_the_accent_is_the_company_button_colour(self):
        self.assertIn('#13579b', self._wrap())

    def test_the_logo_is_this_database_by_default(self):
        self.assertIn(f'/logo.png?company={self.company.id}', self._wrap())

    def test_a_configured_logo_address_wins(self):
        """A database that sleeps cannot serve its own logo to a mail
        client; another address is configured instead."""
        self.ICP.set_param(
            'incubacloud.mail_logo_url', 'https://brand.example.com/logo.png',
        )
        email = self._wrap()
        self.assertIn('https://brand.example.com/logo.png', email)
        self.assertNotIn('/logo.png?company=', email)

    def test_the_footer_names_the_company(self):
        email = self._wrap()
        self.assertIn(self.company.name, email)
        self.assertIn('brand.example.com', email)
        self.assertIn('mailto:help@brand.example.com', email)

    def test_building_it_does_not_need_a_base_url(self):
        """It is built while an alert or a job is recorded; a database
        without ``web.base.url`` must not fail there. Links become
        absolute when ``mail.mail`` sends."""
        # The ORM refuses to delete the parameter; a database can still
        # lack it, as a freshly created tenant test database does.
        self.env.cr.execute(
            "DELETE FROM ir_config_parameter WHERE key = 'web.base.url'",
        )
        self.env.registry.clear_cache()
        self.assertIn('The message itself', self._wrap())

    def test_the_backup_quota_template_uses_it(self):
        template = self.env.ref('incubacloud.mail_template_backup_usage_alert')
        self.assertEqual(template.email_layout_xmlid, MAIL_LAYOUT)


@tagged('post_install', '-at_install')
class TestTheJobEmailUsesIt(TransactionCase):
    """Post-install, like the alert tests: creating a user needs every
    module's partner columns, ``account``'s among them."""

    def test_a_job_email_goes_out_in_the_layout(self):
        self.env.company.email_secondary_color = '#13579b'
        user = self.env['res.users'].create({
            'name': 'job-layout', 'login': 'job-layout',
            'email': 'job-layout@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
            'cloud_notification_level': 'all',
        })
        host = self.env['cloud.host'].create({
            'name': 'job-layout-host', 'ip_address': '10.0.0.61',
            'user': 'ubuntu', 'wildcard_domain': 'job-layout.example.com',
        })
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'host_hardening')], limit=1,
        )
        job = self.env['cloud.job'].create({
            'host_id': host.id, 'job_type_id': job_type.id,
        })
        self.env['cloud.job']._notify_by_email(job, 'failed')
        mail = self.env['mail.mail'].sudo().search([
            ('email_to', '=', user.email),
            ('subject', 'like', '[IncubaCloud] Job%'),
        ])
        self.assertEqual(len(mail), 1)
        self.assertIn('#13579b', mail.body_html)
        self.assertIn('/logo.png?company=', mail.body_html)
