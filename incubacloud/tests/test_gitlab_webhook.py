"""The GitLab webhook receiver (plan-gitlab.md §4.7.1, Fable's review of G3).

The endpoint is called with a stubbed request, as GitHub's receiver is
in ``test_github_webhook_cost``: the checks before the body must refuse
without reading it, and nothing after a valid token answers anything
but 200.
"""
from unittest.mock import MagicMock, patch

from odoo.addons.incubacloud.controllers import gitlab_webhook as gw
from odoo.addons.incubacloud.models.res_users_ext import as_platform
from odoo.http import HTTPRequest, Request, Response
from odoo.tools import mute_logger

from ._gitlab_hooks import GitLabHookCase, dumps, push_body


class TestGitLabWebhookReceiver(GitLabHookCase):

    def _call(self, headers, body=b"{}", key=None):
        """Invoke the receiver with a stubbed request.

        :return: ``(response, get_data mock)``
        :rtype: tuple
        """
        fake = MagicMock(spec=Request)
        fake.env = self.env
        fake.httprequest = MagicMock(spec=HTTPRequest)
        fake.httprequest.remote_addr = "198.51.100.8"
        fake.httprequest.headers = headers
        fake.httprequest.get_data.return_value = body
        fake.make_response.side_effect = (
            lambda body_, status=200, headers=None: Response(
                body_, status=status, headers=headers,
            )
        )
        with patch.object(gw, "request", fake), \
                patch.object(gw, "client_ip", return_value="198.51.100.8"):
            resp = gw.GitLabWebhookController().gitlab_webhook(
                key if key is not None else self.hook.hook_key,
            )
        return resp, fake.httprequest.get_data

    def _headers(self, event="Push Hook", token=None, key="idem-1"):
        """Return the headers of a delivery to the test webhook.

        :rtype: dict
        """
        headers = {
            "X-Gitlab-Event": event,
            "X-Gitlab-Token": self.hook.sudo().secret if token is None else token,
        }
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def test_the_route_caps_the_body_at_8_mb(self):
        routing = gw.GitLabWebhookController.gitlab_webhook.original_routing
        self.assertEqual(routing.get("max_content_length"), 8 * 1024 * 1024)

    def test_the_ip_bucket_is_counted_at_read_committed(self):
        """Over the cap: 429 without reading, counted by consume_quota."""
        with patch.object(gw, "consume_quota", return_value=False) as quota:
            resp, get_data = self._call(self._headers())
        self.assertEqual(resp.status_code, 429)
        get_data.assert_not_called()
        self.assertEqual(quota.call_args.args[1], "webhook_ip:198.51.100.8")
        self.assertEqual(quota.call_args.kwargs["cap_key"], "rate_limit_webhook_per_min")

    def test_an_unknown_key_or_a_bad_token_is_401_without_reading(self):
        for key, token in (("0" * 32, None), ("not-hex", None), (None, "wrong")):
            with self.subTest(key=key, token=token):
                resp, get_data = self._call(self._headers(token=token), key=key)
                self.assertEqual(resp.status_code, 401)
                get_data.assert_not_called()

    def test_a_valid_push_is_recorded_and_rebuilds(self):
        resp, _g = self._call(self._headers(), dumps(push_body()))
        self.assertEqual(resp.status_code, 200)
        event = self.Event.search([("hook_id", "=", self.hook.id)])
        self.assertTrue(event.processed)
        self.assertEqual(len(self._rebuilds()), 1)
        platform = as_platform(self.env["cloud.gitlab.event"]).env.uid
        self.assertEqual(event.create_uid.id, platform, "authored by the platform")

    def test_ignored_types_and_unsafe_branches_answer_200(self):
        resp, _g = self._call(self._headers("System Hook"), dumps({}))
        self.assertEqual(resp.status_code, 200)
        resp, _g = self._call(self._headers(key="k2"), dumps(push_body(branch="a..b")))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._rebuilds())

    def test_a_resend_and_the_same_push_are_duplicates(self):
        """A «Resend» keeps its Idempotency-Key; the same before/after is the same push."""
        self._call(self._headers(), dumps(push_body()))
        with mute_logger("odoo.sql_db"):
            resp, _g = self._call(self._headers(), dumps(push_body()))
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"Duplicate", resp.get_data())
            resp, _g = self._call(self._headers(key="other"), dumps(push_body()))
            self.assertIn(b"Duplicate", resp.get_data())
            resp, _g = self._call(self._headers(key=""), dumps(push_body()))
            self.assertIn(b"Duplicate", resp.get_data())
        self.assertEqual(self.Event.search_count([("hook_id", "=", self.hook.id)]), 1)
        self.assertEqual(len(self._rebuilds()), 1)

    def test_a_dispatch_that_raises_answers_200_and_keeps_the_error(self):
        with patch.object(
            type(self.Event), "_process_push", side_effect=RuntimeError("boom"),
        ), mute_logger("odoo.addons.incubacloud.models.cloud_gitlab_event"):
            resp, _g = self._call(self._headers(), dumps(push_body()))
        self.assertEqual(resp.status_code, 200)
        event = self.Event.search([("hook_id", "=", self.hook.id)])
        self.assertFalse(event.processed)
        self.assertIn("boom", event.error)
