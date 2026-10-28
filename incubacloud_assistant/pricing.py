"""What a model's tokens cost, approximately, in euros.

Anthropic, OpenAI and Google publish prices on their sites, not in their
model listings: the tables below hold the models we know, by their id or
a dated version of it (``same_model``), never a variant (``claude-opus-5-7``
is not ``claude-opus-5``). OpenRouter's listing carries its prices. A
model without a known price costs None — "unknown" in the panel —, never
zero.
"""

from .connectors.base import same_model

#: Euros per US dollar, approximate: the ECB reference rate of the day these
#: tables were last checked (see the date below), 1.1133 dollars per euro on
#: 2026-10-28. The panel shows "≈ €".
EUR_PER_USD = 0.898
#: When the tables and the rate were last checked against the providers' and
#: the ECB's pages.
CHECKED_ON = "2026-10-28"

#: US dollars per million tokens: input, output, cache write (5 minutes), cache
#: read. https://www.anthropic.com/pricing
ANTHROPIC = {
    "claude-fable-5-1": (10.0, 50.0, 12.5, 0.25),
    "claude-fable-5": (10.0, 50.0, 12.5, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 6.25, 0.50),
    # Cache reads at 0.05 of the input on Opus 5.5 and Sonnet 5.5.
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
}
#: Same columns. https://platform.openai.com/docs/pricing (now
#: https://developers.openai.com/api/docs/pricing), Standard prices of the models
#: that take tools in Chat Completions. OpenAI lists no cache write price: it is
#: the input price. GPT-5.6 Sol's promotional price is available at least
#: through 2026-11-21: check it when the candidate is prepared.
OPENAI = {
    "gpt-6-astra": (10.0, 50.0, 10.0, 1.0),
    "gpt-6.1-sol": (2.0, 10.0, 2.0, 0.10),
    "gpt-6.1-terra": (1.0, 5.0, 1.0, 0.05),
    "gpt-6-sol": (2.0, 10.0, 2.0, 0.20),
    "gpt-6-luna": (0.10, 0.50, 0.10, 0.01),
    "gpt-5.6-sol": (4.0, 20.0, 4.0, 0.40),
    "gpt-5.6-terra": (2.0, 12.0, 2.0, 0.20),
    "gpt-5.6-luna": (0.20, 1.20, 0.20, 0.02),
    "gpt-5.5": (5.0, 30.0, 5.0, 0.50),
    "gpt-5.4-mini": (0.75, 4.50, 0.75, 0.075),
    "gpt-5.4-nano": (0.20, 1.25, 0.20, 0.02),
    "gpt-5.4": (2.50, 15.0, 2.50, 0.25),
    "gpt-5.2": (1.75, 14.0, 1.75, 0.175),
    "gpt-5.1": (1.25, 10.0, 1.25, 0.125),
    "gpt-5-mini": (0.25, 2.0, 0.25, 0.025),
    "gpt-5-nano": (0.05, 0.40, 0.05, 0.005),
    "gpt-5": (1.25, 10.0, 1.25, 0.125),
    "gpt-4.1-mini": (0.40, 1.60, 0.40, 0.10),
    "gpt-4.1-nano": (0.10, 0.40, 0.10, 0.025),
    "gpt-4.1": (2.0, 8.0, 2.0, 0.50),
    "gpt-4o-mini": (0.15, 0.60, 0.15, 0.075),
    "gpt-4o": (2.50, 10.0, 2.50, 1.25),
    "o4-mini": (1.10, 4.40, 1.10, 0.275),
    "o3": (2.0, 8.0, 2.0, 0.50),
}
#: Same columns. https://ai.google.dev/gemini-api/docs/pricing, paid tier,
#: standard prices, for the models that take tools. Gemini charges no cache write
#: per call (storage is billed by the hour): the input price. Announced
#: changes: Gemini 4 Argon doubles on 2026-12-01 (input 4.00, output 20.00,
#: cache read 0.20); Gemini 3.8 and 3.6 Flash double on 2027-01-01 (input 1.50,
#: output 7.50, cache read 0.15). Check them when the candidate is prepared.
GOOGLE = {
    "gemini-4-argon": (2.0, 10.0, 2.0, 0.10),
    "gemini-3.8-flash": (0.75, 3.75, 0.75, 0.075),
    "gemini-3.6-flash": (0.75, 3.75, 0.75, 0.075),
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.30, 0.03),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.25, 0.025),
    "gemini-3.1-pro-preview-customtools": (2.0, 12.0, 2.0, 0.20),
    "gemini-3.1-pro-preview": (2.0, 12.0, 2.0, 0.20),
    "gemini-3-flash-preview": (0.50, 3.0, 0.50, 0.05),
    "gemini-2.5-pro": (1.25, 10.0, 1.25, 0.125),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.10, 0.01),
    "gemini-2.5-flash": (0.30, 2.50, 0.30, 0.03),
}
_TABLES = {"anthropic": ANTHROPIC, "openai": OPENAI, "google": GOOGLE}
#: Where a provider charges more for a long prompt: ``{model: (prompt tokens
#: beyond which, the prices then)}``, the prompt counting its cached tokens
#: (Anthropic's page lists both sets of Haiku 5.5's prices).
LONG_PROMPT = {
    "anthropic": {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.625, 0.05))},
    # OpenAI's flagships past 272,000 input tokens.
    "openai": {
        "gpt-6-astra": (272_000, (20.0, 75.0, 20.0, 2.0)),
        "gpt-6.1-sol": (272_000, (4.0, 15.0, 4.0, 0.20)),
        "gpt-6.1-terra": (272_000, (2.0, 7.50, 2.0, 0.10)),
        "gpt-6-sol": (272_000, (4.0, 15.0, 4.0, 0.40)),
        "gpt-6-luna": (272_000, (0.20, 0.75, 0.20, 0.02)),
        "gpt-5.6-sol": (272_000, (8.0, 30.0, 8.0, 0.80)),
        "gpt-5.6-terra": (272_000, (4.0, 18.0, 4.0, 0.40)),
        "gpt-5.6-luna": (272_000, (0.40, 1.80, 0.40, 0.04)),
        "gpt-5.5": (272_000, (10.0, 45.0, 10.0, 1.0)),
        "gpt-5.4": (272_000, (5.0, 22.50, 5.0, 0.50)),
    },
    # Gemini Pro models past 200,000 prompt tokens.
    "google": {
        "gemini-3.1-pro-preview-customtools": (200_000, (4.0, 18.0, 4.0, 0.40)),
        "gemini-3.1-pro-preview": (200_000, (4.0, 18.0, 4.0, 0.40)),
        "gemini-2.5-pro": (200_000, (2.50, 15.0, 2.50, 0.25)),
    },
}


def _in_table(table, model_id):
    """Return the entry of *table* for *model_id* or a dated version of it, or None."""
    if not model_id:
        return None
    if model_id in table:
        return table[model_id]
    for key, entry in table.items():
        if same_model(key, model_id):
            return entry
    return None


def price_for(provider, model_id, listing, prompt_tokens=0):
    """Return ``(input, output, cache write, cache read)`` per million, or None.

    :param listing: ``model id -> entry of the provider's listing`` (or None)
    :param int prompt_tokens: the call's prompt, cached tokens included: past
        a model's threshold (:data:`LONG_PROMPT`) its higher prices apply
    """
    model_id = model_id or ""
    if provider in _TABLES:
        prices = _in_table(_TABLES[provider], model_id)
        tier = _in_table(LONG_PROMPT.get(provider, {}), model_id)
        if prices and tier and prompt_tokens > tier[0]:
            return tier[1]
        return prices
    entry = listing(model_id) or {}
    price_in, price_out = entry.get("price_in"), entry.get("price_out")
    if price_in is None or price_out is None:
        return None
    if float(price_in) < 0 or float(price_out) < 0:
        # OpenRouter's routers list -1: their price is the model they pick.
        return None
    # The listing gives no cache prices: cached tokens at the input price.
    return (float(price_in), float(price_out), float(price_in), float(price_in))


def cost_eur(prices, usage):
    """Return what *usage* costs at *prices*, in euros, or None without prices."""
    if not prices:
        return None
    price_in, price_out, price_write, price_read = prices
    usd = (
        usage.input_tokens * price_in
        + usage.output_tokens * price_out
        + usage.cache_write_tokens * price_write
        + usage.cache_read_tokens * price_read
    ) / 1_000_000
    return round(usd * EUR_PER_USD, 6)


def response_cost(provider, response, listing, requested=""):
    """Return what a :class:`ChatResponse` cost in euros, or None without a price.

    At the price of the model that ran: the one asked for (*requested*)
    when the answer names it or a dated version of it, else the one the
    answer names. The prompt's size picks a long-prompt price.
    """
    usage = response.usage
    model = (
        requested
        if requested and same_model(requested, response.model)
        else response.model
    )
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    return cost_eur(price_for(provider, model, listing, prompt), usage)


def model_usable(config, model_id):
    """Return whether *model_id* may be used now.

    Allowed by the manager and, when the panel has a monthly limit, with a
    known price: the limit cannot count what an unpriced model costs.

    :param config: the assistant's ``AssistantConfig``
    """
    if model_id not in config.allowed_models:
        return False
    return not config.monthly_limit or (
        price_for(config.provider, model_id, config.listing) is not None
    )
