"""What a job closed with its step still on the server leaves to its queue.

A job can be closed while a step it ran apart still runs on its server:
its worker died for good, its queue.job was ended by hand, a stop came
back to it. A step past the job's point of no return is not stopped
then: it is left to end (``_close_on_server``). Its queue must not start
the next job beside it, so the checkpoint that step leaves keeps the
queue busy (``holds_queue_since``) until the reconciliation finds the
step ended, or until it expires. A step before the point of no return is
stopped, or left to the reconciliation if the server cannot be reached:
it never keeps the queue, so the rollback of a move whose new server is
gone is not held back.

Whoever closes the job sees how the step ended when the server says so:
its real exit status, not a step that "did not end". A hidden job's step
holds a scope of its own on the server, apart from the visible jobs of
the same instance. And a job whose closing hooks fetch something through
the panel starts them in a fresh attempt when its own has run long.

The steps run for real, on this machine, as in ``test_job_parking``.
"""
import contextlib
import os
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import asyncssh

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.queue_job.exception import JobError

from ..models import abstract_executor
from .test_detached_step import _Apart, _local_transport
from .test_job_parking import _Parking, _ParkingCase, _StopCase


class TestTheScopeOfAStepApart(TransactionCase):
    """A hidden job's step does not hold the scope of the visible ones."""

    def test_a_hidden_job_has_a_scope_of_its_own(self):
        """Per instance or host, and per type for the hidden ones."""
        host = self.env["cloud.host"].create({
            "name": "scope-host",
            "ip_address": "192.0.2.97",
            "user": "odoo",
            "wildcard_domain": "scope.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Scope"})
        inst = self.env["cloud.instance"].create({
            "name": "scope-inst",
            "project_id": project.id,
            "environment": "production",
            "host_id": host.id,
        })

        def scope(code, instance=None):
            """The scope a step apart of a job of type *code* holds."""
            job_type = self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            )
            self.assertTrue(job_type, code)
            job = self.env["cloud.job"].sudo().create({
                "host_id": host.id,
                "instance_id": instance.id if instance else False,
                "job_type_id": job_type.id,
                "name": code,
            })
            return _Apart(job, host)._detached_scope()

        self.assertEqual(scope("rebuild_instance", inst), f"instance:{inst.id}")
        self.assertEqual(
            scope("instance_health", inst), f"instance:{inst.id}:instance_health",
        )
        self.assertEqual(scope("host_probe"), "host")
        self.assertEqual(scope("docker_prune"), "host:docker_prune")
