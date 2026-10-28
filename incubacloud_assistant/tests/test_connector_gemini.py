"""Gemini: generateContent, function declarations it accepts, its failures."""

from unittest.mock import patch

import requests

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.tool_spec import obj
from odoo.addons.incubacloud_mcp.tests.common import http_response

from ..connectors.base import ChatRequest, ProviderError, ToolCall, ToolDef, Turn
from ..connectors.gemini import BASE_URL, GeminiConnector, gemini_schema
from . import recorded

KEY = "AIza-gemini-connector-test-0001"
VALS = {"type": "object", "additionalProperties": True, "description": "Fields to set."}


class TestGeminiConnector(BaseCase):
    """Requests to the fixed address, with the key in a header, never in the URL."""

    def _answer(self, *answers):
        """Patch ``requests.request`` to answer *answers* in turn; return the mock."""
        patcher = patch.object(
            requests, "request", autospec=True, side_effect=list(answers)
        )
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _request(self):
        """One tool with a free-form object, and a turn through it."""
        schema = {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "minimum": 1},
                "vals": VALS,
            },
            "required": ["project_id"],
            "additionalProperties": False,
        }
        return ChatRequest(
            model="gemini-2.5-pro",
            system="Rules.",
            tools=(ToolDef("update_project", "Change a project.", schema),),
            turns=(Turn("user", "Rename project 4"),),
        )

    def test_the_request(self):
        """Address, key header, system instruction, declarations Gemini accepts."""
        sent = self._answer(http_response(200, recorded.GEMINI_FUNCTION_CALL))
        GeminiConnector(KEY).chat(self._request())
        method, url = sent.call_args.args
        self.assertEqual(
            (method, url), ("POST", f"{BASE_URL}/models/gemini-2.5-pro:generateContent")
        )
        self.assertNotIn(KEY, url)
        kwargs = sent.call_args.kwargs
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], KEY)
        body = kwargs["json"]
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "Rules."}]})
        declaration = body["tools"][0]["functionDeclarations"][0]
        self.assertNotIn("additionalProperties", str(declaration))
        self.assertEqual(
            declaration["parameters"]["properties"]["vals"]["type"], "string"
        )

    def test_a_tool_without_arguments_is_declared_without_parameters(self):
        """Gemini wants an object with properties, or nothing at all."""
        sent = self._answer(http_response(200, recorded.GEMINI_FUNCTION_CALL))
        GeminiConnector(KEY).chat(
            ChatRequest(
                model="gemini-2.5-pro",
                system="Rules.",
                tools=(ToolDef("panel_overview", "The panel at a glance.", obj()),),
                turns=(Turn("user", "hi"),),
            )
        )
        declaration = sent.call_args.kwargs["json"]["tools"][0]["functionDeclarations"][
            0
        ]
        self.assertNotIn("parameters", declaration)

    def test_a_free_form_object_travels_as_json_text(self):
        """Gemini cannot declare a free object: it sends JSON text, decoded back."""
        self.assertEqual(gemini_schema(VALS)["type"], "string")
        answer = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "update_project",
                                    "args": {
                                        "project_id": 4,
                                        "vals": '{"name": "Acme"}',
                                    },
                                }
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        self._answer(http_response(200, answer))
        response = GeminiConnector(KEY).chat(self._request())
        self.assertEqual(
            response.tool_calls[0].arguments,
            {"project_id": 4, "vals": {"name": "Acme"}},
        )

    def test_the_answer_skips_thoughts_and_keeps_the_parts(self):
        """Thought parts are not text; the parts go back with their signature."""
        self._answer(http_response(200, recorded.GEMINI_FUNCTION_CALL))
        response = GeminiConnector(KEY).chat(self._request())
        self.assertEqual(response.text, "")
        self.assertEqual(response.tool_calls[0].name, "get_project")
        self.assertEqual(response.stop, "tool_use")
        self.assertEqual(response.raw[1]["thoughtSignature"], "sig-g")
        self.assertEqual(
            (
                response.usage.input_tokens,
                response.usage.output_tokens,
                response.usage.cache_read_tokens,
            ),
            (800, 100, 100),
        )

    def test_tool_results_go_back_as_function_responses(self):
        """One user turn with the function responses: a result, or an error."""
        turns = (
            Turn("user", "hi"),
            Turn(
                "assistant",
                "",
                (ToolCall("gemini_call_1", "get_project", {}),),
                raw=recorded.GEMINI_FUNCTION_CALL["candidates"][0]["content"]["parts"],
                raw_provider="google",
            ),
            Turn(
                "tool",
                '{"ok": true}',
                tool_call_id="gemini_call_1",
                tool_name="get_project",
            ),
            Turn(
                "tool",
                "Not allowed.",
                tool_call_id="gemini_call_2",
                tool_name="get_project",
                is_error=True,
            ),
        )
        contents = GeminiConnector(KEY)._contents(turns)
        self.assertEqual(contents[1]["role"], "model")
        self.assertEqual(contents[1]["parts"][1]["thoughtSignature"], "sig-g")
        self.assertEqual(
            [part["functionResponse"]["response"] for part in contents[2]["parts"]],
            [{"result": '{"ok": true}'}, {"error": "Not allowed."}],
        )

    def test_calls_keep_their_ids_and_another_provider_s_skip_the_signature(self):
        """Gemini's ids go back; another provider's turn says it has no signature."""
        turns = (
            Turn("user", "hi"),
            Turn(
                "assistant",
                "",
                (ToolCall("toolu_01", "get_project", {}),),
                raw=[{"type": "tool_use"}],
                raw_provider="anthropic",
            ),
            Turn("tool", "{}", tool_call_id="toolu_01", tool_name="get_project"),
        )
        contents = GeminiConnector(KEY)._contents(turns)
        call = contents[1]["parts"][0]
        self.assertEqual(call["functionCall"]["id"], "toolu_01")
        self.assertEqual(call["thoughtSignature"], "skip_thought_signature_validator")
        self.assertEqual(contents[2]["parts"][0]["functionResponse"]["id"], "toolu_01")

    def test_a_blocked_prompt_is_a_refusal_and_a_malformed_call_an_outage(self):
        """No candidate: refused. A call Gemini could not write: try again."""
        malformed = {
            "candidates": [
                {"content": {"parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL"}
            ]
        }
        self._answer(
            http_response(200, recorded.GEMINI_BLOCKED), http_response(200, malformed)
        )
        connector = GeminiConnector(KEY)
        self.assertEqual(connector.chat(self._request()).stop, "refusal")
        with self.assertRaises(ProviderError) as caught:
            connector.chat(self._request())
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_a_limit_says_how_long_to_wait(self):
        """RESOURCE_EXHAUSTED carries its pause in its details."""
        self._answer(http_response(429, recorded.GEMINI_EXHAUSTED))
        with self.assertRaises(ProviderError) as caught:
            GeminiConnector(KEY).chat(self._request())
        self.assertEqual(
            (caught.exception.kind, caught.exception.retry_after), ("rate_limited", 7.0)
        )

    def test_the_models_that_generate_content(self):
        """No embeddings, speech, images or ``-latest`` aliases; no ``models/``."""
        self._answer(http_response(200, recorded.GEMINI_MODELS))
        models = GeminiConnector(KEY).list_models()
        self.assertEqual(
            [(m.id, m.name) for m in models], [("gemini-2.5-pro", "Gemini 2.5 Pro")]
        )

    def test_a_model_id_that_would_change_the_path_is_refused(self):
        """No request leaves for ``../x`` or ``a?b``."""
        sent = self._answer()
        for model in ("../files", "a?key=x", "a#b"):
            with self.subTest(model), self.assertRaises(ProviderError):
                GeminiConnector(KEY).chat(
                    ChatRequest(model=model, system="", tools=(), turns=())
                )
        sent.assert_not_called()

    def test_a_bad_key_is_named_as_such(self):
        """Gemini answers 400 INVALID_ARGUMENT for a bad key."""
        self._answer(http_response(400, recorded.GEMINI_KEY_ERROR))
        with self.assertRaises(ProviderError) as caught:
            GeminiConnector(KEY).list_models()
        self.assertEqual(caught.exception.kind, "invalid_key")
        self.assertNotIn(KEY, caught.exception.message)

    def test_gemini_3_s_call_ids_go_back_with_their_answers(self):
        """An id Gemini gave a call is the id of its function response."""
        self._answer(http_response(200, recorded.GEMINI_3_FUNCTION_CALL))
        response = GeminiConnector(KEY).chat(self._request())
        call = response.tool_calls[0]
        self.assertEqual(call.id, "fc_1")
        turns = (
            Turn("user", "hi"),
            Turn("assistant", "", (call,), raw=response.raw, raw_provider="google"),
            Turn("tool", "{}", tool_call_id=call.id, tool_name=call.name),
        )
        answer = GeminiConnector(KEY)._contents(turns)[2]["parts"][0]
        self.assertEqual(answer["functionResponse"]["id"], "fc_1")

    def test_a_failure_gemini_billed_carries_its_tokens(self):
        """A malformed call was generated: the engine counts what it cost."""
        malformed = {
            "candidates": [
                {"content": {"parts": []}, "finishReason": "TOO_MANY_TOOL_CALLS"}
            ],
            "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 40},
            "modelVersion": "gemini-2.5-pro",
        }
        self._answer(http_response(200, malformed))
        with self.assertRaises(ProviderError) as caught:
            GeminiConnector(KEY).chat(self._request())
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertEqual(caught.exception.usage.input_tokens, 500)
