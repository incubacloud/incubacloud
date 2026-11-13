"""The guard on the proxy's shared network (M-3, 13-nov-2026).

Every instance joins the network the host's proxy reaches it on, so one
instance could open another's Odoo directly. Full Setup installs a guard
on the host's firewall that lets only the proxy's fixed address open
connections there (``scripts/shared_guard.sh``, its bats suite covers the
rules). Here: the fixed address in the proxy's compose file, carried to
hosts stored before; the switch; the step, after the proxy starts and
never fatal; the alert of a guard left open; and the teardown.
"""
import asyncio
import pathlib
from unittest.mock import patch

import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load._routes_crud import (
    CrudMixin,
)
from odoo.addons.incubacloud.models.cloud_host import PROXY_SHARED_MAC
from odoo.addons.incubacloud.models.cloud_host_shared_guard import (
    SHARED_GUARD_ALERT,
    SHARED_GUARD_STEP,
    shared_guard_verdict,
)
from odoo.addons.incubacloud.models.full_setup_executor import FullSetupExecutor

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "data" / "traefik" / "inverseproxy.yaml"
_TEARDOWN = _ROOT / "ansible" / "playbooks" / "host_teardown.yml"
_SCRIPT = _ROOT / "scripts" / "shared_guard.sh"
_PROXY_START = _ROOT / "scripts" / "proxy_start.sh"

#: A compose file stored before 1.0.228, with the proxy's networks as a map.
_LEGACY = (
    'version: "3.8"\n\n'
    "services:\n"
    "  proxy:\n"
    "    image: traefik:v2.11\n"
    "    networks:\n"
    "      shared:\n"
    "      private:\n"
    "      public:\n"
    "    ports:\n"
    '      - "80:80"\n'
    "  dockersocket:\n"
    "    networks:\n"
    "      private:\n\n"
    "networks:\n"
    "  shared:\n"
    "    internal: true\n"
)


def _proxy_networks(content):
    """Return the ``proxy`` service's networks as Compose reads them."""
    return yaml.safe_load(content)["services"]["proxy"]["networks"]


class TestTheProxyHasAFixedAddress(TransactionCase):
    """The template, and the retrofit of the copies hosts store."""

    def setUp(self):
        """The model class, whose retrofit helpers are static."""
        super().setUp()
        self.Host = type(self.env["cloud.host"])

    def test_the_template_fixes_it_on_shared_only(self):
        networks = _proxy_networks(_TEMPLATE.read_text())
        self.assertEqual(networks["shared"], {"mac_address": PROXY_SHARED_MAC})
        self.assertIsNone(networks["private"])
        self.assertIsNone(networks["public"])

    def test_it_is_locally_administered_and_the_script_agrees(self):
        """No card has it; the script's default is the same address."""
        first = int(PROXY_SHARED_MAC.split(":")[0], 16)
        self.assertTrue(first & 0b10, "locally administered bit")
        self.assertFalse(first & 0b1, "not multicast")
        self.assertIn(
            f'MAC="${{IC_PROXY_SHARED_MAC:-{PROXY_SHARED_MAC}}}"',
            _SCRIPT.read_text(),
        )
        # proxy_start.sh drops that same line under a Compose too old.
        self.assertIn(
            f'proxy_mac="{PROXY_SHARED_MAC}"', _PROXY_START.read_text(),
        )

    def test_a_stored_copy_gains_it(self):
        out = self.Host._add_proxy_shared_mac(_LEGACY)
        self.assertIn(
            "      shared:\n"
            f'        mac_address: "{PROXY_SHARED_MAC}"\n'
            "      private:\n",
            out,
        )
        networks = _proxy_networks(out)
        self.assertEqual(networks["shared"], {"mac_address": PROXY_SHARED_MAC})
        # The top-level network and the socket proxy are left as they were.
        self.assertEqual(yaml.safe_load(out)["networks"], {"shared": {"internal": True}})
        self.assertEqual(self.Host._add_proxy_shared_mac(out), out)

    def test_settings_of_the_entry_keep_their_indent(self):
        with_alias = _LEGACY.replace(
            "      shared:\n",
            "      shared:\n        aliases:\n          - edge\n",
        )
        networks = _proxy_networks(self.Host._add_proxy_shared_mac(with_alias))
        self.assertEqual(
            networks["shared"],
            {"mac_address": PROXY_SHARED_MAC, "aliases": ["edge"]},
        )

    def test_a_nested_list_before_shared_does_not_stop_it(self):
        listed = _LEGACY.replace(
            "    networks:\n      shared:\n      private:\n",
            "    networks:\n      private:\n        aliases:\n          - p\n"
            "      shared:\n",
        )
        networks = _proxy_networks(self.Host._add_proxy_shared_mac(listed))
        self.assertEqual(networks["shared"], {"mac_address": PROXY_SHARED_MAC})

    def test_what_it_cannot_place_is_left_alone(self):
        """The guard then stays open, and Full Setup says so."""
        own = _LEGACY.replace(
            "      shared:\n", '      shared:\n        mac_address: "02:00:00:00:00:09"\n',
        )
        as_list = _LEGACY.replace(
            "      shared:\n      private:\n      public:\n",
            "      - shared\n      - private\n      - public\n",
        )
        no_shared = _LEGACY.replace("      shared:\n", "")
        for content in (own, as_list, no_shared, "", None):
            with self.subTest(content=content):
                self.assertEqual(self.Host._add_proxy_shared_mac(content), content)

    def test_the_upgrade_carries_it_to_a_stored_host(self):
        host = self.env["cloud.host"].create({
            "name": "sg-old-host",
            "ip_address": "10.0.0.71",
            "user": "ubuntu",
            "wildcard_domain": "sg-old.example.com",
        })
        host.traefik_inverseproxy_yaml = host.traefik_inverseproxy_yaml.replace(
            f'        mac_address: "{PROXY_SHARED_MAC}"\n', "",
        )
        self.assertNotIn("mac_address", host.traefik_inverseproxy_yaml)
        self.assertTrue(host.template_drift)
        self.env["cloud.host"].init_traefik_templates()
        host.invalidate_recordset()
        self.assertEqual(
            _proxy_networks(host.traefik_inverseproxy_yaml)["shared"],
            {"mac_address": PROXY_SHARED_MAC},
        )
        self.assertIn("mac_address", host._shipped_inverseproxy_yaml())
        self.assertFalse(host.template_drift)


class TestTheSwitch(TransactionCase):
    """On by default, saved from the host's page, applied by Full Setup."""

    def setUp(self):
        """A host as the new-host form creates it."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "sg-switch-host",
            "ip_address": "10.0.0.72",
            "user": "ubuntu",
            "wildcard_domain": "sg-switch.example.com",
        })

    def test_a_new_host_has_it_on(self):
        self.assertTrue(self.host.shared_guard)

    def test_turning_it_off_is_a_change_full_setup_applies(self):
        self.assertIn("shared_guard", self.host._config_snapshot_fields())
        self.host.write(self.host._applied_config_vals())
        self.assertFalse(self.host.config_dirty)
        self.host.shared_guard = False
        self.host.invalidate_recordset(["config_dirty"])
        self.assertTrue(self.host.config_dirty)

    def test_the_host_page_reads_and_saves_it(self):
        self.assertIn("shared_guard", CrudMixin._HOST_ALLOWED)


class TestFullSetupAppliesTheGuard(TransactionCase):
    """The step runs once the proxy is up, and never fails the setup."""

    def setUp(self):
        """A host and the Full Setup executor of a job on it."""
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "sg-setup-host",
            "ip_address": "10.0.0.73",
            "user": "ubuntu",
            "wildcard_domain": "sg-setup.example.com",
        })
        job = self.env["cloud.job"].create({
            "host_id": self.host.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "full_setup")], limit=1,
            ).id,
            "name": "Full Setup",
        })
        self.executor = FullSetupExecutor(job, self.host)

    def _commands(self):
        """Return the run's commands, label to command."""
        return {c[0]: c[1] for c in self.executor.get_commands()}

    def _good_results(self):
        """Return what a run that went well printed, step by step."""
        results = {
            label: {"stdout": "ok", "exit_status": 0}
            for label, *_rest in self.executor.get_commands()
        }
        results["check:os"]["stdout"] = "Linux"
        results["check:disk"]["stdout"] = str(100 * 1024 ** 3)
        return results

    def _succeed(self, step):
        """Run ``on_success`` with *step* as the guard step's result."""
        results = {SHARED_GUARD_STEP: step} if step is not None else {}
        with patch.object(
            type(self.host), "_enqueue_observability",
            autospec=True, return_value=False,
        ):
            asyncio.run(self.executor.on_success(results))
        self.env.invalidate_all()

    def _active(self):
        """Return the host's active guard alerts."""
        return self.env["cloud.alert"].search([
            ("host_id", "=", self.host.id),
            ("code", "=", SHARED_GUARD_ALERT),
            ("state", "=", "active"),
        ])

    def test_it_runs_right_after_the_proxy_starts(self):
        commands = self.executor.get_commands()
        labels = [c[0] for c in commands]
        at = labels.index(SHARED_GUARD_STEP)
        self.assertGreater(at, labels.index("Start Traefik"))
        self.assertLess(at, labels.index("Create globalwhitelist directory"))
        self.assertIn("shared_guard.sh install", commands[at][1])

    def test_a_host_with_it_off_has_it_removed(self):
        self.host.shared_guard = False
        self.assertIn(
            "shared_guard.sh uninstall", self._commands()[SHARED_GUARD_STEP],
        )

    def test_it_never_fails_the_setup(self):
        results = self._good_results()
        results[SHARED_GUARD_STEP] = {"stdout": "", "exit_status": 1}
        self.assertEqual(self.executor.parse_results(results), [])

    def test_left_open_raises_its_alert_and_closed_resolves_it(self):
        self._succeed({
            "stdout": "[incubacloud] x\nIC_SHARED_GUARD_OPEN the proxy is not running on inverseproxy_shared\n",
            "exit_status": 0,
        })
        alert = self._active()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")
        self.assertIn("the proxy is not running", alert.message)
        self._succeed({"stdout": "IC_SHARED_GUARD_ON bridge br-x, proxy m\n", "exit_status": 0})
        self.assertFalse(self._active())

    def test_unsupported_or_failed_raise_it_off_resolves_it(self):
        for step in (
            {"stdout": "IC_SHARED_GUARD_UNSUPPORTED the firewall has no MAC match (xt_mac)\n",
             "exit_status": 0},
            {"stdout": "", "exit_status": 1},
        ):
            with self.subTest(step=step):
                self._succeed(step)
                self.assertTrue(self._active())
                if step["exit_status"] != 0:
                    self.assertIn("did not finish (exit 1)", self._active().message)
                self._succeed({"stdout": "IC_SHARED_GUARD_OFF removed\n", "exit_status": 0})
                self.assertFalse(self._active())

    def test_a_run_without_the_step_leaves_the_alert_as_it_was(self):
        self.env["cloud.alert"].raise_alert(
            SHARED_GUARD_ALERT, "open", host=self.host,
        )
        self._succeed(None)
        self.assertTrue(self._active())

    def test_the_verdict_reads_the_last_marker(self):
        self.assertEqual(
            shared_guard_verdict({
                "stdout": "IC_SHARED_GUARD_OPEN a\nIC_SHARED_GUARD_ON b c\n",
                "exit_status": 0,
            }),
            ("ON", "b c"),
        )
        self.assertEqual(
            shared_guard_verdict({"stdout": "IC_SHARED_GUARD_ON x", "exit_status": 2}),
            ("FAILED", "exit 2"),
        )
        self.assertEqual(shared_guard_verdict(None), ("FAILED", "exit 1"))


class TestTheTeardownRemovesIt(TransactionCase):
    """A server the panel forgets keeps no timer of the guard."""

    def test_the_teardown_uninstalls_it_before_the_proxy_goes(self):
        play = yaml.safe_load(_TEARDOWN.read_text())[0]
        self.assertEqual(
            play["vars"]["ic_shared_guard_dir"],
            "/usr/local/lib/incubacloud-shared-guard",
        )
        self.assertIn(
            'INSTALL_DIR="${IC_SHARED_GUARD_DIR:-/usr/local/lib/incubacloud-shared-guard}"',
            _SCRIPT.read_text(),
        )
        names = [t["name"] for t in play["tasks"]]
        self.assertLess(
            names.index("Remove the guard on the proxy network"),
            names.index("Stop the Traefik stack"),
        )
        block = next(
            t for t in play["tasks"] if t["name"] == "Remove the guard on the proxy network"
        )
        self.assertEqual(block["when"], "ic_shared_guard.stat.exists")
        self.assertIn("rescue", block)
        task = block["block"][0]
        self.assertEqual(task["become"], "{{ ansible_user_id != 'root' }}")
        self.assertIn("shared_guard.sh uninstall", task["ansible.builtin.command"]["cmd"])
