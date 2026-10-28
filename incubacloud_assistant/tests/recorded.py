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
