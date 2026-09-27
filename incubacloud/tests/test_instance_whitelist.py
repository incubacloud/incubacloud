"""A staging reaches what its operator wrote down, and nothing else.

Egress used to be decided per host: one proxy container per hostname in
``~/globalwhitelist``, shared by every test instance on the machine.
That answers "what may stagings on this box reach", which is never the
question a customer asks — theirs is "this staging has to reach *their*
API". From template v9.6.0 the doodba template answers that question
directly, and F1 adopts it.

Adopting it means taking responsibility for what the template renders,
because two of its choices are not ours to ship:

* it mounts the host's docker socket into ``odoo_net_setup``
  unconditionally — no copier question turns it off — and ``:ro`` does
  not make the Docker API read-only, so on a shared host that mount is
  root over every customer on it;
* it ships that sidecar with ``healthcheck: disable: true``, while the
  route injection it performs happens once at start. Restart odoo and
  the injection is gone, the sidecar stays ``Up``, and the staging is
  on the open internet with every light green. That is the 2026-08-06
  incident on this very panel: three and a half hours of Telegram to
  the live channel.

Both are undone in the deploy override, and these tests are what say
so. The socket one in particular cannot be asserted loosely: an empty
``volumes:`` list does **not** remove a mount, because compose merges
volumes by target path. It has to be re-pointed, and the test has to
know the difference.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import yaml

from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers import _rate_limit, terminal
from odoo.addons.incubacloud.models.cloud_host_whitelist import (
    DEFAULT_WHITELIST,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)

_SOCKET = "/var/run/docker.sock"


class _WhitelistCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Egress F1"})
        self.host = self.env["cloud.host"].create({
            "name": "f1-host",
            "ip_address": "192.0.2.90",
            "user": "ubuntu",
            "wildcard_domain": "f1.example.com",
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "f1stag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        self.prod = self.env["cloud.instance"].create({
            "name": "f1prod",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _set(self, instance, hostnames):
        """Replace *instance*'s whitelist with *hostnames*, in order."""
        instance.whitelist_ids.unlink()
        self.env["cloud.instance.whitelist"].create([
            {"instance_id": instance.id, "hostname": h, "sequence": i * 10}
            for i, h in enumerate(hostnames, 1)
        ])

    def _executor(self, executor_cls, job_type_code, inst):
        """Build *executor_cls* around a fresh job on *inst*."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", job_type_code)], limit=1,
        )
        self.assertTrue(job_type, f"job type {job_type_code} must exist")
        job = self.env["cloud.job"].create({
            "name": f"F1 {job_type_code}",
            "host_id": self.host.id,
            "instance_id": inst.id,
            "job_type_id": job_type.id,
        })
        return executor_cls(job, self.host)

    def _override(self, inst, executor_cls=DeployInstanceExecutor,
                  code="deploy_instance"):
        """Return the parsed override this instance's deploy would ship."""
        raw = self._executor(executor_cls, code, inst)\
            ._resource_override_content()
        self.assertTrue(raw, "override must never be None")
        return yaml.safe_load(raw)


class TestTheListIsSeeded(_WhitelistCase):
    """Nobody should have to fill in five hostnames to get a staging."""

    def test_a_new_staging_inherits_its_hosts_list(self):
        self.env["cloud.host.whitelist"].search(
            [("host_id", "=", self.host.id)]
        ).unlink()
        self.env["cloud.host.whitelist"].create([
            {"host_id": self.host.id, "hostname": "api.example.com"},
        ])
        inst = self.env["cloud.instance"].create({
            "name": "f1seeded",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        self.assertEqual(
            inst.whitelist_ids.mapped("hostname"), ["api.example.com"],
        )

    def test_a_staging_without_a_host_falls_back_to_the_default(self):
        """A staging can be created before its host is chosen. It still
        needs an answer, and the platform default is the same five."""
        inst = self.env["cloud.instance"].create({
            "name": "f1hostless",
            "project_id": self.project.id,
            "environment": "staging",
        })
        self.assertEqual(
            inst.whitelist_ids.mapped("hostname"), list(DEFAULT_WHITELIST),
        )

    def test_production_is_not_seeded(self):
        """Its answer is always []. A row nobody reads is a row somebody
        will eventually believe."""
        self.assertFalse(self.prod.whitelist_ids)

    def test_an_explicit_list_is_not_overwritten(self):
        inst = self.env["cloud.instance"].create({
            "name": "f1explicit",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
            "whitelist_ids": [(0, 0, {"hostname": "only.example.com"})],
        })
        self.assertEqual(
            inst.whitelist_ids.mapped("hostname"), ["only.example.com"],
        )


class TestAnEntryIsAHostname(_WhitelistCase):
    """The list is space-joined into the sidecar's ``ALLOWED_HOSTS``."""

    def test_a_space_would_have_become_two_rules(self):
        with self.assertRaises(Exception):
            self.env["cloud.instance.whitelist"].create({
                "instance_id": self.staging.id,
                "hostname": "api.example.com evil.example.com",
            })

    def test_a_url_is_refused(self):
        with self.assertRaises(Exception):
            self.env["cloud.instance.whitelist"].create({
                "instance_id": self.staging.id,
                "hostname": "https://api.example.com/path",
            })

    def test_a_plain_hostname_is_accepted(self):
        entry = self.env["cloud.instance.whitelist"].create({
            "instance_id": self.staging.id,
            "hostname": "api.example.com",
        })
        self.assertTrue(entry.exists())


class TestTheSidecarsAreExpected(_WhitelistCase):
    """A sidecar nobody expects is a sidecar nobody probes."""

    def test_a_list_brings_the_gateway_and_the_sidecar(self):
        self._set(self.staging, ["api.example.com"])
        self.assertEqual(
            set(self.staging.expected_services()),
            {"odoo", "db", "smtp", "proxy_general", "odoo_net_setup"},
        )

    def test_an_empty_list_renders_neither(self):
        """The template gates both on exactly this condition, so the
        probe has to gate on it too — or it would alert forever about
        containers the compose file never asked for."""
        self._set(self.staging, [])
        self.assertEqual(
            set(self.staging.expected_services()), {"odoo", "db", "smtp"},
        )

    def test_production_never_renders_them(self):
        self._set(self.prod, ["api.example.com"])
        self.assertNotIn("odoo_net_setup", self.prod.expected_services())


class TestTheSocketDoesNotTravel(_WhitelistCase):
    """The mount the template makes unconditionally, undone."""

    def test_the_socket_is_not_the_source_of_that_mount(self):
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        volumes = data["services"]["odoo_net_setup"]["volumes"]
        self.assertNotIn(
            f"{_SOCKET}:{_SOCKET}:ro", volumes,
            "the host's docker socket must not reach the sidecar",
        )

    def test_the_mount_is_repointed_rather_than_dropped(self):
        """Compose merges volumes by target path: an empty ``volumes:``
        list adds nothing and the base file's mount survives untouched.
        Measured, not assumed — so the override pins the same target to
        a source nobody can connect to."""
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        volumes = data["services"]["odoo_net_setup"]["volumes"]
        self.assertTrue(volumes, "an empty list would not remove the mount")
        targets = [v.split(":")[1] for v in volumes]
        self.assertIn(_SOCKET, targets)
        sources = [v.split(":")[0] for v in volumes]
        self.assertEqual(sources, ["/dev/null"])

    def test_the_lookup_that_wanted_it_is_switched_off(self):
        """The socket is read by one function of the image's entrypoint,
        guarded by this variable — whose default in the script itself is
        ``0``. It is the template that turns it on."""
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        env = data["services"]["odoo_net_setup"]["environment"]
        self.assertEqual(env["DNS_INTERNAL_FROM_DOCKER"], "0")

    def test_the_rebuild_re_ships_all_of_it(self):
        """``copier update`` rewrites the compose files on every
        rebuild, so anything written once is written again or lost."""
        self._set(self.staging, ["api.example.com"])
        data = self._override(
            self.staging, RebuildInstanceExecutor, "rebuild_instance",
        )
        svc = data["services"]["odoo_net_setup"]
        self.assertEqual([v.split(":")[0] for v in svc["volumes"]],
                         ["/dev/null"])
        self.assertEqual(svc["environment"]["DNS_INTERNAL_FROM_DOCKER"], "0")


class TestTheHealthcheckIsOurs(_WhitelistCase):
    """The 2026-08-06 incident, written as an assertion."""

    def test_the_check_is_not_disabled(self):
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        health = data["services"]["odoo_net_setup"]["healthcheck"]
        self.assertFalse(
            health.get("disable"),
            "compose merges the healthcheck key by key, so upstream's "
            "'disable: true' survives unless it is said false here",
        )

    def test_the_check_looks_at_the_default_route(self):
        """A lost injection puts the route back on Docker's own bridge
        gateway, which is always the .1 of the subnet. Anything that
        does not look at the route is checking the wrong thing: the
        sidecar's last line is ``tail -f /dev/null``, so it stays up
        whatever happened to the route."""
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        health = data["services"]["odoo_net_setup"]["healthcheck"]
        self.assertEqual(health["test"][0], "CMD-SHELL")
        self.assertIn("ip route show default", health["test"][1])

    def test_nothing_of_this_is_emitted_without_a_list(self):
        """No list, no sidecar — and an override naming a service the
        compose file does not define is a compose error."""
        self._set(self.staging, [])
        data = self._override(self.staging)
        self.assertNotIn("odoo_net_setup", data["services"])
        self.assertNotIn("proxy_general", data["services"])


class TestTheSidecarsAreStillManaged(_WhitelistCase):
    """Expected does not only mean probed."""

    def test_they_carry_the_prune_label(self):
        """``docker system prune`` sweeps unlabelled *stopped*
        containers, and a stopped sidecar is exactly the shape a
        restart leaves behind."""
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        for svc in ("proxy_general", "odoo_net_setup"):
            self.assertTrue(
                data["services"][svc]["labels"],
                f"{svc} must be protected from the daily prune",
            )

    def test_they_carry_a_log_limit(self):
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        for svc in ("proxy_general", "odoo_net_setup"):
            self.assertEqual(
                data["services"][svc]["logging"]["driver"], "json-file",
            )

    def test_they_get_no_invented_resource_limits(self):
        """Two shell scripts and a dnsmasq. A ceiling the operator never
        set would be a number this module made up."""
        self._set(self.staging, ["api.example.com"])
        data = self._override(self.staging)
        for svc in ("proxy_general", "odoo_net_setup"):
            self.assertNotIn("mem_limit", data["services"][svc])
            self.assertNotIn("cpus", data["services"][svc])


class TestTheShellDoesNotReachThem(_WhitelistCase):
    """Infrastructure this platform put on the host is not the
    customer's to sit inside."""

    def test_the_selector_offers_only_the_stack(self):
        self.staging.compose_services = (
            "odoo,db,smtp,proxy_general,odoo_net_setup"
        )
        self.assertEqual(
            self.staging._shell_services(), ["odoo", "db", "smtp"],
        )

    def test_an_unknown_service_is_not_offered_either(self):
        """The list is an allow-list, not a deny-list: a service the
        template grows next year is excluded until somebody says
        otherwise."""
        self.staging.compose_services = "odoo,db,something_new"
        self.assertEqual(self.staging._shell_services(), ["odoo", "db"])

    def test_the_default_still_works(self):
        self.staging.compose_services = False
        self.assertEqual(self.staging._shell_services(), ["odoo", "db"])


class TestTheTerminalRouteRefusesThem(_WhitelistCase):
    """The SPA's list is not a gate.

    ``terminal_open`` used to validate the *shape* of a service name
    with a regex and nothing else, so anything the browser sent that
    looked like a compose service was opened — and the browser is not
    where this decision can live.
    """

    def setUp(self):
        super().setUp()
        self.staging._transition("deployed")
        self.staging.sudo().write({
            "running": True,
            "compose_services": "odoo,db,smtp,proxy_general,odoo_net_setup",
        })
        self.controller = terminal.TerminalController()

    @contextmanager
    def _patched_request(self):
        """Bind ``request`` in both modules the route goes through.

        ``_rate_limit`` imports ``request`` into its own namespace, so
        patching only the route module leaves the gate reaching for a
        request that is not bound.
        """
        fake = MagicMock(spec=Request)
        fake.env = self.env
        Security = type(self.env["cloud.security.mixin"])
        with patch.object(terminal, "request", fake), \
                patch.object(_rate_limit, "request", fake), \
                patch.object(
                    Security, "_check_can_use_terminal", lambda self: True,
                ):
            yield

    def test_the_sidecar_is_refused_although_its_name_is_valid(self):
        with self._patched_request():
            res = self.controller.terminal_open(
                self.staging.id, service="odoo_net_setup",
            )
        self.assertFalse(res["ok"])

    def test_the_gateway_is_refused_too(self):
        with self._patched_request():
            res = self.controller.terminal_open(
                self.staging.id, service="proxy_general",
            )
        self.assertFalse(res["ok"])

    def test_a_service_outside_the_stack_is_refused(self):
        """It passes the regex, it is not in this stack, and before F1
        the route would have run ``docker compose exec`` on it."""
        with self._patched_request():
            res = self.controller.terminal_open(
                self.staging.id, service="db-of-another-project",
            )
        self.assertFalse(res["ok"])
