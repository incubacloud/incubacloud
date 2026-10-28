"""OpenAI and OpenRouter: Chat Completions with tools."""

from unittest.mock import patch

import requests

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.tests.common import http_response

from ..connectors.base import ChatRequest, ProviderError, ToolCall, ToolDef, Turn
from ..connectors.openai_compat import OpenAIConnector, OpenRouterConnector
from . import recorded

KEY = "sk-openai-connector-test-0001"


class TestChatCompletions(BaseCase):
    """Requests to the fixed addresses; answers and failures read."""

    def _answer(self, *answers):
        """Patch ``requests.request`` to answer *answers* in turn; return the mock."""
        patcher = patch.object(
            requests, "request", autospec=True, side_effect=list(answers)
        )
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _request(self):
        """A conversation that went through one tool."""
        return ChatRequest(
            model="gpt-5.1",
            system="Rules.",
            tools=(ToolDef("get_project", "One project.", {"type": "object"}),),
            turns=(
                Turn("user", "Which version?"),
                Turn(
                    "assistant",
                    "",
                    (ToolCall("call_1", "get_project", {"project_id": 4}),),
                ),
                Turn("tool", "{}", tool_call_id="call_1", tool_name="get_project"),
            ),
        )

    def test_openai_s_request(self):
        """Its address, the bearer key, system first, tool calls and results."""
        sent = self._answer(http_response(200, recorded.OPENAI_TOOL_CALLS))
        OpenAIConnector(KEY).chat(self._request())
        method, url = sent.call_args.args
        self.assertEqual(
            (method, url), ("POST", "https://api.openai.com/v1/chat/completions")
        )
        kwargs = sent.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {KEY}")
        body = kwargs["json"]
        self.assertEqual(body["messages"][0], {"role": "system", "content": "Rules."})
        self.assertEqual(
            body["messages"][2]["tool_calls"][0]["function"],
            {"name": "get_project", "arguments": '{"project_id": 4}'},
        )
        self.assertEqual(
            body["messages"][3],
            {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        )
        self.assertIn("max_completion_tokens", body)
        self.assertEqual(body["tools"][0]["function"]["name"], "get_project")

    def test_the_answer_with_cached_tokens_apart(self):
        """Tool calls decoded; cached prompt tokens are not counted twice."""
        self._answer(http_response(200, recorded.OPENAI_TOOL_CALLS))
        response = OpenAIConnector(KEY).chat(self._request())
        self.assertEqual(
            response.tool_calls, (ToolCall("call_1", "get_project", {"project_id": 4}),)
        )
        self.assertEqual(response.stop, "tool_use")
        usage = response.usage
        self.assertEqual(
            (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens),
            (300, 600, 100),
        )

    def test_an_empty_answer_of_the_model_is_not_sent_back(self):
        """No text and no calls: OpenAI refuses such a turn in the next request."""
        sent = self._answer(http_response(200, recorded.OPENAI_TOOL_CALLS))
        request = ChatRequest(
            model="gpt-5.1",
            system="Rules.",
            tools=(),
            turns=(Turn("user", "hi"), Turn("assistant", ""), Turn("user", "again")),
        )
        OpenAIConnector(KEY).chat(request)
        roles = [m["role"] for m in sent.call_args.kwargs["json"]["messages"]]
        self.assertEqual(roles, ["system", "user", "user"])

    def test_openai_lists_its_chat_models_only(self):
        """Embeddings and realtime models are left out; tools unknown."""
        self._answer(http_response(200, recorded.OPENAI_MODELS))
        models = OpenAIConnector(KEY).list_models()
        self.assertEqual([m.id for m in models], ["gpt-5.1", "o4-mini"])
        self.assertIsNone(models[0].tools)

    def test_openrouter_lists_the_models_with_tools_and_their_prices(self):
        """The key first; then those that take tools, never a router; per million."""
        sent = self._answer(
            http_response(200, recorded.OPENROUTER_KEY),
            http_response(200, recorded.OPENROUTER_MODELS),
        )
        models = OpenRouterConnector(KEY).list_models()
        self.assertEqual(
            sent.call_args_list[0].args, ("GET", "https://openrouter.ai/api/v1/key")
        )
        self.assertEqual(len(models), 1)
        self.assertEqual(
            (models[0].id, models[0].tools, models[0].price_in, models[0].price_out),
            ("anthropic/claude-opus-5.5", True, 4.0, 20.0),
        )

    def test_openrouter_tries_the_key_its_listing_does_not_need(self):
        """The listing answers without a key: a refused key is told on save."""
        self._answer(
            http_response(
                401, {"error": {"message": "No auth credentials found", "code": 401}}
            ),
        )
        with self.assertRaises(ProviderError) as caught:
            OpenRouterConnector(KEY).list_models()
        self.assertEqual(caught.exception.kind, "invalid_key")

    def test_openrouter_s_errors_inside_a_200_and_its_403(self):
        """An upstream error is an outage; a guardrail's 403 in a 200, a refusal
        never retried, its tokens billed; an HTTP 403, a limit, never the key."""
        self._answer(
            http_response(200, recorded.OPENROUTER_ERROR_IN_200),
            http_response(200, recorded.OPENROUTER_POLICY_IN_200),
            http_response(
                403, {"error": {"message": "Key limit exceeded", "code": 403}}
            ),
        )
        connector = OpenRouterConnector(KEY)
        failures = []
        for kind in ("unavailable", "bad_request", "quota"):
            with self.subTest(kind), self.assertRaises(ProviderError) as caught:
                connector.chat(self._request())
            self.assertEqual(caught.exception.kind, kind)
            failures.append(caught.exception)
        self.assertFalse(failures[1].retryable)
        self.assertEqual(failures[1].usage.input_tokens, 120)

    def test_openrouter_s_request_names_one_model_and_no_fallback(self):
        """Its fixed address; the model allowed and nothing else to fall back to."""
        sent = self._answer(http_response(200, recorded.OPENAI_TOOL_CALLS))
        OpenRouterConnector(KEY).chat(self._request())
        self.assertEqual(
            sent.call_args.args[1], "https://openrouter.ai/api/v1/chat/completions"
        )
        body = sent.call_args.kwargs["json"]
        self.assertEqual(body["model"], "gpt-5.1")
        self.assertIn("max_tokens", body)
        for key in ("models", "route", "fallbacks"):
            self.assertNotIn(key, body)

    def test_failures_by_kind(self):
        """Context, quota, credit, key, limit: each named, never the key in it."""
        cases = (
            (400, recorded.OPENAI_CONTEXT_ERROR, "context_too_long"),
            (429, recorded.OPENAI_QUOTA_ERROR, "quota"),
            (
                429,
                {
                    "error": {
                        "message": "Credit balance exhausted.",
                        "type": "credit_balance_exhausted",
                    }
                },
                "quota",
            ),
            (
                401,
                {
                    "error": {
                        "message": f"Incorrect API key provided: {KEY[:8]}…",
                        "code": "invalid_api_key",
                    }
                },
                "invalid_key",
            ),
            (429, {"error": {"message": "Rate limit reached"}}, "rate_limited"),
        )
        # One patch for every case: patching it again with autospec fails.
        self._answer(*(http_response(status, body) for status, body, _kind in cases))
        for _status, _body, kind in cases:
            with self.subTest(kind), self.assertRaises(ProviderError) as caught:
                OpenAIConnector(KEY).chat(self._request())
            self.assertEqual(caught.exception.kind, kind)
