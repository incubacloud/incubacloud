"""The egress allow-list is answered, never left to the template.

``copier --defaults`` takes the template's own default for every
question the answers file leaves out. From template v9.6.0 on, the test
environment gained ``whitelisted_hosts_test``, whose default is a
32-entry list — payment gateways and tax agencies among them. Leaving
it out never meant "no allow-list": it meant every staging silently got
an egress policy nobody chose.

So it is always answered. What it is answered *with* is the instance's
own list (F1): a non-empty list makes the template render a NAT gateway
and a sidecar that points the odoo container's default route at it, so
a staging reaches what its operator wrote down and nothing else. Empty
is still meaningful — it leaves the staging on the host's central
whitelist, which is what every staging did before this model existed.

Same failure mode as ``backup_backend_password``, which this module
already answers explicitly for exactly this reason.
"""
from odoo.tests.common import TransactionCase


class TestWhitelistAnswers(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Egress"})
        self.host = self.env["cloud.host"].create({
            "name": "egress-host",
            "ip_address": "192.0.2.72",
            "user": "ubuntu",
            "wildcard_domain": "egress.example.com",
        })
        self.prod = self.env["cloud.instance"].create({
            "name": "egressprod",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "egressstag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })

    def _set(self, instance, hostnames):
        """Replace *instance*'s whitelist with *hostnames*, in order."""
        instance.whitelist_ids.unlink()
        self.env["cloud.instance.whitelist"].create([
            {"instance_id": instance.id, "hostname": h, "sequence": i * 10}
            for i, h in enumerate(hostnames, 1)
        ])

    def test_the_question_is_always_answered(self):
        """Both environments, both keys: an unanswered question is the
        template's 32-host default, not an empty list."""
        for inst in (self.staging, self.prod):
            answers = inst._render_copier_answers()
            for key in ("whitelisted_hosts_test", "whitelisted_hosts_devel"):
                self.assertIn(key, answers, f"{key} must be answered")

    def test_staging_answers_its_own_list(self):
        self._set(self.staging, ["api.example.com", "cdn.example.net"])
        answers = self.staging._render_copier_answers()
        self.assertEqual(
            answers["whitelisted_hosts_test"],
            ["api.example.com", "cdn.example.net"],
        )

    def test_the_order_is_the_operators(self):
        """The answer feeds the config-drift hash, so the same list has
        to render the same way twice — and a reordering is a real
        change to the file, not noise to be sorted away."""
        self._set(self.staging, ["b.example.com", "a.example.com"])
        self.assertEqual(
            self.staging._render_copier_answers()["whitelisted_hosts_test"],
            ["b.example.com", "a.example.com"],
        )

    def test_production_is_never_filtered_by_us(self):
        """A production instance is the customer's own deployment. Rows
        on it would be rows nobody applies, so the answer stays []."""
        self._set(self.prod, ["api.example.com"])
        self.assertEqual(
            self.prod._render_copier_answers()["whitelisted_hosts_test"], [],
        )

    def test_an_empty_list_is_a_valid_answer(self):
        """It is the fallback to the host's central whitelist, which is
        how every staging worked before F1."""
        self._set(self.staging, [])
        self.assertEqual(
            self.staging._render_copier_answers()["whitelisted_hosts_test"],
            [],
        )

    def test_the_devel_list_stays_empty(self):
        """The generated ``devel.yaml`` is never used — the panel points
        docker-compose.yml at prod.yaml or test.yaml — but an
        unanswered question would still let the template decide part of
        a file we ship."""
        self._set(self.staging, ["api.example.com"])
        self.assertEqual(
            self.staging._render_copier_answers()["whitelisted_hosts_devel"],
            [],
        )

    def test_the_compose_project_is_stated_outright(self):
        """The template falls back to ``$COMPOSE_PROJECT_NAME``, which
        only resolves because the deploy writes it into ``.env``. One
        file rescuing another is not an answer."""
        self.assertEqual(
            self.staging._render_copier_answers()[
                "whitelist_docker_project_test"
            ],
            self.staging.doodba_project_name,
        )

    def test_the_lists_are_not_shared_between_instances(self):
        """A mutable list handed out by reference would let one
        instance's edit leak into another's answers file."""
        self._set(self.staging, ["api.example.com"])
        first = self.staging._render_copier_answers()["whitelisted_hosts_test"]
        first.append("evil.example.com")
        second = self.staging._render_copier_answers()[
            "whitelisted_hosts_test"
        ]
        self.assertEqual(second, ["api.example.com"])
