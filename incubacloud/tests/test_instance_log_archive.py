"""Odoo logs to a file on the host, kept as a dated archive.

Docker's json-file log lives inside the container and dies with it, so
a rebuild — which recreates the container by design — takes the only
copy of yesterday's log with it. Instances therefore write Odoo's log
to ``<instance>/logs/odoo.log``, a bind mount on the host, which
logrotate turns into ``odoo.log.<date>(.gz)``. That is the file an
operator opens three days after the fact.

The flag is passed on the service's ``command`` rather than through
``odoo.conf`` on purpose: the panel runs one-shot containers
(``docker compose run --rm odoo …`` for module installs and
``click-odoo-update`` for the boot test) whose output has to keep
reaching the job log. Those runs replace the command, so they are
untouched by construction — whereas a ``logfile`` in the conf would
have silently swallowed them.
"""
from unittest.mock import MagicMock, patch

import yaml
from werkzeug.exceptions import NotFound

from odoo.exceptions import ValidationError
from odoo.http import Request, Response
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers import _rate_limit, main
from odoo.addons.incubacloud.controllers._data_load import _routes_ops
from odoo.addons.incubacloud.controllers._data_load._helpers import (
    is_safe_log_archive,
    log_archive_download_command,
    log_archive_list_command,
    log_archive_search_command,
    odoo_log_live_command,
    odoo_log_read_command,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    ODOO_LOGFILE,
    ODOO_LOG_DIR,
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.registry import executor_registry


class _LogCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Archive Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "archive-host",
            "ip_address": "192.0.2.64",
            "user": "ubuntu",
            "wildcard_domain": "archive.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "archiveinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.settings = self.env["cloud.settings"].sudo()._get()

    def _job(self, code, inst=None):
        inst = inst or self.instance
        JobType = self.env["cloud.job.type"]
        job_type = JobType.search([("code", "=", code)], limit=1)
        self.assertTrue(job_type, f"job type {code} must exist")
        return self.env["cloud.job"].create({
            "name": f"Archive {code}",
            "host_id": self.host.id,
            "instance_id": inst.id,
            "job_type_id": job_type.id,
        })

    def _override(self, executor_cls, code, inst=None):
        raw = executor_cls(
            self._job(code, inst), self.host,
        )._resource_override_content()
        self.assertTrue(raw, "override must never be None")
        return yaml.safe_load(raw)

    def _staging(self):
        return self.env["cloud.instance"].create({
            "name": "archivestag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })


class TestOdooLogFileOverride(_LogCase):

    def test_production_odoo_logs_to_the_host_file(self):
        data = self._override(DeployInstanceExecutor, "deploy_instance")
        odoo = data["services"]["odoo"]
        self.assertEqual(odoo["command"][-1], f"--logfile={ODOO_LOGFILE}")
        self.assertIn(f"./logs:{ODOO_LOG_DIR}", odoo["volumes"])

    def test_production_command_keeps_the_image_default(self):
        """prod.yaml sets no command, so the image's CMD is what we extend."""
        data = self._override(DeployInstanceExecutor, "deploy_instance")
        self.assertEqual(
            data["services"]["odoo"]["command"][0], "/usr/local/bin/odoo",
        )

    def test_staging_command_keeps_the_template_arguments(self):
        """test.yaml pins ``--workers=3 --max-cron-threads=1``.

        The override replaces the command outright, so dropping them
        here would quietly move every staging instance to threaded mode
        on its next rebuild.
        """
        data = self._override(
            DeployInstanceExecutor, "deploy_instance", inst=self._staging(),
        )
        command = data["services"]["odoo"]["command"]
        self.assertIn("--workers=3", command)
        self.assertIn("--max-cron-threads=1", command)
        self.assertEqual(command[-1], f"--logfile={ODOO_LOGFILE}")

    def test_rebuild_carries_the_same_command_and_mount(self):
        """The retrofit rides on the rebuild, so it has to render it too."""
        data = self._override(RebuildInstanceExecutor, "rebuild_instance")
        odoo = data["services"]["odoo"]
        self.assertEqual(odoo["command"][-1], f"--logfile={ODOO_LOGFILE}")
        self.assertIn(f"./logs:{ODOO_LOG_DIR}", odoo["volumes"])

    def test_only_odoo_gets_the_command_and_mount(self):
        """db/backup/smtp keep their own entrypoints and json-file caps."""
        self.instance.write({"smtp_relay_host": "smtp.example.com"})
        data = self._override(DeployInstanceExecutor, "deploy_instance")
        for svc, entry in data["services"].items():
            if svc == "odoo":
                continue
            with self.subTest(service=svc):
                self.assertNotIn("command", entry)
                self.assertNotIn("volumes", entry)
                self.assertTrue(entry.get("logging"))

    def test_limits_label_and_logging_survive(self):
        self.instance.write({"odoo_memory_limit": "1g", "odoo_cpus": 2.0})
        data = self._override(DeployInstanceExecutor, "deploy_instance")
        odoo = data["services"]["odoo"]
        self.assertEqual(odoo["mem_limit"], "1g")
        self.assertTrue(odoo.get("labels"))
        self.assertEqual(odoo["logging"]["driver"], "json-file")
        self.assertTrue(odoo.get("command"))

    def test_every_registered_deploy_flavour_logs_to_the_file(self):
        """A flavour that stops calling super() fails here, not in prod."""
        flavours = {
            code: cls
            for code, cls in executor_registry.all().items()
            if isinstance(cls, type)
            and issubclass(cls, DeployInstanceExecutor)
        }
        self.assertIn("deploy_instance", flavours)
        self.assertIn("rebuild_instance", flavours)
        for code, cls in flavours.items():
            with self.subTest(job_type=code, executor=cls.__name__):
                data = self._override(cls, code)
                odoo = data["services"]["odoo"]
                self.assertEqual(
                    odoo.get("command", [""])[-1], f"--logfile={ODOO_LOGFILE}",
                    f"{cls.__name__}: odoo lost the logfile flag",
                )
                self.assertIn(f"./logs:{ODOO_LOG_DIR}", odoo.get("volumes", []))


class TestLogDirectoryIsPrepared(_LogCase):
    """Docker creates a missing bind-mount source as root.

    If ``logs/`` is not there — owned by the container's uid — before
    the stack starts, Odoo cannot write to it and falls back to stdout,
    which is exactly the silent failure this feature exists to remove.
    """

    def _steps(self, executor_cls, code):
        """Return the steps as ``(label, command)``.

        A step may carry a third element (``{'stop_on_failure': True}``),
        which is not what these assertions are about.
        """
        executor = executor_cls(self._job(code), self.host)
        return [(item[0], item[1]) for item in executor.get_commands()]

    def _find(self, steps, needle):
        return [(label, cmd) for label, cmd in steps if needle in cmd]

    def test_deploy_prepares_the_directory_before_starting(self):
        steps = self._steps(DeployInstanceExecutor, "deploy_instance")
        labels = [label for label, _cmd in steps]
        prepared = self._find(steps, "instance_logs.sh")
        self.assertTrue(prepared, "deploy must prepare the log directory")
        self.assertIn("install", prepared[0][1])
        start = labels.index("Start instance")
        self.assertLess(
            labels.index(prepared[0][0]), start,
            "the directory must exist before the stack starts, or Docker "
            "creates it as root and Odoo cannot write to it",
        )

    def test_rebuild_prepares_the_directory_before_restarting(self):
        steps = self._steps(RebuildInstanceExecutor, "rebuild_instance")
        labels = [label for label, _cmd in steps]
        prepared = self._find(steps, "instance_logs.sh")
        self.assertTrue(prepared, "rebuild must prepare the log directory")
        self.assertLess(
            labels.index(prepared[0][0]), labels.index("Restart instance"),
        )

    def test_the_retention_setting_reaches_the_script(self):
        self.settings.write({"odoo_log_archive_days": 45})
        steps = self._steps(DeployInstanceExecutor, "deploy_instance")
        prepared = self._find(steps, "instance_logs.sh")
        self.assertIn("45", prepared[0][1])


class TestLogArchiveSettings(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get()

    def test_default_matches_the_reference_platform(self):
        self.assertEqual(self.settings.odoo_log_archive_days, 60)

    def test_rejects_zero_days(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"odoo_log_archive_days": 0})

    def test_rejects_negative_days(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"odoo_log_archive_days": -1})


class TestArchiveNamesAreConstrained(TransactionCase):
    """The archive name reaches a shell; only the shapes logrotate makes."""

    def test_accepts_the_live_file(self):
        self.assertTrue(is_safe_log_archive("odoo.log"))

    def test_accepts_a_dated_archive(self):
        self.assertTrue(is_safe_log_archive("odoo.log.2026-08-17"))
        self.assertTrue(is_safe_log_archive("odoo.log.2026-08-17.gz"))

    def test_rejects_traversal_and_anything_else(self):
        for name in (
            "../../etc/passwd",
            "odoo.log.2026-08-17.gz; rm -rf /",
            "odoo.log.$(whoami)",
            "/etc/shadow",
            "odoo.log.2026-08-17.zip",
            "backup.log",
            "",
            None,
        ):
            with self.subTest(name=name):
                self.assertFalse(is_safe_log_archive(name))


class TestLogReadCommands(TransactionCase):

    def test_live_read_prefers_the_file_and_falls_back(self):
        """An instance not rebuilt yet has no file; it must still show logs."""
        cmd = odoo_log_live_command("~/proj/inst", "prod.yaml", 200)
        self.assertIn("logs/odoo.log", cmd)
        self.assertIn("tail -n 200", cmd)
        self.assertIn("docker compose", cmd)
        self.assertIn("--tail=200", cmd)

    def test_archive_read_handles_plain_and_compressed(self):
        cmd = odoo_log_read_command("~/proj/inst", "odoo.log.2026-08-17.gz", 500)
        self.assertIn("zcat -f", cmd)
        self.assertIn("odoo.log.2026-08-17.gz", cmd)
        self.assertIn("tail -n 500", cmd)

    def test_archive_read_quotes_the_search_term(self):
        cmd = odoo_log_read_command(
            "~/proj/inst", "odoo.log", 100, search="boom; rm -rf /",
        )
        self.assertIn("grep", cmd)
        self.assertNotIn("; rm -rf /\n", cmd)
        self.assertIn("'boom; rm -rf /'", cmd)

    def test_listing_survives_an_instance_without_logs(self):
        cmd = log_archive_list_command("~/proj/inst")
        self.assertIn("odoo\\.log", cmd)
        self.assertIn("|| true", cmd)


class TestRotationSettingsStayOutOfDrift(_LogCase):
    """Global knobs must not light the fleet's "Changes not deployed" pill."""

    def test_archive_days_do_not_move_the_config_snapshot(self):
        before = self.instance._config_snapshot_hash()
        self.settings.write({"odoo_log_archive_days": 30})
        self.assertEqual(self.instance._config_snapshot_hash(), before)


class TestLogArchiveRoutes(TransactionCase):
    """The listing endpoint, without an SSH connection.

    The parsing is where this can go wrong quietly: a name the host
    reports and the panel does not recognise must be dropped rather
    than handed to a shell, and the order is what the picker shows.
    """

    def setUp(self):
        super().setUp()
        self.controller = _routes_ops.OpsMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.project = self.env["cloud.project"].create({"name": "Routes Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "routes-host",
            "ip_address": "192.0.2.67",
            "user": "ubuntu",
            "wildcard_domain": "routes.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "routesinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _request(self):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        return fake_req

    def _list(self, stdout):
        """Call the route with both ``request`` handles patched.

        The rate-limit gate reads ``request`` from its own module, so
        patching the route's alone leaves it reaching for a real HTTP
        request that a test does not have.
        """
        self.controller._ssh_run = lambda host, command: (stdout, "")
        fake_req = self._request()
        with patch.object(_routes_ops, "request", fake_req), \
                patch.object(_rate_limit, "request", fake_req):
            return self.controller.cloud_instance_log_archives(self.instance.id)

    def test_the_live_file_comes_first_then_the_days_newest_down(self):
        result = self._list(
            "odoo.log.2026-08-15.gz|4096|1755000000\n"
            "odoo.log|1024|1755300000\n"
            "odoo.log.2026-08-17|8192|1755200000\n"
            "odoo.log.2026-08-16.gz|2048|1755100000\n"
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["archived"])
        self.assertEqual(
            [a["name"] for a in result["archives"]],
            [
                "odoo.log",
                "odoo.log.2026-08-17",
                "odoo.log.2026-08-16.gz",
                "odoo.log.2026-08-15.gz",
            ],
        )

    def test_compression_is_reported_so_the_viewer_can_label_it(self):
        result = self._list("odoo.log.2026-08-16.gz|2048|1755100000\n")
        self.assertTrue(result["archives"][0]["compressed"])

    def test_anything_that_is_not_an_odoo_log_is_dropped(self):
        result = self._list(
            "odoo.log|1024|1755300000\n"
            "backup.log|10|1\n"
            "../../etc/passwd|10|1\n"
            "odoo.log.2026-08-17.zip|10|1\n"
        )
        self.assertEqual(
            [a["name"] for a in result["archives"]], ["odoo.log"],
        )

    def test_an_instance_without_the_archive_says_so(self):
        """Not rebuilt yet: an empty list, not an error."""
        result = self._list("")
        self.assertTrue(result["ok"])
        self.assertFalse(result["archived"])
        self.assertEqual(result["archives"], [])

    def test_reading_an_unknown_file_is_refused_before_any_ssh(self):
        def _boom(host, command):
            raise AssertionError("must not reach the host")

        self.controller._ssh_run = _boom
        fake_req = self._request()
        with patch.object(_routes_ops, "request", fake_req), \
                patch.object(_rate_limit, "request", fake_req):
            result = self.controller.cloud_fetch_log_archive(
                self.instance.id, "../../etc/passwd",
            )
        self.assertFalse(result["ok"])


class TestArchiveSearchCommand(TransactionCase):
    """Finding the day is the hard part once the archive is 60 files.

    Scrolling a dropdown only works if you already know the date; the
    honest question is "which day mentions this error", and that is a
    grep across the archive — run on the host, where the files are.
    """

    def test_the_search_reads_every_shape_and_counts_matches(self):
        cmd = log_archive_search_command("~/proj/inst", "boom")
        self.assertIn("zcat -f", cmd)
        self.assertIn("grep -acF", cmd)

    def test_the_term_is_quoted(self):
        cmd = log_archive_search_command("~/proj/inst", "a'; rm -rf /")
        self.assertNotIn("; rm -rf /;", cmd)
        self.assertIn("rm -rf", cmd)

    def test_the_search_is_bounded_in_time_and_files(self):
        """A 60-day sweep must not hold an SSH channel open forever."""
        cmd = log_archive_search_command("~/proj/inst", "boom")
        self.assertIn("timeout ", cmd)
        self.assertIn("head -n ", cmd)

    def test_the_search_marks_its_own_completion(self):
        """Without a marker, a timed-out sweep reads as 'no matches'."""
        self.assertIn("IC_DONE", log_archive_search_command("~/p/i", "boom"))

    def test_newest_days_are_searched_first(self):
        """Ordered by mtime, from ``find`` — regular files only, so a
        planted link never takes one of the bounded sweep's slots."""
        cmd = log_archive_search_command("~/p/i", "boom")
        self.assertIn("-type f", cmd)
        self.assertIn("%T@", cmd)
        self.assertIn("sort -rn", cmd)


class TestArchiveSearchRoute(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = _routes_ops.OpsMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.project = self.env["cloud.project"].create({"name": "Search Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "search-host",
            "ip_address": "192.0.2.68",
            "user": "ubuntu",
            "wildcard_domain": "search.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "searchinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _request(self):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        return fake_req

    def _search(self, stdout, term="boom"):
        self.controller._ssh_run = lambda host, command: (stdout, "")
        fake_req = self._request()
        with patch.object(_routes_ops, "request", fake_req), \
                patch.object(_rate_limit, "request", fake_req):
            return self.controller.cloud_search_log_archives(
                self.instance.id, term,
            )

    def test_matching_days_come_back_with_their_counts(self):
        result = self._search(
            "odoo.log|3\nodoo.log.2026-08-17|47\nIC_DONE\n",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["complete"])
        self.assertEqual(
            result["matches"],
            [
                {"name": "odoo.log", "count": 3},
                {"name": "odoo.log.2026-08-17", "count": 47},
            ],
        )

    def test_a_sweep_cut_short_says_so(self):
        """The timeout fired: 'nothing else matched' would be a lie."""
        result = self._search("odoo.log.2026-08-17|47\n")
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])

    def test_lines_that_are_not_log_files_are_dropped(self):
        result = self._search("passwd|3\nodoo.log|1\nIC_DONE\n")
        self.assertEqual([m["name"] for m in result["matches"]], ["odoo.log"])

    def test_a_term_too_short_never_reaches_the_host(self):
        def _boom(host, command):
            raise AssertionError("must not reach the host")

        self.controller._ssh_run = _boom
        fake_req = self._request()
        with patch.object(_routes_ops, "request", fake_req), \
                patch.object(_rate_limit, "request", fake_req):
            result = self.controller.cloud_search_log_archives(
                self.instance.id, "a",
            )
        self.assertFalse(result["ok"])


class TestArchiveReadsRefuseSymlinks(TransactionCase):
    """The reader runs on the host, as the host's SSH user.

    ``logs/`` belongs to the container's uid, so anyone with a shell in
    the instance's Odoo container — which the panel hands to a
    Developer, deliberately confined to the container — can create
    ``logs/odoo.log.2026-08-17`` pointing at ``/etc/shadow``. Following
    it would turn the log viewer into a host file reader with the SSH
    user's privileges: a container-to-host escalation the terminal
    itself refuses to give. Every command that touches the archive
    therefore reads regular files only, and the listing never offers
    anything else.
    """

    def test_the_listing_only_reports_regular_files(self):
        cmd = log_archive_list_command("~/proj/inst")
        self.assertIn("-type f", cmd)

    def test_reading_a_day_refuses_a_symlink(self):
        cmd = odoo_log_read_command("~/proj/inst", "odoo.log.2026-08-17", 100)
        self.assertIn("! -L", cmd)

    def test_the_live_tail_refuses_a_symlink(self):
        cmd = odoo_log_live_command("~/proj/inst", "prod.yaml", 100)
        self.assertIn("! -L", cmd)

    def test_downloading_refuses_a_symlink(self):
        cmd = log_archive_download_command("~/proj/inst", "odoo.log.2026-08-17")
        self.assertIn("! -L", cmd)

    def test_the_cross_day_sweep_skips_symlinks(self):
        cmd = log_archive_search_command("~/proj/inst", "boom")
        self.assertIn("-L", cmd)


class TestLogAccessBounds(TransactionCase):
    """The three costs of reading logs are configurable, not baked in.

    A sweep decompresses days of logs on the customer's host and a
    download pulls a whole day through the panel: what is safe depends
    on the host, so the operator owns the numbers.
    """

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get()

    def test_defaults_are_the_documented_ones(self):
        self.assertEqual(self.settings.log_download_max_mb, 64)
        self.assertEqual(self.settings.log_search_max_files, 60)
        self.assertEqual(self.settings.log_search_timeout_s, 30)

    def test_a_download_cap_below_one_megabyte_is_refused(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"log_download_max_mb": 0})

    def test_sweeping_zero_files_is_refused(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"log_search_max_files": 0})

    def test_a_timeout_too_short_to_finish_anything_is_refused(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"log_search_timeout_s": 1})

    def test_the_download_command_honours_the_configured_cap(self):
        cmd = log_archive_download_command(
            "~/p/i", "odoo.log", max_bytes=8 * 1024 * 1024,
        )
        self.assertIn(str(8 * 1024 * 1024), cmd)

    def test_the_sweep_honours_the_configured_bounds(self):
        cmd = log_archive_search_command(
            "~/p/i", "boom", max_files=7, timeout_s=11,
        )
        self.assertIn("head -n 7", cmd)
        self.assertIn("timeout 11", cmd)


class TestLogAccessIsRateLimitedAndAudited(TransactionCase):
    """Reading logs is privileged, repeatable and expensive on the host.

    Rate limits keep one operator (or a runaway viewer) from pinning a
    customer's host, and the audit rows answer "who read whose logs"
    — the two things the log viewer was missing when it only ever ran
    ``docker compose logs``.
    """

    def setUp(self):
        super().setUp()
        self.controller = _routes_ops.OpsMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.controller._ssh_run = lambda host, command: ("", "")
        self.project = self.env["cloud.project"].create({"name": "Gate Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "gate-host",
            "ip_address": "192.0.2.69",
            "user": "ubuntu",
            "wildcard_domain": "gate.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "gateinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _bound(self, **ctx):
        """Patch both modules' ``request`` with one env.

        The gate reads ``request`` from its own module, so patching the
        route's is not enough; ``force_rate_limit`` opts the counter
        back in under ``test_enable``.
        """
        env = self.env(context=self.env.context | ctx)
        fake_req = MagicMock(spec=Request)
        fake_req.env = env
        return (
            patch.object(_routes_ops, "request", fake_req),
            patch.object(_rate_limit, "request", fake_req),
        )

    def _call(self, fn, *args, **ctx):
        route_patch, gate_patch = self._bound(**ctx)
        with route_patch, gate_patch:
            return fn(*args)

    def _audit(self, action):
        return self.env["cloud.audit.log"].search([
            ("instance_id", "=", self.instance.id),
            ("action", "=", action),
        ])

    def test_the_sweep_is_capped_per_user(self):
        cap = self.env["cloud.settings"].sudo()._get()
        cap.write({"rate_limit_log_search_per_min": 2})
        results = [
            self._call(
                self.controller.cloud_search_log_archives,
                self.instance.id, "boom",
                force_rate_limit=True,
            )
            for _i in range(4)
        ]
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[-1]["ok"], "the cap must eventually deny")

    def test_reading_a_day_is_capped_per_user(self):
        cap = self.env["cloud.settings"].sudo()._get()
        cap.write({"rate_limit_logs_per_min": 2})
        results = [
            self._call(
                self.controller.cloud_fetch_log_archive,
                self.instance.id, "odoo.log",
                force_rate_limit=True,
            )
            for _i in range(4)
        ]
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[-1]["ok"])

    def test_the_live_tail_stays_usable_at_its_default_cap(self):
        """The viewer polls every 4 s; a cap that breaks it is a bug."""
        default = self.env["cloud.settings"].sudo()._get().rate_limit_logs_per_min
        self.assertGreaterEqual(default, 15 * 2)

    def test_each_read_endpoint_counts_in_its_own_bucket(self):
        """The viewer opens by calling two of them at once.

        Sharing one bucket makes those two upserts contend for the
        same row on every open: Postgres raises a serialization
        failure, Odoo retries the request, and the only visible trace
        is an error in the server log on every single page open.
        """
        route_patch, gate_patch = self._bound()
        with route_patch, gate_patch:
            buckets = {
                self.controller._log_read_rule(kind).bucket
                for kind in ("tail", "list", "day")
            }
        self.assertEqual(len(buckets), 3)

    def test_a_sweep_is_audited_with_its_term(self):
        self._call(
            self.controller.cloud_search_log_archives,
            self.instance.id, "boom",
        )
        entry = self._audit("Searched instance logs")
        self.assertTrue(entry)
        self.assertIn("boom", entry[0].details or "")

    def test_polling_the_live_tail_is_not_audited(self):
        """One row per 4-second poll would bury the rows that matter."""
        self._call(
            self.controller.cloud_fetch_container_logs,
            self.instance.id, "odoo",
        )
        self.assertFalse(self._audit("Viewed instance logs"))


class TestViewerAndDownloadAreAudited(TransactionCase):
    """The two HTTP routes of the viewer leave their own audit rows.

    Opening the viewer is audited once per open (the polling it starts
    is not), and a download is audited *after* the file arrived:
    a failed attempt ends in ``not_found``, which rolls the request
    back — a row written before the fetch would vanish with it, and
    the one download worth accounting for is the one that succeeded.
    """

    def setUp(self):
        super().setUp()
        self.controller = main.CloudController()
        self.project = self.env["cloud.project"].create({"name": "Audit Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "audit-host",
            "ip_address": "192.0.2.70",
            "user": "ubuntu",
            "wildcard_domain": "audit.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "auditinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _patches(self, fetch):
        """Bind both modules' ``request`` to the test env; stub the SSH fetch.

        ``fetch`` is what ``run_async`` returns — or raises — in place
        of the SSH round trip; the coroutine the route built is closed
        so it does not warn about never being awaited. The request's
        response makers return real responses: the ``@http.route``
        wrapper passes whatever the route returns through
        ``Response.load``, which rejects anything else — and *raises*
        a returned ``NotFound``, which is what rolls a failed request
        back and why the audit row has to wait for the fetch.
        """
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        fake_req.not_found.return_value = NotFound()
        fake_req.make_response.return_value = Response(b"")
        fake_req.render.return_value = Response("<html/>")

        def _run_async(coro):
            coro.close()
            if isinstance(fetch, Exception):
                raise fetch
            return fetch

        return (
            patch.object(main, "request", fake_req),
            patch.object(_rate_limit, "request", fake_req),
            patch.object(main, "run_async", side_effect=_run_async),
            fake_req,
        )

    def _audit(self, action):
        return self.env["cloud.audit.log"].search([
            ("instance_id", "=", self.instance.id),
            ("action", "=", action),
        ])

    def test_opening_the_viewer_is_audited(self):
        req_patch, gate_patch, run_patch, _req = self._patches(b"")
        with req_patch, gate_patch, run_patch, patch.object(
            self.env.registry["ir.http"], "session_info", return_value={},
        ):
            self.controller.cloud_instance_logs(self.instance.id)
        self.assertEqual(len(self._audit("Viewed instance logs")), 1)

    def test_a_download_is_audited_once_the_file_arrived(self):
        req_patch, gate_patch, run_patch, req = self._patches(b"\x1f\x8b-bytes")
        with req_patch, gate_patch, run_patch:
            self.controller.cloud_instance_log_archive_download(
                self.instance.id, "odoo.log.2026-08-17.gz",
            )
        entry = self._audit("Downloaded instance log")
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry.details, "odoo.log.2026-08-17.gz")
        req.make_response.assert_called_once()

    def test_a_download_that_fails_on_the_host_leaves_no_row(self):
        req_patch, gate_patch, run_patch, req = self._patches(
            OSError("ssh down"),
        )
        with req_patch, gate_patch, run_patch, self.assertRaises(NotFound):
            self.controller.cloud_instance_log_archive_download(
                self.instance.id, "odoo.log.2026-08-17.gz",
            )
        self.assertFalse(self._audit("Downloaded instance log"))
        req.make_response.assert_not_called()

    def test_an_unknown_file_name_is_refused_before_any_ssh(self):
        req_patch, gate_patch, run_patch, _req = self._patches(b"")
        with req_patch, gate_patch, run_patch as run_async, \
                self.assertRaises(NotFound):
            self.controller.cloud_instance_log_archive_download(
                self.instance.id, "../../etc/shadow",
            )
        run_async.assert_not_called()
        self.assertFalse(self._audit("Downloaded instance log"))
