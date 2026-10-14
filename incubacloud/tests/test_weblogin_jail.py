"""The web-login jail: failed form logins to the instances, banned at the
host firewall, read from the proxy's access log.

The filter is pinned against lines as Traefik v2.11 writes them to its
access log file -- one JSON object per request -- including the attempt
that made Odoo's own log unusable for this: text the client controls
posing as another field. Traefik escapes the quotes, so the filter's
exact ``"ClientHost":"`` only ever matches the real one.
"""
import os
import re

import yaml
from jinja2 import Environment

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase

_LINE = (
    '{'
    '"ClientAddr":"203.0.113.5:4711","ClientHost":"203.0.113.5",'
    '"ClientPort":"4711","ClientUsername":"-","DownstreamContentSize":510,'
    '"DownstreamStatus":%(status)s,"Duration":3632737,'
    '"RequestHost":"erp.example.com","RequestMethod":"%(method)s",'
    '"RequestPath":"%(path)s","RequestPort":"-",'
    '"RouterName":"acme-19-0-prod-main-secure-0@docker",'
    '"StartUTC":"2026-10-13T08:35:29.996272385Z","entryPointName":"https",'
    '"level":"info","msg":"","time":"2026-10-13T08:35:29Z"}'
)


def _task(name):
    """Return the ``copy`` content of the hardening task called *name*."""
    path = os.path.join(
        get_module_path("incubacloud"), "ansible", "playbooks",
        "host_hardening.yml",
    )
    with open(path, encoding="utf-8") as fh:
        tasks = yaml.safe_load(fh)[0]["tasks"]
    for task in tasks:
        if task.get("name") == name:
            return task["ansible.builtin.copy"]["content"]
    raise AssertionError(f"no task named {name!r}")


class TestWebLoginFilter(BaseCase):
    """What the filter counts as a failed login, and whose address."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        content = _task("Write the fail2ban web-login filter")
        failregex = re.search(r"^failregex = (.+)$", content, re.MULTILINE)[1]
        # fail2ban's <HOST> captures an address; this is enough of it to
        # say which one the line is blamed on.
        cls.regex = re.compile(
            failregex.replace("<HOST>", r"(?P<host>[0-9A-Fa-f.:]+)"),
        )
        cls.content = content

    def _host_of(self, status, method, path):
        """Return the address a line is blamed on, or None."""
        match = self.regex.search(
            _LINE % {"status": status, "method": method, "path": path},
        )
        return match["host"] if match else None

    def test_a_failed_form_login_counts(self):
        """POST answered 200: the form was drawn again."""
        for path in (
            "/web/login", "/web/login?redirect=%2Fodoo", "/web/login/totp",
        ):
            self.assertEqual(self._host_of(200, "POST", path), "203.0.113.5", path)

    def test_a_successful_login_and_a_page_view_do_not(self):
        """A login that works is redirected; a GET only shows the form."""
        self.assertIsNone(self._host_of(303, "POST", "/web/login"))
        self.assertIsNone(self._host_of(200, "GET", "/web/login"))
        self.assertIsNone(self._host_of(200, "POST", "/web/login/reset"))

    def test_text_from_the_client_cannot_name_another_address(self):
        """Traefik escapes the quotes the client sends, so a field forged
        inside the path never matches the filter's exact one."""
        forged = r'/web/login?x=\",\"ClientHost\":\"9.9.9.9'
        self.assertEqual(self._host_of(200, "POST", forged), "203.0.113.5")
        posing = r'/x?\"RequestMethod\":\"POST\",\"RequestPath\":\"/web/login\"'
        self.assertIsNone(self._host_of(200, "GET", posing))

    def test_the_time_is_read_from_the_lines_own_field(self):
        """Traefik writes several timestamps per line; ``time`` is the one
        the request is dated by, in UTC (the jail says so)."""
        self.assertIn('datepattern = "time":"%%Y-%%m-%%dT%%H:%%M:%%S', self.content)


class TestWebLoginJail(BaseCase):
    """Where the jail bans, and when it is on."""

    def _render(self, **extra):
        """Render the jail as Ansible would."""
        env = Environment(trim_blocks=True, autoescape=False)  # nosec B701
        # Ansible's ``bool``: the playbook reads the flag through it so a
        # string passed by hand ("false") is not taken as true.
        env.filters["bool"] = lambda v: (
            v if isinstance(v, bool)
            else str(v).strip().lower() in ("1", "true", "yes", "on")
        )
        values = {"ic_fail2ban_ignoreip": "127.0.0.1/8 ::1 203.0.113.7"}
        values.update(extra)
        return env.from_string(
            _task("Write the fail2ban web-login jail"),
        ).render(**values)

    def test_on_for_a_host_reached_directly(self):
        self.assertIn("enabled = true", self._render(ic_behind_cdn=False))
        self.assertIn("enabled = true", self._render())

    def test_off_behind_a_cdn(self):
        """There the connecting address is the CDN's: banning it would cut
        every visitor of that edge."""
        self.assertIn("enabled = false", self._render(ic_behind_cdn=True))

    def test_the_ban_lands_where_instance_traffic_flows(self):
        """Forward hook, 80/443 only: the instances' traffic is DNAT'd to
        the proxy container and never reaches input; SSH keeps its own
        jail and nothing outbound is touched."""
        jail = self._render()
        self.assertIn("chain_hook=forward", jail)
        self.assertIn('port="80,443"', jail)
        self.assertTrue(jail.endswith("ignoreip = 127.0.0.1/8 ::1 203.0.113.7"), jail)

    def test_it_has_a_chain_of_its_own(self):
        """The action's default chain is the one the sshd jail creates on
        the input hook wherever the distro bans with nftables (Ubuntu
        24.04); a chain cannot be on two hooks, so the second jail to
        start would silently fail to ban."""
        self.assertIn("chain=f2b-weblogin", self._render())

    def test_the_proxies_the_host_trusts_are_never_banned(self):
        """A failed login arriving through one is the visitor's."""
        jail = self._render(
            ic_trusted_proxy_ranges="198.51.100.0/24 2001:db8::/32",
        )
        self.assertTrue(jail.endswith(
            "ignoreip = 127.0.0.1/8 ::1 203.0.113.7 198.51.100.0/24 2001:db8::/32",
        ), jail)

    def test_it_watches_the_proxys_own_access_log(self):
        """A stable path on the host, whatever the container's id."""
        jail = self._render()
        self.assertIn("logpath = /var/log/traefik/access.log", jail)
        self.assertIn("logtimezone = UTC", jail)

    def test_the_file_exists_before_fail2ban_restarts(self):
        """fail2ban refuses a jail whose file is missing, and hardening
        runs before Full Setup has started the proxy."""
        path = os.path.join(
            get_module_path("incubacloud"), "ansible", "playbooks",
            "host_hardening.yml",
        )
        with open(path, encoding="utf-8") as fh:
            names = [t.get("name") for t in yaml.safe_load(fh)[0]["tasks"]]
        self.assertLess(
            names.index("Make sure the proxy access log exists"),
            names.index("Enable and (re)start fail2ban"),
        )
