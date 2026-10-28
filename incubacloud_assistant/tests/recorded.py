"""Providers' answers, recorded in the shapes each connector pins.

Used by the connectors' tests through a patched ``requests.request``: no
test reaches a provider. Anthropic's are in the shapes of its Messages API
version 2023-06-01 (``connectors/anthropic_connector.py``); task 25 replaces
them with answers saved from the real test of QA walk 29 when they differ.
"""

#: GET /v1/models?limit=1000, first page.
ANTHROPIC_MODELS = {
    "data": [
        {
            "type": "model",
            "id": "claude-opus-5-5",
            "display_name": "Claude Opus 5.5",
            "created_at": "2026-09-01T00:00:00Z",
        },
    ],
    "has_more": True,
    "first_id": "claude-opus-5-5",
    "last_id": "claude-opus-5-5",
}
#: GET /v1/models?limit=1000&after_id=claude-opus-5-5, last page.
ANTHROPIC_MODELS_PAGE_2 = {
    "data": [
        {
            "type": "model",
            "id": "claude-haiku-4-5-20251001",
            "display_name": "Claude Haiku 4.5",
            "created_at": "2025-10-01T00:00:00Z",
        },
    ],
    "has_more": False,
    "first_id": "claude-haiku-4-5-20251001",
    "last_id": "claude-haiku-4-5-20251001",
}

#: POST /v1/messages: an answer that asks for a tool, after some thinking.
ANTHROPIC_TOOL_USE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Let me look at the project."},
        {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "get_project",
            "input": {"project_id": 4},
        },
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cache_creation_input_tokens": 9000,
        "cache_read_input_tokens": 0,
    },
}


def anthropic_error(kind, message):
    """Return an error body of Anthropic's: ``{"type": "error", "error": {…}}``."""
    return {"type": "error", "error": {"type": kind, "message": message}}


#: GET https://api.openai.com/v1/models
OPENAI_MODELS = {
    "object": "list",
    "data": [
        {"id": "gpt-5.1", "object": "model", "owned_by": "openai"},
        {"id": "text-embedding-3-large", "object": "model", "owned_by": "openai"},
        {"id": "o4-mini", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4o-realtime-preview", "object": "model", "owned_by": "openai"},
    ],
}

#: POST /v1/chat/completions, asking for a tool.
OPENAI_TOOL_CALLS = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-5.1",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_project",
                            "arguments": '{"project_id": 4}',
                        },
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
    },
}

OPENAI_CONTEXT_ERROR = {
    "error": {
        "message": "This model's maximum context length is 128000 tokens.",
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
    }
}
OPENAI_QUOTA_ERROR = {
    "error": {
        "message": "You exceeded your current quota.",
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
}

#: GET https://openrouter.ai/api/v1/key: the key OpenRouter knows (its
#: listing of models answers without one).
OPENROUTER_KEY = {"data": {"label": "sk-or-v1-abc...", "limit": None, "usage": 0}}
#: POST /api/v1/chat/completions: a 200 that carries an error of the model's
#: provider instead of an answer.
OPENROUTER_ERROR_IN_200 = {
    "id": "gen-1",
    "model": "anthropic/claude-opus-5.5",
    "choices": [
        {
            "index": 0,
            "finish_reason": "error",
            "message": {"role": "assistant", "content": ""},
            "error": {"code": 502, "message": "Upstream provider error"},
        }
    ],
}

#: POST /api/v1/chat/completions: a 200 whose choice a guardrail refused, after
#: the provider read (and billed) the prompt.
OPENROUTER_POLICY_IN_200 = {
    "id": "gen-2",
    "model": "anthropic/claude-opus-5.5",
    "choices": [
        {
            "index": 0,
            "finish_reason": "error",
            "message": {"role": "assistant", "content": ""},
            "error": {
                "code": 403,
                "message": "Request blocked",
                "metadata": {"error_type": "content_policy_violation"},
            },
        }
    ],
    "usage": {"prompt_tokens": 120, "completion_tokens": 0},
}

#: GET https://openrouter.ai/api/v1/models
OPENROUTER_MODELS = {
    "data": [
        {
            "id": "anthropic/claude-opus-5.5",
            "name": "Anthropic: Claude Opus 5.5",
            "supported_parameters": ["tools", "tool_choice", "max_tokens"],
            "pricing": {"prompt": "0.000004", "completion": "0.00002"},
        },
        {
            "id": "some/model-without-tools",
            "name": "No tools",
            "supported_parameters": ["max_tokens"],
            "pricing": {"prompt": "0.000001", "completion": "0.000001"},
        },
        {
            "id": "openrouter/auto",
            "name": "Auto Router",
            "supported_parameters": ["tools", "max_tokens"],
            "pricing": {"prompt": "-1", "completion": "-1"},
        },
        {
            "id": "someone/router",
            "name": "A router of someone else's",
            "supported_parameters": ["tools"],
            "pricing": {"prompt": "-1", "completion": "-1"},
        },
    ]
}


#: GET https://generativelanguage.googleapis.com/v1beta/models
GEMINI_MODELS = {
    "models": [
        {
            "name": "models/gemini-2.5-pro",
            "displayName": "Gemini 2.5 Pro",
            "supportedGenerationMethods": ["generateContent", "countTokens"],
        },
        {
            "name": "models/text-embedding-004",
            "displayName": "Text Embedding 004",
            "supportedGenerationMethods": ["embedContent"],
        },
        {
            "name": "models/gemini-flash-latest",
            "displayName": "Gemini Flash Latest",
            "supportedGenerationMethods": ["generateContent"],
        },
        {
            "name": "models/gemini-2.5-flash-preview-tts",
            "displayName": "Gemini 2.5 Flash TTS",
            "supportedGenerationMethods": ["generateContent"],
        },
    ]
}

#: POST …/models/gemini-2.5-pro:generateContent, asking for a tool.
GEMINI_FUNCTION_CALL = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {"text": "Looking.", "thought": True},
                    {
                        "functionCall": {
                            "name": "get_project",
                            "args": {"project_id": 4},
                        },
                        "thoughtSignature": "sig-g",
                    },
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 900,
        "candidatesTokenCount": 30,
        "thoughtsTokenCount": 70,
        "cachedContentTokenCount": 100,
    },
    "modelVersion": "gemini-2.5-pro",
}

GEMINI_KEY_ERROR = {
    "error": {
        "code": 400,
        "message": "API key not valid. Please pass a valid API key.",
        "status": "INVALID_ARGUMENT",
    }
}
GEMINI_EXHAUSTED = {
    "error": {
        "code": 429,
        "message": "Resource has been exhausted.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
        ],
    }
}
#: POST …:generateContent of a Gemini 3 model: its calls carry an id.
GEMINI_3_FUNCTION_CALL = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "fc_1",
                            "name": "get_project",
                            "args": {"project_id": 4},
                        },
                        "thoughtSignature": "sig-g3",
                    }
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 30},
    "modelVersion": "gemini-3-pro",
}
#: A prompt Gemini blocked: no candidate.
GEMINI_BLOCKED = {"promptFeedback": {"blockReason": "SAFETY"}}
