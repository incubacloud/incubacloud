"""Tests for the repatriated host-hardening executor (paso 5).

Cover the wiring that moved to core: the port/user/allowlist resolution,
the extra-vars handed to the playbook, the fact-based ``parse_results``
gate that refuses to disable root unless the new SSH port came up, and
the registry replacement + layer-1/layer-2 split (core carries no edge
firewall; the SaaS subclass overrides the hook).

SSH-dependent finalize (``_finalize_disable_root``) is proven live on a
real VPS, not here — it opens a real asyncssh connection.
"""
import asyncio

from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.host_hardening_executor import (
    DEFAULT_HARDENED_USER,
    HostHardeningExecutor,
    _build_ssh_allowlist,
    _pick_new_ssh_port,
    _SSH_PORT_MAX,
    _SSH_PORT_MIN,
)
from odoo.addons.incubacloud.models.registry import executor_registry


class TestHardeningHelpers(TransactionCase):
    """Pure port/allowlist helpers."""

    def test_pick_new_ssh_port_rotates_default(self):
        """Port 22 rotates into the private range."""
        for _ in range(20):
            port = _pick_new_ssh_port(22)
            self.assertTrue(_SSH_PORT_MIN <= port <= _SSH_PORT_MAX)

    def test_pick_new_ssh_port_keeps_non_default(self):
        """An already-rotated port is kept as-is."""
        self.assertEqual(_pick_new_ssh_port(22345), 22345)

    def test_build_allowlist_primary_plus_extras_dedup(self):
        """Primary IP + extras, de-duplicated, comma-joined."""
        out = _build_ssh_allowlist("1.2.3.4", "5.6.7.8, 1.2.3.4 , 9.9.9.9")
        self.assertEqual(out, "1.2.3.4, 5.6.7.8, 9.9.9.9")

    def test_build_allowlist_empty_is_permissive(self):
        """Nothing known degrades to 0.0.0.0/0 (never lock everyone out)."""
        self.assertEqual(_build_ssh_allowlist("", ""), "0.0.0.0/0")


class TestHardeningPreflight(TransactionCase):
    """The playbook must refuse an unsupported host before touching it.

    The pre-Ansible executor ran this gate as its first command with
    ``stop_on_failure``; it was lost once and must not be lost again, so
    these assertions pin the shape of the preflight rather than its prose.
    """

    def _playbook(self):
        """Return the parsed hardening playbook (single play)."""
        import os

        import yaml

        from odoo.modules.module import get_module_path

        path = os.path.join(
            get_module_path("incubacloud"),
            "ansible", "playbooks", "host_hardening.yml",
        )
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)[0]

    def test_preflight_runs_before_any_privileged_task(self):
        """Facts are gathered unprivileged and the gate lives in pre_tasks."""
        play = self._playbook()
        self.assertIs(play.get("gather_facts"), False)
        pre = play.get("pre_tasks") or []
        self.assertTrue(pre, "the playbook must keep a preflight block")
        # Everything in the gate runs unprivileged, so a missing NOPASSWD
        # sudo surfaces as our message and not an escalation error.
        for task in pre:
            self.assertIs(
                task.get("become"), False,
                "preflight task %r must run with become: false"
                % task.get("name"),
            )

    def test_preflight_gates_the_os(self):
        """An OS assert names both supported families."""
        play = self._playbook()
        asserts = [
            t for t in (play.get("pre_tasks") or [])
            if "ansible.builtin.assert" in t
        ]
        blob = str(asserts)
        self.assertIn("ansible_distribution", blob)
        self.assertIn("Ubuntu", blob)
        self.assertIn("Debian", blob)
        self.assertEqual(play["vars"]["supported_ubuntu"], ["22.04", "24.04"])
        self.assertEqual(play["vars"]["debian_min_major"], 12)

    def test_the_rotated_port_survives_a_reboot(self):
        """The socket's listen port must be pinned by our own drop-in.

        Ubuntu 22.10+ runs sshd socket-activated: systemd owns the port and
        sshd_config's ``Port`` is ignored. Merely disabling ssh.socket is
        not durable — an openssh upgrade or ``systemctl preset`` re-enables
        it and the next reboot returns sshd to 22, which the nftables
        allowlist drops (reproduced live). Pinning ListenStream in a
        drop-in we own is what makes the rotated port survive, so this
        pins the mechanism, not the wording.
        """
        play = self._playbook()
        tasks = play.get("tasks") or []
        overrides = [
            t for t in tasks
            if "ssh.socket.d" in str(t.get("ansible.builtin.copy", {}).get("dest", ""))
        ]
        self.assertEqual(
            len(overrides), 1,
            "hardening must ship exactly one ssh.socket port drop-in",
        )
        content = overrides[0]["ansible.builtin.copy"]["content"]
        # The empty assignment must come first: without it systemd ADDS our
        # port and keeps listening on 22 as well.
        self.assertIn("ListenStream=\n", content)
        self.assertIn("ListenStream={{ ssh_port }}", content)
        self.assertLess(
            content.index("ListenStream=\n"),
            content.index("ListenStream={{ ssh_port }}"),
            "the reset must precede the port or 22 stays open",
        )
        # And the socket stays enabled — disabling it is the fragile path.
        sockets = [
            t for t in tasks
            if t.get("ansible.builtin.systemd_service", {}).get("name")
            == "ssh.socket"
        ]
        self.assertTrue(sockets)
        for task in sockets:
            self.assertIsNot(
                task["ansible.builtin.systemd_service"].get("enabled"), False,
                "ssh.socket must not be disabled: that is the path that "
                "silently reverts to port 22 on the next reboot",
            )

    def test_fail2ban_never_bans_the_panel(self):
        """The sshd jail must exempt the addresses the firewall allows.

        Without ``ignoreip``, three failed authentications from the panel
        — one agent offering too many keys reaches that on its own — ban
        it for the full ``bantime``. The panel is the only thing that
        manages the host, so every job against it fails until the ban
        lapses; Tenants1 was unmanageable for exactly one hour on
        2026-08-13 this way. Exempting them costs nothing: they are
        already the only addresses nftables lets near the port.
        """
        play = self._playbook()
        jails = [
            t for t in (play.get("tasks") or [])
            if "fail2ban/jail.d" in str(
                t.get("ansible.builtin.copy", {}).get("dest", "")
            )
        ]
        self.assertEqual(
            len(jails), 1, "hardening must ship exactly one sshd jail",
        )
        content = jails[0]["ansible.builtin.copy"]["content"]
        self.assertIn("ignoreip", content)
        self.assertIn(
            "ic_fail2ban_ignoreip", content,
            "the jail must exempt the operators the firewall trusts — "
            "including the control IP the play itself came from",
        )

    def test_fail2ban_never_exempts_the_whole_internet(self):
        """The exemption list must not inherit the firewall's catch-all.

        ``_build_ssh_allowlist`` degrades to ``0.0.0.0/0`` when no
        operator IP is known — deliberately, since locking every operator
        out is worse than a reachable port. Both production hosts sit in
        exactly that state. Copying it into ``ignoreip`` would exempt
        every source on the internet and disable the jail, and the file
        would still read like a hardened config: the failure is
        completely invisible. The list is therefore built separately,
        dropping catch-alls and keeping named addresses.
        """
        play = self._playbook()
        facts = [
            t for t in (play.get("tasks") or [])
            if "ic_fail2ban_ignoreip" in str(
                t.get("ansible.builtin.set_fact", {})
            )
        ]
        self.assertEqual(
            len(facts), 1,
            "the exemption list must be built once, in its own fact",
        )
        expr = facts[0]["ansible.builtin.set_fact"]["ic_fail2ban_ignoreip"]
        for catch_all in ("0.0.0.0/0", "::/0"):
            self.assertIn(
                f"reject('equalto', '{catch_all}')", expr,
                f"{catch_all} must be filtered out of ignoreip, or "
                f"fail2ban silently stops banning anyone",
            )
        # Whitespace-separated: fail2ban does not split ignoreip on
        # commas, and the allowlist arrives comma-joined for nftables.
        self.assertIn("join(' ')", expr)

    def test_the_ruleset_replaces_only_our_own_table(self):
        """``flush ruleset`` would take Docker's packet rules with it.

        Docker programs its filter/nat chains through iptables-nft, so a
        host-wide flush deletes them. The daemon never notices and never
        re-adds them: every published port loses its DNAT, containers
        keep answering on localhost, and the outside world gets nothing.
        That is how re-running hardening on a live host took the whole
        fleet down on 2026-08-14 — it had only ever run before Docker
        existed, so the flush had never had anything to destroy.
        """
        play = self._playbook()
        rulesets = [
            t for t in (play.get("tasks") or [])
            if str(t.get("ansible.builtin.copy", {}).get("dest", ""))
            == "/etc/nftables.conf"
        ]
        self.assertEqual(len(rulesets), 1)
        content = rulesets[0]["ansible.builtin.copy"]["content"]
        # Directives only: the comment above the ruleset names the thing
        # it is warning against, and prose must not fail the assertion.
        directives = [
            line.strip() for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertNotIn(
            "flush ruleset", directives,
            "a host-wide flush deletes Docker's chains along with ours",
        )
        # Declare-then-delete: the bare declaration creates the table when
        # it is missing, so the delete cannot fail on a first run.
        self.assertIn("table inet filter\ndelete table inet filter", content)

    def test_the_operator_cannot_fence_themselves_off(self):
        """The firewall rule must allow the address the play came from.

        nftables is applied BEFORE the new-port login is proven, so a
        stale ``allowed_ssh_ips`` (a rotated residential IP, a changed NAT
        egress) locks everyone out even though sshd still permits root —
        "root preserved" is worthless when the packet is dropped.
        Reproduced live. The rule therefore keys on the effective
        allowlist, which folds in ``SSH_CONNECTION``'s client address.
        """
        tasks = self._playbook().get("tasks") or []
        blob = str(tasks)
        self.assertIn("SSH_CONNECTION", blob)
        # The nftables rule must use the effective list, never the raw
        # configured one.
        rules = [
            t for t in tasks
            if "nftables.conf" in str(
                t.get("ansible.builtin.copy", {}).get("dest", "")
            )
        ]
        self.assertEqual(len(rules), 1)
        content = rules[0]["ansible.builtin.copy"]["content"]
        self.assertIn("ic_effective_allowlist", content)
        self.assertNotIn("{{ ssh_allowlist }}", content)

    def test_preflight_gates_sudo(self):
        """A non-root connection is probed for passwordless sudo."""
        pre = self._playbook().get("pre_tasks") or []
        probes = [
            t for t in pre
            if t.get("ansible.builtin.command") == "sudo -n true"
        ]
        self.assertEqual(len(probes), 1, "expected exactly one sudo probe")
        self.assertIn("ansible_user", str(probes[0].get("when")))
        gates = [
            t for t in pre
            if "ic_sudo_probe" in str(t.get("ansible.builtin.assert", ""))
        ]
        self.assertTrue(gates, "the sudo probe result must be asserted on")


class TestHardeningExecutorWiring(TransactionCase):
    """Extra-vars, parse_results and the layer split."""

    def _make(self, **host_attrs):
        """Build a core executor without ``__init__`` (no SSH/job record)."""
        host = MagicMock(spec=type(self.env["cloud.host"]))
        host.id = 7
        host.name = "host-x"
        host.ip_address = "10.0.0.5"
        host.port = host_attrs.get("port", 22)
        host.user = host_attrs.get("user", "root")
        host.hardened = host_attrs.get("hardened", False)
        host.allowed_ssh_ips = host_attrs.get("allowed_ssh_ips", "203.0.113.7")
        host.auto_security_updates = host_attrs.get("auto_security_updates", True)

        ex = object.__new__(HostHardeningExecutor)
        ex.env = self.env
        ex.job = MagicMock(spec=type(self.env["cloud.job"]))
        ex.job.host_id = host
        ex._log_buffer = []
        ex._sys = lambda *a, **k: None
        ex._facts = {}
        return ex

    def test_extra_vars_root_start_creates_operator(self):
        """From root: operator user = default, port rotated, allowlist set."""
        ex = self._make(user="root", port=22, allowed_ssh_ips="203.0.113.7")
        ev = ex.get_extra_vars()
        self.assertEqual(ev["ic_hardened_user"], DEFAULT_HARDENED_USER)
        self.assertTrue(_SSH_PORT_MIN <= ev["ic_ssh_port"] <= _SSH_PORT_MAX)
        self.assertEqual(ev["ic_ssh_allowlist"], "203.0.113.7")
        self.assertIs(ev["ic_auto_reboot"], True)
        self.assertTrue(ex._needs_user_creation)

    def test_extra_vars_byoh_keeps_existing_user_and_port(self):
        """Non-root BYOH: keep the connecting user and the existing port."""
        ex = self._make(user="ubuntu", port=2222)
        ev = ex.get_extra_vars()
        self.assertEqual(ev["ic_hardened_user"], "ubuntu")
        self.assertEqual(ev["ic_ssh_port"], 2222)
        self.assertFalse(ex._needs_user_creation)

    def test_parse_results_gates_on_port_listening(self):
        """parse_results refuses to proceed unless the new port came up."""
        ex = self._make()
        ex._new_port = 22345
        playbook = ex._playbook

        ex._facts = {"ic_port_listening": "True"}
        self.assertEqual(ex.parse_results({playbook: {"exit_status": 0}}), [])

        ex._facts = {"ic_port_listening": "False"}
        self.assertTrue(ex.parse_results({playbook: {"exit_status": 0}}))

        ex._facts = {"ic_port_listening": "True"}
        self.assertTrue(ex.parse_results({playbook: {"exit_status": 1}}))

    def test_core_edge_firewall_hook_is_noop(self):
        """Core carries no edge firewall — the hook is a no-op."""
        ex = self._make()
        self.assertIsNone(asyncio.run(ex._open_edge_firewall_port(22345)))

    def test_core_operator_ip_is_empty(self):
        """Core has no cached operator IP; the allowlist is field-driven."""
        ex = self._make()
        self.assertEqual(ex._operator_ip(), "")

    def test_registry_binding_is_hardening_executor(self):
        """'host_hardening' resolves to the core executor or a subclass.

        In this devel DB (core + saas_manager) the SaaS subclass wins and
        overrides the edge-firewall hook; either way it IS-A core executor.
        """
        bound = executor_registry.get("host_hardening")
        self.assertIsNotNone(bound)
        self.assertTrue(issubclass(bound, HostHardeningExecutor))

    def test_overriding_an_override_is_reported(self):
        """Two peer modules claiming one job type must not do so quietly.

        ``incubacloud_saas_manager`` and ``incubacloud_tenant`` each ship
        a ``host_hardening`` variant for their own deployment, and both
        used to write straight into the registry's private dict: the
        second import silently won, with load order decided by the
        dependency graph. They are meant for different databases, but
        CI installs every module of the repo into one — so the ambiguity
        is real and must be visible.
        """
        code = "test_override_reporting"
        first, second = type("A", (), {}), type("B", (), {})
        try:
            with self.assertLogs(
                "odoo.addons.incubacloud.models.registry", "WARNING",
            ) as logs:
                executor_registry.override(code, first)
                executor_registry.override(code, second)
            self.assertTrue(
                any("overridden again" in line for line in logs.output),
                "a second override was accepted without a word",
            )
            self.assertIs(executor_registry.get(code), second)
        finally:
            executor_registry._executors.pop(code, None)
            executor_registry._overridden.discard(code)

    def test_a_first_override_is_silent(self):
        """Replacing core's own binding is the supported, normal case."""
        code = "test_override_first"
        replacement = type("C", (), {})
        try:
            executor_registry.override(code, replacement)
            self.assertIs(executor_registry.get(code), replacement)
        finally:
            executor_registry._executors.pop(code, None)
            executor_registry._overridden.discard(code)
