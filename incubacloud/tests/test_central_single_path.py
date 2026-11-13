"""The central's single path: one name, the platform port, the panel's authority.

Behind ``metrics_central_single_path``. Off, every path of before is what
the other tests of the central check (``test_observability_central_public``
and the direct-path tests run with it off). On, with a public name: the
name is published on the host's platform port with a certificate the
panel signs, the panel and the agents move to it only once it answers from
the central's host, and the switch turned off puts back what they used.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
import requests
import yaml

from odoo import fields
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers import central_egress
from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.models import cloud_settings_central as central
from odoo.addons.incubacloud.models._direct_probe import ANY_ADDRESS
from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.cloud_metric_rule import promql_query
from odoo.addons.incubacloud.models.observability_agents_executor import (
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.models.cloud_host_central import CERT_REFUSED
from odoo.addons.incubacloud.models.observability_central_executor import (
    ACME_SAME_NAME_ALERT,
    ObservabilityCentralExecutor,
)
from odoo.addons.incubacloud.models.sync_direct_path_executor import (
    SyncDirectPathExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.tests import _agents_playbook as agents_playbook
from odoo.addons.incubacloud.tests._agents_playbook import PLAYBOOK
from odoo.addons.incubacloud.tests._direct import make_host
from odoo.addons.incubacloud.tests.test_observability_central_public import (
    _compose,
)
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)

NAME = "metrics.example.test"
LEGACY_READ = "http://172.17.0.1:8428/r"
LEGACY_WRITE = "https://metrics-cdn.example.test/w/api/v1/write"
SINGLE_READ = f"https://{NAME}:9443/r"
SINGLE_WRITE = f"https://{NAME}:9443/w/api/v1/write"
_OK = {"ok": True, "layer": None, "detail": "", "cert_expires": None, "source": "job"}
_DNS = {
    "ok": False, "layer": "dns", "detail": "resolves to 104.21.0.1",
    "cert_expires": None, "source": "job",
}
_TLS = {"ok": False, "layer": "tls", "detail": "curl 60", "cert_expires": None, "source": "job"}
_LIGHT_OK = _OK | {"source": "light"}
#: What the uri module says when the server refused the certificate.
_CERT_REFUSED_MSG = (
    "Status code was -1 and not [401]: Request failed: <urlopen error [SSL: "
    "CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local "
    "issuer certificate (_ssl.c:1010)>"
)
_CENTRAL_PLAYBOOK = (
    Path(__file__).resolve().parent.parent / "ansible" / "playbooks"
    / "observability_central.yml"
)


def _central_task(name):
    """Return the central's playbook task called *name*.

    :param str name: the task's name.
    :rtype: dict
    """
    return next(
        t for t in yaml.safe_load(_CENTRAL_PLAYBOOK.read_text())[0]["tasks"]
        if t.get("name") == name
    )


def _load(pem):
    """Return the certificate in *pem*.

    :rtype: cryptography.x509.Certificate
    """
    from cryptography import x509

    return x509.load_pem_x509_certificate(pem.encode())


class TestTheAuthority(BaseCase):
    """The authority and the leaf, without a database."""

    def test_the_authority_signs_leaves_only(self):
        from cryptography import x509

        cert, key = central.mint_authority("test CA")
        ca = _load(cert)
        constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
        self.assertTrue(constraints.ca)
        self.assertEqual(constraints.path_length, 0)
        self.assertIn("PRIVATE KEY", key)
        self.assertGreater(
            ca.not_valid_after - ca.not_valid_before, timedelta(days=3600),
        )

    def test_a_leaf_names_the_central_and_validates_against_the_authority(self):
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import ec

        ca_cert, ca_key = central.mint_authority("test CA")
        leaf_pem, leaf_key, expires = central.issue_leaf(ca_cert, ca_key, NAME)
        leaf = _load(leaf_pem)
        names = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName,
        ).value.get_values_for_type(x509.DNSName)
        self.assertEqual(names, [NAME])
        self.assertEqual(leaf.issuer, _load(ca_cert).subject)
        _load(ca_cert).public_key().verify(
            leaf.signature, leaf.tbs_certificate_bytes,
            ec.ECDSA(leaf.signature_hash_algorithm),
        )
        self.assertIn("PRIVATE KEY", leaf_key)
        self.assertLess(
            abs(
                (expires - datetime.now(timezone.utc).replace(tzinfo=None))
                - central.LEAF_LIFETIME
            ),
            timedelta(minutes=5),
        )

    def test_the_bundle_holds_the_usual_roots_and_the_authority(self):
        import certifi

        cert, _key = central.mint_authority("test CA")
        with tempfile.TemporaryDirectory() as directory:
            path = central.ca_bundle_path(cert, directory)
            text = Path(path).read_text(encoding="utf-8")
            roots = Path(certifi.where()).read_text(encoding="utf-8")
            self.assertIn(roots.strip()[:200], text)
            self.assertIn(cert.strip(), text)
            # Named by the authority: the same file the next time.
            self.assertEqual(central.ca_bundle_path(cert, directory), path)
            self.assertEqual(len(os.listdir(directory)), 1)


class SinglePathCase(ObservabilityExecutorCase):
    """A central on HX at a global address, with the platform port applied."""

    def setUp(self):
        super().setUp()
        self.host.write({
            "ip_address": "46.4.0.80", "direct_port": 9443,
            "direct_port_shipped": 9443, "traefik_deployed": True,
        })
        self.settings.write({
            "metrics_central_single_path": True,
            "metrics_central_public_host": NAME,
            "metrics_central_host_id": self.host.id,
            "metrics_central_url": LEGACY_READ,
            "metrics_remote_write_url": LEGACY_WRITE,
        })

    def _refusal(self):
        """Run the deployment's pre-run checks."""
        executor = self._make(ObservabilityCentralExecutor)
        executor.job.instance_id = self.env["cloud.instance"]
        executor.job.payload = {}
        with patch.object(
            ObservabilityCentralExecutor, "_write_system_direct", autospec=True,
        ):
            executor.pre_run_checks()

    def _deployed(self, **facts):
        """Run a deployment's extra vars and ``on_success``; return the vars.

        What the job's log said is kept in ``self.lines``.

        :param facts: what the playbook exported besides its health.
        """
        executor = self._make(ObservabilityCentralExecutor)
        self.lines = []
        executor._sys = self.lines.append
        executor.job.id = False
        extra = executor.get_extra_vars()
        executor._facts = {
            "ic_central_up": "True", "ic_central_gateway": "http://172.17.0.1:8428",
        } | facts
        executor._shipped_accounts = []
        asyncio.run(executor.on_success({}))
        self.settings.invalidate_recordset()
        return extra

    def _check(self, outcome):
        """Apply one check of the single name, as the sync job does.

        On a cursor of its own: what the test wrote is flushed first, and
        what the check wrote is read again after.
        """
        self.env.flush_all()
        result = self.env["cloud.settings"].sudo()._direct_probe_apply(
            central.NAME_PREFIX, outcome,
        )
        self.env.invalidate_all()
        return result

    def _jobs(self, code):
        """Return the jobs of type *code* on the central's host."""
        return self.env["cloud.job"].search([
            ("host_id", "=", self.host.id), ("job_type_id.code", "=", code),
        ])

    def _alert(self, code, host=None):
        """Return the active alert *code*, for *host* when given."""
        domain = [("code", "=", code), ("state", "=", "active")]
        if host is not None:
            domain.append(("host_id", "=", host.id))
        return self.env["cloud.alert"].sudo().search(domain)


class TestTheSettings(SinglePathCase):

    def test_the_authority_is_minted_once(self):
        first = self.settings._ensure_metrics_ca()
        self.assertEqual(self.settings._ensure_metrics_ca(), first)
        self.assertIn("BEGIN CERTIFICATE", first[0])

    def test_its_key_is_for_system_administrators_only(self):
        field = self.env["cloud.settings"]._fields["metrics_ca_key"]
        self.assertEqual(field.groups, "base.group_system")

    def test_verify_is_the_usual_roots_without_an_authority(self):
        self.assertIs(self.env["cloud.settings"]._central_tls_verify(), True)

    def test_verify_is_a_bundle_with_the_authority(self):
        cert, _key = self.settings._ensure_metrics_ca()
        path = self.env["cloud.settings"]._central_tls_verify()
        self.assertIn(cert.strip(), Path(path).read_text(encoding="utf-8"))

    def test_a_central_recorded_before_keeps_the_switch_off(self):
        """The update leaves a running central on the paths it had."""
        self.env.flush_all()
        turned = self.env["cloud.settings"]._single_path_off_where_a_central_ran()
        self.assertEqual(turned, 1)
        self.settings.invalidate_recordset()
        self.assertFalse(self.settings.metrics_central_single_path)

    def test_without_a_central_the_switch_is_on(self):
        self.settings.metrics_central_host_id = False
        self.env.flush_all()
        self.env["cloud.settings"]._single_path_off_where_a_central_ran()
        self.settings.invalidate_recordset()
        self.assertTrue(self.settings.metrics_central_single_path)

    def test_the_single_urls_carry_the_port(self):
        self.assertEqual(
            self.env["cloud.settings"]._central_single_urls(),
            {"read": SINGLE_READ, "write": SINGLE_WRITE},
        )

    def test_without_a_name_there_is_no_single_path(self):
        self.settings.metrics_central_public_host = False
        self.assertFalse(self.env["cloud.settings"]._central_single_path())
        self.assertEqual(self.env["cloud.settings"]._central_single_urls(), {})


class TestTheDeployment(SinglePathCase):

    def test_the_playbook_gets_the_name_the_port_and_a_leaf(self):
        extra = self._deployed()
        self.assertEqual(extra["ic_single_host"], NAME)
        self.assertEqual(extra["ic_central_port"], 9443)
        self.assertEqual(extra["ic_public_host"], "")
        leaf = _load(extra["ic_central_cert"])
        self.assertEqual(leaf.issuer, _load(self.settings.metrics_ca_cert).subject)
        self.assertIn("127.0.0.1/32", extra["ic_single_operator_sources"])
        self.assertIn("172.16.0.0/12", extra["ic_single_operator_sources"])
        self.assertTrue(self.settings.metrics_central_cert_expires)

    def test_switched_off_the_playbook_gets_what_it_got_before(self):
        self.settings.metrics_central_single_path = False
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_single_host"], "")
        self.assertEqual(extra["ic_central_cert"], "")
        self.assertEqual(extra["ic_public_host"], NAME)
        self.assertFalse(self.settings.metrics_ca_cert)

    def test_the_urls_stay_until_the_name_answers(self):
        self._deployed()
        self.assertEqual(self.settings.metrics_central_url, LEGACY_READ)
        self.assertEqual(self.settings.metrics_remote_write_url, LEGACY_WRITE)
        self.assertTrue(self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "sync_direct_path"),
        ]))

    def test_the_log_does_not_send_the_agents_to_the_bridge(self):
        """They move to the name: the hint of a central on one server is wrong."""
        self._deployed()
        self.assertFalse([line for line in self.lines if "local gateway" in line])

    def test_a_stored_certificate_of_the_name_is_said(self):
        """Traefik would serve it or the panel's, by turns (H3)."""
        self._deployed(ic_acme_same_name="found")
        alert = self._alert(ACME_SAME_NAME_ALERT, self.host)
        self.assertEqual(alert.level, "warning")
        self.assertIn(NAME, alert.message)
        self.assertIn("RB-35", alert.message)
        self.assertTrue([
            line for line in self.lines if line.startswith("⚠") and NAME in line
        ])

    def test_a_store_without_it_closes_the_alert(self):
        self._deployed(ic_acme_same_name="found")
        self._deployed(ic_acme_same_name="absent")
        self.assertFalse(self._alert(ACME_SAME_NAME_ALERT, self.host))

    def test_a_store_that_cannot_be_read_is_said_in_the_log_only(self):
        self._deployed(ic_acme_same_name="unread")
        self.assertFalse(self._alert(ACME_SAME_NAME_ALERT, self.host))
        self.assertTrue([line for line in self.lines if "could not be read" in line])

    def test_a_store_that_cannot_be_read_keeps_an_open_alert(self):
        """Nothing shows the certificate was removed: the alert stays."""
        self._deployed(ic_acme_same_name="found")
        self._deployed(ic_acme_same_name="unread")
        self.assertTrue(self._alert(ACME_SAME_NAME_ALERT, self.host))

    def test_the_store_is_looked_at_under_the_single_name_only(self):
        task = _central_task("Look for a stored certificate of the single name")
        self.assertEqual(task["when"], "ic_single_host | length > 0")
        self.assertIs(task["failed_when"], False)
        argv = task["ansible.builtin.command"]["argv"]
        self.assertIn('"{{ ic_single_host }}"', argv)
        self.assertEqual(argv[-1], "/etc/traefik/acme/acme.json")

    def test_what_the_store_said_is_exported(self):
        stats = _central_task("Export the central facts")["ansible.builtin.set_stats"]
        template = jinja2.Environment(  # nosec B701 — renders a fact as Ansible does, never HTML
            autoescape=False,
        ).from_string(stats["data"]["ic_acme_same_name"])
        for grep, said in (
            ({"rc": 0, "stdout": "1"}, "found"),
            ({"rc": 1, "stdout": "0"}, "absent"),
            ({"rc": 1, "stdout": ""}, "unread"),
            ({"rc": 2, "stdout": ""}, "unread"),
            ({"skipped": True}, ""),
        ):
            with self.subTest(grep=grep):
                self.assertEqual(template.render(ic_acme_grep=grep).strip(), said)

    def test_a_first_deployment_reads_on_the_bridge_meanwhile(self):
        self.settings.write({
            "metrics_central_url": False, "metrics_remote_write_url": False,
        })
        self._deployed()
        self.assertEqual(self.settings.metrics_central_url, "http://172.17.0.1:8428/r")

    def test_without_the_platform_port_it_is_refused(self):
        self.host.direct_port_shipped = 0
        with self.assertRaisesRegex(JobRefused, "no direct port applied"):
            self._refusal()

    def test_a_certificate_close_to_its_end_deploys_the_central_again(self):
        from odoo.addons.incubacloud.models.observability_central_executor import (
            CENTRAL_REVISION,
        )

        self.settings.write({
            "metrics_central_revision": CENTRAL_REVISION,
            "metrics_central_cert_expires": fields.Datetime.now() + timedelta(days=200),
        })
        Host = self.env["cloud.host"]
        self.assertFalse(Host._reconcile_central_revision())
        self.settings.metrics_central_cert_expires = (
            fields.Datetime.now() + timedelta(days=59)
        )
        self.assertTrue(Host._reconcile_central_revision())

    def test_a_certificate_close_to_its_end_raises_the_alert(self):
        self.settings.metrics_central_cert_expires = (
            fields.Datetime.now() + timedelta(days=20)
        )
        self.env["cloud.settings"]._central_cert_check()
        self.assertEqual(self._alert("metrics_central_cert_expiring").level, "critical")


class TestTheComposeFile(BaseCase):

    def _single(self):
        """The compose file of a central on its single name, parsed."""
        return yaml.safe_load(_compose(
            ic_single_host=NAME, ic_central_port=9443,
            ic_single_operator_sources=["10.0.0.0/8", "127.0.0.1/32"],
        ))

    def test_the_account_routes_on_the_platform_port_with_their_option(self):
        labels = self._single()["services"]["vmauth"]["labels"]
        prefix = "traefik.http.routers.ic-central."
        self.assertEqual(labels[prefix + "entrypoints"], "direct")
        self.assertEqual(labels[prefix + "tls.options"], "ic-central@file")
        self.assertEqual(labels[prefix + "priority"], "10010")
        self.assertTrue(labels[prefix + "rule"].startswith(f"Host(`{NAME}`)"))
        self.assertNotIn(prefix + "tls.certresolver", labels)

    def test_the_operator_routes_answer_the_named_sources_only(self):
        labels = self._single()["services"]["vmauth"]["labels"]
        self.assertEqual(
            labels["traefik.http.routers.ic-central-operator.middlewares"],
            "ic-central-operator-sources",
        )
        self.assertEqual(
            labels[
                "traefik.http.middlewares.ic-central-operator-sources"
                ".ipwhitelist.sourcerange"
            ],
            "10.0.0.0/8,127.0.0.1/32",
        )

    def test_nothing_is_published_on_443(self):
        labels = self._single()["services"]["vmauth"]["labels"]
        self.assertFalse([
            key for key, value in labels.items()
            if key.endswith(".entrypoints") and value != "direct"
        ])

    def test_both_names_side_by_side_during_the_move(self):
        labels = yaml.safe_load(_compose(
            ic_single_host=NAME, ic_direct_host=f"direct.{NAME}",
            ic_single_operator_sources=["127.0.0.1/32"],
        ))["services"]["vmauth"]["labels"]
        self.assertIn("traefik.http.routers.ic-central.rule", labels)
        self.assertIn("traefik.http.routers.ic-central-direct.rule", labels)


class TestTheAgentsFiles(BaseCase):
    """What the agents' playbook writes with and without the authority.

    Rendered as Ansible renders it (``trim_blocks`` on): an inline
    ``{% if %}`` in Fluent Bit's YAML left its indentation to the next
    line and broke the file in both modes (spike of 13-nov-2026).
    """

    _CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"

    def _loki(self, **overrides):
        """Fluent Bit's Loki output, parsed."""
        files = agents_playbook.fluent_bit_files(**overrides)
        return yaml.safe_load(files["fluent-bit.yaml"])["pipeline"]["outputs"][0]

    def _vmagent(self, **overrides):
        """vmagent's service in the agents' compose file, parsed."""
        compose = yaml.safe_load(agents_playbook.render(
            "Write the agents compose file", **agents_playbook.defaults(**overrides),
        ))
        return compose["services"]["vmagent"]

    def test_fluent_bit_without_the_authority_is_valid_yaml(self):
        loki = self._loki(ic_central_ca="")
        self.assertNotIn("tls.ca_file", loki)
        self.assertEqual(loki["http_user"], "acct")

    def test_fluent_bit_with_the_authority_trusts_its_file(self):
        loki = self._loki(ic_central_ca=self._CA)
        self.assertEqual(loki["tls.ca_file"], "/etc/central-ca.pem")
        self.assertEqual(loki["http_user"], "acct")

    def test_vmagent_keeps_its_queue_in_its_volume(self):
        """A re-apply recreates the container: the queue must not live in it (H4)."""
        for ca in ("", self._CA):
            with self.subTest(authority=bool(ca)):
                vmagent = self._vmagent(ic_central_ca=ca)
                self.assertIn("-remoteWrite.tmpDataPath=/vmagentdata", vmagent["command"])
                self.assertIn("vmagent-data:/vmagentdata", vmagent["volumes"])

    def test_vmagent_trusts_the_authority_only_when_given(self):
        flag = "-remoteWrite.tlsCAFile=/etc/central-ca.pem"
        self.assertNotIn(flag, self._vmagent(ic_central_ca="")["command"])
        vmagent = self._vmagent(ic_central_ca=self._CA)
        self.assertIn(flag, vmagent["command"])
        self.assertIn(
            "/home/incubacloud/observability/central-ca.pem:/etc/central-ca.pem:ro",
            vmagent["volumes"],
        )


class TestTheName(SinglePathCase):

    def test_a_name_that_answers_moves_the_urls_and_keeps_the_old_ones(self):
        self._check(_OK)
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_central_url, SINGLE_READ)
        self.assertEqual(self.settings.metrics_remote_write_url, SINGLE_WRITE)
        self.assertEqual(
            self.env["cloud.settings"]._central_legacy_urls(),
            {"read": LEGACY_READ, "write": LEGACY_WRITE},
        )

    def test_a_name_still_on_the_cdn_leaves_the_urls_and_says_which_record(self):
        self._check(_DNS)
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_remote_write_url, LEGACY_WRITE)
        alert = self._alert("metrics_central_dns_pending")
        self.assertEqual(alert.level, "warning")
        self.assertIn("46.4.0.80", alert.message)
        self.assertIn(NAME, alert.message)
        # Out of failing: two good checks, 30 minutes after it began.
        self.settings.metrics_central_name_failing_since = (
            fields.Datetime.now() - timedelta(minutes=31)
        )
        self._check(_OK)
        self._check(_OK)
        self.assertFalse(self._alert("metrics_central_dns_pending"))
        self.assertEqual(self.settings.metrics_remote_write_url, SINGLE_WRITE)

    def test_pending_for_half_a_day_turns_critical(self):
        self._check(_DNS)
        self.settings.metrics_central_name_failing_since = (
            fields.Datetime.now() - timedelta(hours=13)
        )
        self._check(_DNS)
        self.assertEqual(self._alert("metrics_central_dns_pending").level, "critical")

    def test_a_name_in_use_that_fails_twice_is_down(self):
        self._check(_OK)
        self._check(_TLS)
        self.assertFalse(self._alert("metrics_central_name_down"))
        self._check(_TLS)
        alert = self._alert("metrics_central_name_down")
        self.assertEqual(alert.level, "critical")
        self.assertIn("tls", alert.message)

    def test_a_record_pointing_elsewhere_once_in_use_is_pending_not_down(self):
        """After a move to another server, the record is the step left."""
        self._check(_OK)
        self._check(_DNS)
        self.assertFalse(self._alert("metrics_central_name_down"))
        self.assertTrue(self._alert("metrics_central_dns_pending"))

    def test_one_check_after_the_record_is_fixed_moves_the_urls(self):
        """A pending name needs no second check nor half an hour."""
        self._check(_DNS)
        self.assertEqual(self.settings.metrics_central_name_state, "failing")
        _old, new = self._check(_OK)
        self.assertEqual(new, "ok")
        self.assertEqual(self.settings.metrics_central_name_state, "ok")
        self.assertEqual(self.settings.metrics_remote_write_url, SINGLE_WRITE)
        self.assertFalse(self._alert("metrics_central_dns_pending"))

    def test_a_light_probe_does_not_move_a_pending_name(self):
        """It reads no DNS: only the complete check may say the record is right."""
        self._check(_DNS)
        self._check(_LIGHT_OK)
        self.assertEqual(self.settings.metrics_remote_write_url, LEGACY_WRITE)
        self.assertTrue(self._alert("metrics_central_dns_pending"))

    def test_a_record_lost_while_in_use_is_critical_at_once(self):
        self._check(_OK)
        self._check(_DNS)
        alert = self._alert("metrics_central_dns_pending")
        self.assertEqual(alert.level, "critical")
        self.assertIn("46.4.0.80", alert.message)
        # A passing resolver failure, or a move with the record still on
        # the earlier server, gives the same answer: it does not say that
        # no agent can write.
        self.assertNotIn("No agent can write", alert.message)
        self.assertIn("server the central is moving from", alert.message)
        self.assertIn("passing failure of the resolver", alert.message)

    def _cron_checks(self):
        """Run the cron's pass on a name last checked 11 minutes ago.

        :return: whether it asked the central's host to check the name.
        :rtype: bool
        """
        self.env["cloud.settings"]._direct_note_checked(
            central.NAME_PREFIX, fields.Datetime.now() - timedelta(minutes=11),
        )
        self.env.invalidate_all()
        with patch.object(
            type(self.host), "_enqueue_direct_sync", autospec=True, return_value=1,
        ) as enqueue:
            self.env["cloud.host"]._reconcile_central_name()
        return self.host in [call.args[0] for call in enqueue.call_args_list]

    def test_in_use_the_open_record_alert_is_checked_by_the_cron(self):
        """Not left to the hourly sync: a false alert closes within minutes."""
        self._check(_OK)
        self._check(_DNS)
        self.assertTrue(self._cron_checks())

    def test_in_use_without_the_alert_the_cron_leaves_it_to_the_sync(self):
        self._check(_OK)
        self.assertFalse(self._cron_checks())

    def test_in_use_one_good_check_closes_the_record_s_alert(self):
        """The record is right again; leaving failing keeps its hysteresis."""
        self._check(_OK)
        self._check(_DNS)
        self._check(_OK)
        self.assertFalse(self._alert("metrics_central_dns_pending"))
        self.assertFalse(self._alert("metrics_central_name_down"))
        self.assertEqual(self.settings.metrics_central_name_state, "failing")
        self.assertEqual(self.settings.metrics_remote_write_url, SINGLE_WRITE)

    def test_switched_off_the_urls_of_before_come_back(self):
        self._check(_OK)
        self.settings.metrics_central_single_path = False
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_central_url, LEGACY_READ)
        self.assertEqual(self.settings.metrics_remote_write_url, LEGACY_WRITE)
        self.assertEqual(self.env["cloud.settings"]._central_legacy_urls(), {})

    def test_switched_off_the_central_is_deployed_again(self):
        """Its certificate stays in force on every entrypoint until then (H2)."""
        self._check(_OK)
        self.settings.metrics_central_single_path = False
        self.assertTrue(self._jobs("deploy_metrics_central"))

    def test_switched_off_before_the_move_the_central_is_deployed_again(self):
        """The certificate is installed at the deployment, not at the move."""
        self.settings.metrics_central_single_path = False
        self.assertTrue(self._jobs("deploy_metrics_central"))

    def test_switched_off_without_a_name_nothing_is_deployed(self):
        self.settings.metrics_central_public_host = False
        self.settings.metrics_central_single_path = False
        self.assertFalse(self._jobs("deploy_metrics_central"))

    def test_switched_off_with_observability_off_nothing_is_deployed(self):
        """The deployment would switch observability on."""
        self.settings.metrics_enabled = False
        self.settings.metrics_central_single_path = False
        self.assertFalse(self._jobs("deploy_metrics_central"))

    def test_switched_off_the_name_moves_nothing(self):
        self.settings.metrics_central_single_path = False
        self._check(_OK)
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_remote_write_url, LEGACY_WRITE)

    def test_a_new_name_starts_over(self):
        self._check(_OK)
        self.settings.metrics_central_public_host = "other.example.test"
        self.assertEqual(self.settings.metrics_central_name_state, "unknown")

    def test_retiring_the_central_forgets_the_urls_of_before(self):
        self._check(_OK)
        self.env["cloud.settings"]._forget_metrics_central()
        self.assertEqual(self.env["cloud.settings"]._central_legacy_urls(), {})
        self.assertFalse(self.settings.metrics_central_cert_expires)


class TestTheList(SinglePathCase):

    def setUp(self):
        super().setUp()
        self.other = make_host(
            self.env, "sp-other", "46.4.0.81",
            direct_port=9443, direct_port_shipped=9443,
        )

    def test_the_central_takes_the_panels_servers_by_default(self):
        peers = self.host._direct_peers_for_host()
        self.assertIn("46.4.0.81/32", peers)
        self.assertNotIn(ANY_ADDRESS, peers)

    def test_any_address_opens_the_central_s_port_alone(self):
        self.settings.metrics_central_write_access = "anyone"
        self.assertEqual(self.host._direct_peers_for_host(), [ANY_ADDRESS])
        self.assertNotIn(ANY_ADDRESS, self.other._direct_peers_for_host())
        document = yaml.safe_load(self.host._shipped_direct_yml())
        self.assertEqual(
            document["http"]["middlewares"]["direct-peers"]["ipWhiteList"]["sourceRange"],
            [ANY_ADDRESS],
        )

    def test_any_address_needs_the_single_path(self):
        self.settings.write({
            "metrics_central_write_access": "anyone",
            "metrics_central_single_path": False,
        })
        self.assertNotIn(ANY_ADDRESS, self.host._direct_peers_for_host())

    def test_a_layer_may_fix_the_choice(self):
        self.settings.metrics_central_write_access = "anyone"
        with patch.object(
            type(self.env["cloud.settings"]), "_central_write_access_fixed",
            autospec=True, return_value="servers",
        ):
            self.assertNotIn(ANY_ADDRESS, self.host._direct_peers_for_host())

    def test_the_panel_outside_the_fleet_is_admitted(self):
        self.settings.metrics_central_operator_sources = "198.51.100.7/32\n2001:db8::/32"
        peers = self.host._direct_peers_for_host()
        self.assertIn("198.51.100.7/32", peers)
        self.assertNotIn("2001:db8::/32", peers)

    def test_switched_off_the_operator_sources_stay_out(self):
        self.settings.write({
            "metrics_central_operator_sources": "198.51.100.7/32",
            "metrics_central_single_path": False,
        })
        self.assertNotIn("198.51.100.7/32", self.host._direct_peers_for_host())


class TestTheSyncJob(SinglePathCase):

    def test_the_name_is_probed_with_the_authority(self):
        self.settings._ensure_metrics_ca()
        job_type = self.env["cloud.job.type"].search([("code", "=", "sync_direct_path")])
        job = self.env["cloud.job"].create({
            "host_id": self.host.id, "job_type_id": job_type.id, "name": "sync",
        })
        executor = SyncDirectPathExecutor(job, self.host)
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        asyncio.run(executor.before_execute(transport))
        names = {n["prefix"]: n for n in executor._names}
        single = names[central.NAME_PREFIX]
        uploaded = transport.upload_text_files.await_args.args[0]
        self.assertEqual(uploaded[single["ca_path"]], self.settings.metrics_ca_cert.strip())
        command = {c[0]: c[1] for c in executor.get_commands()}[f"Probe {NAME}"]
        self.assertIn(f"direct_probe.sh --cacert {single['ca_path']} {NAME} 46.4.0.80 9443", command)

    def test_switched_off_the_name_is_not_probed(self):
        self.settings.metrics_central_single_path = False
        self.assertEqual(self.host._direct_published_names(), [])


class TestTheAgents(SinglePathCase):

    def setUp(self):
        super().setUp()
        self.settings._ensure_metrics_ca()

    def test_before_the_move_they_get_no_authority(self):
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_central_ca"], "")
        self.assertEqual(extra["ic_panel_url"], "")
        self.assertEqual(extra["ic_remote_write_url"], LEGACY_WRITE)

    def test_after_the_move_they_get_the_url_and_the_authority(self):
        self._check(_OK)
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_remote_write_url"], SINGLE_WRITE)
        self.assertEqual(extra["ic_central_ca"], self.settings.metrics_ca_cert.strip())
        self.assertEqual(extra["ic_direct_write_url"], "")
        self.assertTrue(extra["ic_panel_url"])

    def test_a_host_installed_before_is_reapplied_after_the_move(self):
        self.host.write({
            "metrics_agents_state": "installed",
            "metrics_agents_write_url": LEGACY_WRITE,
            "known_hosts_key": "hx ssh-ed25519 AAAA",
        })
        self._check(_OK)
        with patch.object(
            type(self.host), "_enqueue_observability", autospec=True, return_value=1,
        ) as enqueue:
            self.env["cloud.host"]._reconcile_direct_agents()
        self.assertIn(self.host, [call.args[0] for call in enqueue.call_args_list])

    def _install(self, status, egress="203.0.113.9", error=""):
        """Run an install's ``on_success`` with what the server's check said."""
        self._check(_OK)
        executor = self._make(ObservabilityAgentsExecutor)
        executor._checks_central = True
        executor._facts = {
            "ic_agents_running_services": '["node-exporter", "cadvisor", "vmagent"]',
            "ic_write_url_installed": SINGLE_WRITE,
            "ic_central_reach": str(status), "ic_egress_ip": egress,
            "ic_central_reach_error": error,
        }
        asyncio.run(executor.on_success({}))

    def test_a_server_the_port_refuses_is_named_with_its_address(self):
        self._install(403)
        alert = self._alert("metrics_central_writer_outside", self.host)
        self.assertIn("203.0.113.9", alert.message)
        self.assertIn("46.4.0.80", alert.message)
        self.assertIn("Any address", alert.message)
        self.assertEqual(self.host.metrics_egress_ip, "203.0.113.9")

    def test_a_server_the_central_answers_is_not(self):
        self._install(-1)
        self.assertTrue(self._alert("metrics_central_writer_outside", self.host))
        self._install(401)
        self.assertFalse(self._alert("metrics_central_writer_outside", self.host))

    def test_a_certificate_the_server_refuses_is_not_blamed_on_its_address(self):
        """The port let it in: another certificate of the name answered (H3)."""
        self._install(-1, egress="46.4.0.80", error=_CERT_REFUSED_MSG)
        self.assertEqual(self.host.metrics_central_reach, CERT_REFUSED)
        alert = self._alert("metrics_central_writer_outside", self.host)
        self.assertNotIn("Correct the server's address", alert.message)
        self.assertNotIn("Any address", alert.message)
        self.assertIn("certificate", alert.message)
        self.assertIn("RB-35", alert.message)

    def test_its_silence_says_the_same(self):
        self._install(-1, egress="46.4.0.80", error=_CERT_REFUSED_MSG)
        now = self._silent_beside_a_writer()
        self.env["cloud.alert"].sudo().resolve_alert(
            "metrics_central_writer_outside", host=self.host,
        )
        self.assertEqual(self.env["cloud.host"]._central_silent_writers(now), 1)
        alert = self._alert("metrics_central_writer_outside", self.host)
        self.assertIn("RB-35", alert.message)
        self.assertNotIn("Correct the server's address", alert.message)

    def test_no_answer_for_another_reason_is_still_the_port(self):
        self._install(-1, error="Status code was -1 and not [401]: Request failed: "
                      "<urlopen error timed out>")
        self.assertEqual(self.host.metrics_central_reach, -1)
        alert = self._alert("metrics_central_writer_outside", self.host)
        self.assertIn("Correct the server's address", alert.message)

    def test_why_it_got_no_answer_is_exported(self):
        stats = agents_playbook.task("Export the agent facts")["ansible.builtin.set_stats"]
        self.assertIn("ic_central_try.msg", stats["data"]["ic_central_reach_error"])

    def test_an_answer_that_is_not_an_address_is_dropped(self):
        """A CDN's challenge page is not shown as the address."""
        self._install(403, egress="<!DOCTYPE html><title>Just a moment...</title>")
        self.assertFalse(self.host.metrics_egress_ip)
        alert = self._alert("metrics_central_writer_outside", self.host)
        self.assertNotIn("DOCTYPE", alert.message)
        self.assertIn("an address the panel could not learn", alert.message)

    def test_an_ipv6_answer_is_dropped(self):
        """The name has an A record only: the port never sees an IPv6 address."""
        self._install(403, egress="2a01:4f8:c0c:1::1")
        self.assertFalse(self.host.metrics_egress_ip)

    def test_the_address_is_asked_over_ipv4(self):
        with open(PLAYBOOK, encoding="utf-8") as handle:
            tasks = yaml.safe_load(handle)[0]["tasks"]
        task = next(
            t for t in tasks
            if t.get("name") == "Ask the panel which address this server leaves with"
        )
        argv = task["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:2], ["curl", "-4"])
        self.assertTrue(argv[-1].endswith("/cloud/egress_ip"))

    def test_where_a_layer_fixes_the_choice_the_alert_offers_one_way_out(self):
        with patch.object(
            type(self.env["cloud.settings"]), "_central_write_access_fixed",
            autospec=True, return_value="servers",
        ):
            self._install(403)
        alert = self._alert("metrics_central_writer_outside", self.host)
        self.assertNotIn("Any address", alert.message)

    def test_a_silent_server_while_others_write_is_named(self):
        self._check(_OK)
        other = make_host(self.env, "sp-writer", "46.4.0.82")
        now = fields.Datetime.now()
        old = now - timedelta(hours=1)
        for host, seen in ((self.host, old), (other, now)):
            host.write({
                "metrics_agents_state": "installed",
                "metrics_agents_write_url": SINGLE_WRITE,
                "metrics_agents_since": old,
                "metrics_last_seen": seen,
            })
        self.assertEqual(self.env["cloud.host"]._central_silent_writers(now), 1)
        self.assertTrue(self._alert("metrics_central_writer_outside", self.host))
        self.assertFalse(self._alert("metrics_central_writer_outside", other))
        self.host.metrics_last_seen = now
        self.env["cloud.host"]._central_silent_writers(now)
        self.assertFalse(self._alert("metrics_central_writer_outside", self.host))

    def _silent_beside_a_writer(self):
        """Leave this test's host silent for an hour while another writes.

        :return: the cron's time.
        :rtype: datetime
        """
        self._check(_OK)
        other = make_host(self.env, "sp-writer", "46.4.0.82")
        now = fields.Datetime.now()
        old = now - timedelta(hours=1)
        for host, seen in ((self.host, old), (other, now)):
            host.write({
                "metrics_agents_state": "installed",
                "metrics_agents_write_url": SINGLE_WRITE,
                "metrics_agents_since": old,
                "metrics_last_seen": seen,
            })
        return now

    def test_a_silent_server_its_own_check_got_through_is_not_named(self):
        """401 leaving with the address the panel has: the list admits it."""
        now = self._silent_beside_a_writer()
        self.host.write({"metrics_central_reach": 401, "metrics_egress_ip": "46.4.0.80"})
        self.assertEqual(self.env["cloud.host"]._central_silent_writers(now), 0)
        self.assertFalse(self._alert("metrics_central_writer_outside", self.host))

    def test_a_silent_server_the_panel_cannot_reach_is_not_named(self):
        """Switched off, or its network down: not the list."""
        now = self._silent_beside_a_writer()
        self.env["cloud.alert"].sudo().raise_alert(
            "host_unreachable", "Host unreachable", level="critical", host=self.host,
        )
        self.assertEqual(self.env["cloud.host"]._central_silent_writers(now), 0)
        self.assertFalse(self._alert("metrics_central_writer_outside", self.host))

    def test_switched_off_no_server_is_said_to_be_outside(self):
        now = self._silent_beside_a_writer()
        self.settings.metrics_central_single_path = False
        self.assertEqual(self.env["cloud.host"]._central_silent_writers(now), 0)
        self.assertFalse(self._alert("metrics_central_writer_outside", self.host))

    def test_switched_off_they_get_what_they_got_before(self):
        self.settings.metrics_central_single_path = False
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_central_ca"], "")
        self.assertEqual(extra["ic_panel_url"], "")
        self.assertEqual(extra["ic_remote_write_url"], LEGACY_WRITE)

    def test_all_silent_at_once_is_the_central_not_the_servers(self):
        self._check(_OK)
        old = fields.Datetime.now() - timedelta(hours=1)
        self.host.write({
            "metrics_agents_state": "installed",
            "metrics_agents_write_url": SINGLE_WRITE,
            "metrics_agents_since": old, "metrics_last_seen": old,
        })
        self.assertEqual(
            self.env["cloud.host"]._central_silent_writers(fields.Datetime.now()), 0,
        )


def _fingerprint(pem):
    """Return the SHA-256 fingerprint of the certificate in *pem*, in hex.

    :rtype: str
    """
    from cryptography.hazmat.primitives import hashes

    return _load(pem).fingerprint(hashes.SHA256()).hex()


class TestTheAuthorityInstalled(SinglePathCase):
    """Each host keeps the fingerprint of the authority its agents trust.

    The reconciliation re-applies a host whose fingerprint is not the one
    in force, as it does one whose URL is not: installed with the name's
    URL and no authority, or with an authority since rotated.
    """

    def setUp(self):
        super().setUp()
        self.settings._ensure_metrics_ca()
        self.host.write({
            "metrics_agents_state": "installed",
            "metrics_agents_write_url": SINGLE_WRITE,
            "known_hosts_key": "hx ssh-ed25519 AAAA",
        })

    def _reasons(self):
        """Run the reconciliation; return why this host was queued, if at all.

        :return: the reasons given for this host, in order.
        :rtype: list
        """
        with patch.object(
            type(self.host), "_enqueue_observability", autospec=True, return_value=1,
        ) as enqueue:
            self.env["cloud.host"]._reconcile_direct_agents()
        return [
            call.args[1] for call in enqueue.call_args_list
            if call.args[0] == self.host
        ]

    def _installed(self):
        """Run an install from its extra vars to its ``on_success``."""
        executor = self._make(ObservabilityAgentsExecutor)
        extra = executor.get_extra_vars()
        executor._facts = {
            "ic_agents_running_services": '["node-exporter", "cadvisor", "vmagent"]',
            "ic_write_url_installed": extra["ic_remote_write_url"],
            "ic_central_reach": "401", "ic_egress_ip": "46.4.0.80",
        }
        asyncio.run(executor.on_success({}))
        return extra

    def test_an_install_records_the_authority_it_installed(self):
        self._check(_OK)
        self._installed()
        self.assertEqual(
            self.host.metrics_agents_ca_fingerprint,
            _fingerprint(self.settings.metrics_ca_cert),
        )
        self.assertEqual(self._reasons(), [])

    def test_a_host_on_the_name_without_the_authority_is_reapplied(self):
        self._check(_OK)
        self.host.metrics_agents_ca_fingerprint = False
        self.assertEqual(self._reasons(), ["central authority changed"])

    def test_a_host_with_a_rotated_authority_is_reapplied(self):
        self._check(_OK)
        self.host.metrics_agents_ca_fingerprint = "0" * 64
        self.assertEqual(self._reasons(), ["central authority changed"])

    def test_a_host_with_the_authority_in_force_is_left_alone(self):
        self._check(_OK)
        self.host.metrics_agents_ca_fingerprint = _fingerprint(
            self.settings.metrics_ca_cert,
        )
        self.assertEqual(self._reasons(), [])

    def test_an_authority_forgotten_until_the_next_deployment_reapplies_nothing(self):
        """Rotating: between forgetting the authority and minting the next."""
        self._check(_OK)
        self.host.metrics_agents_ca_fingerprint = _fingerprint(
            self.settings.metrics_ca_cert,
        )
        self.settings.metrics_ca_cert = False
        self.assertEqual(self._reasons(), [])

    def test_a_changed_url_is_still_the_reason_given(self):
        self._check(_OK)
        self.host.write({
            "metrics_agents_write_url": LEGACY_WRITE,
            "metrics_agents_ca_fingerprint": False,
        })
        self.assertEqual(self._reasons(), ["direct path changed"])

    def test_switched_off_an_authority_on_the_panel_reapplies_nothing(self):
        """Off, no authority goes to the agents: no fingerprint, no re-apply."""
        self.settings.metrics_central_single_path = False
        self.host.write({
            "metrics_agents_write_url": LEGACY_WRITE,
            "metrics_agents_ca_fingerprint": False,
        })
        self.assertTrue(self.settings.metrics_ca_cert)
        self.assertEqual(self._reasons(), [])
        extra = self._installed()
        self.assertEqual(extra["ic_central_ca"], "")
        self.assertFalse(self.host.metrics_agents_ca_fingerprint)
        self.assertEqual(self._reasons(), [])

    def test_switched_off_a_host_recorded_before_is_left_alone(self):
        """A host with no fingerprint, as every host before this field, stays."""
        self.settings.metrics_central_single_path = False
        self.host.metrics_agents_write_url = False
        self.assertEqual(self._reasons(), [])


class TestTheReads(TransactionCase):

    def test_a_query_verifies_with_what_it_is_given(self):
        response = MagicMock(spec=requests.Response)
        response.json.return_value = {"status": "success", "data": {"result": []}}
        with patch(
            "odoo.addons.incubacloud.models.cloud_metric_rule.requests.get",
            return_value=response,
        ) as get:
            promql_query("https://m.example.test:9443/r", "up", verify="/tmp/ca.pem")
        self.assertEqual(get.call_args.kwargs["verify"], "/tmp/ca.pem")

    def test_the_charts_read_with_the_central_s_verification(self):
        from odoo.addons.incubacloud.models.metrics_charts import metrics_source

        settings = self.env["cloud.settings"].sudo()._get_system()
        settings.write({
            "metrics_enabled": True, "metrics_central_url": "http://vm.test:8428/r",
        })
        self.assertIs(metrics_source(self.env)["verify"], True)


class TestTheEgressAnswer(TransactionCase):

    def test_the_route_is_public_and_answers_the_caller(self):
        from odoo.addons.incubacloud.controllers.central_egress import (
            CentralEgressController,
        )

        routing = CentralEgressController.egress_ip.original_routing
        self.assertEqual(routing["auth"], "none")
        self.assertEqual(routing["methods"], ["GET"])

    def test_it_answers_without_writing_anything(self):
        """No rate-limit row per address: IPv6 gives a caller as many as it wants."""
        fake = MagicMock(spec=Request)
        fake.env = self.env
        Limit = self.env["cloud.rate.limit"].sudo()
        before = Limit.search_count([])
        with patch.object(central_egress, "request", fake), \
                patch.object(central_egress, "client_ip", return_value="203.0.113.9"):
            # The function under the route: its wrapper wants a real response.
            answer = central_egress.CentralEgressController.egress_ip.__wrapped__
            for _attempt in range(40):
                answer(central_egress.CentralEgressController())
        self.assertEqual(Limit.search_count([]), before)
        self.assertEqual(fake.make_response.call_args.args[0], "203.0.113.9")
        self.assertNotIn("status", fake.make_response.call_args.kwargs)


class TestTheSettingsScreen(SinglePathCase):
    """Settings › Monitoring reads, saves and checks the single path."""

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]

    def _route(self, method, **values):
        """Call the route handler *method* with *values* on this environment."""
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, "request", fake):
            return getattr(self.controller, method)(**values)

    def test_the_switch_who_writes_and_the_name_are_read_back(self):
        data = self._route("cloud_get_general_settings")
        self.assertIs(data["metrics_central_single_path"], True)
        self.assertEqual(data["metrics_central_write_access"], "servers")
        self.assertIs(data["metrics_central_write_access_fixed"], False)
        self.assertEqual(data["metrics_central_name"]["state"], "unknown")
        self.assertIn("46.4.0.80", data["metrics_central_name"]["record"])

    def test_switched_off_there_is_no_name_to_show(self):
        self.settings.metrics_central_single_path = False
        data = self._route("cloud_get_general_settings")
        self.assertIs(data["metrics_central_name"], False)

    def test_both_are_saved(self):
        answer = self._route(
            "cloud_save_general_settings",
            metrics_central_single_path=False, metrics_central_write_access="anyone",
        )
        self.assertEqual(answer, {"ok": True})
        self.assertFalse(self.settings.metrics_central_single_path)
        self.assertEqual(self.settings.metrics_central_write_access, "anyone")

    def test_a_choice_a_layer_fixes_is_not_saved(self):
        with patch.object(
            type(self.env["cloud.settings"]), "_central_write_access_fixed",
            autospec=True, return_value="servers",
        ):
            self._route("cloud_save_general_settings", metrics_central_write_access="anyone")
        self.assertEqual(self.settings.metrics_central_write_access, "servers")

    def test_check_now_queues_the_direct_path_on_the_central(self):
        answer = self._route("cloud_monitoring_check_central_name")
        self.assertTrue(answer["ok"])
        job = self.env["cloud.job"].browse(answer["job_id"])
        self.assertEqual(job.job_type_id.code, "sync_direct_path")
        self.assertEqual(job.host_id, self.host)

    def test_check_now_without_a_single_path_says_why(self):
        self.settings.metrics_central_single_path = False
        answer = self._route("cloud_monitoring_check_central_name")
        self.assertFalse(answer["ok"])
        self.assertIn("One path", answer["error"])
