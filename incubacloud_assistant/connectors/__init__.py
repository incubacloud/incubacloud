"""The assistant's connectors, one per provider, behind one shape."""

from . import anthropic_connector, gemini, openai_compat
from .base import REGISTRY, ProviderError


def connector_for(provider, api_key):
    """Return the connector of *provider* with *api_key*.

    :raises ProviderError: ``bad_request`` for a provider nobody registered
    """
    cls = REGISTRY.get(provider)
    if cls is None:
        raise ProviderError(
            "bad_request", f"Unknown AI provider: {provider}", ours=True
        )
    return cls(api_key)
