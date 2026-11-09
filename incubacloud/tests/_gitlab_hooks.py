"""Fixtures shared by the GitLab webhook tests."""
import json

from odoo.tests.common import TransactionCase

ZERO = "0" * 40
BEFORE = "1" * 40
AFTER = "a" * 40


def push_body(path="acme/web", branch="main", before=BEFORE, after=AFTER,
              project_id=101, ref=None):
    """Return a GitLab «Push Hook» body.

    :rtype: dict
    """
    return {
        "object_kind": "push",
        "ref": ref or f"refs/heads/{branch}",
        "before": before, "after": after, "checkout_sha": after,
        "user_username": "dev", "user_email": "dev@example.com",
        "user_name": "Dev Eloper",
        "project_id": project_id,
        "project": {"id": project_id, "path_with_namespace": path},
        "commits": [{
            "id": after, "title": "fix: the thing",
            "message": "fix: the thing\n\nbody",
            "author": {"name": "Dev", "email": "dev@example.com"},
        }],
    }


class GitLabHookCase(TransactionCase):
    """A GitLab provider, a webhook of acme/web and a deployed instance on it."""

    PATH = "acme/web"

    @classmethod
    def setUpClass(cls):
        """Create the provider, the webhook, a host and an instance with a GitLab line."""
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.platform_addresses", "192.0.2.1",
        )
        cls.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://panel.example.com",
        )
        cls.provider = cls.env["cloud.git.provider"].create({
            "name": "GitLab.com", "base_url": "https://gitlab.com",
        })
        cls.Hook = cls.env["cloud.gitlab.hook"]
        cls.Event = cls.env["cloud.gitlab.event"]
        cls.hook = cls.Hook.create({
            "provider_id": cls.provider.id, "scope": "project", "path": cls.PATH,
        })
        cls.host = cls.env["cloud.host"].create({
            "name": "gl-host", "ip_address": "10.0.0.93", "user": "ubuntu",
            "wildcard_domain": "gl.example.com",
        })
        cls.project = cls.env["cloud.project"].create({"name": "GL"})
        cls.instance = cls.env["cloud.instance"].create({
            "name": "gl-staging", "project_id": cls.project.id,
            "host_id": cls.host.id, "environment": "staging",
            "auto_rebuild": True, "state": "deployed",
        })
        cls.line = cls.env["cloud.instance.repo"].create({
            "instance_id": cls.instance.id,
            "url": f"https://gitlab.com/{cls.PATH}.git", "branch": "main",
        })

    def _event(self, body, event_type="Push Hook", key="idem-1", hook=None):
        """Record a delivery of *body* as the receiver does.

        :rtype: cloud.gitlab.event
        """
        return self.Event._record_delivery(
            hook or self.hook, event_type, key, body,
        )

    def _rebuilds(self, instance=None):
        """Return the rebuild jobs of the instance.

        :rtype: cloud.job
        """
        return self.env["cloud.job"].search([
            ("instance_id", "=", (instance or self.instance).id),
            ("job_type_id.code", "in", ("rebuild_instance",)),
        ])


def dumps(body):
    """Return *body* as the bytes GitLab sends.

    :rtype: bytes
    """
    return json.dumps(body).encode()
