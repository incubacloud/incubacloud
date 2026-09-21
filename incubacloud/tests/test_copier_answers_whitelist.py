"""The egress allow-list is answered, never left to the template.

``copier --defaults`` takes the template's own default for every
question the answers file leaves out. From template v9.6.0 on, the test
environment gained ``whitelisted_hosts_test``, whose default is a
32-entry list — payment gateways and tax agencies among them. A
non-empty list makes the template render a NAT gateway plus a sidecar
that shares the odoo container's network namespace, both with
``NET_ADMIN`` and the sidecar mounting the host's docker socket.

So not answering it does not mean "no allow-list": it means every
staging silently gets an egress policy nobody chose and two privileged
containers this module neither limits nor probes. Egress for test
instances is the host's own whitelist network
(``cloud.host.whitelist``), which the template keeps declaring as
external and joining.

Same failure mode as ``backup_backend_password``, which this module
already answers explicitly for exactly this reason.
"""
from odoo.tests.common import TransactionCase

_KEYS = ("whitelisted_hosts_test", "whitelisted_hosts_devel")


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

    def test_staging_answers_both_questions_empty(self):
        answers = self.staging._render_copier_answers()
        for key in _KEYS:
            self.assertIn(key, answers, f"{key} must be answered explicitly")
            self.assertEqual(answers[key], [], f"{key} must stay empty")

    def test_production_answers_them_too(self):
        """The answers file is one file per instance and copier reads all
        of it; leaving the question out of a production answers file
        would let the default decide a devel.yaml we still ship."""
        answers = self.prod._render_copier_answers()
        for key in _KEYS:
            self.assertIn(key, answers)
            self.assertEqual(answers[key], [])

    def test_the_lists_are_not_shared_between_instances(self):
        """A mutable default shared across renders would let one
        instance's edit leak into another's answers file."""
        first = self.staging._render_copier_answers()["whitelisted_hosts_test"]
        first.append("evil.example.com")
        second = self.prod._render_copier_answers()["whitelisted_hosts_test"]
        self.assertEqual(second, [])
