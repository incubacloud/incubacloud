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
import importlib.util
import os

from unittest.mock import MagicMock

from lxml import etree

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase, TransactionCase

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

    def test_apt_is_bounded_in_time_and_retried(self):
        """A stalled download times out and is retried, rather than waited
        on: a new host's cache update hung 17 minutes on a connection the
        mirror had closed (2026-10-09)."""
        tasks = self._playbook()["tasks"]
        names = [task.get("name") for task in tasks]
        config = tasks[names.index("Bound apt's downloads in time, and retry them")]
        update = tasks[names.index("Update the apt cache")]
        self.assertLess(names.index(config["name"]), names.index(update["name"]))
        content = config["ansible.builtin.copy"]["content"]
        self.assertIn('Acquire::Retries "3";', content)
        self.assertIn('Acquire::https::Timeout "30";', content)
        # Each attempt has its own limit, under the task's.
        self.assertTrue(
            update["ansible.builtin.command"].startswith("timeout 180 apt-get"),
        )
        self.assertEqual(update["retries"], 3)
        self.assertEqual(update["until"], "ic_apt_cache is succeeded")
        self.assertGreater(update["timeout"], 4 * (180 + update["delay"]))
        # Nothing privileged may come before the first of them.
        self.assertEqual(names.index(config["name"]), 0)

    def test_apt_waits_for_the_first_boot(self):
        """cloud-init holds apt's lock on a server just bought: the update
        waited on it until it ran out of time (QA, 2026-10-20)."""
        tasks = self._playbook()["tasks"]
        names = [task.get("name") for task in tasks]
        wait = names.index("Wait for the server's first boot (cloud-init) to finish")
        self.assertLess(wait, names.index("Update the apt cache"))
        script = tasks[wait]["ansible.builtin.shell"]
        # Nothing to wait for without cloud-init; its own errors are its own.
        self.assertIn("command -v cloud-init >/dev/null || exit 0", script)
        self.assertIn("cloud-init status --wait", script)
        self.assertIn("|| true", script)

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
        jails = {
            str(t["ansible.builtin.copy"]["dest"]): t["ansible.builtin.copy"]["content"]
            for t in (play.get("tasks") or [])
            if "fail2ban/jail.d" in str(
                t.get("ansible.builtin.copy", {}).get("dest", "")
            )
        }
        # The sshd jail, and since 1.0.183 the web-login one: a ban on
        # 80/443 of the panel's address would cut its health checks and
        # every tenant it serves from there.
        self.assertEqual(set(jails), {
            "/etc/fail2ban/jail.d/sshd-incubacloud.local",
            "/etc/fail2ban/jail.d/incubacloud-weblogin.local",
        })
        for dest, content in jails.items():
            self.assertIn("ignoreip", content, dest)
            self.assertIn(
                "ic_fail2ban_ignoreip", content,
                "every jail must exempt the operators the firewall trusts — "
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
        content = self._ruleset_task()["ansible.builtin.copy"]["content"]
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
        content = self._ruleset_task()["ansible.builtin.copy"]["content"]
        self.assertIn("ic_effective_allowlist", content)
        self.assertNotIn("{{ ssh_allowlist }}", content)

    # ── the sshd drop-in has to win, and the play has to prove it ──────
    _SSHD_LEGACY = "/etc/ssh/sshd_config.d/99-incubacloud-hardening.conf"

    def _sshd_dropin_task(self):
        """Return the task that writes the sshd hardening drop-in."""
        tasks = self._playbook().get("tasks") or []
        return tasks[self._task_index(tasks, "Write the sshd hardening drop-in")]

    def test_the_sshd_dropin_is_read_before_cloud_init(self):
        """sshd keeps the FIRST value per keyword and reads drop-ins in
        name order; Hetzner's ``50-cloud-init.conf`` says
        ``PasswordAuthentication yes``. Under the old ``99-`` name the
        host kept taking passwords (measured on 2026-09-30)."""
        dest = self._sshd_dropin_task()["ansible.builtin.copy"]["dest"]
        self.assertTrue(dest.startswith("/etc/ssh/sshd_config.d/"), dest)
        self.assertLess(dest.rsplit("/", 1)[1], "50-cloud-init.conf")

    def test_the_executor_edits_the_file_the_playbook_writes(self):
        """The final root-disable step seds the drop-in by path; a rename
        on one side only would make it edit a file that does not exist."""
        from odoo.addons.incubacloud.models import host_hardening_executor
        self.assertEqual(
            host_hardening_executor._SSHD_DROPIN,
            self._sshd_dropin_task()["ansible.builtin.copy"]["dest"],
        )

    def test_the_old_dropin_is_removed(self):
        """A host hardened under the old name keeps no second copy."""
        tasks = self._playbook().get("tasks") or []
        removals = [
            t for t in tasks
            if t.get("ansible.builtin.file", {}).get("path")
            == self._SSHD_LEGACY
        ]
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0]["ansible.builtin.file"]["state"], "absent")

    def test_the_effective_sshd_settings_are_checked_before_restart(self):
        """``sshd -t`` proves the files parse, not that ours win. The play
        reads ``sshd -T`` and asserts the three values that matter before
        sshd is moved to the new port, so a host where another file
        outranks ours keeps its current sshd."""
        tasks = self._playbook().get("tasks") or []
        read = tasks[self._task_index(
            tasks, "Read the settings sshd will actually use",
        )]
        self.assertEqual(read["ansible.builtin.command"], "sshd -T")
        self.assertIs(read.get("changed_when"), False)
        check = tasks[self._task_index(
            tasks, "Refuse to go on if another sshd file outranks ours",
        )]
        that = " ".join(check["ansible.builtin.assert"]["that"])
        for needle in (
            "'passwordauthentication no'",
            "'permitrootlogin without-password'",
            "'port ' ~ ssh_port",
        ):
            self.assertIn(needle, that)
        order = [
            self._task_index(tasks, name) for name in (
                "Write the sshd hardening drop-in",
                "Validate the sshd configuration",
                "Read the settings sshd will actually use",
                "Refuse to go on if another sshd file outranks ours",
                "Wait for sshd to listen on the new port",
            )
        ]
        self.assertEqual(order, sorted(order))

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

    # ── Phase 4b: the per-source connection-rate cap (P2, SEC-008) ──────
    _RULESET_LIVE = "/etc/nftables.conf"
    _RULESET_STAGING = "/etc/nftables.conf.new"

    def _ruleset_task(self):
        """Return the one task that renders the ruleset, to its staging file."""
        rules = [
            t for t in (self._playbook().get("tasks") or [])
            if "content" in t.get("ansible.builtin.copy", {})
            and str(t["ansible.builtin.copy"].get("dest", "")).startswith(
                self._RULESET_LIVE,
            )
        ]
        self.assertEqual(len(rules), 1)
        return rules[0]

    def _ruleset_content(self):
        """Return the raw ruleset template the playbook writes."""
        return self._ruleset_task()["ansible.builtin.copy"]["content"]

    def _task_index(self, tasks, name):
        """Return the position of the task called *name*, which must exist."""
        names = [t.get("name") for t in tasks]
        self.assertIn(name, names)
        return names.index(name)

    def test_the_ruleset_is_parsed_on_the_host_before_it_goes_live(self):
        """Render to a staging file, ``nft -c`` it there, then install.

        The kernel loads a ruleset atomically, so a bad one never half
        applies -- but the file it was read from is what the nftables
        service loads at boot, and a bad file left there is a host that
        comes back with no firewall. 2026-09-29: a rule the fleet's nft
        could not type overwrote the live file and failed.
        """
        tasks = self._playbook().get("tasks") or []
        write = self._ruleset_task()
        self.assertEqual(
            write["ansible.builtin.copy"]["dest"], self._RULESET_STAGING,
        )
        check = tasks[self._task_index(
            tasks, "Check the ruleset loads on this host's nftables",
        )]
        self.assertEqual(
            check["ansible.builtin.command"],
            f"nft -c -f {self._RULESET_STAGING}",
        )
        self.assertIs(check.get("changed_when"), False)
        install = tasks[self._task_index(tasks, "Install the checked ruleset")]
        self.assertEqual(
            install["ansible.builtin.copy"]["src"], self._RULESET_STAGING,
        )
        self.assertEqual(
            install["ansible.builtin.copy"]["dest"], self._RULESET_LIVE,
        )
        self.assertIs(install["ansible.builtin.copy"]["remote_src"], True)
        apply = tasks[self._task_index(tasks, "Apply the nftables ruleset")]
        self.assertEqual(
            apply["ansible.builtin.command"], f"nft -f {self._RULESET_LIVE}",
        )
        order = [tasks.index(t) for t in (write, check, install, apply)]
        self.assertEqual(order, sorted(order))

    def test_nothing_else_writes_the_live_ruleset(self):
        """Only the copy of the checked staging file may land there."""
        tasks = self._playbook().get("tasks") or []
        writers = [
            t["name"] for t in tasks
            if self._RULESET_LIVE in (
                str(t.get("ansible.builtin.copy", {}).get("dest", "")),
                str(t.get("ansible.builtin.template", {}).get("dest", "")),
            )
        ]
        self.assertEqual(writers, ["Install the checked ruleset"])

    def _render_ruleset(self, **extra):
        """Render the ruleset template as Ansible would, with *extra* vars.

        Through the renderer CI parses the ruleset with
        (``tests/shell/render_firewall_ruleset.py``): Ansible templates the
        ``copy`` content with ``trim_blocks`` on, so the gate around the
        conn-rate meter is exercised exactly as it runs on a host, and the
        SSH lists are the facts the play's allowlist task makes of
        ``ssh_allowlist``.
        """
        base = {"ssh_port": 22222, "ssh_allowlist": "1.2.3.4"}
        base.update(extra)
        return _firewall_renderer().render(base)

    @staticmethod
    def _active_lines(rendered):
        """Rendered lines that are directives, not comments or blanks."""
        return [
            l for l in rendered.splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]

    _CDN = {
        "ic_http_allowed_ranges": "203.0.113.0/24, 2001:db8::/32",
        "ic_http_allowed_ranges_v4": "203.0.113.0/24",
        "ic_http_allowed_ranges_v6": "2001:db8::/32",
    }

    def test_a_host_reached_directly_declares_no_set(self):
        """The pre-existing shape: 80/443 open, nothing named."""
        active = "\n".join(self._active_lines(self._render_ruleset()))
        self.assertNotIn("ic_cdn_v4", active)
        self.assertIn("tcp dport { 80, 443 } accept", active)

    def test_a_filtered_host_declares_a_set_per_family(self):
        active = "\n".join(
            self._active_lines(self._render_ruleset(**self._CDN)),
        )
        self.assertIn("set ic_cdn_v4 {", active)
        self.assertIn("type ipv4_addr", active)
        self.assertIn("set ic_cdn_v6 {", active)
        self.assertIn("type ipv6_addr", active)

    def test_the_ranges_are_intervals_not_addresses(self):
        """Without the flag nftables refuses a CIDR in the set."""
        active = "\n".join(
            self._active_lines(self._render_ruleset(**self._CDN)),
        )
        cdn_sets = active.split("set ic_cdn_v4 {", 1)[1]
        self.assertEqual(cdn_sets.count("flags interval"), 2)

    def test_the_rule_names_the_set_rather_than_the_ranges(self):
        """The whole point: a named set can be refreshed later without
        rewriting the ruleset, so the daily job that notices the CDN
        published new ranges has something it can update. Inlined
        ranges left the firewall on whatever list the host was handed
        the day it was hardened."""
        active = "\n".join(
            self._active_lines(self._render_ruleset(**self._CDN)),
        )
        self.assertIn("tcp dport { 80, 443 } ip saddr @ic_cdn_v4", active)
        self.assertIn("tcp dport { 80, 443 } ip6 saddr @ic_cdn_v6", active)
        rule = [l for l in active.splitlines() if "@ic_cdn_v4" in l][0]
        self.assertNotIn("203.0.113.0/24", rule)

    def test_a_host_with_no_ipv6_range_declares_no_ipv6_set(self):
        """A rule naming a set that does not exist refuses to load, and
        an empty one would match nobody."""
        active = "\n".join(self._active_lines(self._render_ruleset(
            ic_http_allowed_ranges="203.0.113.0/24",
            ic_http_allowed_ranges_v4="203.0.113.0/24",
            ic_http_allowed_ranges_v6="",
        )))
        self.assertIn("set ic_cdn_v4 {", active)
        self.assertNotIn("ic_cdn_v6", active)

    def test_the_set_is_declared_before_the_chain_that_uses_it(self):
        rendered = self._render_ruleset(**self._CDN)
        self.assertLess(
            rendered.index("set ic_cdn_v4 {"),
            rendered.index("chain input {"),
        )

    # ── the SSH allowlist, one set per address family ──────────────────
    # A set holds one type of address and nft refuses any other: the
    # IPv6 address an operator's browser seeded a production host's list
    # with stopped its hardening at ``nft -c`` (27-oct-2026).
    _SSH_MIXED = "203.0.113.7, 2001:db8::7, 198.51.100.0/24, 2001:db8:1::/48"

    @staticmethod
    def _set_block(rendered, name):
        """Return the directives of the set *name*, up to its closing brace.

        :param str rendered: a rendered ruleset
        :param str name: the set's name
        :return: the set's lines, stripped, comments left out
        :rtype: list
        """
        lines = rendered.split(f"set {name} {{", 1)[1].splitlines()
        body = []
        for line in lines[1:]:
            if line.strip() == "}":
                return body
            if line.strip() and not line.strip().startswith("#"):
                body.append(line.strip())
        raise AssertionError(f"set {name} is never closed")

    def test_each_family_of_the_ssh_allowlist_has_its_own_set(self):
        """IPv4 entries in ``ic_ssh_allow``, IPv6 in ``ic_ssh_allow6``,
        addresses and networks alike, each set taking intervals and
        overlapping entries."""
        rendered = self._render_ruleset(ssh_allowlist=self._SSH_MIXED)
        v4 = self._set_block(rendered, "ic_ssh_allow")
        v6 = self._set_block(rendered, "ic_ssh_allow6")
        self.assertIn("type ipv4_addr", v4)
        self.assertIn("elements = { 203.0.113.7, 198.51.100.0/24 }", v4)
        self.assertIn("type ipv6_addr", v6)
        self.assertIn("elements = { 2001:db8::7, 2001:db8:1::/48 }", v6)
        # Networks in both, and overlapping entries (a catch-all beside an
        # address) in both.
        for block in (v4, v6):
            self.assertIn("flags interval", block)
            self.assertIn("auto-merge", block)

    def test_ssh_is_accepted_from_either_set(self):
        """One rule per family, ``ip`` and ``ip6 saddr``, after the sets
        they name."""
        rendered = self._render_ruleset(ssh_allowlist=self._SSH_MIXED)
        active = [l.strip() for l in self._active_lines(rendered)]
        self.assertIn("tcp dport 22222 ip saddr @ic_ssh_allow accept", active)
        self.assertIn("tcp dport 22222 ip6 saddr @ic_ssh_allow6 accept", active)
        self.assertLess(
            rendered.index("set ic_ssh_allow6 {"),
            rendered.index("chain input {"),
        )

    def test_a_family_with_no_address_has_an_empty_set(self):
        """Declared all the same, so the rule naming it loads and the
        panel can fill it later (``ssh_allowlist.sh``); without an
        ``elements`` line, which nft refuses empty."""
        for allowlist, empty, full in (
            ("203.0.113.7", "ic_ssh_allow6", "ic_ssh_allow"),
            ("2001:db8::7", "ic_ssh_allow", "ic_ssh_allow6"),
        ):
            with self.subTest(allowlist=allowlist):
                rendered = self._render_ruleset(ssh_allowlist=allowlist)
                self.assertFalse(
                    [l for l in self._set_block(rendered, empty)
                     if l.startswith("elements")],
                )
                self.assertIn(
                    f"elements = {{ {allowlist} }}",
                    self._set_block(rendered, full),
                )

    def test_the_catch_all_opens_ssh_in_both_families(self):
        """With no operator address known the panel sends ``0.0.0.0/0``:
        reachable rather than everyone locked out, IPv6 included."""
        rendered = self._render_ruleset(
            ssh_allowlist="0.0.0.0/0, 203.0.113.7, 2001:db8::7",
        )
        self.assertIn(
            "elements = { 0.0.0.0/0, 203.0.113.7 }",
            self._set_block(rendered, "ic_ssh_allow"),
        )
        self.assertIn(
            "elements = { ::/0, 2001:db8::7 }",
            self._set_block(rendered, "ic_ssh_allow6"),
        )
        # A list with addresses only opens nothing more.
        rendered = self._render_ruleset(ssh_allowlist="203.0.113.7")
        self.assertNotIn("::/0", rendered)

    def _allowlist_facts(self, ssh_allowlist, connection=""):
        """Return the facts the play's allowlist task sets.

        :param str ssh_allowlist: the configured list
        :param str connection: the address the play connects from
        :rtype: dict
        """
        renderer = _firewall_renderer()
        variables = {"ssh_allowlist": ssh_allowlist}
        if connection:
            variables["ic_ssh_connection"] = renderer._connection(connection)
        return renderer.allowlist_facts(variables)

    def _task_template(self, name, path, **facts):
        """Render the template at *path* of the task called *name*.

        :param str name: the task's name
        :param tuple path: the keys leading to the template in the task
        :rtype: str
        """
        from jinja2 import Environment

        tasks = self._playbook().get("tasks") or []
        value = tasks[self._task_index(tasks, name)]
        for key in path:
            value = value[key]
        return Environment(autoescape=False).from_string(value).render(  # nosec B701
            ssh_port=22222, **facts,
        )

    def test_the_address_the_play_comes_from_joins_its_family(self):
        """Connecting over IPv6, the play lets itself in through the IPv6
        set."""
        facts = self._allowlist_facts("203.0.113.7", connection="2001:db8::99")
        self.assertEqual(facts["ic_effective_allowlist_v4"], "203.0.113.7")
        self.assertEqual(facts["ic_effective_allowlist_v6"], "2001:db8::99")

    def test_the_run_reports_what_each_family_allows(self):
        """With no operator address the log says IPv6 is open to anyone
        too, rather than leave ``::/0`` to the ruleset alone."""
        facts = self._allowlist_facts("0.0.0.0/0", connection="95.217.1.126")
        message = self._task_template(
            "Report the effective allowlist", ("ansible.builtin.debug", "msg"),
            ssh_allowlist="0.0.0.0/0", **facts,
        )
        self.assertIn("IPv4 0.0.0.0/0, 95.217.1.126", message)
        self.assertIn("IPv6 ::/0", message)
        facts = self._allowlist_facts("203.0.113.7")
        message = self._task_template(
            "Report the effective allowlist", ("ansible.builtin.debug", "msg"),
            ssh_allowlist="203.0.113.7", **facts,
        )
        self.assertIn("IPv6 none", message)

    def test_the_panel_is_handed_what_the_firewall_allows(self):
        """``set_stats`` exports both families, the IPv6 catch-all
        included."""
        facts = self._allowlist_facts("0.0.0.0/0", connection="95.217.1.126")
        exported = self._task_template(
            "Export the hardening facts",
            ("ansible.builtin.set_stats", "data", "ic_effective_allowlist"),
            **facts,
        )
        self.assertEqual(exported, "0.0.0.0/0, 95.217.1.126, ::/0")
        # A family with no address adds nothing, not "None".
        exported = self._task_template(
            "Export the hardening facts",
            ("ansible.builtin.set_stats", "data", "ic_effective_allowlist"),
            **self._allowlist_facts("203.0.113.7, 198.51.100.4"),
        )
        self.assertEqual(exported, "203.0.113.7, 198.51.100.4")

    # ── the allowlist judges the forward hook ─────────────────────────
    _FWD_ACCEPT4 = (
        "meta l4proto tcp ct state new ct status dnat"
        " ct original proto-dst { 80, 443 } ip saddr @ic_cdn_v4 counter accept"
    )
    _FWD_ACCEPT6 = (
        "meta l4proto tcp ct state new ct status dnat"
        " ct original proto-dst { 80, 443 } ip6 saddr @ic_cdn_v6 counter accept"
    )
    _FWD_DROP = (
        "meta l4proto tcp ct state new ct status dnat"
        " ct original proto-dst { 80, 443 } counter drop"
    )

    def _forward_chain(self, rendered):
        """Return the forward chain's directives, stripped, in order."""
        fwd = rendered.index("chain forward {")
        out = rendered.index("chain output {")
        return [l.strip() for l in self._active_lines(rendered[fwd:out])]

    def test_the_allowlist_is_enforced_in_the_forward_chain(self):
        """Instance traffic is DNAT'd to the Traefik container and crosses
        the forward hook, never input. Measured in a lab on the fleet's
        iptables-nft backend: the list in the input chain alone let a
        source outside it straight through (200); the same list in
        forward dropped it."""
        forward = self._forward_chain(self._render_ruleset(**self._CDN))
        self.assertIn(self._FWD_ACCEPT4, forward)
        self.assertIn(self._FWD_ACCEPT6, forward)
        self.assertIn(self._FWD_DROP, forward)

    def test_every_verdict_on_a_new_web_connection_is_counted(self):
        """A panel that waits on new connections (QA run of 21-oct-2026)
        is told apart by the counters ``nft list ruleset`` shows: the CDN
        accepted or not, something dropped or not. QA keeps that listing
        when a walk fails. The input chain counts what reaches its drop
        policy, with no verdict of its own."""
        for extra in (self._CDN, {"ic_http_conn_rate": 50}):
            forward = self._forward_chain(self._render_ruleset(**extra))
            verdicts = [l for l in forward if "ct status dnat" in l]
            self.assertTrue(verdicts, extra)
            for line in verdicts:
                self.assertRegex(line, r" counter (accept|drop)$", line)
        rendered = self._render_ruleset()
        inp = rendered.index("chain input {")
        fwd = rendered.index("chain forward {")
        last = [l.strip() for l in self._active_lines(rendered[inp:fwd])][-2]
        self.assertEqual(last, 'counter comment "input policy drop"')

    def test_the_drop_comes_after_both_accepts(self):
        """A chain is read top to bottom; a drop above an accept would
        refuse the CDN as well."""
        forward = self._forward_chain(self._render_ruleset(**self._CDN))
        drop = forward.index(self._FWD_DROP)
        self.assertLess(forward.index(self._FWD_ACCEPT4), drop)
        self.assertLess(forward.index(self._FWD_ACCEPT6), drop)

    # ── the direct port ────────────────────────────────────────────────
    _DIRECT = {
        "ic_direct_port": 9443,
        "ic_direct_allowlist": "10.0.0.0/8, 46.4.0.20/32, 127.0.0.0/8",
    }

    def test_a_host_without_a_direct_port_names_nothing_of_it(self):
        for extra in ({}, self._CDN):
            with self.subTest(cdn=bool(extra)):
                self.assertNotIn("ic_direct", self._render_ruleset(**extra))

    def test_the_direct_set_carries_the_list_in_force(self):
        active = "\n".join(
            self._active_lines(self._render_ruleset(**self._DIRECT)),
        )
        self.assertIn("set ic_direct_v4 {", active)
        self.assertIn("auto-merge", active)
        self.assertIn(
            "elements = { 10.0.0.0/8, 46.4.0.20/32, 127.0.0.0/8 }", active,
        )

    def test_three_rules_judge_the_direct_port_in_forward(self):
        forward = self._forward_chain(
            self._render_ruleset(**(self._CDN | self._DIRECT)),
        )
        direct = [l for l in forward if 'comment "ic-direct"' in l]
        self.assertEqual(len(direct), 3)
        self.assertIn(
            "proto-dst 9443 ip saddr != @ic_direct_v4 counter drop", direct[0],
        )
        self.assertIn(
            "meter ic_direct_conn4 { ip saddr limit rate over 20/second"
            " burst 50 packets }",
            direct[1],
        )
        self.assertTrue(direct[2].startswith("meta nfproto ipv6 meta l4proto tcp"))
        # After the CDN's own verdicts, which they do not touch.
        self.assertLess(forward.index(self._FWD_DROP), forward.index(direct[0]))

    def test_only_the_first_packet_of_a_redirected_connection_is_judged(self):
        """Every allowlist rule in forward carries all four guards.

        Without ``ct state new`` the replies are judged too -- their
        source is the container and the connection still carries the
        dnat status -- and every response dies. Without ``ct status
        dnat`` the containers' own outbound 80/443 (backups, packages,
        webhooks) is weighed against the CDN's ranges. ``ct original
        proto-dst`` names the port the visitor dialled, not whatever
        the container listens on -- and nftables 1.0.2, the fleet's,
        can only type it once ``meta l4proto tcp`` has been stated
        before it: without that the whole ruleset is refused and the
        hardening run fails.
        """
        forward = self._forward_chain(self._render_ruleset(**self._CDN))
        rules = [
            l for l in forward
            if l.endswith(" accept") or l.endswith(" drop")
        ]
        self.assertEqual(len(rules), 3)
        for line in rules:
            self.assertTrue(line.startswith("meta l4proto tcp ct "), line)
            for guard in (
                "ct state new", "ct status dnat",
                "ct original proto-dst { 80, 443 }",
            ):
                self.assertIn(guard, line)

    def test_the_allowlist_replaces_the_cap_rather_than_stacking(self):
        """Behind a CDN every visitor arrives from a handful of edge
        addresses, so a per-source cap counts edges, not visitors: a
        rate left on the host is not rendered once the ranges are."""
        rendered = self._render_ruleset(**self._CDN, ic_http_conn_rate=50)
        forward = self._forward_chain(rendered)
        self.assertFalse([l for l in forward if "meter ic_http_conn" in l])
        self.assertIn(self._FWD_DROP, forward)

    def test_a_host_reached_directly_judges_nothing_in_forward(self):
        """Without ranges the forward chain allowlists nobody: the plain
        policy, plus the cap when a rate is set -- whose meters are the
        only rules there."""
        forward = self._forward_chain(self._render_ruleset())
        self.assertFalse([l for l in forward if "ct status dnat" in l])
        forward = self._forward_chain(self._render_ruleset(ic_http_conn_rate=50))
        judged = [l for l in forward if "ct status dnat" in l]
        self.assertEqual(len(judged), 2)
        self.assertTrue(all("meter ic_http_conn" in l for l in judged))
        self.assertFalse([l for l in forward if "@ic_cdn_v" in l])

    def test_a_host_with_no_ipv6_range_judges_no_ipv6_in_forward(self):
        forward = self._forward_chain(self._render_ruleset(
            ic_http_allowed_ranges="203.0.113.0/24",
            ic_http_allowed_ranges_v4="203.0.113.0/24",
            ic_http_allowed_ranges_v6="",
        ))
        self.assertIn(self._FWD_ACCEPT4, forward)
        self.assertNotIn(self._FWD_ACCEPT6, forward)
        self.assertIn(self._FWD_DROP, forward)

    def test_conn_rate_meter_is_absent_without_a_rate(self):
        """A run that passes no rate renders no cap.

        The executor always passes the host's rate; a playbook run by
        hand without one, or a host set to 0, keeps the plain accept path.
        """
        active = self._active_lines(self._render_ruleset())
        self.assertFalse(
            [l for l in active if "meter ic_http_conn" in l],
            "the conn-rate meter must be gated off when ic_http_conn_rate "
            "is unset",
        )

    def test_conn_rate_meter_targets_the_forward_hook_when_enabled(self):
        """Set a rate and both families gain a per-source cap on 80/443.

        Tenant traffic is DNAT'd to the Traefik container, so it crosses
        the forward hook, not input — the meter has to live in the forward
        chain or it never sees an instance request.
        """
        rendered = self._render_ruleset(ic_http_conn_rate=50)
        active = "\n".join(self._active_lines(rendered))
        # Both families, keyed on the real source address.
        self.assertIn("meter ic_http_conn4", active)
        self.assertIn("ip saddr limit rate over 50/second", active)
        self.assertIn("meter ic_http_conn6", active)
        self.assertIn("ip6 saddr limit rate over 50/second", active)
        # Only NEW connections are metered, and the excess is dropped.
        self.assertIn("ct state new", active)
        self.assertIn("drop", active)
        # …inside the forward chain, never input.
        fwd = rendered.index("chain forward {")
        out = rendered.index("chain output {")
        meter = rendered.index("meter ic_http_conn4")
        self.assertTrue(
            fwd < meter < out,
            "the meter must sit in the forward chain, between forward and "
            "output",
        )

    def test_the_cap_weighs_only_connections_redirected_to_the_proxy(self):
        """The cap carries the same four guards as the CDN allowlist.

        Matched on ``tcp dport`` alone it also counted the containers'
        own outbound 80/443, so an Odoo calling an API in a burst was
        throttled like a visitor. ``ct status dnat`` keeps that traffic
        out, ``ct original proto-dst`` names the port the visitor
        dialled, and ``meta l4proto tcp`` comes first because nftables
        1.0.2 cannot type the original port without it.
        """
        forward = self._forward_chain(self._render_ruleset(ic_http_conn_rate=50))
        meters = [l for l in forward if "meter ic_http_conn" in l]
        self.assertEqual(len(meters), 2)
        for line in meters:
            self.assertTrue(line.startswith("meta l4proto tcp ct "), line)
            for guard in (
                "ct state new", "ct status dnat",
                "ct original proto-dst { 80, 443 }",
            ):
                self.assertIn(guard, line)
            self.assertNotIn("tcp dport", line)

    def test_conn_rate_never_breaks_the_ruleset(self):
        """On or off, the ruleset stays valid and Docker-safe.

        Balanced braces (a swallowed brace is how a templating slip takes
        the firewall down), never a host-wide ``flush``, and the
        declare-then-delete idiom kept intact.
        """
        for extra in (
            {}, {"ic_http_conn_rate": 50},
            self._CDN, self._CDN | {"ic_http_conn_rate": 50},
        ):
            rendered = self._render_ruleset(**extra)
            self.assertEqual(
                rendered.count("{"), rendered.count("}"),
                f"unbalanced braces with extra={extra}",
            )
            self.assertNotIn(
                "flush ruleset", self._active_lines(rendered),
                "a host-wide flush deletes Docker's chains along with ours",
            )
            self.assertIn(
                "table inet filter\ndelete table inet filter", rendered,
            )



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
        host.http_conn_rate = host_attrs.get("http_conn_rate", 0)
        host.behind_cdn = host_attrs.get("behind_cdn", False)
        host.block_direct_access = host_attrs.get("block_direct_access", False)
        host.direct_port_shipped = host_attrs.get("direct_port_shipped", 0)
        host.direct_allowlist_shipped = host_attrs.get(
            "direct_allowlist_shipped", "",
        )
        host._effective_trusted_proxy_ranges.return_value = host_attrs.get(
            "trusted_proxy_ranges", [],
        )
        # Set, not left to the mock: an unset method answers a truthy
        # MagicMock, and every host would get the CDN allowlist.
        host._hardening_admits_only_proxies.return_value = host_attrs.get(
            "admits_only_proxies", False,
        )
        # The panel's own address, as the host says it: none, as in core,
        # unless a case gives one.
        host._hardening_panel_ip.return_value = host_attrs.get("panel_ip", "")

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

    def test_a_host_without_a_direct_port_gets_no_direct_vars(self):
        ev = self._make().get_extra_vars()
        self.assertNotIn("ic_direct_port", ev)

    def test_the_direct_port_and_its_list_reach_the_playbook(self):
        ev = self._make(
            direct_port_shipped=9443,
            direct_allowlist_shipped="10.0.0.0/8\n46.4.0.20/32",
        ).get_extra_vars()
        self.assertEqual(ev["ic_direct_port"], 9443)
        self.assertEqual(ev["ic_direct_allowlist"], "10.0.0.0/8, 46.4.0.20/32")

    def test_extra_vars_carries_the_http_conn_rate(self):
        """The per-host conn-rate cap reaches the playbook as set, 0 included."""
        off = self._make(user="root", port=22).get_extra_vars()
        self.assertEqual(off["ic_http_conn_rate"], 0)
        on = self._make(
            user="root", port=22, http_conn_rate=50,
        ).get_extra_vars()
        self.assertEqual(on["ic_http_conn_rate"], 50)

    def test_behind_a_cdn_the_cap_is_left_out_whatever_the_host_says(self):
        """Every visitor arrives from the same handful of edge addresses
        there, so a per-source cap would throttle the CDN itself. Open or
        closed at the origin alike: open is the state a pool host is in
        between moving its names and closing its firewall."""
        open_ = self._make(
            user="root", port=22, http_conn_rate=50, behind_cdn=True,
        ).get_extra_vars()
        self.assertEqual(open_["ic_http_conn_rate"], 0)
        closed = self._make(
            user="root", port=22, http_conn_rate=50, behind_cdn=True,
            block_direct_access=True, trusted_proxy_ranges=["198.51.100.0/24"],
            admits_only_proxies=True,
        ).get_extra_vars()
        self.assertEqual(closed["ic_http_conn_rate"], 0)

    def test_extra_vars_name_the_proxies_the_jail_must_not_ban(self):
        ex = self._make(
            user="root", port=22,
            trusted_proxy_ranges=["198.51.100.0/24", "2001:db8::/32"],
        )
        self.assertEqual(
            ex.get_extra_vars()["ic_trusted_proxy_ranges"],
            "198.51.100.0/24 2001:db8::/32",
        )
        self.assertEqual(
            self._make(user="root", port=22).get_extra_vars()["ic_trusted_proxy_ranges"],
            "",
        )

    def test_extra_vars_tell_the_playbook_whether_a_cdn_fronts_the_host(self):
        """The web-login jail bans by connecting address, which is the
        CDN's on a host behind one: the playbook needs to know."""
        direct = self._make(user="root", port=22).get_extra_vars()
        self.assertIs(direct["ic_behind_cdn"], False)
        fronted = self._make(user="root", port=22, behind_cdn=True).get_extra_vars()
        self.assertIs(fronted["ic_behind_cdn"], True)

    _CDN_KEYS = (
        "ic_http_allowed_ranges",
        "ic_http_allowed_ranges_v4",
        "ic_http_allowed_ranges_v6",
    )

    def test_the_cdn_allowlist_goes_only_to_a_host_closed_to_others(self):
        """``cloud.host._hardening_admits_only_proxies`` decides: a host
        that answers anybody gets no allowlist, whatever ranges it
        knows, and one closed to all but its proxies gets them by
        family."""
        ranges = ["198.51.100.0/24", "2001:db8::/32"]
        open_ = self._make(
            user="root", port=22, behind_cdn=True, trusted_proxy_ranges=ranges,
        ).get_extra_vars()
        for key in self._CDN_KEYS:
            self.assertNotIn(key, open_)
        closed = self._make(
            user="root", port=22, behind_cdn=True, block_direct_access=True,
            trusted_proxy_ranges=ranges, admits_only_proxies=True,
        ).get_extra_vars()
        self.assertEqual(
            [closed[key] for key in self._CDN_KEYS],
            ["198.51.100.0/24, 2001:db8::/32", "198.51.100.0/24", "2001:db8::/32"],
        )

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
        """A host that knows no panel address: the allowlist is field-driven.

        The address is the host's to say (``_hardening_panel_ip``, which
        the hardening dialog reads too), so this holds whichever layers
        are installed beside core.
        """
        ex = self._make(allowed_ssh_ips="")
        self.assertEqual(ex._operator_ip(), "")
        self.assertEqual(ex.get_extra_vars()["ic_ssh_allowlist"], "0.0.0.0/0")

    def test_the_operator_ip_is_the_host_s_panel_address(self):
        """One source for the panel's address: the executor asks the host."""
        ex = self._make(panel_ip="198.51.100.9", allowed_ssh_ips="203.0.113.7")
        self.assertEqual(ex._operator_ip(), "198.51.100.9")
        self.assertEqual(
            ex.get_extra_vars()["ic_ssh_allowlist"], "198.51.100.9, 203.0.113.7",
        )

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


def _firewall_renderer():
    """Import ``tests/shell/render_firewall_ruleset.py``, CI's renderer."""
    path = os.path.join(
        get_module_path("incubacloud"), "tests", "shell",
        "render_firewall_ruleset.py",
    )
    spec = importlib.util.spec_from_file_location(
        "ic_render_firewall_ruleset", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_conn_rate_migration():
    """Import the 1.0.183 post-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.183",
        "post-migrate.py",
    )
    spec = importlib.util.spec_from_file_location(
        "ic_post_migrate_1_0_179", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConnRateDefault(TransactionCase):
    """The cap is on by default, for new hosts and for those left at 0."""

    def _host(self, name, **vals):
        """Create a host with the minimum fields, plus *vals*."""
        return self.env["cloud.host"].create({
            "name": name,
            "ip_address": "192.0.2.40",
            "user": "root",
            "wildcard_domain": f"{name}.example.com",
        } | vals)

    def test_a_new_host_gets_the_cap(self):
        """50 new connections per second per source, not 0."""
        self.assertEqual(self._host("rate-new").http_conn_rate, 50)

    def test_the_migration_raises_only_hosts_left_at_zero(self):
        """A host at 0 takes the default; one tuned by hand keeps its rate."""
        off = self._host("rate-off", http_conn_rate=0)
        tuned = self._host("rate-tuned", http_conn_rate=120)
        _load_conn_rate_migration().migrate(self.env.cr, "1.0.181")
        (off | tuned).invalidate_recordset(["http_conn_rate"])
        self.assertEqual(off.http_conn_rate, 50)
        self.assertEqual(tuned.http_conn_rate, 120)



class TestAuditRetention(TransactionCase):
    """auditd keeps weeks, not days (13-oct-2026: Cloud 1 held 2-3 days)."""

    def _tasks(self):
        """Return the hardening playbook's tasks."""
        import yaml

        path = os.path.join(
            get_module_path("incubacloud"), "ansible", "playbooks",
            "host_hardening.yml",
        )
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)[0]["tasks"]

    def test_container_bpf_noise_is_excluded_before_the_real_rules(self):
        """Three quarters of the log was systemd's BPF loads for container
        starts and stops; "never" rules only spare what follows them."""
        rules = next(
            t for t in self._tasks() if t.get("name") == "Write the auditd rules"
        )["ansible.builtin.copy"]["content"]
        lines = [
            l for l in rules.splitlines() if l.strip() and not l.startswith("#")
        ]
        self.assertEqual(lines[0], "-a never,exclude -F msgtype=BPF")
        self.assertEqual(lines[1], "-a never,exit -F arch=b64 -S bpf")
        self.assertTrue(all(l.startswith("-w ") or "always" in l for l in lines[2:]))

    def test_the_log_is_capped_and_kept_long(self):
        """Rotation is by size: 12 files of 25 MB, 300 MB at most."""
        task = next(
            t for t in self._tasks()
            if t.get("name") == "Set how much the audit log keeps"
        )
        values = {i["key"]: i["value"] for i in task["loop"]}
        self.assertEqual(values["max_log_file"], "25")
        self.assertEqual(values["num_logs"], "12")
        self.assertEqual(values["max_log_file_action"], "ROTATE")

    def test_restarting_journald_leaves_auditing_on(self):
        """On Ubuntu 22.04 the journald restart of this very playbook
        switched the kernel's auditing off until the next reboot."""
        content = next(
            t for t in self._tasks()
            if t.get("name") == "Write the journald limits"
        )["ansible.builtin.copy"]["content"]
        self.assertIn("\nAudit=yes\n", content)


class TestHardeningScreensSpacing(BaseCase):
    """A value and the words a branch adds after it read as one sentence.

    OWL drops a text node that is only whitespace with a line break in
    it. A count in ``<strong>`` on one line and ``<t t-if="...">running
    instances.</t>`` on the next drew "interrupt 2running instances" in
    the hardening dialog (27-oct-2026). A ``<t>`` draws no box, so no
    style can put the space back: it has to be in the template.
    """

    _TEMPLATES = ("host_hardening_modal", "host_detail")
    _INLINE = {"t", "strong", "b", "em", "i", "span", "code", "a", "small"}

    @classmethod
    def _ends_in_a_word(cls, node):
        """Whether *node* draws a value, a word or punctuation last.

        :param node: an element of the template
        :return: ``False`` when it ends in a space, an icon or a block,
            or draws nothing
        :rtype: bool
        """
        if node.tag not in cls._INLINE:
            return False
        value = node.get("t-esc") or node.get("t-out")
        if value:
            return "env.icon" not in value
        children = [c for c in node if isinstance(c.tag, str)]
        last_text = (children[-1].tail if children else node.text) or ""
        if last_text.strip():
            return not last_text[-1].isspace()
        if children and ("\n" in last_text or not last_text):
            return cls._ends_in_a_word(children[-1])
        return False

    def test_no_value_runs_into_the_words_after_it(self):
        """Every pair of siblings parted by a dropped line break, where a
        value ends and a ``<t>`` opens with a word, is reported."""
        for name in self._TEMPLATES:
            path = os.path.join(
                get_module_path("incubacloud"),
                "static", "src", "components", name, f"{name}.xml",
            )
            glued = []
            for parent in etree.parse(path).iter():
                kids = [k for k in parent if isinstance(k.tag, str)]
                for before, branch in zip(kids, kids[1:]):
                    gap = before.tail or ""
                    # A gap with something in it, or a space alone on
                    # the line, is kept.
                    if gap.strip() or "\n" not in gap:
                        continue
                    if branch.tag != "t" or any(
                        branch.get(a) is not None for a in
                        ("t-esc", "t-out", "t-call", "t-elif", "t-else")
                    ):
                        continue
                    if (branch.text or "")[:1].isalnum() and self._ends_in_a_word(before):
                        glued.append(branch.sourceline)
            with self.subTest(template=name):
                self.assertFalse(
                    glued,
                    f"{name}.xml, lines {glued}: the text opens with no "
                    f"space after a value, and draws glued to it",
                )

    def test_the_production_gate_asks_in_one_sentence(self):
        """"Type <name> to confirm:" was two terms around the name, and
        the first one, "Type", is also the noun of every type column:
        Spanish read "Tipo <name> para confirmar:". One term with the name
        as a placeholder lets the sentence be translated as a whole."""
        root = get_module_path("incubacloud")
        component = os.path.join(
            root, "static", "src", "components", "host_hardening_modal",
            "host_hardening_modal",
        )
        texts = [
            (t or "").strip() for el in etree.parse(f"{component}.xml").iter()
            for t in (el.text, el.tail)
        ]
        self.assertNotIn("Type", texts)
        self.assertNotIn("to confirm:", texts)
        with open(f"{component}.js", encoding="utf-8") as fh:
            self.assertIn('_t("Type %(host)s to confirm:"', fh.read())
        with open(os.path.join(root, "i18n", "es.po"), encoding="utf-8") as fh:
            self.assertIn(
                'msgid "Type %(host)s to confirm:"\n'
                'msgstr "Escriba %(host)s para confirmar:"',
                fh.read(),
            )
