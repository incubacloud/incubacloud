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
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

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
        self.assertIn("tail -n 5000 /var/log/traefik/access.log", command)
        self.assertIn("--tail 5000", command)

    def test_a_missing_tail_still_produces_a_command(self):
        self.assertIn("docker logs", self.instance._access_log_command())

    def test_the_file_is_read_and_the_container_only_as_a_fallback(self):
        """1.0.183 moved the log to its own file; a host not set up since
        still logs to the container, and is read there until then."""
        command = self.instance._access_log_command()
        self.assertTrue(command.startswith(
            "if [ -s /var/log/traefik/access.log ]"
            " || ls /var/log/traefik/access.log.* >/dev/null 2>&1; then ",
        ))
        self.assertIn("else docker logs", command)

    def test_an_empty_file_with_nothing_rotated_means_not_set_up_yet(self):
        """The hardening creates the file empty before Full Setup moves
        the proxy's log into it; until then the proxy still logs to the
        container, and an empty page would be a lie."""
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "access.log").write_text("")
            path = os.path.join(directory, "access.log")
            with patch.object(
                type(self.env["cloud.host"]), "PROXY_ACCESS_LOG", path,
            ):
                command = self.instance._access_log_command(tail=5)
            branch = subprocess.run(
                ["bash", "-c", command.replace("docker logs", "echo container")],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("container", branch)

    def test_an_empty_file_right_after_a_rotation_is_still_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "access.log").write_text("")
            Path(directory, "access.log.2026-10-13-1").write_text("y-1\ny-2\n")
            self.assertEqual(
                self._run_access_log_command(directory, tail=5), ["y-1", "y-2"],
            )

    def _run_access_log_command(self, directory, tail):
        """Run the command against a log kept in *directory*; return lines."""
        path = os.path.join(directory, "access.log")
        with patch.object(
            type(self.env["cloud.host"]), "PROXY_ACCESS_LOG", path,
        ):
            command = self.instance._access_log_command(tail=tail)
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True,
            check=True,
        )
        return result.stdout.splitlines()

    def test_right_after_a_rotation_the_previous_file_is_read_too(self):
        """Rotation empties the file every night; the page must not."""
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "access.log").write_text("today-1\n")
            rotated = Path(directory, "access.log.2026-10-13-1")
            rotated.write_text("".join(f"yesterday-{i}\n" for i in range(5)))
            older = Path(directory, "access.log.2026-10-12-1.gz")
            older.write_text("compressed, never read\n")
            os.utime(older, (1, 1))
            os.utime(rotated, (2, 2))
            self.assertEqual(
                self._run_access_log_command(directory, tail=3),
                ["yesterday-3", "yesterday-4", "today-1"],
            )

    def test_before_any_rotation_only_the_file_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "access.log").write_text("a\nb\n")
            self.assertEqual(
                self._run_access_log_command(directory, tail=5), ["a", "b"],
            )
