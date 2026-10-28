"""Anthropic: the request it sends, the answer it reads, the failures it names."""

from unittest.mock import patch

import requests

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.tests.common import http_response

from ..connectors.anthropic_connector import (
    API_VERSION,
    BASE_URL,
    MAX_TOKENS,
    AnthropicConnector,
)
from ..connectors.base import ChatRequest, ProviderError, ToolCall, ToolDef, Turn
from . import recorded

KEY = "sk-ant-connector-test-0001"


class TestAnthropicConnector(BaseCase):
    """Requests to the fixed address with the pinned version; answers and failures."""

    def _answer(self, *answers):
        """Patch ``requests.request`` to answer *answers* in turn; return the mock."""
        patcher = patch.object(
            requests, "request", autospec=True, side_effect=list(answers)
        )
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _request(self, model="claude-opus-5-5"):
        """A short conversation with one tool."""
        return ChatRequest(
            model=model,
            system="You are the panel's assistant.",
            tools=(ToolDef("get_project", "One project.", {"type": "object"}),),
            turns=(Turn("user", "Which version is project 4?"),),
        )

    def test_the_request_names_the_address_the_version_the_model_and_the_key(self):
        """One POST; the pinned version, the beta, thinking settings; no fallback."""
        sent = self._answer(http_response(200, recorded.ANTHROPIC_TOOL_USE))
        AnthropicConnector(KEY).chat(self._request())
        self.assertEqual(sent.call_args.args, ("POST", f"{BASE_URL}/v1/messages"))
        kwargs = sent.call_args.kwargs
        self.assertIs(kwargs["allow_redirects"], False)
        headers, body = kwargs["headers"], kwargs["json"]
        self.assertEqual(headers["x-api-key"], KEY)
        self.assertEqual(headers["anthropic-version"], API_VERSION)
        self.assertEqual(
            headers["anthropic-beta"], "thinking-binding-controls-2026-08-01"
        )
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["max_tokens"], MAX_TOKENS)
        self.assertEqual(
            body["thinking"],
            {
                "type": "adaptive",
                "block_binding": {"prefix_mismatch_behavior": "drop_block"},
            },
        )
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["tools"][0]["input_schema"], {"type": "object"})
        for key in ("fallbacks", "stream", "temperature", "models"):
            self.assertNotIn(key, body)

    def test_every_model_that_checks_its_thinking_gets_drop_block(self):
        """Claude Haiku 5.5, untested: thinking bound with drop_block; no effort."""
        answer = recorded.ANTHROPIC_TOOL_USE | {"model": "claude-haiku-5-5"}
        sent = self._answer(http_response(200, answer))
        AnthropicConnector(KEY).chat(self._request("claude-haiku-5-5"))
        kwargs = sent.call_args.kwargs
        self.assertEqual(
            kwargs["json"]["thinking"]["block_binding"],
            {"prefix_mismatch_behavior": "drop_block"},
        )
        self.assertNotIn("output_config", kwargs["json"])
        self.assertEqual(
            kwargs["headers"]["anthropic-beta"], "thinking-binding-controls-2026-08-01"
        )

    def test_a_refused_thinking_signature_is_sent_again_without_thinking(self):
        """A newer model refuses an edited history: once more, its thinking left out."""
        refused = http_response(
            400,
            recorded.anthropic_error(
                "invalid_request_error",
                "messages.1.content.0: Invalid `signature` in `thinking` block.",
            ),
        )
        answer = recorded.ANTHROPIC_TOOL_USE | {"model": "claude-opus-6"}
        sent = self._answer(refused, http_response(200, answer))
        raw = recorded.ANTHROPIC_TOOL_USE["content"]
        request = ChatRequest(
            model="claude-opus-6",
            system="Rules.",
            tools=(),
            turns=(
                Turn("user", "hi"),
                Turn("assistant", "Let me look.", raw=raw, raw_provider="anthropic"),
                Turn("user", "go on"),
            ),
            max_tokens=4000,
        )
        response = AnthropicConnector(KEY).chat(request)
        self.assertEqual(response.model, "claude-opus-6")
        again = sent.call_args_list[1].kwargs["json"]
        self.assertEqual(
            [block["type"] for block in again["messages"][1]["content"]],
            ["text", "tool_use"],
        )
        self.assertEqual(again["max_tokens"], 4000)

    def test_an_untested_model_gets_the_plain_request(self):
        """No thinking, no effort, no beta a model of another generation refuses."""
        answer = recorded.ANTHROPIC_TOOL_USE | {"model": "claude-haiku-4-5-20251001"}
        sent = self._answer(http_response(200, answer))
        AnthropicConnector(KEY).chat(self._request("claude-haiku-4-5-20251001"))
        kwargs = sent.call_args.kwargs
        for key in ("thinking", "output_config"):
            self.assertNotIn(key, kwargs["json"])
        self.assertNotIn("anthropic-beta", kwargs["headers"])

    def test_the_answer_comes_back_as_text_tool_calls_usage_and_model(self):
        """Text, the tool call, the stop, the tokens, the model; the blocks kept."""
        self._answer(http_response(200, recorded.ANTHROPIC_TOOL_USE))
        response = AnthropicConnector(KEY).chat(self._request())
        self.assertEqual(response.text, "Let me look at the project.")
        self.assertEqual(
            response.tool_calls,
            (ToolCall("toolu_01", "get_project", {"project_id": 4}),),
        )
        self.assertEqual(
            (response.stop, response.model), ("tool_use", "claude-opus-5-5")
        )
        self.assertEqual(
            (response.usage.input_tokens, response.usage.cache_write_tokens),
            (1200, 9000),
        )
        self.assertEqual(response.raw[0]["signature"], "sig-abc")

    def test_the_conversation_is_sent_back_as_it_came(self):
        """The model's own content goes back unchanged; tool answers as results."""
        raw = recorded.ANTHROPIC_TOOL_USE["content"]
        turns = (
            Turn("user", "Which version?"),
            Turn(
                "assistant",
                "Let me look.",
                (ToolCall("toolu_01", "get_project", {"project_id": 4}),),
                raw=raw,
                raw_provider="anthropic",
            ),
            Turn(
                "tool",
                '{"odoo_version": "19.0"}',
                tool_call_id="toolu_01",
                tool_name="get_project",
            ),
            Turn("user", '<data source="job">done</data>'),
        )
        messages = AnthropicConnector(KEY)._messages(turns)
        self.assertEqual(messages[1], {"role": "assistant", "content": raw})
        self.assertEqual(
            messages[2]["content"][0],
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01",
                "content": '{"odoo_version": "19.0"}',
                "is_error": False,
            },
        )
        self.assertEqual(messages[2]["content"][1]["type"], "text")

    def test_another_provider_s_turn_is_rebuilt_from_its_text_and_calls(self):
        """No raw content of Anthropic's: text block and tool_use blocks."""
        turns = (
            Turn("user", "hi"),
            Turn(
                "assistant",
                "",
                (ToolCall("c1", "search_docs", {"query": "x"}),),
                raw=[{"parts": []}],
                raw_provider="google",
            ),
            Turn("tool", "[]", tool_call_id="c1", tool_name="search_docs"),
        )
        content = AnthropicConnector(KEY)._messages(turns)[1]["content"]
        self.assertEqual(
            content,
            [
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "search_docs",
                    "input": {"query": "x"},
                }
            ],
        )

    def test_the_models_listed_page_by_page(self):
        """Every model of every page, with its name; Claude models all take tools."""
        sent = self._answer(
            http_response(200, recorded.ANTHROPIC_MODELS),
            http_response(200, recorded.ANTHROPIC_MODELS_PAGE_2),
        )
        models = AnthropicConnector(KEY).list_models()
        self.assertEqual(
            [(m.id, m.name, m.tools) for m in models],
            [
                ("claude-opus-5-5", "Claude Opus 5.5", True),
                ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", True),
            ],
        )
        self.assertEqual(
            sent.call_args_list[1].args[1],
            f"{BASE_URL}/v1/models?limit=1000&after_id=claude-opus-5-5",
        )
        self.assertEqual(
            sent.call_args.kwargs["headers"]["anthropic-version"], API_VERSION
        )

    def test_failures_by_kind(self):
        """Key, limit and its pause, outage, too long, no credit; never the key."""
        limited = http_response(
            429, recorded.anthropic_error("rate_limit_error", "slow down")
        )
        limited.headers["retry-after"] = "7"
        cases = (
            (
                http_response(
                    401,
                    recorded.anthropic_error(
                        "authentication_error", f"invalid x-api-key {KEY}"
                    ),
                ),
                "invalid_key",
            ),
            (limited, "rate_limited"),
            (
                http_response(
                    529, recorded.anthropic_error("overloaded_error", "Overloaded")
                ),
                "unavailable",
            ),
            (
                http_response(
                    400,
                    recorded.anthropic_error(
                        "invalid_request_error",
                        "prompt is too long: 1050000 tokens > 1000000 maximum",
                    ),
                ),
                "context_too_long",
            ),
            (
                http_response(
                    400,
                    recorded.anthropic_error(
                        "invalid_request_error",
                        "Your credit balance is too low to access the Anthropic API.",
                    ),
                ),
                "quota",
            ),
            (
                http_response(
                    402,
                    recorded.anthropic_error(
                        "billing_error", "Your payment method was declined."
                    ),
                ),
                "quota",
            ),
        )
        # One patch for every case: patching it again with autospec fails.
        self._answer(*(answer for answer, _kind in cases))
        for _response, kind in cases:
            with self.subTest(kind):
                with self.assertRaises(ProviderError) as caught:
                    AnthropicConnector(KEY).chat(self._request())
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn(KEY, caught.exception.message)
                if kind == "rate_limited":
                    self.assertEqual(caught.exception.retry_after, 7.0)

    def test_no_connection_is_unreachable(self):
        """A refused connection: the panel cannot reach the provider."""
        patcher = patch.object(
            requests,
            "request",
            autospec=True,
            side_effect=requests.ConnectionError("no route"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(ProviderError) as caught:
            AnthropicConnector(KEY).list_models()
        self.assertEqual(caught.exception.kind, "unreachable")
