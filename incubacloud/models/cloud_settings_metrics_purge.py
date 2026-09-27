"""Delete the stored series of instances and hosts that no longer exist.

Nothing deleted series before this: an instance removed from the panel
kept its series on the central until the global retention (90 days by
default) dropped them, which is disk spent on things that are gone — and
stagings and PR previews come and go all the time.

**The selector always names the account.** ``instance_id`` and
``host_id`` are row ids of the panel that owns them, and several panels
can share one central, so ``instance_id="5"`` exists in many accounts at
once. A deletion by id alone would take the instance 5 of every one of
them. :func:`series_selector` is the only thing that builds a selector,
and it refuses to build one without an account.

**Deferred, not immediate.** Measured in production on 2026-09-19: the
series of a removed instance kept receiving samples for three minutes
after the record was gone, because the per-instance disk collector
rewrites its file every ten minutes and node_exporter serves the old one
until then. And measured in the lab (2026-09-27): a sample that arrives
after the deletion recreates the series. So the deletion runs as a
queued job, :data:`PURGE_DELAY_MINUTES` after the removal. Queuing it
inside the removal's own transaction also means a rollback drops it:
the series of a record that still exists are never touched.

**Best effort.** A deletion that fails leaves series that age out with
the retention anyway, so it raises a warning rather than failing
anything. Without the warning it would be a function that can be broken
for months without anyone knowing.

Only the panel that owns the central can delete: the operator
credential and VictoriaMetrics' deletion key live there alone. Anywhere
else the hooks here do nothing, and a layer above can send the request
to whoever does own it.
"""
import logging
import re
from datetime import timedelta

import requests

from odoo import models

_logger = logging.getLogger(__name__)

#: Minutes between a removal and the deletion of its series. Covers the
#: disk collector's ten-minute rewrite, the agent's label refresh queued
#: by the removal, and the agent's own send buffer.
PURGE_DELAY_MINUTES = 30

#: Alert raised when a deletion could not be carried out.
PURGE_FAILED_CODE = "metrics_purge_failed"

#: The operator's deletion route on the central's gateway. The gateway
#: adds VictoriaMetrics' key; the panel never sends it.
_DELETE_ROUTE = "/admin-d/api/v1/admin/tsdb/delete_series"

#: ``metrics_central_url`` of a central this panel deployed ends in the
#: account read prefix. Anything else is a backend that is not ours.
_READ_SUFFIX = "/r"

_TIMEOUT = 15

#: What a label value may contain here. Accounts are ``acct_<hex>`` and
#: ids are integers, so this refuses nothing real — and it makes quoting
#: inside the selector a non-question.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_-]+$")


def series_selector(account, instance_id=None, host_id=None, whole_account=False):
    """Return the series selector for one deletion, always with the account.

    Exactly one target must be named:

    * ``instance_id`` — every series of that instance;
    * ``host_id`` — that host's own series, NOT those of instances it
      carried (``instance_id=""`` matches only series without one): an
      instance that moved to another host keeps its history;
    * ``whole_account=True`` — every series of the account.

    :param account: the ``ic_account`` label value. Required.
    :param instance_id: a ``cloud.instance`` id of that account's panel.
    :param host_id: a ``cloud.host`` id of that account's panel.
    :param whole_account: select the account's every series.
    :return: the selector, e.g. ``{ic_account="acct_1",instance_id="5"}``.
    :raises ValueError: without an account, with a malformed value, or
        unless exactly one target is named.
    """
    if not isinstance(account, str) or not _SAFE_VALUE.match(account):
        raise ValueError(
            f"Refusing a series selector without a valid account: {account!r}"
        )
    targets = [
        t for t in (instance_id is not None, host_id is not None, whole_account)
        if t
    ]
    if len(targets) != 1:
        raise ValueError(
            "A series selector names exactly one of instance_id, host_id "
            "or the whole account."
        )
    for value in (instance_id, host_id):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"Not a record id: {value!r}")
    if instance_id is not None:
        return f'{{ic_account="{account}",instance_id="{instance_id}"}}'
    if host_id is not None:
        return f'{{ic_account="{account}",host_id="{host_id}",instance_id=""}}'
    return f'{{ic_account="{account}"}}'


class CloudSettings(models.Model):
    _inherit = "cloud.settings"

    def _metrics_purge_endpoint(self):
        """Return ``(url, (user, password))`` to delete through, or None.

        None whenever this panel cannot delete: observability off, no
        central of ours (``metrics_central_url`` not ending in the account
        read prefix), no operator credential, or a central deployed
        before deletions existed (no deletion key yet). Read only — none
        of these is minted here, because minting would describe a central
        that does not exist.

        :return: the deletion URL and the operator credential, or None.
        """
        settings = self.sudo()._get_system()
        if not settings.metrics_enabled:
            return None
        if not (settings.metrics_operator_token and settings.metrics_delete_auth_key):
            return None
        base = (settings.metrics_central_url or "").strip().rstrip("/")
        if not base.endswith(_READ_SUFFIX):
            return None
        root = base[: -len(_READ_SUFFIX)]
        return f"{root}{_DELETE_ROUTE}", settings._metrics_auth(operator=True)

    def _purge_instance_metrics(self, instance_ids):
        """Delete, later, the series of instances this panel just removed.

        Called by ``cloud.instance.unlink`` inside the removal's own
        transaction. Downstream layers where the central belongs to
        someone else override it to send the request there instead.

        :param instance_ids: ids of the removed ``cloud.instance`` rows.
        """
        account = self.sudo()._get_system().metrics_account
        self._enqueue_metrics_purge(account, instance_ids=instance_ids)

    def _purge_host_metrics(self, host_ids):
        """Delete, later, the series of hosts this panel just retired.

        Same contract as :meth:`_purge_instance_metrics`, for hosts: a
        host leaves the fleet by being archived, so this is called on
        that transition and on the unlink of a host still active.

        :param host_ids: ids of the retired ``cloud.host`` rows.
        """
        account = self.sudo()._get_system().metrics_account
        self._enqueue_metrics_purge(account, host_ids=host_ids)

    def _enqueue_metrics_purge(
        self, account, instance_ids=(), host_ids=(), whole_account=False,
    ):
        """Queue the deletion of *account*'s series for the given targets.

        No-op when this panel cannot delete, or when there is nothing to
        name. Never raises: it runs inside removals that must not fail
        because monitoring could not tidy up after them.

        :param account: the ``ic_account`` the series belong to.
        :param instance_ids: instance ids of that account's panel.
        :param host_ids: host ids of that account's panel.
        :param whole_account: delete every series of the account.
        :return: True if a job was queued.
        """
        if not account or not self._metrics_purge_endpoint():
            return False
        try:
            selectors = [
                series_selector(account, instance_id=i) for i in instance_ids
            ] + [
                series_selector(account, host_id=h) for h in host_ids
            ]
            if whole_account:
                selectors.append(series_selector(account, whole_account=True))
        except ValueError:
            _logger.exception(
                "[metrics] not purging series of %s: bad selector", account,
            )
            return False
        if not selectors:
            return False
        self.sudo()._get_system().with_delay(
            eta=timedelta(minutes=PURGE_DELAY_MINUTES),
            channel="root.bg",
            description=f"Delete {len(selectors)} metrics selector(s)",
        )._job_purge_metrics(selectors)
        return True

    def _job_purge_metrics(self, selectors):
        """Delete the series matching *selectors* on the central (queued).

        One request: VictoriaMetrics deletes what matches ANY ``match[]``
        (measured, lab 2026-09-27). The endpoint is resolved now, not
        when queued, so a key rotated in between is the one used.

        :param selectors: selectors built by :func:`series_selector`.
        """
        endpoint = self.sudo()._get_system()._metrics_purge_endpoint()
        if not endpoint:
            _logger.info(
                "[metrics] purge of %s selector(s) skipped: this panel can "
                "no longer delete on the central.", len(selectors),
            )
            return
        url, auth = endpoint
        Alert = self.env["cloud.alert"].sudo()
        try:
            response = requests.post(
                url,
                data=[("match[]", selector) for selector in selectors],
                auth=auth,
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            _logger.warning(
                "[metrics] could not delete %s series selector(s): %s",
                len(selectors), exc,
            )
            Alert.raise_alert(
                PURGE_FAILED_CODE,
                "The series of removed instances or hosts could not be "
                "deleted from the metrics central. They will expire with "
                "the retention period instead; see the server log.",
                level="warning",
            )
            return
        _logger.info("[metrics] deleted series for %s selector(s)", len(selectors))
        Alert.resolve_alert(PURGE_FAILED_CODE)
