"""The AI providers the assistant speaks to, and the models we tested.

Fixed addresses only: no provider takes a URL from the panel's settings
(a free URL would let a manager's typo, or an attacker with the manager's
session, point the panel's requests anywhere).
"""

#: ``(code, label, beta)``: beta until a real key has tried it.
PROVIDERS = (
    ("anthropic", "Anthropic", False),
    ("openai", "OpenAI", True),
    ("google", "Google Gemini", True),
    ("openrouter", "OpenRouter", True),
)
#: The models tested for real with the panel's tools (QA walk 29), by provider.
#: Every other model listed is "untested".
RECOMMENDED = {"anthropic": ("claude-opus-5-5", "claude-sonnet-5-5")}


def provider_label(code):
    """Return the provider's name, or *code* when unknown."""
    return next((label for key, label, _beta in PROVIDERS if key == code), code)
