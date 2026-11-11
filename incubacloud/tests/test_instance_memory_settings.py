"""The memory an instance is deployed with follows its containers' limits.

Measured on 5-Nov-2026 on panels with an Odoo of 896 MiB: nothing put a
ceiling on Odoo's workers (its defaults are 2 and 2.5 GiB per worker),
so they grew in steps for days and never recycled; and every database
the panel deploys ran the template's ``work_mem = 512MB``, which lets
one hash take 1 GiB. The deploy now sizes both from the instance's
limits: Odoo's per-process limits in ``odoo.conf`` unless the user set
them, and PostgreSQL's through the ``db`` service's ``CONF_EXTRA``. See
docs/architecture.md, "Memory an instance is deployed with".
"""
import configparser

import yaml

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
    _db_conf_extra,
    _memory_bytes,
    _odoo_memory_limits,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)

_MIB = 1024 * 1024


class TestMemoryFormulas(BaseCase):
    """The formulas, on the sizes a layer may ship and core's defaults."""

    def test_memory_values_read_as_compose_reads_them(self):
        self.assertEqual(_memory_bytes("1g"), 1024 * _MIB)
        self.assertEqual(_memory_bytes("896m"), 896 * _MIB)
        self.assertEqual(_memory_bytes("1.5G"), 1536 * _MIB)
        self.assertEqual(_memory_bytes("262144k"), 256 * _MIB)
        self.assertEqual(_memory_bytes(""), 0)
        self.assertEqual(_memory_bytes(False), 0)
        self.assertEqual(_memory_bytes("a lot"), 0)

    def _pg(self, limit):
        """Return ``{setting: value}`` of the CONF_EXTRA for *limit*."""
        lines = _db_conf_extra(limit).splitlines()
        return dict(
            (part.strip() for part in line.split("=", 1)) for line in lines
        )

    def test_postgres_takes_a_quarter_a_sixteenth_and_a_half(self):
        """Every size a layer may ship, and core's own default of 1g."""
        expected = {
            "192m": ("48MB", "12MB", "96MB"),
            "256m": ("64MB", "16MB", "128MB"),
            "384m": ("96MB", "24MB", "192MB"),
            "512m": ("128MB", "32MB", "256MB"),
            "768m": ("192MB", "48MB", "384MB"),
            "1g": ("256MB", "64MB", "512MB"),
            # work_mem stops at the manager's own 64MB: it is per sort or
            # hash of every connection, the other two once per server.
            "4g": ("1024MB", "64MB", "2048MB"),
        }
        for limit, (shared, work, cache) in expected.items():
            with self.subTest(limit=limit):
                self.assertEqual(self._pg(limit), {
                    "shared_buffers": shared,
                    "work_mem": work,
                    "effective_cache_size": cache,
                })

    def test_a_tiny_database_keeps_its_floors(self):
        """32 MiB would give 8, 2 and 16 MB: below what PostgreSQL works in."""
        self.assertEqual(self._pg("32m"), {
            "shared_buffers": "16MB",
            "work_mem": "4MB",
            "effective_cache_size": "32MB",
        })

    def test_a_database_without_a_limit_keeps_the_template_s(self):
        self.assertEqual(_db_conf_extra(""), "")
        self.assertEqual(_db_conf_extra("unlimited"), "")

    def test_odoo_takes_60_and_150_percent_within_its_bounds(self):
        expected = {
            # The floors: a soft limit under a worn worker's VMS recycles
            # it on every request; a hard one under 1.5 GiB starves
            # wkhtmltopdf, which inherits it.
            "640m": (640, 1536),
            "1024m": (640, 1536),
            "1280m": (768, 1920),
            # Core's default instance.
            "2g": (1228, 2560),
            # Odoo's own defaults are the ceilings: never loosened.
            "4g": (2048, 2560),
            "16g": (2048, 2560),
        }
        for limit, (soft, hard) in expected.items():
            with self.subTest(limit=limit):
                self.assertEqual(_odoo_memory_limits(limit), {
                    "limit_memory_soft": soft * _MIB,
                    "limit_memory_hard": hard * _MIB,
                })

    def test_the_hard_limit_is_always_above_the_soft_one(self):
        for mib in range(128, 8193, 64):
            limits = _odoo_memory_limits(f"{mib}m")
            self.assertGreater(
                limits["limit_memory_hard"], limits["limit_memory_soft"], mib,
            )

    def test_an_odoo_without_a_limit_keeps_odoo_s_defaults(self):
        self.assertEqual(_odoo_memory_limits(""), {})


class TestMemoryRendered(TransactionCase):
    """What the deploy and the rebuild write on the host."""

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Mem Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "memset-host",
            "ip_address": "192.0.2.66",
            "user": "ubuntu",
            "wildcard_domain": "memset.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "memsetinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "odoo_memory_limit": "1024m",
            "db_memory_limit": "256m",
        })

    def _executor(self, executor_cls=DeployInstanceExecutor, code="deploy_instance"):
        """Build *executor_cls* around a fresh job on the instance."""
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        job = self.env["cloud.job"].create({
            "name": f"Mem {code}",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": job_type.id,
        })
        return executor_cls(job, self.host)

    def _db_service(self, executor):
        """Return the ``db`` entry of the override *executor* renders."""
        return yaml.safe_load(executor._resource_override_content())["services"]["db"]

    def _options(self, executor):
        """Return the ``[options]`` of the odoo.conf *executor* renders."""
        cp = configparser.ConfigParser()
        cp.read_string(executor._conf_content())
        return cp["options"]

    def test_the_deploy_sizes_postgres_to_the_database_s_limit(self):
        db = self._db_service(self._executor())
        self.assertEqual(db["mem_limit"], "256m")
        self.assertEqual(
            db["environment"]["CONF_EXTRA"],
            "shared_buffers = 64MB\nwork_mem = 16MB\neffective_cache_size = 128MB\n",
        )

    def test_the_rebuild_sizes_it_too(self):
        """``copier update`` rewrites the compose files on every rebuild."""
        self.instance.db_memory_limit = "1g"
        db = self._db_service(
            self._executor(RebuildInstanceExecutor, "rebuild_instance"),
        )
        self.assertIn("work_mem = 64MB", db["environment"]["CONF_EXTRA"])

    def test_a_database_without_a_limit_gets_no_conf_extra(self):
        self.instance.write({"db_memory_limit": False, "db_cpus": 0.5})
        db = self._db_service(self._executor())
        self.assertNotIn("environment", db)

    def test_odoo_gets_its_per_process_limits(self):
        options = self._options(self._executor())
        self.assertEqual(options["limit_memory_soft"], str(640 * _MIB))
        self.assertEqual(options["limit_memory_hard"], str(1536 * _MIB))
        # What the user's configuration says still stands beside them.
        self.assertEqual(options["workers"], "2")
        self.assertEqual(options["proxy_mode"], "True")

    def test_the_user_s_limits_stand(self):
        self.instance.odoo_conf = (
            "[options]\nworkers = 2\nlimit_memory_soft = 1073741824\n"
            "limit_memory_hard = 3221225472\n"
        )
        options = self._options(self._executor())
        self.assertEqual(options["limit_memory_soft"], "1073741824")
        self.assertEqual(options["limit_memory_hard"], "3221225472")

    def test_one_limit_of_the_user_s_keeps_the_other_odoo_s(self):
        """A hard limit of ours under the user's soft one would fail
        allocations before a worker ever recycled."""
        self.instance.odoo_conf = "[options]\nlimit_memory_soft = 3221225472\n"
        options = self._options(self._executor())
        self.assertEqual(options["limit_memory_soft"], "3221225472")
        self.assertNotIn("limit_memory_hard", options)

    def test_a_threaded_odoo_gets_none(self):
        """Without workers the soft limit is measured on the whole server,
        past 640 MiB from the start, and Odoo would reload in a loop."""
        for conf in (False, "[options]\nworkers = 0\n", "[options]\nworkers = x\n"):
            with self.subTest(conf=conf):
                self.instance.odoo_conf = conf
                options = self._options(self._executor())
                self.assertNotIn("limit_memory_soft", options)
                self.assertNotIn("limit_memory_hard", options)

    def test_a_staging_runs_workers_from_its_command(self):
        """``--workers=3`` on staging's command wins over the configuration."""
        self.instance.write({"environment": "staging", "odoo_conf": False})
        executor = self._executor()
        self.assertIn("--workers=3", executor._odoo_command())
        options = self._options(executor)
        self.assertEqual(options["limit_memory_soft"], str(640 * _MIB))
        self.assertEqual(options["limit_memory_hard"], str(1536 * _MIB))

    def test_an_odoo_without_a_limit_gets_none(self):
        self.instance.odoo_memory_limit = False
        options = self._options(self._executor())
        self.assertNotIn("limit_memory_soft", options)
        self.assertNotIn("limit_memory_hard", options)

    def test_the_saved_configuration_is_not_touched(self):
        """Rendered at deploy time: the user's text is theirs."""
        before = self.instance.odoo_conf
        self._executor()._conf_content()
        self.assertEqual(self.instance.odoo_conf, before)
