# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Facade for the Cloud data-load controller.

Historically this file held every /cloud/* JSON-RPC route in a single
3.6k-line controller. The routes are now organised by domain under the
``_data_load/`` package (crud / github / ops / backends), and this
module just re-exports the public symbols and composes the final
``CloudDataLoadController`` out of the mixins.

Public re-exports (kept for backwards-compatibility with external
importers — ``saas_manager``, ``warm_pool``, ``tenant`` controllers and
``deploy_instance_executor`` all import from this module):

    * ``CloudDataLoadController`` — the mounted controller class
    * ``_job_response`` — used by saas_manager to wrap deploy responses
    * ``_parse_github_repo_path`` — used by deploy_instance_executor
    * ``_capped_search`` / ``_LIST_MAX`` — shared pagination helper
    * ``_gh_seg``, ``_has_pat``, ``_normalize_domain``,
      ``_is_safe_remote_path``, ``_quote_remote_path``, ``_SECRET_FIELDS``
"""
from odoo.http import Controller, request

# Re-export helpers that are imported from outside this package.
from ._data_load._helpers import (  # noqa: F401
    _BROWSE_PATH_RE,
    _LIST_MAX,
    _SECRET_FIELDS,
    _TAG_MODEL_BY_SCOPE,
    _capped_search,
    _gh_seg,
    _has_pat,
    _is_safe_remote_path,
    _job_response,
    _normalize_domain,
    _parse_github_repo_path,
    _quote_remote_path,
)
from ._data_load._routes_backends import BackendsMixin
from ._data_load._routes_crud import CrudMixin
from ._data_load._routes_github import GitHubMixin
from ._data_load._routes_ops import OpsMixin


class CloudDataLoadController(
    CrudMixin,
    GitHubMixin,
    OpsMixin,
    BackendsMixin,
    Controller,
):
    """Entry-point controller for the /cloud/* SPA endpoints.

    Each mixin contributes a set of domain-specific ``@http.route``
    handlers. ``_sec()`` below is the single permission-check helper
    used by every route.
    """

    def _sec(self):
        return request.env['cloud.security.mixin']
