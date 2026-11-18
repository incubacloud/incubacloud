"""The MCP's aliases give way to another module that declares their URL.

The MCP answers at ``/cloud/mcp``, with its RFC 9728 document at
``/.well-known/oauth-protected-resource/cloud/mcp``. The panels that
connected their agents before keep ``/mcp``, its document and the root
one: aliases. Another MCP server module installed in the same Odoo may
declare the same URLs, and Odoo puts both rules in the map in the
alphabetical order of their modules, with no word: the first one wins
(``ir.http.routing_map``). Whichever of the two that is, its agents would
reach the wrong server.

Here, as soon as another installed module declares any of the three, all
of them leave the map, with a warning that names that module: a client
that met the other module on one of them must not meet this panel on the
next, and the URLs another module declares are its own. The map is built
again on every load of the registry, so in each worker and after any
module is installed or removed; when the other module goes, the aliases
come back by themselves. ``/cloud/mcp`` and its document stay ours.
"""

import functools
import logging

from odoo import api, http, models, tools

from ..protocol.canonical import ALIAS_PATH, MCP_PATH, METADATA_PATH

_logger = logging.getLogger(__name__)

#: The module whose controllers serve the MCP and its documents.
MODULE = "incubacloud_mcp"
#: The URLs this module gives up to another module that declares them.
ALIASES = frozenset({ALIAS_PATH, METADATA_PATH + ALIAS_PATH, METADATA_PATH})


def _alias(url):
    """Return the alias *url* is, or ``''``.

    The map does not tell ``/mcp`` from ``/mcp/`` (``strict_slashes`` is
    off): a module that declares one takes both.

    :param str url: a rule's URL, as the routing generator yields it
    :rtype: str
    """
    plain = url.rstrip("/") or "/"
    return plain if plain in ALIASES else ""


def route_module(endpoint):
    """Return the addon whose controller declares *endpoint*, or ``''``.

    The routing generator binds each route to an instance of a class built
    from a top controller (a direct subclass of ``http.Controller``, which
    Odoo registers under its addon) and the classes that extend it. The
    top controller is the one that declared the route, whichever module
    extends it, and whatever mixins the classes list beside it.

    :param endpoint: an endpoint of the routing map (a ``functools.partial``
        of the bound method)
    :rtype: str
    """
    func = endpoint
    while isinstance(func, functools.partial):
        func = func.func
    owner = getattr(func, "__self__", None)
    if owner is None:
        return ""
    for cls in reversed(type(owner).__mro__):
        if http.Controller not in cls.__bases__:
            continue
        parts = cls.__module__.split(".")
        if parts[:2] == ["odoo", "addons"] and len(parts) > 2:
            return parts[2]
    return ""


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def _generate_routing_rules(self, modules, converters):
        """Leave out all the MCP's aliases once another module declares one.

        Each URL another module declares is logged as a warning naming that
        module: it answers that URL from now on, the panel answers none of
        its aliases, and its agents go to ``/cloud/mcp``.
        """
        rules = list(super()._generate_routing_rules(modules, converters))
        taken = {}
        for url, endpoint in rules:
            alias = _alias(url)
            if not alias:
                continue
            module = route_module(endpoint)
            if module != MODULE:
                taken.setdefault(alias, set()).add(module or "?")
        for alias in sorted(taken):
            _logger.warning(
                "[mcp] %s is also declared by %s: this panel's aliases (%s) leave "
                "the routing map and that module answers it; agents reach this "
                "panel at %s",
                alias,
                ", ".join(sorted(taken[alias])),
                ", ".join(sorted(ALIASES)),
                MCP_PATH,
            )
        for url, endpoint in rules:
            if taken and _alias(url) and route_module(endpoint) == MODULE:
                continue
            yield url, endpoint

    @api.model
    @tools.ormcache(cache="routing")
    def _mcp_alias_owners(self):
        """Return which module answers each alias the MCP gave up.

        Read from the routing map, so as each worker built it. Cached with
        the map (``routing``): both are built again on every load of the
        registry.

        :return: ``((alias, module), …)``, sorted, of the aliases another
            module declares (the MCP gave up all three); empty when every
            alias is the MCP's own
        :rtype: tuple
        """
        owners = {}
        for rule in self.routing_map().iter_rules():
            alias = _alias(rule.rule)
            if not alias:
                continue
            module = route_module(rule.endpoint)
            if module != MODULE:
                owners.setdefault(alias, module or "?")
        return tuple(sorted(owners.items()))
