"""Attributing proxy access-log lines to the instance that served them.

The proxy logs every request for every instance on the host into one
stream, so the whole value of this view depends on the filter being
exactly right. Too loose and one tenant reads another's traffic — IP
addresses of somebody else's end users; too tight and the panel shows an
empty page during the incident it exists for.

The join key is the Traefik router/service name, not the Host header:
the names are derived from the project name the panel itself feeds
copier, whereas a host can serve several domains and redirect between
them.
"""
import json

from odoo.tests.common import TransactionCase


def _line(service, path="/web/login", status=200, ip="203.0.113.9"):
    """Build one proxy access-log line as Traefik emits it."""
    return json.dumps({
        "time": "2026-08-09T00:00:00Z",
        "ClientHost": ip,
        "RequestMethod": "GET",
        "RequestPath": path,
        "RequestHost": "acme.example.com",
        "DownstreamStatus": status,
        "Duration": 1234,
        "RouterName": f"https-{service}-1@docker",
        "ServiceName": f"{service}@docker",
    })


class AccessLogCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HLog", "ip_address": "10.0.0.60", "user": "root",
            "wildcard_domain": "hlog.example.com",
        })
        self.project = self.env["cloud.project"].create({
            "name": "Acme", "remote_folder": "acme",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        self.service = f"{self.instance.doodba_project_name}-19-0-prod-main"


class TestAttribution(AccessLogCase):

    def test_its_own_requests_are_returned(self):
        raw = "\n".join([_line(self.service), _line(self.service, "/web")])
        rows = self.instance._parse_access_log(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ClientHost"], "203.0.113.9")

    def test_another_instance_on_the_same_host_is_excluded(self):
        """The whole point: one proxy log, several instances.

        A leak here is not cosmetic — these lines carry the IP addresses
        of another instance's end users.
        """
        raw = "\n".join([
            _line(self.service),
            _line("other-19-0-prod-main", ip="198.51.100.4"),
        ])
        rows = self.instance._parse_access_log(raw)
        self.assertEqual(len(rows), 1)
        self.assertNotIn(
            "198.51.100.4", [r["ClientHost"] for r in rows],
            "another instance's client IPs leaked into this view",
        )

    def test_a_prefix_collision_does_not_leak(self):
        """``acme-prod`` must not swallow ``acme-prod-two``.

        Prefix matching is the mechanism, so the case where one project
        name starts with another is the one that has to be right.
        """
        sibling = self.env["cloud.instance"].create({
            "name": "prod-two", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        raw = _line(f"{sibling.doodba_project_name}-19-0-prod-main")
        self.assertEqual(self.instance._parse_access_log(raw), [])

    def test_the_proxy_own_log_lines_are_ignored(self):
        """Traefik's operational messages share the stream."""
        raw = "\n".join([
            "time=2026-08-09 level=info msg=Configuration loaded",
            _line(self.service),
            "not json at all",
        ])
        self.assertEqual(len(self.instance._parse_access_log(raw)), 1)

    def test_longpolling_traffic_counts_as_the_same_instance(self):
        """One instance answers under several service names."""
        raw = _line(
            f"{self.instance.doodba_project_name}-19-0-prod-longpolling",
        )
        self.assertEqual(len(self.instance._parse_access_log(raw)), 1)


class TestSummary(AccessLogCase):
    """What the view actually shows when something is going on."""

    def test_it_counts_status_codes_and_ranks_clients(self):
        raw = "\n".join(
            [_line(self.service, status=401, ip="203.0.113.9")] * 5
            + [_line(self.service, status=200, ip="198.51.100.7")]
        )
        rows = self.instance._parse_access_log(raw)
        summary = self.instance._access_log_summary(rows)
        self.assertEqual(summary["total"], 6)
        top_status = summary["by_status"][0]
        self.assertEqual((top_status["key"], top_status["count"]), ("401", 5))
        top_client = summary["top_clients"][0]
        self.assertEqual(top_client["key"], "203.0.113.9")

    def test_an_empty_slice_summarises_without_blowing_up(self):
        summary = self.instance._access_log_summary([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["top_clients"], [])


class TestCommandBounds(AccessLogCase):

    def test_the_tail_is_capped(self):
        """One click must not drag the entire log across the wire."""
        command = self.instance._access_log_command(tail=10 ** 9)
        self.assertIn("--tail 5000", command)

    def test_a_missing_tail_still_produces_a_command(self):
        self.assertIn("docker logs", self.instance._access_log_command())
