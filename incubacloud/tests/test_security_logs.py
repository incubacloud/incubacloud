"""Each server's security logs: Fluent Bit, its switch, and the update.

Fluent Bit replaced promtail on 12-nov-2026 (F3 of the observability
plan): it runs only where the server's *Send security logs* is on and the
central has a log store, holds its credential in a 0600 file, labels with
a closed set, and keeps its cursors in a volume the switch and the
teardown remove. The switch re-applies that server alone, also after a
failed install; the update gives every server the value
``_security_logs_after_upgrade`` decides.
"""
import asyncio
import base64
import importlib.util
import os
import re
from unittest.mock import patch

import asyncssh
import yaml

from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.observability_agents_executor import (
    AGENTS_REVISION,
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.tests import _agents_playbook as playbook
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)

#: The label names the security counters and Loki's streams may carry
#: (risk 7 of the plan): never an address, a user or a path.
CLOSED_LABELS = {"kind", "method", "jail", "action", "key", "who", "source"}

#: And the values each one may take.
CLOSED_VALUES = {
    "kind": {"panel", "other", "ansible", "panel_user"},
    "method": {"publickey", "password"},
    "jail": {"sshd", "incubacloud-weblogin"},
    "action": {"found", "ban", "unban", "already_banned"},
    "key": {"passwd_changes", "shadow_changes", "sudoers_changes", "ssh_config_changes"},
    "who": {"system", "root", "panel", "user"},
    "source": {"sshd", "sudo", "fail2ban", "auditd"},
}


def _key_file():
    """Return a new key as ``key_file`` stores it, and its fingerprint."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    return (
        base64.b64encode(key.export_private_key()).decode(),
        key.get_fingerprint("sha256"),
    )


class TestTheRenderedCollector(TransactionCase):
    """What the playbook writes for Fluent Bit."""

    def _config(self, **overrides):
        return yaml.safe_load(playbook.fluent_bit_files(**overrides)["fluent-bit.yaml"])

    def _compose(self, **overrides):
        return yaml.safe_load(playbook.render(
            "Write the agents compose file", **playbook.defaults(**overrides),
        ))

    def test_the_labels_are_a_closed_set(self):
        """Every counter's labels and Loki's are among the closed names."""
        config = self._config()
        lua = playbook.fluent_bit_files()["classify.lua"]
        labels = set()
        for flt in config["pipeline"]["filters"]:
            if flt["name"] != "log_to_metrics":
                continue
            for label in flt.get("add_label") or []:
                name, _value = label.split()
                labels.add(name)
        loki = config["pipeline"]["outputs"][0]
        self.assertEqual(loki["labels"], "log_type=security")
        self.assertEqual(loki["label_keys"], "$host,$source")
        self.assertLessEqual(labels, CLOSED_LABELS)
        # Every value the classification gives a label is a literal of
        # its closed set.
        fields = {
            "c_acc_kind": "kind", "c_acc_method": "method", "c_sudo_kind": "kind",
            "c_f2b_action": "action", "c_audit_who": "who", "source": "source",
        }
        for field, label in fields.items():
            for value in re.findall(rf'out\.{field} = "([a-z_]+)"', lua):
                self.assertIn(value, CLOSED_VALUES[label], (field, value))
        for value in re.findall(r'= "([a-z_]+)"', lua.split("local F2B =")[1].split("\n")[0]):
            self.assertIn(value, CLOSED_VALUES["action"])

    def test_the_credential_is_in_a_0600_file_never_logged(self):
        task = playbook.task("Write the security log collector's configuration")
        self.assertEqual(task["ansible.builtin.copy"]["mode"], "0600")
        self.assertIs(task["no_log"], True)
        rendered = playbook.fluent_bit_files()["fluent-bit.yaml"]
        self.assertIn("s3cret-token", rendered)
        service = self._compose()["services"]["fluent-bit"]
        self.assertNotIn("environment", service)
        self.assertNotIn("s3cret-token", yaml.safe_dump(self._compose()))

    def test_fluent_bit_runs_only_with_the_switch_and_a_log_store(self):
        self.assertIn("fluent-bit", self._compose()["services"])
        self.assertIn("fluent-bit-data", self._compose()["volumes"])
        off = self._compose(ic_fluent_bit=False)
        self.assertNotIn("fluent-bit", off["services"])
        self.assertNotIn("fluent-bit-data", off["volumes"] or {})
        decide = playbook.task("Decide whether this server sends its security logs")
        self.assertIn(
            "(ic_security_logs | bool) and (ic_logs_url | length > 0)",
            decide["ansible.builtin.set_fact"]["ic_fluent_bit"],
        )

    def test_no_promtail_and_no_docker_socket(self):
        compose = yaml.safe_dump(self._compose())
        self.assertNotIn("promtail", compose)
        self.assertNotIn("docker.sock", compose)
        names = [t.get("name") for t in playbook.tasks()]
        self.assertIn("Remove what promtail left", names)
        self.assertIn("Remove promtail's image", names)
        self.assertIs(
            playbook.task("Remove promtail's image")["failed_when"], False,
        )

    def test_the_container_has_no_power_it_does_not_need(self):
        service = self._compose()["services"]["fluent-bit"]
        self.assertIs(service["read_only"], True)
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["cap_add"], ["DAC_READ_SEARCH"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["mem_limit"], "128m")
        self.assertNotIn("ports", service)
        self.assertIn("/var/log:/var/log:ro", service["volumes"])
        self.assertIn("fluent-bit-data:/var/lib/fluent-bit", service["volumes"])

    def test_a_central_that_does_not_answer_keeps_the_lines(self):
        """M1: retried for ever, on disk, within 64 MB."""
        config = self._config()
        loki = config["pipeline"]["outputs"][0]
        self.assertEqual(loki["retry_limit"], "no_limits")
        self.assertEqual(loki["storage.total_limit_size"], "64M")
        self.assertEqual(config["service"]["scheduler.cap"], 300)
        self.assertEqual(config["service"]["storage.max_chunks_up"], 16)
        for source in config["pipeline"]["inputs"]:
            self.assertEqual(source["storage.type"], "filesystem")

    def test_the_first_start_sends_nothing_old(self):
        inputs = self._config()["pipeline"]["inputs"]
        journal = inputs[0]
        self.assertIs(journal["read_from_tail"], True)
        self.assertIn("SYSLOG_IDENTIFIER=sshd-session", journal["systemd_filter"])
        self.assertIn("SYSLOG_IDENTIFIER=sudo", journal["systemd_filter"])
        for tail in inputs[1:]:
            self.assertIs(tail["read_from_head"], False)
            self.assertTrue(tail["db"].startswith("/var/lib/fluent-bit/"))

    def test_the_log_store_url_is_split(self):
        loki = self._config(
            ic_logs_url="http://172.17.0.1:8428/lw/loki/api/v1/push",
        )["pipeline"]["outputs"][0]
        self.assertEqual(
            (loki["host"], loki["port"], loki["uri"], loki["tls"]),
            ("172.17.0.1", 8428, "/lw/loki/api/v1/push", False),
        )
        loki = self._config()["pipeline"]["outputs"][0]
        self.assertEqual((loki["host"], loki["port"], loki["tls"]), ("m.example.com", 443, True))

    def test_a_server_name_with_spaces_and_quotes_is_one_lua_string(self):
        panel = playbook.fluent_bit_files(
            ic_host_name='Cloud 1 "año"', ic_panel_key_fingerprints=[],
        )["panel.lua"]
        self.assertIn('host = "Cloud 1 \\"año\\""', panel)
        self.assertIn('uid = "1000"', panel)

    def test_vmagent_scrapes_the_counters_only_with_fluent_bit(self):
        def jobs(**overrides):
            scrape = yaml.safe_load(playbook.render(
                "Write the vmagent scrape configuration", **playbook.defaults(**overrides),
            ))
            return {j["job_name"]: j for j in scrape["scrape_configs"]}

        on = jobs()
        self.assertEqual(on["security"]["static_configs"][0]["targets"], ["fluent-bit:2021"])
        self.assertEqual(
            on["security-agent"]["metrics_path"], "/api/v2/metrics/prometheus",
        )
        self.assertFalse({"security", "security-agent"} & set(jobs(ic_fluent_bit=False)))

    def test_switched_off_its_cursors_go_with_it(self):
        """M2: back on, it must not send what happened while off."""
        task = playbook.task("Remove the security log collector, switched off")
        self.assertEqual(task["when"], "not (ic_fluent_bit | bool)")
        cmds = [t["ansible.builtin.command"]["cmd"] for t in task["block"]
                if "ansible.builtin.command" in t]
        self.assertEqual(cmds, ["docker volume rm incubacloud-observability_fluent-bit-data"])

    def test_the_teardown_removes_the_volumes(self):
        path = os.path.join(
            get_module_path("incubacloud"), "ansible", "playbooks", "host_teardown.yml",
        )
        with open(path, encoding="utf-8") as fh:
            tasks = yaml.safe_load(fh)[0]["tasks"]
        stop = next(t for t in tasks if t["name"] == "Stop the metrics agents")
        self.assertIn("down --volumes", stop["ansible.builtin.command"]["cmd"])


class TestTheExecutorAndTheSwitch(ObservabilityExecutorCase):
    """What the install reads from the server, and what it says."""

    def test_the_switch_the_key_and_the_user_reach_the_playbook(self):
        key_file, fingerprint = _key_file()
        self.host.write({"key_file": key_file, "user": "incubacloud"})
        self.host.security_logs_enabled = False
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertIs(extra["ic_security_logs"], False)
        self.assertEqual(extra["ic_panel_key_fingerprints"], [fingerprint])
        self.assertEqual(extra["ic_panel_ssh_user"], "incubacloud")
        self.assertNotIn("ic_ship_security_logs", extra)
        self.host.security_logs_enabled = True
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertIs(extra["ic_security_logs"], True)

    def test_a_password_host_has_no_fingerprint(self):
        self.host.login_type = "password"
        self.assertEqual(self.host._panel_key_fingerprints(), [])

    def _errors(self, **facts):
        executor = self._make(ObservabilityAgentsExecutor)
        executor._facts = facts
        return executor.parse_results({executor._playbook: {"exit_status": 0}})

    def test_four_containers_with_fluent_bit_three_without(self):
        three = '["cadvisor", "node-exporter", "vmagent"]'
        self.assertEqual(self._errors(ic_agents_running_services=three, ic_fluent_bit="False"), [])
        errors = self._errors(ic_agents_running_services=three, ic_fluent_bit="True")
        self.assertEqual(len(errors), 1)
        self.assertIn("Fluent Bit", errors[0])
        self.assertIn("Turn off Send security logs", errors[0])
        four = '["cadvisor", "fluent-bit", "node-exporter", "vmagent"]'
        self.assertEqual(self._errors(ic_agents_running_services=four, ic_fluent_bit="True"), [])
        errors = self._errors(
            ic_agents_running_services='["fluent-bit", "node-exporter"]', ic_fluent_bit="True",
        )
        self.assertIn("cadvisor, vmagent did not", errors[0])

    def test_the_revision_is_two(self):
        self.assertEqual(AGENTS_REVISION, 2)

    def _succeed(self, sent, **facts):
        executor = self._make(ObservabilityAgentsExecutor)
        lines = []
        executor._sys = lines.append
        executor._sent_collect_pg = bool(self.settings.metrics_collect_pg)
        executor._sent_security_logs = sent
        executor._facts = {"ic_agents_running": "3", "ic_instances_labelled": "0"} | facts
        asyncio.run(executor.on_success({}))
        return "\n".join(lines)

    def test_a_switch_moved_during_the_install_records_no_revision(self):
        self.host.security_logs_enabled = True
        log = self._succeed(False, ic_fluent_bit="False")
        self.assertEqual(self.host.metrics_agents_revision, 0)
        self.assertIn("applied again", log)
        self._succeed(True, ic_fluent_bit="True", ic_journal_persistent="True")
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)

    def test_a_journal_in_memory_is_said_not_failed(self):
        log = self._succeed(True, ic_fluent_bit="True", ic_journal_persistent="False")
        self.assertIn("/var/log/journal is missing", log)
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)


class TestTheSwitchReappliesItsServer(ObservabilityExecutorCase):
    """The switch, the key and the user apply now, to that server alone."""

    def setUp(self):
        super().setUp()
        self.host.write({
            "known_hosts_key": "hx ssh-ed25519 AAAA",
            "traefik_deployed": True,
            "metrics_agents_state": "installed",
        })
        self.other = self.env["cloud.host"].create({
            "name": "HY", "ip_address": "10.0.0.41", "user": "root",
            "wildcard_domain": "hy.example.com", "known_hosts_key": "hy ssh-ed25519 AAAA",
            "traefik_deployed": True, "metrics_agents_state": "installed",
        })
        self.settings.metrics_account = "acct_test01"

    def _queued(self, host):
        return self.env["cloud.job"].search([
            ("host_id", "=", host.id),
            ("job_type_id.code", "=", "install_observability"),
        ])

    def test_turning_it_off_queues_that_server_and_is_audited(self):
        self.host.security_logs_enabled = False
        self.assertEqual(len(self._queued(self.host)), 1)
        self.assertFalse(self._queued(self.other))
        audit = self.env["cloud.audit.log"].search([
            ("host_id", "=", self.host.id), ("action", "=", "Config changed"),
        ], limit=1, order="id desc")
        self.assertIn("Send security logs: True→False", audit.details)

    def test_a_failed_install_is_queued_too(self):
        """M3: the switch is how a Fluent Bit that does not start goes."""
        self.host.metrics_agents_state = "failed"
        self.host.security_logs_enabled = False
        self.assertEqual(len(self._queued(self.host)), 1)

    def test_a_server_never_installed_is_left_to_the_cron(self):
        self.host.metrics_agents_state = "never"
        self.host.security_logs_enabled = False
        self.assertFalse(self._queued(self.host))

    def test_nothing_with_observability_off(self):
        self.settings.metrics_enabled = False
        self.host.security_logs_enabled = False
        self.assertFalse(self._queued(self.host))

    def test_the_same_value_queues_nothing(self):
        self.host.write({"security_logs_enabled": True, "name": "HX2"})
        self.assertFalse(self._queued(self.host))

    def test_a_new_key_queues_it(self):
        key_file, _fingerprint = _key_file()
        self.host.key_file = key_file
        self.assertEqual(len(self._queued(self.host)), 1)

    def test_a_new_user_queues_it(self):
        self.host.user = "incubacloud"
        self.assertEqual(len(self._queued(self.host)), 1)


def _load_migration():
    """Import 1.0.229's end-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.229", "end-migrate.py",
    )
    spec = importlib.util.spec_from_file_location("ic_end_1_0_229", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheUpdateGivesEveryServerTheSwitch(TransactionCase):
    """The switch moves from the panel to each server."""

    def setUp(self):
        super().setUp()
        self.hosts = self.env["cloud.host"].create([{
            "name": f"SL{i}", "ip_address": f"10.0.8.{i + 1}", "user": "root",
            "wildcard_domain": f"sl{i}.example.com",
        } for i in range(2)])
        self.hosts.write({"security_logs_enabled": False})
        self.hosts[1].active = False

    def test_every_server_on_also_with_the_panel_wide_setting_off(self):
        """The owner, 10-nov-2026: on everywhere, said in the changelog."""
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, "1.0.228")
        self.env.invalidate_all()
        self.assertEqual(set(self.hosts.mapped("security_logs_enabled")), {True})

    def test_a_layer_decides_otherwise(self):
        self.env.flush_all()
        with patch.object(
            type(self.env["cloud.host"]), "_security_logs_after_upgrade",
            return_value=False,
        ):
            self.hosts.write({"security_logs_enabled": True})
            self.env.flush_all()
            _load_migration().migrate(self.env.cr, "1.0.228")
        self.env.invalidate_all()
        self.assertEqual(set(self.hosts.mapped("security_logs_enabled")), {False})

    def test_a_fresh_install_is_left_alone(self):
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, None)
        self.env.invalidate_all()
        self.assertEqual(set(self.hosts.mapped("security_logs_enabled")), {False})

    def test_a_new_server_starts_on(self):
        host = self.env["cloud.host"].create({
            "name": "SLnew", "ip_address": "10.0.8.9", "user": "root",
            "wildcard_domain": "slnew.example.com",
        })
        self.assertTrue(host.security_logs_enabled)


class TestWithheldSecurityLogs(TransactionCase):
    """Where a layer withholds them, no server can turn them on."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "SW", "ip_address": "10.0.8.20", "user": "root",
            "wildcard_domain": "sw.example.com",
        })
        self.host.security_logs_enabled = False
        patcher = patch.object(
            type(self.env["cloud.host"]), "_security_logs_available",
            return_value=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_turning_them_on_is_refused(self):
        with self.assertRaises(UserError):
            self.host.security_logs_enabled = True

    def test_a_server_created_sending_them_is_refused(self):
        with self.assertRaises(UserError):
            self.env["cloud.host"].create({
                "name": "SW2", "ip_address": "10.0.8.21", "user": "root",
                "wildcard_domain": "sw2.example.com", "security_logs_enabled": True,
            })

    def test_off_and_other_fields_still_write(self):
        self.host.write({"security_logs_enabled": False, "name": "SW3"})
        self.assertEqual(self.host.name, "SW3")
