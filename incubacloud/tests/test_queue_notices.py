"""What the panel is told about a job it just asked for (core 1.0.198).

A button that enqueues says "Started: <type>" or "Queued behind <type>".
The type's name is what people read, so it is translated
(``cloud.job.type.name``) and read in the reader's language; the
endpoints whose answer carried nothing to say it with now carry the job's
id, and the hardening dialog tells a hardening that waits from one that
runs.
"""
from unittest.mock import MagicMock, patch

import polib

from odoo import http
from odoo.tests.common import TransactionCase
from odoo.tools.misc import file_path

from ..controllers._data_load import _routes_crud
from ..controllers._data_load._helpers import _job_response

#: Spanish names written by the tests, so they hold without the shipped
#: catalogue loaded (a fresh test database has English alone).
_SPANISH = {
    "rebuild_instance": "Reconstruir instancia",
    "restart_instance": "Reiniciar instancia",
}


class _NoticeCase(TransactionCase):
    """A host, a project and an instance; Spanish installed and named."""

    def setUp(self):
        """A deployed staging on a host; Spanish active, two types named in it."""
        super().setUp()
        self.registry_enter_test_mode()
        self.env["res.lang"]._activate_lang("es_ES")
        self.host = self.env["cloud.host"].create({
            "name": "notice-host",
            "ip_address": "10.0.0.83",
            "user": "ubuntu",
            "wildcard_domain": "notice.example.com",
            "status": "compatible",
            "traefik_deployed": True,
        })
        project = self.env["cloud.project"].create({"name": "Notice"})
        self.inst = self.env["cloud.instance"].create({
            "name": "notice-inst",
            "project_id": project.id,
            "host_id": self.host.id,
            "environment": "staging",
            "state": "deployed",
        })
        for code, name in _SPANISH.items():
            self._type(code).update_field_translations("name", {"es_ES": name})

    def _type(self, code):
        """The ``cloud.job.type`` *code* of the data files."""
        job_type = self.env["cloud.job.type"].search([("code", "=", code)])
        self.assertTrue(job_type, code)
        return job_type

    def _job(self, code, state, inst=None, host=None):
        """A job of *code* in *state*, on *inst* or, with ``inst=False``, the host."""
        if inst is None:
            inst = self.inst
        return self.env["cloud.job"].create({
            "name": code,
            "host_id": (host or self.host).id,
            "instance_id": inst.id if inst else False,
            "job_type_id": self._type(code).id,
            "state": state,
        })


class TestJobTypeNames(_NoticeCase):
    """What the panel shows of a type is the reader's language's.

    Its name, its description and the confirmation an action asks for;
    code never compares them.
    """

    _SHOWN = ("name", "description", "action_confirm")

    def test_the_shown_fields_are_translated_and_stored_as_jsonb(self):
        """Each shown field is translated jsonb, its English kept as en_US."""
        for field in self._SHOWN:
            self.assertTrue(self.env["cloud.job.type"]._fields[field].translate, field)
            self.env.cr.execute(
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_name = 'cloud_job_type' AND column_name = %s",
                (field,),
            )
            self.assertEqual(self.env.cr.fetchone()[0], "jsonb", field)
        self.env.cr.execute(
            "SELECT name->>'en_US' FROM cloud_job_type WHERE code = %s",
            ("rebuild_instance",),
        )
        self.assertEqual(self.env.cr.fetchone()[0], "Rebuild Instance")

    def test_the_notice_names_the_types_in_spanish_for_a_spanish_reader(self):
        """``_job_response`` names both types in Spanish for a Spanish reader."""
        self._job("rebuild_instance", "started")
        restart = self._job("restart_instance", "queued")
        answer = _job_response(self.env(context={"lang": "es_ES"}), restart.id)
        self.assertEqual(answer["state"], "queued")
        self.assertEqual(answer["job_type"], "Reiniciar instancia")
        self.assertEqual(answer["queued_behind"], "Reconstruir instancia")

    def test_and_in_english_for_an_english_one(self):
        """And in English for an English one."""
        self._job("rebuild_instance", "started")
        restart = self._job("restart_instance", "queued")
        answer = _job_response(self.env(context={"lang": "en_US"}), restart.id)
        self.assertEqual(answer["job_type"], "Restart Instance")
        self.assertEqual(answer["queued_behind"], "Rebuild Instance")

    def test_every_type_of_core_is_shown_in_spanish(self):
        """The shipped catalogues translate what core and PITR declare.

        Each type's name, and its description and confirmation where it
        has them. The SaaS layer's types are its own catalogues' business.
        """
        modules = {"incubacloud", "incubacloud_pitr"}
        ours = self.env["ir.model.data"].search([
            ("model", "=", "cloud.job.type"), ("module", "in", list(modules)),
        ])
        self.assertTrue(ours)
        translated = set()
        for module in set(ours.mapped("module")):
            catalogue = polib.pofile(file_path(f"{module}/i18n/es.po"))
            translated |= {
                occurrence.removeprefix("model:cloud.job.type,")
                for entry in catalogue
                if entry.msgstr and not entry.obsolete
                and "fuzzy" not in entry.flags
                for occurrence, _line in entry.occurrences
                if occurrence.startswith("model:cloud.job.type,")
            }
        expected = set()
        for xmlid in ours:
            job_type = self.env["cloud.job.type"].browse(xmlid.res_id)
            for field in self._SHOWN:
                if job_type.with_context(lang="en_US")[field]:
                    expected.add(f"{field}:{xmlid.module}.{xmlid.name}")
        self.assertEqual(expected - translated, set())


class TestChainSteps(_NoticeCase):
    """The lists know a step of a chain, which takes the rest with it."""

    def test_a_chain_step_says_so_in_the_lists(self):
        """``_format`` marks each step of a waiting chain, not a lone job."""
        self._job("rebuild_instance", "started")
        Job = self.env["cloud.job"]
        steps = Job.browse(Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": code, "payload": {}}
            for code in ("restart_instance", "backup_create")
        ]))
        lone = Job.browse(Job.enqueue(self.host.id, self.inst.id, "stop_instance"))
        self.assertEqual(set((steps | lone).mapped("state")), {"queued"})
        listed = {row["id"]: row["in_chain"] for row in (steps | lone)._format()}
        self.assertEqual(listed, {steps[0].id: True, steps[1].id: True, lone.id: False})


class TestRoutesAnswerTheJob(_NoticeCase):
    """Routes whose answer the panel announces carry what it needs."""

    def _call(self, method, env=None, **kwargs):
        """Drive a CRUD route on the test cursor, as the superuser.

        :param str method: the controller method of the route
        :param env: the request's environment, ``self.env`` by default
            (one with another ``lang`` reads as a user of that language)
        :param kwargs: the route's parameters
        :return: what the route answers
        """
        from odoo.addons.incubacloud.controllers import data_load

        fake_request = MagicMock(spec=http.Request)
        fake_request.env = env or self.env
        ctrl = data_load.CloudDataLoadController()
        with (
            patch.object(http, "request", fake_request),
            patch.object(data_load, "request", fake_request),
            patch.object(_routes_crud, "request", fake_request),
        ):
            return getattr(ctrl, method)(**kwargs)

    def test_resolving_an_addon_conflict_answers_the_job_put_back(self):
        """The addon resolution answers the id of the deploy back in its queue."""
        alert = self.inst._raise_addon_conflict_alert(
            [{"addon": "web_sheet_full_width", "repos": ["odoo-addons", "web"]}],
        )
        deploy = self.env["cloud.job"].browse(self.env["cloud.job"].enqueue(
            self.host.id, self.inst.id, "deploy_instance",
        ))
        self.assertEqual(deploy.blocked_alert_id, alert)
        answer = self._call(
            "cloud_resolve_addon_conflict",
            alert_id=alert.id, resolutions={"web_sheet_full_width": "web"},
        )
        self.assertEqual(answer, {"ok": True, "job_id": deploy.id})
        self.assertEqual(deploy.state, "queued")

    def test_resolving_a_pip_conflict_answers_the_job_put_back(self):
        """The pip resolution answers it too."""
        self.inst.pip_dependencies = (
            "<<<<<<< existing\nlibx==1.0\n=======\nlibx==2.0\n"
            ">>>>>>> acme/repo-a (19.0)\n"
        )
        deploy = self.env["cloud.job"].browse(self.env["cloud.job"].enqueue(
            self.host.id, self.inst.id, "deploy_instance",
        ))
        alert = deploy.blocked_alert_id
        self.assertEqual(alert.code, "pip_conflict")
        answer = self._call(
            "cloud_resolve_pip_conflict",
            alert_id=alert.id, resolutions={"libx": "libx==2.0"},
        )
        self.assertEqual(answer, {"ok": True, "job_id": deploy.id})
        self.assertEqual(deploy.state, "queued")

    def test_the_hardening_dialog_tells_waiting_from_running(self):
        """The preflight says whether the hardening waits in the queue or runs."""
        self._job("full_setup", "started", inst=False)
        self.env["cloud.job"].enqueue(self.host.id, False, "host_hardening")
        waiting = self._call("cloud_preflight_host_hardening", host_id=self.host.id)
        self.assertTrue(waiting["has_active_job"])
        self.assertTrue(waiting["active_job_queued"])
        self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "host_hardening"),
        ]).write({"state": "started"})
        running = self._call("cloud_preflight_host_hardening", host_id=self.host.id)
        self.assertTrue(running["has_active_job"])
        self.assertFalse(running["active_job_queued"])

    def test_the_alert_history_names_the_resolving_job_in_spanish(self):
        """The job that resolved an alert reads in the reader's language."""
        rebuild = self._job("rebuild_instance", "done")
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "pip_conflict", "conflict", instance=self.inst,
        )
        alert.resolved_by_job_id = rebuild
        answer = self._call(
            "cloud_get_alert_history",
            env=self.env(context=self.env.context | {"lang": "es_ES"}),
        )
        row = next(row for row in answer["alerts"] if row["id"] == alert.id)
        self.assertEqual(row["resolved_by_job_name"], "Reconstruir instancia")
