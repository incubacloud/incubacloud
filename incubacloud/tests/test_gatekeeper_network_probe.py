"""The health probe tells when odoo is not in its gatekeeper's network.

Under template v10 odoo runs in the network namespace of the staging's
``gatekeeper``, and Traefik reaches it there. The gatekeeper gets a new
namespace every time it starts — restarted, recreated, started again by
an ``up`` — and a running odoo stays in the one it joined (Docker 29,
Compose 5). Its own curl, which is where the HTTP check asks, still
answers there while every visitor gets a 502. That namespace loses
Docker's interface and every route through it, but keeps the rest: a
bridge the gatekeeper made, the tunnels the kernel adds to each new
namespace, the IPv6 link-local and multicast routes of those that are
up. The probe now reads odoo's routes, and grades an odoo without one
but its loopback's, or with its gatekeeper not running, as it grades an
odoo that does not answer: ``instance_unresponsive``, critical, on the
second probe in a row.
"""
import asyncio

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.instance_health_executor import (
    InstanceHealthExecutor,
)

_ZERO = "0" * 32


def _route6(destination, length, device):
    """One line of ``/proc/net/ipv6_route``.

    :param str destination: 32 hexadecimal digits
    :param str length: the prefix length, in two hexadecimal digits
    :param str device: the interface
    """
    return (
        f"{destination} {length} {_ZERO} 00 {_ZERO} 00000100 00000001 "
        f"00000000 00000001 {device:>8}"
    )


def _routes(v4=(), v6=(), link6=(), rc=0):
    """What the ``gatekeeper_network`` command prints.

    :param v4: interfaces with an IPv4 route in ``/proc/net/route``
    :param v6: interfaces with an IPv6 route that is not link-local
    :param link6: interfaces with IPv6 link-local and multicast routes
        alone, as any interface that is up has in any namespace
    :param int rc: the exit status of the exec that read them; on a
        failure nothing was read
    :return: both files as the kernel writes them, then the ``rc=`` line
    """
    if rc:
        return f"rc={rc}\n"
    lines = [
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask"
        "\t\tMTU\tWindow\tIRTT",
    ]
    lines += [
        f"{name}\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0"
        for name in v4
    ]
    lines += [_route6("fd5b1e6f7a01" + "0" * 20, "40", name) for name in v6]
    for name in link6:
        lines += [
            _route6("fe80" + "0" * 28, "40", name),
            _route6("ff" + "0" * 30, "08", name),
        ]
    lines.append(_route6("0" * 31 + "1", "80", "lo"))
    return "\n".join([*lines, "rc=0", ""])


_JOINED = _routes(v4=["eth0"], link6=["eth0"])
# What the reviewer's gatekeeper left: a bridge of its own, up, which
# keeps its link-local and multicast routes in odoo's old namespace.
_LEFT_BEHIND = _routes(link6=["br9"])
# The kernel's tunnels (tunl0, sit0) in a new namespace: down, no routes.
_NO_ROUTE = _routes()
_UNREADABLE = _routes(rc=1)


class TestGatekeeperNetworkProbe(TransactionCase):

    def setUp(self):
        """A staging with an allowlist, rendered by template v10."""
        super().setUp()
        # The probe writes alerts on a cursor of its own; without test
        # mode those writes cannot see the records created here.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "GkNetProbe"})
        self.host = self.env["cloud.host"].create({
            "name": "gk-net-host",
            "ip_address": "192.0.2.93",
            "user": "ubuntu",
            "wildcard_domain": "gk-net.example.com",
        })
        self.staging = self._instance("gknetstag", "staging", "v10.0.8")
        self.assertIn("gatekeeper", self.staging.expected_services())
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _instance(self, name, environment, template_ref):
        """Create an instance with an allowlist of its own.

        :param str name: the instance's name
        :param str environment: ``staging`` or ``production``
        :param str template_ref: the template its stack was rendered by
        :return: the ``cloud.instance``
        """
        instance = self.env["cloud.instance"].create({
            "name": name,
            "project_id": self.project.id,
            "environment": environment,
            "host_id": self.host.id,
        })
        instance.stack_template_ref = template_ref
        self.env["cloud.instance.whitelist"].create({
            "instance_id": instance.id,
            "hostname": "api.example.com",
            "sequence": 10,
        })
        return instance

    def _executor(self, instance=None):
        """Return a probe of *instance* (the staging by default)."""
        instance = instance or self.staging
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        return executor

    def _probe(self, network, gatekeeper="running", odoo="running", http=0):
        """Run one probe that reads *network* from odoo.

        :param str network: the ``gatekeeper_network`` output
        :param str gatekeeper: the gatekeeper's state in the listing
        :param str odoo: odoo's state in the listing
        :param int http: the exit status of the HTTP check
        :return: the executor, after ``on_success``
        """
        states = {"gatekeeper": gatekeeper, "odoo": odoo}
        listing = "\n".join(
            f"{svc}\t{states.get(svc, 'running')}\tUp 2 hours"
            for svc in self.staging.expected_services()
        )
        executor = self._executor()
        results = {
            "container_state": {"stdout": listing},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": f"exit:{http}"},
            "error_lines": {"stdout": ""},
            "gatekeeper_network": {"stdout": network, "exit_status": 0},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))
        return executor

    def _alert(self):
        """The staging's active ``instance_unresponsive`` alert, if any."""
        return self.env["cloud.alert"].search([
            ("code", "=", "instance_unresponsive"),
            ("instance_id", "=", self.staging.id),
            ("state", "=", "active"),
        ])

    def _log(self, executor):
        """Every line *executor* wrote, joined."""
        return "\n".join(line for line, _source in executor._log_buffer)

    def _assert_unreachable(self, streak, alerted):
        """The staging is unhealthy, with *streak* probes in a row."""
        self.assertEqual(self.staging.http_fail_streak, streak)
        self.assertEqual(self.staging.status, "error")
        self.assertEqual(bool(self._alert()), alerted)

    def test_only_a_stack_with_a_gatekeeper_reads_odoo_s_network(self):
        """Production, and a staging filtered by the sidecars, are probed as before."""
        labels = [step[0] for step in self._executor().get_commands()]
        self.assertIn("gatekeeper_network", labels)
        sidecar = self._instance("gknetstag9", "staging", "v9.7.0")
        self.assertIn("odoo_net_setup", sidecar.expected_services())
        production = self._instance("gknetprod", "production", "v10.0.8")
        for instance in (sidecar, production):
            self.assertNotIn("gatekeeper", instance.expected_services())
            labels = [
                step[0] for step in self._executor(instance).get_commands()
            ]
            self.assertNotIn("gatekeeper_network", labels, instance.name)

    def test_the_command_reads_odoo_s_own_routes(self):
        """Both route files from inside odoo, in 30 seconds, never failing."""
        command = dict(self._executor().get_commands())["gatekeeper_network"]
        self.assertIn("timeout 30 docker compose exec -T odoo sh -c", command)
        self.assertIn("/proc/net/route", command)
        self.assertIn("/proc/net/ipv6_route 2>/dev/null || true", command)
        self.assertIn('echo "rc=$?"', command)

    def test_odoo_with_a_route_is_healthy(self):
        """The normal case: no alert, no failure counted."""
        executor = self._probe(_JOINED)
        self.assertEqual(executor._gatekeeper_network, "joined")
        self.assertFalse(self._alert())
        self.assertEqual(self.staging.http_fail_streak, 0)
        self.assertEqual(self.staging.status, "ok")
        self.assertIn("healthy", executor._verdict)
        self.assertNotIn("not checked", executor._verdict)

    def test_a_route_over_ipv6_alone_is_a_network(self):
        """A network without IPv4 still reaches odoo."""
        executor = self._probe(_routes(v6=["eth0"], link6=["eth0"]))
        self.assertEqual(executor._gatekeeper_network, "joined")
        self.assertEqual(self.staging.http_fail_streak, 0)

    def test_odoo_left_behind_is_unreachable(self):
        """The 6f6 risk: alert, critical, with the cause and the remedy.

        The bridge odoo still sees has no route but its link-local and
        multicast ones, which every interface that is up has anywhere.
        """
        executor = self._probe(_LEFT_BEHIND)
        self.assertEqual(executor._gatekeeper_network, "detached")
        # The first sighting may be a restart caught midway.
        self._assert_unreachable(streak=1, alerted=False)
        self.assertIn("not in the network of the gatekeeper", self._log(executor))
        self._probe(_LEFT_BEHIND)
        self._assert_unreachable(streak=2, alerted=True)
        alert = self._alert()
        self.assertEqual(alert.level, "critical")
        self.assertIn("not in the network of its current gatekeeper", alert.message)
        self.assertIn("Restart the instance", alert.message)

    def test_interfaces_without_a_route_are_not_a_network(self):
        """The kernel's tunnels: present in the namespace, no route."""
        executor = self._probe(_NO_ROUTE)
        self.assertEqual(executor._gatekeeper_network, "detached")
        self._assert_unreachable(streak=1, alerted=False)

    def test_the_alert_closes_once_odoo_is_back_in(self):
        """A restart of the instance puts odoo in the gatekeeper's network again."""
        self._probe(_LEFT_BEHIND)
        self._assert_unreachable(streak=1, alerted=False)
        self._probe(_LEFT_BEHIND)
        self._assert_unreachable(streak=2, alerted=True)
        self._probe(_JOINED)
        self.assertFalse(self._alert())
        self.assertEqual(self.staging.http_fail_streak, 0)
        self.assertEqual(self.staging.status, "ok")

    def test_a_stopped_gatekeeper_leaves_odoo_unreachable(self):
        """Visitors reach odoo through the gatekeeper: the same alert,
        which does not speak of a current gatekeeper there is none of."""
        executor = self._probe(_JOINED, gatekeeper="exited")
        self.assertEqual(executor._gatekeeper_network, "gatekeeper_down")
        self._assert_unreachable(streak=1, alerted=False)
        self.assertIn("The gatekeeper is not running", self._log(executor))
        self._probe(_JOINED, gatekeeper="exited")
        self._assert_unreachable(streak=2, alerted=True)
        alert = self._alert()
        self.assertIn("its gatekeeper is not running", alert.message)
        self.assertNotIn("current gatekeeper", alert.message)
        self.assertIn("Restart the instance", alert.message)

    def test_a_warning_from_docker_is_not_a_route(self):
        """Only lines shaped like the route files' count."""
        warning = (
            'time="2026-10-23T14:53:54Z" level=warning msg="version is '
            'obsolete"\n'
        )
        executor = self._probe(warning + _LEFT_BEHIND)
        self.assertEqual(executor._gatekeeper_network, "detached")

    def test_routes_docker_could_not_read_are_not_a_failure(self):
        """Not checked, said in the verdict, and nothing raised: a failed
        exec, one that timed out, an answer without the route file."""
        for network in (_UNREADABLE, _routes(rc=124), "rc=0\n"):
            executor = self._probe(network)
            self.assertEqual(executor._gatekeeper_network, "unchecked", network)
            self.assertFalse(self._alert())
            self.assertEqual(self.staging.http_fail_streak, 0)
            self.assertEqual(self.staging.status, "ok")
            self.assertIn("gatekeeper network not checked", executor._verdict)
            self.assertIn("Not checked", self._log(executor))

    def test_a_reading_not_made_keeps_the_last_verdict(self):
        """It invents no success: the alert and the streak stand."""
        self._probe(_LEFT_BEHIND)
        self._probe(_LEFT_BEHIND)
        self._assert_unreachable(streak=2, alerted=True)
        executor = self._probe(_UNREADABLE)
        self.assertEqual(executor._gatekeeper_network, "unchecked")
        self._assert_unreachable(streak=2, alerted=True)

    def test_a_reading_not_made_with_http_failing_counts_the_http_failure(self):
        """The HTTP check still says what it says, in its own words."""
        self._probe(_UNREADABLE, http=7)
        self._assert_unreachable(streak=1, alerted=False)
        self._probe(_UNREADABLE, http=7)
        self._assert_unreachable(streak=2, alerted=True)
        self.assertIn("HTTP health check failed", self._alert().message)

    def test_odoo_left_behind_with_http_failing_counts_once_per_probe(self):
        """Two readings of one outage: one failure, named by its cause."""
        executor = self._probe(_LEFT_BEHIND, http=7)
        self._assert_unreachable(streak=1, alerted=False)
        self.assertEqual(self._log(executor).count("✗"), 1)
        self._probe(_LEFT_BEHIND, http=7)
        self._assert_unreachable(streak=2, alerted=True)
        self.assertIn(
            "not in the network of its current gatekeeper", self._alert().message,
        )

    def test_a_stopped_odoo_is_left_to_instance_down(self):
        """Nothing to read in a container that does not run."""
        executor = self._probe(_UNREADABLE, odoo="exited")
        self.assertIsNone(executor._gatekeeper_network)
        self.assertEqual(self.staging.http_fail_streak, 0)
        self.assertFalse(self._alert())

    def test_a_left_behind_odoo_that_answers_is_running(self):
        """``running`` is about the container; reachability is graded apart.

        With the metrics owning the flag and reading False, an odoo whose
        own curl answers is written running even outside its gatekeeper's
        network, and is graded unreachable all the same.
        """
        self.env["cloud.settings"].sudo()._get_system().write({
            "metrics_enabled": True,
        })
        self.staging.write({
            "running": False, "metrics_last_seen": fields.Datetime.now(),
        })
        self.assertTrue(self.staging._liveness_covered_by_metrics())
        executor = self._probe(_LEFT_BEHIND)
        self.assertEqual(executor._gatekeeper_network, "detached")
        self.assertTrue(self.staging.running)
        self._assert_unreachable(streak=1, alerted=False)
