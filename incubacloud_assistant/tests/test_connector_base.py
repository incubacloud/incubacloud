"""The connectors' common pieces: the registry, errors, sides, JSON requests."""

from unittest.mock import patch

import requests

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.tests.common import http_response

from ..connectors import _http, connector_for
from ..connectors.base import (
    ProviderError,
    ToolCall,
    Turn,
    parse_arguments,
    same_model,
    sides,
)


class TestConnectorBase(BaseCase):
    """What every connector relies on."""

    def test_an_unknown_provider_is_refused(self):
        """A provider code nobody registered is a bad request."""
        with self.assertRaises(ProviderError) as caught:
            connector_for("acme-ai", "key")
        self.assertEqual(caught.exception.kind, "bad_request")

    def test_tool_answers_and_the_person_s_text_make_one_side(self):
        """After the model's turn, its tools' answers and the next text go together."""
        turns = (
            Turn("user", "hi"),
            Turn("assistant", "", (ToolCall("c1", "get_project", {}),)),
            Turn("tool", "{}", tool_call_id="c1", tool_name="get_project"),
            Turn("user", "job ended"),
        )
        self.assertEqual(
            [(role, len(group)) for role, group in sides(turns)],
            [("user", 1), ("assistant", 1), ("user", 2)],
        )

    def test_an_answer_names_the_model_asked_for_or_a_dated_version_of_it(self):
        """A snapshot of the same model is the same; any other model is not."""
        self.assertTrue(same_model("claude-opus-5-5", "claude-opus-5-5"))
        self.assertTrue(same_model("gpt-5.1", "gpt-5.1-2026-08-07"))
        self.assertTrue(same_model("claude-haiku-4-5", "claude-haiku-4-5-20251001"))
        self.assertTrue(same_model("gemini-2.0-flash", "gemini-2.0-flash-001"))
        self.assertTrue(same_model("gpt-4", "gpt-4-0613"))
        self.assertTrue(same_model("m/x", ""))
        self.assertFalse(same_model("claude-opus-5", "claude-opus-5-5"))
        self.assertFalse(same_model("claude-opus-5-5", "claude-opus-4-8"))
        self.assertFalse(same_model("anthropic/claude-opus-5.5", "openai/gpt-5.1"))

    def test_arguments_that_are_not_json_are_kept_for_the_validator(self):
        """Broken JSON never raises: the tool's validator refuses it."""
        self.assertEqual(parse_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_arguments("{oops"), {"_unparsed": "{oops"})
        self.assertEqual(parse_arguments(None), {})

    def test_a_json_request_never_follows_a_redirect(self):
        """``allow_redirects`` is off, a timeout is set, the body read and closed."""
        with patch.object(
            requests,
            "request",
            autospec=True,
            return_value=http_response(200, {"a": 1}),
        ) as sent:
            status, _headers, data = _http.call_json("GET", "https://x.test/y", {})
        self.assertEqual((status, data), (200, {"a": 1}))
        kwargs = sent.call_args.kwargs
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], _http.TIMEOUT)

    def test_an_answer_that_is_not_a_json_object_is_an_outage(self):
        """A 200 the panel cannot read as an object is no answer of a model."""
        broken = http_response(200, body=b'["not", "an object"]')
        with (
            patch.object(requests, "request", autospec=True, return_value=broken),
            self.assertRaises(ProviderError) as caught,
        ):
            _http.call_json("POST", "https://x.test/y", {})
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_unreachable_and_slow_providers_are_told_apart(self):
        """A timeout is an outage; a refused connection, unreachable."""
        for error, kind in (
            (requests.Timeout("slow"), "unavailable"),
            (requests.ConnectionError("no route"), "unreachable"),
        ):
            with (
                self.subTest(kind),
                patch.object(requests, "request", autospec=True, side_effect=error),
                self.assertRaises(ProviderError) as caught,
            ):
                _http.call_json("GET", "https://x.test/y", {})
            self.assertEqual(caught.exception.kind, kind)

    def test_kinds_of_http_refusals(self):
        """401/403 key, 402 credit, 408 and 5xx outage, 429 limit, 400 too long."""
        self.assertEqual(_http.kind_of(401, ""), "invalid_key")
        self.assertEqual(_http.kind_of(402, ""), "quota")
        self.assertEqual(_http.kind_of(408, ""), "unavailable")
        self.assertEqual(_http.kind_of(429, ""), "rate_limited")
        self.assertEqual(_http.kind_of(503, ""), "unavailable")
        self.assertEqual(
            _http.kind_of(400, "This model's maximum context length is 128000 tokens"),
            "context_too_long",
        )
        self.assertEqual(_http.kind_of(400, "bad tool"), "bad_request")
        self.assertEqual(_http.retry_after({"retry-after": "7"}), 7.0)
        self.assertEqual(_http.retry_after({"retry-after": "900"}), 60.0)
