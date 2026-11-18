"""Odoo's default admin password, replaced in every flow that loads a database.

Four tiers. The program ``admin_password.sh`` runs in the instance's
container, run here against databases shaped as each series stores its
users (8.0 without ``auth_crypt``, 10.0 with it, 11.0 with
``base.user_root`` only, 19.0 and 20.0): only ``admin`` in a column the
series reads is replaced, the change is marked, and afterwards nothing
stored verifies it. The flows: where the step sits in a deploy, the
restores, a start, a restart and a rebuild, which of them stop on its
failure, and what each keeps on the instance and tells. The checks of the
instances that already run: the update and the sweep queue them only for
running instances still to be checked, and they pass a held queue. And a
real Odoo on its own database: an RPC call signed in with admin/admin is
refused once the program ran and Odoo started again.
"""
import asyncio
import calendar
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2
from lxml import etree
from passlib.context import CryptContext

import odoo
from odoo import fields
from odoo.exceptions import AccessDenied
from odoo.modules.registry import Registry
from odoo.service import common as common_service
from odoo.service import model as model_service
from odoo.sql_db import close_db, connection_info_for
from odoo.tests import tagged
from odoo.tests.common import BaseCase, TransactionCase
from odoo.tools import file_open

from ..models._admin_password import (
    LABEL_ADMIN_PASSWORD,
    parse_admin_password_answer,
)
from ..models.abstract_executor import AbstractExecutor
from ..models.admin_password_executor import AdminPasswordCheckExecutor
from ..models.backup_restore_executor import BackupRestoreExecutor
from ..models.cloud_instance_admin_password import ALERT_CODE, CHECK_JOB_TYPE
from ..models.deploy_instance_executor import DeployInstanceExecutor
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from ..models.restart_instance_executor import RestartInstanceExecutor
from ..models.restore_instance_executor import RestoreInstanceExecutor
from ..models.start_instance_executor import StartInstanceExecutor
from ..models.transport import CommandResult, SSHTransport

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "admin_password.sh"
CTX = CryptContext(
    schemes=["pbkdf2_sha512", "md5_crypt", "plaintext"],
    deprecated=["md5_crypt", "plaintext"],
)
#: The system parameter the program marks a change with.
MARK = "incubacloud.admin_password_replaced_at"


def _program():
    """The Python program ``admin_password.sh`` hands the container."""
    text = SCRIPT.read_text()
    return re.search(r"<<'PYEOF'\n(.*?)\nPYEOF\n", text, re.DOTALL).group(1)


class _Steps(AbstractExecutor):
    """A bare executor: the step loop alone. No ``_job_type``."""

    def get_commands(self):
        """Unused: the test hands ``_run_steps`` its own steps."""
        return []


def _labels(cmds):
    """The labels of a list of steps."""
    return [c[0] for c in cmds]


def _since(command):
    """The ``since`` a step's command hands ``admin_password.sh``."""
    match = re.search(r" (stopped|running) (\d+)$", command)
    return int(match.group(2)) if match else None


def _run_program(dbname, info, since=0, schema=None, timeout=120):
    """Run the program as the container runs it; return status and output.

    :param str dbname: the database it opens (``IC_DB``)
    :param dict info: ``connection_info_for``'s, for the PG variables
    :param int since: ``IC_SINCE``
    :param str schema: a ``search_path`` to work in, if not ``public``
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "IC_DB": dbname,
        "IC_SINCE": str(since),
    }
    if schema:
        env["PGOPTIONS"] = f"-c search_path={schema}"
    for key, var in (("host", "PGHOST"), ("port", "PGPORT"),
                     ("user", "PGUSER"), ("password", "PGPASSWORD")):
        if info.get(key):
            env[var] = str(info[key])
    done = subprocess.run(
        [sys.executable, "-"], input=_program(), env=env,
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return done.returncode, done.stdout + done.stderr


# ---------------------------------------------------------------------------
# Tier 1 — the program, against each series' way of storing users
# ---------------------------------------------------------------------------

class TestTheProgramBySeries(TransactionCase):
    """The program, run as the container runs it, on each series' tables.

    Each case is a schema of its own in the test's database, with the
    three tables the program reads, shaped as that series has them; the
    program reaches it through ``PGOPTIONS`` (``search_path``) and the
    database's own credentials, as it reaches ``public`` in an instance.
    """

    def setUp(self):
        super().setUp()
        self.dbname, info = connection_info_for(self.env.cr.dbname)
        self.info = info
        self.schema = f"ic_admin_pw_{uuid.uuid4().hex[:8]}"
        self.conn = psycopg2.connect(**info)
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {self.schema}")
        self.addCleanup(self._drop)

    def _drop(self):
        with self.conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {self.schema} CASCADE")
        self.conn.close()

    def _sql(self, query, params=()):
        """Run *query* in the case's schema; return its rows, if any."""
        with self.conn.cursor() as cur:
            cur.execute(f"SET search_path TO {self.schema}")
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None

    def _database(self, series, crypt_column=False, auth_crypt=False,
                  xmlids=("user_admin",), passwords=None):
        """Create the tables of a database of *series*.

        :param str series: base's version, e.g. ``"10.0.1.3"``
        :param bool crypt_column: ``res_users`` has ``password_crypt``
        :param bool auth_crypt: ``auth_crypt`` is installed
        :param xmlids: which of base.user_admin / base.user_root exist,
            pointing at the user of id 2 and 1 respectively
        :param dict passwords: ``{user id: {column: value}}``
        """
        crypt = ", password_crypt varchar" if crypt_column else ""
        self._sql(
            "CREATE TABLE ir_model_data (module varchar, name varchar,"
            " model varchar, res_id integer)"
        )
        self._sql(
            "CREATE TABLE ir_module_module (name varchar, state varchar,"
            " latest_version varchar)"
        )
        self._sql(f"CREATE TABLE res_users (id integer, login varchar, password varchar{crypt})")
        self._sql(
            "CREATE TABLE ir_config_parameter (id serial, key varchar UNIQUE,"
            " value text)"
        )
        self._sql(
            "INSERT INTO ir_module_module VALUES ('base', 'installed', %s)",
            (series,),
        )
        if auth_crypt:
            self._sql(
                "INSERT INTO ir_module_module VALUES"
                " ('auth_crypt', 'installed', %s)", (series,),
            )
        for name in xmlids:
            res_id = 2 if name == "user_admin" else 1
            self._sql(
                "INSERT INTO ir_model_data VALUES ('base', %s, 'res.users', %s)",
                (name, res_id),
            )
        for uid, values in (passwords or {}).items():
            columns = ["id", "login", *values]
            self._sql(
                f"INSERT INTO res_users ({', '.join(columns)})"
                f" VALUES ({', '.join(['%s'] * len(columns))})",
                (uid, f"user{uid}", *values.values()),
            )

    def _run(self, since=0):
        """Run the program; return its exit status and output.

        :param int since: when the panel last heard an answer (``IC_SINCE``)
        """
        return _run_program(self.dbname, self.info, since, self.schema)

    def _mark(self):
        """The mark's value, or None."""
        rows = self._sql("SELECT value FROM ir_config_parameter WHERE key = %s", (MARK,))
        return rows[0][0] if rows else None

    def _stored(self, uid, columns=("password",)):
        """What *uid* has stored in *columns*."""
        rows = self._sql(
            f"SELECT {', '.join(columns)} FROM res_users WHERE id = %s", (uid,),
        )
        return dict(zip(columns, rows[0]))

    def _assert_changed(self, rc, out):
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD changed")

    def test_19_and_20_hash_in_password(self):
        for series in ("19.0.1.3", "20.0.1.3"):
            with self.subTest(series=series):
                self._sql(
                    "DROP TABLE IF EXISTS ir_model_data, ir_module_module,"
                    " res_users, ir_config_parameter"
                )
                self._database(series, passwords={
                    1: {"password": None}, 2: {"password": CTX.hash("admin")},
                })
                self._assert_changed(*self._run())
                stored = self._stored(2)["password"]
                self.assertEqual(CTX.identify(stored), "pbkdf2_sha512")
                self.assertFalse(CTX.verify("admin", stored))
                # The other user, the inactive __system__, is not touched.
                self.assertIsNone(self._stored(1)["password"])

    def test_a_plain_admin_on_19_is_hashed(self):
        """A row an import left unhashed is still the default."""
        self._database("19.0.1.3", passwords={2: {"password": "admin"}})
        self._assert_changed(*self._run())
        self.assertEqual(CTX.identify(self._stored(2)["password"]), "pbkdf2_sha512")

    def test_10_with_auth_crypt_writes_password_crypt(self):
        self._database(
            "10.0.1.3", crypt_column=True, auth_crypt=True,
            xmlids=("user_root",),
            passwords={1: {"password": "", "password_crypt": CTX.hash("admin")}},
        )
        self._assert_changed(*self._run())
        stored = self._stored(1, ("password", "password_crypt"))
        self.assertEqual(stored["password"], "")
        self.assertEqual(CTX.identify(stored["password_crypt"]), "pbkdf2_sha512")
        self.assertFalse(CTX.verify("admin", stored["password_crypt"]))

    def test_an_md5_crypt_default_is_found(self):
        """Early auth_crypt hashed with md5_crypt."""
        self._database(
            "9.0.1.3", crypt_column=True, auth_crypt=True, xmlids=("user_root",),
            passwords={1: {"password": "", "password_crypt": CTX.hash("admin", scheme="md5_crypt")}},
        )
        self._assert_changed(*self._run())
        stored = self._stored(1, ("password_crypt",))["password_crypt"]
        self.assertEqual(CTX.identify(stored), "pbkdf2_sha512")

    def test_8_without_auth_crypt_stores_it_as_odoo_compares_it(self):
        """In clear, as Odoo up to 11.0 without auth_crypt compares it."""
        self._database("8.0.1.3", xmlids=("user_root",), passwords={
            1: {"password": "admin"},
        })
        self._assert_changed(*self._run())
        stored = self._stored(1)["password"]
        self.assertNotEqual(stored, "admin")
        self.assertGreaterEqual(len(stored), 32)

    def test_from_12_a_stale_password_crypt_is_not_read(self):
        """A database migrated from 11.0 keeps ``password_crypt``, which
        Odoo 12.0 on never reads: its ``admin`` there is no default, and a
        password of its own in ``password`` is left, with no failure."""
        own = CTX.hash("their own one")
        for auth_crypt in (False, True):
            with self.subTest(auth_crypt=auth_crypt):
                self._sql(
                    "DROP TABLE IF EXISTS ir_model_data, ir_module_module,"
                    " res_users, ir_config_parameter"
                )
                self._database(
                    "19.0.1.3", crypt_column=True, auth_crypt=auth_crypt,
                    passwords={2: {"password": own, "password_crypt": CTX.hash("admin")}},
                )
                rc, out = self._run()
                self.assertEqual(rc, 0, out)
                self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD own")
                self.assertEqual(self._stored(2)["password"], own)
                self.assertIsNone(self._mark())

    def test_up_to_11_with_auth_crypt_both_columns_count(self):
        """``password`` is compared first, in clear: ``admin`` there is
        the default even with a hash of another one in ``password_crypt``."""
        self._database(
            "11.0.1.3", crypt_column=True, auth_crypt=True, xmlids=("user_root",),
            passwords={1: {"password": "admin", "password_crypt": CTX.hash("x")}},
        )
        self._assert_changed(*self._run())
        stored = self._stored(1, ("password", "password_crypt"))
        self.assertEqual(stored["password"], "")
        self.assertFalse(CTX.verify("admin", stored["password_crypt"]))

    def test_11_takes_user_root_when_there_is_no_user_admin(self):
        """The user is found by its XML id, never by the series."""
        self._database(
            "11.0.1.3", crypt_column=True, auth_crypt=True,
            xmlids=("user_root",),
            passwords={
                1: {"password": "", "password_crypt": CTX.hash("admin")},
                2: {"password": "", "password_crypt": CTX.hash("admin")},
            },
        )
        self._assert_changed(*self._run())
        self.assertFalse(CTX.verify(
            "admin", self._stored(1, ("password_crypt",))["password_crypt"],
        ))
        self.assertTrue(CTX.verify(
            "admin", self._stored(2, ("password_crypt",))["password_crypt"],
        ))

    def test_a_customer_password_is_left_as_it_is(self):
        own = CTX.hash("their own one")
        self._database("19.0.1.3", passwords={2: {"password": own}})
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD own")
        self.assertEqual(self._stored(2)["password"], own)

    def test_a_second_run_finds_its_own_password(self):
        """Once the panel heard the answer (``since`` at the mark or later)."""
        self._database("19.0.1.3", passwords={2: {"password": CTX.hash("admin")}})
        self._assert_changed(*self._run())
        first = self._stored(2)["password"]
        rc, out = self._run(since=int(self._mark()))
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD own")
        self.assertEqual(self._stored(2)["password"], first)

    def test_the_change_is_marked_in_its_transaction_without_a_secret(self):
        self._database("19.0.1.3", passwords={2: {"password": CTX.hash("admin")}})
        before = int(time.time())
        self._assert_changed(*self._run())
        value = self._mark()
        self.assertRegex(value, r"^\d+$", "a time, nothing else")
        self.assertLessEqual(before, int(value))
        self.assertLessEqual(int(value), int(time.time()))
        self.assertNotIn(value, self._stored(2)["password"])

    def test_an_answer_lost_after_the_commit_is_given_again(self):
        """A run that finds the mark newer than the last answer says
        ``changed``: the change was its own, and its answer never came."""
        self._database("19.0.1.3", passwords={2: {"password": CTX.hash("admin")}})
        self._assert_changed(*self._run())
        stored = self._stored(2)["password"]
        mark = int(self._mark())
        self._assert_changed(*self._run(since=0))
        self._assert_changed(*self._run(since=mark - 1))
        rc, out = self._run(since=mark)
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD own")
        self.assertEqual(self._stored(2)["password"], stored, "changed once")

    def test_a_mark_older_than_a_load_is_not_a_change(self):
        """A database loaded from elsewhere with a mark of its own: the
        loading flow's ``since`` is its own start."""
        self._database("19.0.1.3", passwords={2: {"password": CTX.hash("their own")}})
        self._sql(
            "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)",
            (MARK, str(int(time.time()) - 3600)),
        )
        rc, out = self._run(since=int(time.time()))
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD own")

    def test_no_admin_user_is_absent(self):
        self._database("19.0.1.3", xmlids=(), passwords={2: {"password": "admin"}})
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD absent")
        self.assertEqual(self._stored(2)["password"], "admin")

    def test_nothing_secret_is_printed(self):
        self._database("19.0.1.3", passwords={2: {"password": CTX.hash("admin")}})
        rc, out = self._run()
        stored = self._stored(2)["password"]
        self.assertNotIn(stored, out)
        self.assertNotIn("$pbkdf2", out)
        self.assertEqual(out.strip(), "ADMIN_PASSWORD changed")


class TestTheAnswer(BaseCase):

    def test_the_last_answer_line_is_read(self):
        self.assertEqual(
            parse_admin_password_answer(
                "[incubacloud] something\nADMIN_PASSWORD own\n[incubacloud] x\n"
                "ADMIN_PASSWORD changed\n",
            ),
            "changed",
        )

    def test_no_line_is_no_answer(self):
        self.assertIsNone(parse_admin_password_answer("Error response from daemon"))
        self.assertIsNone(parse_admin_password_answer(""))
        self.assertIsNone(parse_admin_password_answer("ADMIN_PASSWORD maybe"))


# ---------------------------------------------------------------------------
# Tier 2 — the flows
# ---------------------------------------------------------------------------

class _FlowCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.host = self.env["cloud.host"].create({
            "name": "admin-pw-host",
            "ip_address": "192.0.2.150",
            "user": "ubuntu",
            "wildcard_domain": "adminpw.example.com",
            "status": "compatible",
        })
        self.project = self.env["cloud.project"].create({
            "name": "AdminPw", "remote_folder": "adminpw",
        })
        self.inst = self.env["cloud.instance"].create({
            "name": "adminpw",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _deployed(self, inst=None, running=True):
        inst = inst or self.inst
        inst._transition("deploying")
        inst._transition("deployed")
        inst.sudo().write({"running": running})
        return inst

    def _job(self, code, payload=None, inst=None):
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        return self.env["cloud.job"].sudo().create({
            "name": code,
            "host_id": self.host.id,
            "instance_id": (inst or self.inst).id,
            "job_type_id": job_type.id,
            "payload": payload or {},
        })

    def _executor(self, cls, code, payload=None):
        executor = cls(self._job(code, payload), self.host)
        executor._host_cpus = 0
        return executor

    @staticmethod
    def _results(answer=None, exit_status=0, **extra):
        """Step results with the check's answer, and other steps' *extra*."""
        results = {label: {"stdout": "", "exit_status": 0} for label in extra}
        if answer is not None or exit_status:
            results[LABEL_ADMIN_PASSWORD] = {
                "stdout": f"[incubacloud] x\nADMIN_PASSWORD {answer}\n" if answer else "",
                "exit_status": exit_status,
            }
        return results

    def _hook(self, executor, hook, results, *args):
        with patch.object(type(executor), "_sys"):
            asyncio.run(getattr(executor, hook)(results, *args))
        self.inst.invalidate_recordset()

    def _alerts(self):
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", ALERT_CODE), ("instance_id", "=", self.inst.id),
        ])

    def _audits(self):
        return self.env["cloud.audit.log"].sudo().search([
            ("action", "=", "Admin password replaced"),
            ("instance_id", "=", self.inst.id),
        ])


class TestTheDeploy(_FlowCase):

    def test_the_step_is_the_last_write_before_the_stack_starts(self):
        labels = _labels(self._executor(DeployInstanceExecutor, "deploy_instance").get_commands())
        at = labels.index(LABEL_ADMIN_PASSWORD)
        self.assertEqual(labels[at - 1], "Initialize module checksums")
        self.assertEqual(labels[at + 1], "Set system parameters")
        self.assertLess(at, labels.index("Start instance"))

    def test_the_step_stops_the_deploy_and_runs_with_odoo_stopped(self):
        cmds = self._executor(DeployInstanceExecutor, "deploy_instance").get_commands()
        step = next(c for c in cmds if c[0] == LABEL_ADMIN_PASSWORD)
        self.assertEqual(step[2], {"stop_on_failure": True})
        self.assertIn("admin_password.sh", step[1])
        self.assertRegex(step[1], r" prod stopped \d+$")

    def test_only_a_change_of_its_own_counts(self):
        """``since`` is the deploy's own start: no mark it did not write."""
        before = int(time.time())
        cmds = self._executor(DeployInstanceExecutor, "deploy_instance").get_commands()
        step = next(c for c in cmds if c[0] == LABEL_ADMIN_PASSWORD)
        self.assertGreaterEqual(_since(step[1]), before)

    def test_a_failed_step_stops_the_deploy_before_the_start(self):
        """Through the step loop: nothing after it runs, and the run fails."""
        cmds = self._executor(DeployInstanceExecutor, "deploy_instance").get_commands()
        at = _labels(cmds).index(LABEL_ADMIN_PASSWORD)
        steps = [
            (c[0], c[1], {k: v for k, v in c[2].items() if k != "detached"})
            if len(c) == 3 else c
            for c in cmds[at:]
        ]
        failing = steps[0][1]
        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = (
            lambda command, *_h: CommandResult("", 3 if command == failing else 0)
        )
        loop = object.__new__(_Steps)
        loop._log_buffer = []
        results, skipped = asyncio.run(loop._run_steps(transport, steps))
        self.assertEqual(list(results), [LABEL_ADMIN_PASSWORD])
        self.assertIn("Start instance", skipped)
        self.assertIn("Set system parameters", skipped)

    def test_a_moves_deploy_leaves_it_out(self):
        self.inst.sudo().write({"move_origin_host_id": self.host.id})
        labels = _labels(self._executor(DeployInstanceExecutor, "deploy_instance").get_commands())
        self.assertNotIn(LABEL_ADMIN_PASSWORD, labels)

    def test_the_command_carries_no_secret(self):
        cmds = self._executor(DeployInstanceExecutor, "deploy_instance").get_commands()
        step = next(c for c in cmds if c[0] == LABEL_ADMIN_PASSWORD)
        for secret in (self.inst.odoo_admin_user_password, self.inst.postgres_password,
                       self.inst.odoo_admin_password):
            if secret:
                self.assertNotIn(secret, step[1])

    def test_a_fresh_database_is_marked_without_an_alert(self):
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        self.inst._transition("deploying")
        with patch.object(type(self.host), "refresh_observability_labels"):
            self._hook(executor, "on_success", self._results("changed"))
        self.assertEqual(self.inst.admin_password_state, "changed")
        self.assertTrue(self.inst.admin_password_checked_at)
        self.assertFalse(self._alerts())
        self.assertFalse(self._audits())


class TestTheRestores(_FlowCase):

    def test_the_restore_checks_right_before_odoo_starts(self):
        self._deployed()
        cmds = self._executor(RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"}).get_commands()
        labels = _labels(cmds)
        at = labels.index(LABEL_ADMIN_PASSWORD)
        self.assertEqual(labels[at + 1], "Start Odoo service")
        self.assertEqual(labels[at - 1], "Ensure incubacloud_connect")
        self.assertEqual(len(cmds[at]), 2, "not fatal")
        self.assertRegex(cmds[at][1], r" stopped \d+$")

    def test_a_restore_counts_only_its_own_change(self):
        """The loaded database may bring a mark from where it was taken."""
        self._deployed()
        self.inst.sudo().write({
            "admin_password_state": False,
            "admin_password_checked_at": fields.Datetime.now() - timedelta(days=30),
        })
        before = int(time.time())
        cmds = self._executor(RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"}).get_commands()
        step = next(c for c in cmds if c[0] == LABEL_ADMIN_PASSWORD)
        self.assertGreaterEqual(_since(step[1]), before)

    def test_a_failed_check_does_not_fail_the_restore(self):
        self._deployed()
        executor = self._executor(RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"})
        self.assertEqual(executor.parse_results(self._results(exit_status=1)), [])

    def test_a_restored_default_is_changed_told_and_marked(self):
        self._deployed()
        executor = self._executor(RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"})
        self._hook(executor, "on_success", self._results("changed", **{"Restore database": 1}))
        self.assertEqual(self.inst.admin_password_state, "changed")
        alert = self._alerts()
        self.assertEqual(len(alert), 1)
        self.assertIn("Sign in with Connect to set your own", alert.message)
        audit = self._audits()
        self.assertEqual(len(audit), 1)
        self.assertEqual(
            audit.details,
            "The admin user still had Odoo's default password; it now has a "
            "random one. Sign in with Connect to set your own.",
        )
        self.assertEqual(audit.job_id, executor.job)

    def test_a_restored_customer_password_is_left_and_not_told(self):
        self._deployed()
        executor = self._executor(RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"})
        self._hook(executor, "on_success", self._results("own", **{"Restore database": 1}))
        self.assertEqual(self.inst.admin_password_state, "own")
        self.assertFalse(self._alerts())
        self.assertFalse(self._audits())

    def test_a_loaded_database_without_an_answer_is_to_be_checked(self):
        """The state goes; when it last answered stays, the next ``since``."""
        self._deployed()
        stamp = fields.Datetime.now()
        self.inst.sudo().write({
            "admin_password_state": "own",
            "admin_password_checked_at": stamp,
        })
        executor = self._executor(RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"})
        self._hook(executor, "on_failure", self._results(**{"Restore database": 1}), ["x"])
        self.assertFalse(self.inst.admin_password_state)
        self.assertEqual(self.inst.admin_password_checked_at, stamp)

    def test_a_restore_that_loaded_nothing_keeps_the_mark(self):
        self._deployed()
        stamp = fields.Datetime.now()
        self.inst.sudo().write({"admin_password_state": "own", "admin_password_checked_at": stamp})
        executor = self._executor(RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"})
        self._hook(executor, "on_failure", self._results(**{"Verify backup file": 1}), ["x"])
        self.assertEqual(self.inst.admin_password_checked_at, stamp)

    def test_a_moves_restore_of_a_checked_instance_skips_it(self):
        """The same database on the new host, inside the cut."""
        self._deployed()
        self.inst.sudo().write({"move_target_host_id": self.host.id})
        payload = {"mode": "from_job", "as_move": True}
        cmds = self._executor(RestoreInstanceExecutor, "restore_instance", payload).get_commands()
        self.assertIn(LABEL_ADMIN_PASSWORD, _labels(cmds), "still to be checked: checked")
        stamp = fields.Datetime.now()
        self.inst.sudo().write({"admin_password_state": "own", "admin_password_checked_at": stamp})
        executor = self._executor(RestoreInstanceExecutor, "restore_instance", payload)
        self.assertNotIn(LABEL_ADMIN_PASSWORD, _labels(executor.get_commands()))
        self._hook(executor, "on_success", self._results(**{"Restore database": 1}))
        self.assertEqual(self.inst.admin_password_checked_at, stamp)

    def test_the_backup_restore_checks_before_the_start(self):
        self._deployed()
        cmds = self._executor(BackupRestoreExecutor, "backup_restore", {"time": "now"}).get_commands()
        labels = _labels(cmds)
        at = labels.index(LABEL_ADMIN_PASSWORD)
        self.assertEqual(labels[at + 1], "Start instance")
        self.assertEqual(len(cmds[at]), 2)
        executor = self._executor(BackupRestoreExecutor, "backup_restore", {"time": "now"})
        self.assertEqual(executor.parse_results(self._results(exit_status=1)), [])


class TestTheStart(_FlowCase):

    def test_an_instance_still_to_be_checked_is_checked_once_up(self):
        self._deployed(running=False)
        cmds = self._executor(StartInstanceExecutor, "start_instance").get_commands()
        self.assertEqual(_labels(cmds), ["Start containers", LABEL_ADMIN_PASSWORD])
        self.assertRegex(cmds[1][1], r" prod running 0$")

    def test_a_check_counts_the_changes_since_the_last_answer(self):
        """An instance left to be checked again keeps when it answered."""
        self._deployed(running=False)
        stamp = fields.Datetime.now() - timedelta(days=2)
        self.inst.sudo().write({"admin_password_state": False, "admin_password_checked_at": stamp})
        cmds = self._executor(StartInstanceExecutor, "start_instance").get_commands()
        self.assertEqual(_since(cmds[1][1]), calendar.timegm(stamp.timetuple()))

    def test_a_failed_start_runs_no_check(self):
        """«Start containers» stops the run, but on the exits that say the
        containers started; the check never runs on a stack that did not."""
        self._deployed(running=False)
        cmds = self._executor(StartInstanceExecutor, "start_instance").get_commands()
        self.assertTrue(cmds[0][2]["stop_on_failure"])
        transport = MagicMock(spec=SSHTransport)
        loop = object.__new__(_Steps)
        loop._log_buffer = []
        for status, ran in ((1, ["Start containers"]),
                            (24, ["Start containers", LABEL_ADMIN_PASSWORD])):
            with self.subTest(status=status):
                start = cmds[0][1]
                transport.execute.side_effect = (
                    lambda command, *_h, start=start, status=status:
                    CommandResult("", status if command == start else 0)
                )
                results, _skipped = asyncio.run(loop._run_steps(transport, cmds))
                self.assertEqual(list(results), ran)

    def test_a_checked_instance_starts_as_before(self):
        self._deployed(running=False)
        self.inst.sudo().write({
            "admin_password_state": "changed",
            "admin_password_checked_at": fields.Datetime.now(),
        })
        cmds = self._executor(StartInstanceExecutor, "start_instance").get_commands()
        self.assertEqual(_labels(cmds), ["Start containers"])
        self.assertNotIn("stop_on_failure", cmds[0][2])

    def test_a_failed_check_does_not_fail_the_start(self):
        self._deployed(running=False)
        executor = self._executor(StartInstanceExecutor, "start_instance")
        results = self._results(exit_status=1, **{"Start containers": 1})
        self.assertEqual(executor.parse_results(results), [])

    def test_a_change_at_the_start_is_told(self):
        self._deployed(running=False)
        executor = self._executor(StartInstanceExecutor, "start_instance")
        self._hook(executor, "on_success", self._results("changed", **{"Start containers": 1}))
        self.assertEqual(self.inst.admin_password_state, "changed")
        self.assertEqual(len(self._alerts()), 1)
        self.assertEqual(len(self._audits()), 1)


class TestTheRebuild(BaseCase):
    """A rebuild checks an instance still to be checked, once restarted.

    In the running container: the restart does not always recreate it,
    and only the step's own restart of Odoo after a change ends the
    sessions and what Odoo keeps of the RPC calls signed in."""

    def _cmds(self, **extra):
        from .test_executor_features import _make_rebuild_executor

        return _make_rebuild_executor(**extra).get_commands()

    def test_an_instance_still_to_be_checked_is_checked_after_the_restart(self):
        cmds = self._cmds(admin_password_state=False)
        labels = _labels(cmds)
        at = labels.index(LABEL_ADMIN_PASSWORD)
        self.assertLess(labels.index("Restart instance"), at)
        self.assertLess(labels.index("Remove the previous image"), at)
        self.assertEqual(labels[at + 1], "Set system parameters")
        self.assertRegex(cmds[at][1], r" prod running 0$")
        self.assertEqual(len(cmds[at]), 2, "not fatal")

    def test_a_checked_instance_is_rebuilt_as_before(self):
        self.assertNotIn(LABEL_ADMIN_PASSWORD, _labels(self._cmds()))

    def test_a_failed_check_does_not_fail_the_rebuild(self):
        from .test_executor_features import _make_rebuild_executor

        ex = _make_rebuild_executor(admin_password_state=False)
        results = {
            "Restart instance": {"stdout": "", "exit_status": 0},
            LABEL_ADMIN_PASSWORD: {"stdout": "", "exit_status": 1},
        }
        self.assertEqual(ex.parse_results(results), [])


class TestTheRebuildsHooks(_FlowCase):
    """What a rebuild's check found is kept, whether the rebuild ends well."""

    def test_a_failure_after_the_check_keeps_its_answer(self):
        self._deployed()
        executor = self._executor(RebuildInstanceExecutor, "rebuild_instance")
        results = self._results("changed", **{"Restart instance": 1})
        results["Set system parameters"] = {"stdout": "", "exit_status": 1}
        with patch.object(RebuildInstanceExecutor, "_report_cpu_clamps", autospec=True), \
                patch.object(type(self.inst), "_preview_rebuild_failed", autospec=True):
            self._hook(executor, "on_failure", results, ["x"])
        self.assertEqual(self.inst.admin_password_state, "changed")
        self.assertEqual(len(self._alerts()), 1)

    def test_a_failure_before_the_check_leaves_the_state(self):
        self._deployed()
        executor = self._executor(RebuildInstanceExecutor, "rebuild_instance")
        with patch.object(RebuildInstanceExecutor, "_report_cpu_clamps", autospec=True), \
                patch.object(type(self.inst), "_preview_rebuild_failed", autospec=True):
            self._hook(executor, "on_failure", self._results(**{"Restart instance": 1}), ["x"])
        self.assertFalse(self.inst.admin_password_state)
        self.assertFalse(self._alerts())


class TestTheRestart(_FlowCase):
    """A restart may start an instance never checked: its check is queued."""

    def _restarted(self):
        executor = self._executor(RestartInstanceExecutor, "restart_instance")
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "enqueue", autospec=True, return_value=1) as enqueue:
            self._hook(executor, "on_success", {"Restart containers": {"stdout": "", "exit_status": 0}})
        return [c.args[3] for c in enqueue.call_args_list]

    def test_an_instance_still_to_be_checked_gets_its_check(self):
        self._deployed(running=False)
        self.assertEqual(self._restarted(), [CHECK_JOB_TYPE])

    def test_a_checked_instance_gets_none(self):
        self._deployed(running=False)
        self.inst.sudo().write({"admin_password_state": "own"})
        self.assertEqual(self._restarted(), [])


class TestTheCheckJob(_FlowCase):

    def test_it_runs_in_the_running_container(self):
        self._deployed()
        cmds = self._executor(AdminPasswordCheckExecutor, CHECK_JOB_TYPE).get_commands()
        self.assertEqual(_labels(cmds), [LABEL_ADMIN_PASSWORD])
        self.assertRegex(cmds[0][1], r" prod running 0$")

    def _skipped(self):
        """Run a check's pre-run checks, steps and success hook."""
        executor = self._executor(AdminPasswordCheckExecutor, CHECK_JOB_TYPE)
        executor.pre_run_checks()
        self.assertEqual(executor.get_commands(), [])
        logged = []
        with patch.object(
            AdminPasswordCheckExecutor, "_sys", autospec=True,
            side_effect=lambda _self, line: logged.append(line),
        ):
            asyncio.run(executor.on_success({}))
        self.inst.invalidate_recordset()
        return logged

    def test_a_stopped_instance_is_skipped_quietly(self):
        """Done, said in its log: no failure, no alert, the state kept."""
        self._deployed(running=False)
        logged = self._skipped()
        self.assertEqual(len(logged), 1)
        self.assertIn("does not run", logged[0])
        self.assertFalse(self.inst.admin_password_state)
        self.assertFalse(self.env["cloud.alert"].sudo().search([
            ("instance_id", "=", self.inst.id),
        ]))

    def test_an_instance_checked_meanwhile_is_skipped(self):
        self._deployed()
        self.inst.sudo().write({"admin_password_state": "own"})
        self.assertIn("was checked", self._skipped()[0])
        self.assertEqual(self.inst.admin_password_state, "own")

    def test_pip_conflicts_do_not_refuse_it(self):
        """They refuse a deploy, and hold the queue the check passes; the
        check deploys nothing and runs. A restart still holds it."""
        self._deployed()
        self.inst.sudo().pip_dependencies = (
            "<<<<<<< existing\nlibx==1.0\n=======\nlibx==2.0\n>>>>>>> repo-a\n"
        )
        executor = self._executor(AdminPasswordCheckExecutor, CHECK_JOB_TYPE)
        with patch.object(
            AdminPasswordCheckExecutor, "_defer_while_restarting", autospec=True,
        ) as defer:
            executor.pre_run_checks()
        defer.assert_called_once_with(executor)
        self.assertEqual(_labels(executor.get_commands()), [LABEL_ADMIN_PASSWORD])

    def test_a_change_is_told_and_marked(self):
        self._deployed()
        executor = self._executor(AdminPasswordCheckExecutor, CHECK_JOB_TYPE)
        self._hook(executor, "on_success", self._results("changed"))
        self.assertEqual(self.inst.admin_password_state, "changed")
        self.assertEqual(len(self._alerts()), 1)
        self.assertEqual(len(self._audits()), 1)

    def test_a_failure_leaves_it_to_be_checked(self):
        """Among them a restart that failed after the change: the script
        prints no answer then, and the next check answers from the mark."""
        self._deployed()
        executor = self._executor(AdminPasswordCheckExecutor, CHECK_JOB_TYPE)
        self._hook(executor, "on_failure", self._results(exit_status=1), ["x"])
        self.assertFalse(self.inst.admin_password_state)
        self.assertFalse(self._alerts())

    def test_it_is_visible_and_manager_only(self):
        Job = self.env["cloud.job"]
        self.assertNotIn(CHECK_JOB_TYPE, Job._get_hidden_job_types())
        self.assertEqual(Job._job_type_min_group[CHECK_JOB_TYPE], "group_cloud_manager")

    def test_it_passes_a_queue_a_failure_holds(self):
        """A failed deploy that holds the queue for days does not keep the
        default password in service; what waits behind it still waits."""
        self._deployed()
        failed = self._job("deploy_instance")
        failed.sudo().write({"state": "failed"})
        Job = self.env["cloud.job"].sudo()
        self.assertTrue(Job._passes_hold(self._job(CHECK_JOB_TYPE), failed))
        self.assertFalse(Job._passes_hold(self._job("restart_instance"), failed))


# ---------------------------------------------------------------------------
# Tier 3 — the instances that already run: the update and the sweep
# ---------------------------------------------------------------------------

class TestTheSweep(_FlowCase):

    def setUp(self):
        super().setUp()
        self.enqueued = []
        Job = type(self.env["cloud.job"])

        def enqueue(job_self, host_id, instance_id, code, *args, **kwargs):
            self.enqueued.append((instance_id, code))
            return 1

        patcher = patch.object(Job, "enqueue", enqueue)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _other(self, name, running=True, deployed=True):
        inst = self.env["cloud.instance"].create({
            "name": name,
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        if deployed:
            self._deployed(inst, running=running)
        return inst

    def _queued(self):
        self.enqueued.clear()
        self.env["cloud.instance"].cron_check_admin_passwords()
        return {inst for inst, code in self.enqueued if code == CHECK_JOB_TYPE}

    def test_only_running_instances_still_to_be_checked_are_queued(self):
        running = self._deployed()
        stopped = self._other("adminpw-stopped", running=False)
        draft = self._other("adminpw-draft", deployed=False)
        checked = self._other("adminpw-checked")
        checked.sudo().write({
            "admin_password_state": "own",
            "admin_password_checked_at": fields.Datetime.now(),
        })
        queued = self._queued()
        self.assertIn(running.id, queued)
        self.assertNotIn(stopped.id, queued)
        self.assertNotIn(draft.id, queued)
        self.assertNotIn(checked.id, queued)

    def test_the_update_queues_the_same(self):
        running = self._deployed()
        stopped = self._other("adminpw-stopped2", running=False)
        self.enqueued.clear()
        self.env["cloud.instance"]._queue_admin_password_checks(limit=None)
        queued = {inst for inst, code in self.enqueued}
        self.assertEqual(queued & {running.id, stopped.id}, {running.id})

    def test_only_a_check_in_flight_holds_another_back(self):
        """Another job waiting on the instance does not: the check waits
        behind it, or passes it when a failure holds the queue."""
        self._deployed()
        job = self._job("restart_instance")
        job.sudo().write({"state": "queued"})
        self.assertIn(self.inst.id, self._queued())
        check = self._job(CHECK_JOB_TYPE)
        check.sudo().write({"state": "queued"})
        self.assertNotIn(self.inst.id, self._queued())
        check.sudo().write({"state": "done"})
        self.assertIn(self.inst.id, self._queued())

    def test_one_that_cannot_be_queued_costs_the_others_nothing(self):
        """Each enqueue in a savepoint: a failed one, its SQL aborted,
        leaves the next instances and the caller's transaction usable."""
        first = self._deployed()
        second = self._other("adminpw-second")
        failing = {first.id}

        def enqueue(job_self, host_id, instance_id, code, *args, **kwargs):
            if instance_id in failing:
                job_self.env.cr.execute("SELECT 1/0")
            self.enqueued.append((instance_id, code))
            return 1

        with patch.object(type(self.env["cloud.job"]), "enqueue", enqueue):
            self.enqueued.clear()
            self.env["cloud.instance"]._queue_admin_password_checks(limit=None)
        self.assertIn((second.id, CHECK_JOB_TYPE), self.enqueued)
        self.assertNotIn((first.id, CHECK_JOB_TYPE), self.enqueued)
        self.env.cr.execute("SELECT 1")

    def test_a_failed_check_is_queued_again_once_its_pause_ends(self):
        self._deployed()
        failed = self._job(CHECK_JOB_TYPE)
        failed.sudo().write({"state": "failed"})
        self.assertNotIn(self.inst.id, self._queued())
        self.env.cr.execute(
            "UPDATE cloud_job SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(hours=7), failed.id),
        )
        failed.invalidate_recordset()
        self.assertIn(self.inst.id, self._queued())

    def test_an_instance_whose_check_was_undone_is_queued_again(self):
        """A restore that loaded a database it could not check."""
        self._deployed()
        self.inst.sudo().write({
            "admin_password_state": "own",
            "admin_password_checked_at": fields.Datetime.now(),
        })
        self.assertNotIn(self.inst.id, self._queued())
        self.inst.sudo().write({"admin_password_state": False})
        self.assertIn(self.inst.id, self._queued())

    def test_a_moves_rollback_leaves_it_to_be_checked(self):
        """The origin's database may still be on the default one; a
        postponed move never stopped it and keeps the state."""
        target = self.env["cloud.host"].create({
            "name": "admin-pw-target", "ip_address": "192.0.2.151",
            "user": "ubuntu", "wildcard_domain": "adminpw2.example.com",
            "status": "compatible",
        })
        self._deployed()
        for postponed, state in ((True, "changed"), (False, False)):
            with self.subTest(postponed=postponed):
                self.inst.sudo().write({
                    "admin_password_state": "changed",
                    "move_origin_host_id": self.host.id,
                    "move_target_host_id": target.id,
                })
                self.inst._enqueue_rollback_jobs(postponed=postponed)
                self.assertEqual(self.inst.admin_password_state, state)

    def test_the_cron_is_hourly_and_active(self):
        """Hourly, and active as the module's data creates it.

        ``active`` is read from the data file, not the record: a copy of
        a panel's database (QA's, a deploy's clone) pauses its crons
        before the suite runs, and an update never writes the flag back.
        The record is not declared inactive, so it takes ``ir.cron``'s
        default, which must be active.
        """
        cron = self.env.ref("incubacloud.cron_check_admin_passwords")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "hours"))
        with file_open("incubacloud/data/admin_password_cron.xml", "rb") as handle:
            root = etree.parse(handle).getroot()
        (record,) = root.iterfind(".//record[@id='cron_check_admin_passwords']")
        self.assertEqual(record.get("model"), "ir.cron")
        self.assertIsNone(record.find("field[@name='active']"))
        self.assertIs(self.env["ir.cron"].default_get(["active"])["active"], True)


# ---------------------------------------------------------------------------
# Tier 4 — a real Odoo, on a database of its own
# ---------------------------------------------------------------------------

@tagged("post_install", "-at_install")
class TestAnRpcSignInEndsWithTheRestart(BaseCase):
    """An instance still on admin/admin, an RPC client signed in with it.

    The database is a real one, created for the test by Odoo itself
    (``odoo-bin -i base``, in a process of its own) and dropped after it.
    This process serves it as an instance's Odoo does: RPC calls go
    through ``odoo.service``, the code behind ``/xmlrpc/2`` and
    ``/jsonrpc``, on its own registry and cursors, so the uid and password
    an ``execute_kw`` signed in with stay in that registry's
    ``_check_uid_passwd`` cache. The program then replaces the password
    from outside, with SQL, as it does in the container; the same call
    still passes while that Odoo runs, which is why the step restarts it;
    once the registry is loaded again, as a restarted Odoo loads it, the
    same call is refused.
    """

    @classmethod
    def setUpClass(cls):
        """Create the database with base, as the deploy does."""
        super().setUpClass()
        # ``odoo`` is a namespace package from 19.0: no ``__file__``.
        odoo_bin = next((
            bin_ for bin_ in (Path(path).resolve().parent / "odoo-bin"
                              for path in odoo.__path__)
            if bin_.exists()
        ), None)
        if not odoo_bin:
            raise unittest.SkipTest("no odoo-bin next to the odoo package")
        cls.dbname = f"ic_admin_rpc_{uuid.uuid4().hex[:8]}"
        cls.info = connection_info_for(cls.dbname)[1]
        config = odoo.tools.config
        args = [
            sys.executable, str(odoo_bin), "-d", cls.dbname, "-i", "base",
            "--stop-after-init", "--no-http", "--workers=0",
            "--max-cron-threads=0", "--log-level=warn",
        ]
        rcfile = config.get("config")
        if rcfile and Path(rcfile).exists():
            args += ["-c", rcfile]
        addons = config.get("addons_path")
        if addons:
            args += ["--addons-path", addons if isinstance(addons, str) else ",".join(addons)]
        env = os.environ.copy()
        for key, var in (("db_host", "PGHOST"), ("db_port", "PGPORT"),
                         ("db_user", "PGUSER"), ("db_password", "PGPASSWORD")):
            if config.get(key):
                env[var] = str(config[key])
        cls.addClassCleanup(cls._drop)
        done = subprocess.run(
            args, env=env, capture_output=True, text=True, timeout=900,
            check=False,
        )
        if done.returncode:
            raise AssertionError(f"odoo-bin -i base failed: {done.stderr[-2000:]}")

    @classmethod
    def _drop(cls):
        """Forget the registry and drop the database."""
        Registry.delete(cls.dbname)
        close_db(cls.dbname)
        conn = psycopg2.connect(**{**cls.info, "database": "postgres"})
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (cls.dbname,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{cls.dbname}"')
        finally:
            conn.close()

    def setUp(self):
        """Keep the thread's database and user, which ``odoo.service`` sets."""
        super().setUp()
        thread = threading.current_thread()
        for name in ("dbname", "uid"):
            if hasattr(thread, name):
                self.addCleanup(setattr, thread, name, getattr(thread, name))
            else:
                self.addCleanup(lambda n=name: hasattr(thread, n) and delattr(thread, n))

    def _call(self, uid):
        """An RPC ``execute_kw`` signed in with admin/admin."""
        return model_service.dispatch("execute_kw", [
            self.dbname, uid, "admin", "res.users", "search_count", [[]],
        ])

    def test_the_same_call_is_refused_once_odoo_started_again(self):
        uid = common_service.exp_authenticate(self.dbname, "admin", "admin", {})
        self.assertTrue(uid, "a new database's admin signs in with admin/admin")
        self.assertGreater(self._call(uid), 0)

        rc, out = _run_program(self.dbname, self.info)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD changed")

        # The Odoo that served the call still has it in memory.
        self.assertGreater(self._call(uid), 0)
        # Started again: what admin_password.sh's restart does.
        Registry.delete(self.dbname)
        with self.assertRaises(AccessDenied):
            self._call(uid)
        self.assertFalse(
            common_service.exp_authenticate(self.dbname, "admin", "admin", {}),
        )
        # A second run, told the change was answered, leaves it.
        with Registry(self.dbname).cursor() as cr:
            cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", (MARK,))
            (mark,) = cr.fetchone()
        rc, out = _run_program(self.dbname, self.info, since=int(mark))
        self.assertEqual(out.strip().splitlines()[-1], "ADMIN_PASSWORD own", out)
