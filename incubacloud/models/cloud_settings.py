import logging
import os
import re
import uuid

from psycopg2 import sql

from odoo import _, api, fields, models, tools
from odoo import cli as odoo_cli
from odoo.exceptions import UserError, ValidationError
from odoo.service import server as odoo_server
from odoo.tools import clean_context

from ..net.trusted_proxies import invalid_ranges, parse_ranges
from ._job_channels import channel_problems, configured_channels
from ._job_server_limits import (
    JOB_LIMITS_ALERT_CODE,
    attempt_budget_seconds,
    job_server_limit_problems,
)
from .cloud_instance import AUTOPURGE_FINAL_DAYS, AUTOPURGE_WARN_DAYS
from .cloud_instance_domain import _HOSTNAME_RE
from .encrypted_char import (
    ALERT_CODE_UNREADABLE,
    EncryptedChar,
    EncryptedFieldMixin,
)
from .password_utils import (
    decrypt_value,
    generate_password,
    is_on_primary_key,
    key_is_configured,
    rotate_value,
)
from .res_users_ext import as_platform

_logger = logging.getLogger(__name__)

#: Container log rotation shipped in every instance's compose override.
#: Same figures as the ``daemon.json`` written by ``host_hardening``, so
#: a stack rotates identically whether or not its host was hardened.
CONTAINER_LOG_MAX_SIZE_DEFAULT = "10m"
CONTAINER_LOG_MAX_FILE_DEFAULT = 3
#: Days of Odoo's own log kept on the host as a dated archive
#: (``logs/odoo.log.<date>.gz``), the window an operator can still open
#: after noticing something days later.
ODOO_LOG_ARCHIVE_DAYS_DEFAULT = 60
#: Cost bounds for reading the archive back through the panel. They
#: guard someone else's host, so the operator owns the numbers rather
#: than discovering them baked into a release.
LOG_DOWNLOAD_MAX_MB_DEFAULT = 64
LOG_SEARCH_MAX_FILES_DEFAULT = 60
LOG_SEARCH_TIMEOUT_S_DEFAULT = 30

# Image used by the ephemeral container that empties an archived
# instance's backup prefix. The same image the doodba template renders
# for the ``backup`` service, because it is what the credentials and the
# destination were written for; the purge itself only needs boto3.
ARCHIVED_PURGE_IMAGE_DEFAULT = (
    "ghcr.io/tecnativa/docker-duplicity-postgres-s3:latest"
)
#: Docker's ``max-size`` grammar, restricted to one canonical spelling
#: (lower-case unit) so the override reads the same on every host.
_CONTAINER_LOG_MAX_SIZE_RE = re.compile(r"^[1-9]\d*[kmg]$")


class CloudSettings(models.Model):
    """System-wide IncubaCloud settings — singleton.

    Stores sensitive credentials that must not live in
    ``ir.config_parameter`` (plain text in the database).
    Fields use ``EncryptedChar`` (Fernet) so the DB column
    always holds ``enc:<token>``.
    """

    _name = "cloud.settings"
    _inherit = ["cloud.security.mixin"]
    _description = "IncubaCloud Settings"

    # ── GitHub credentials ─────────────────────────────────────────────────

    github_pat = EncryptedChar(
        string="GitHub Personal Access Token",
        groups="incubacloud.group_cloud_manager",
        help=(
            "PAT used as fallback when no GitHub App is configured. "
            "Stored encrypted — never exposed in logs or API responses."
        ),
    )

    # ── Job log retention ──────────────────────────────────────────────────
    # cloud.job.log.chunk grows monotonically (every SSH command stream
    # writes hundreds of rows per job). The daily purge cron deletes
    # chunks older than this many days that belong to terminal jobs.
    # Active jobs always keep their full log regardless of age.

    job_log_retention_days = fields.Integer(
        string='Job log retention (days)',
        default=30,
        help='Number of days to keep job log chunks (cloud.job.log.chunk) '
             'for terminated jobs. Active jobs are never purged. Set to 0 '
             'to disable purging entirely (table will grow unbounded).',
    )

    # cloud.job rows themselves also grow monotonically (the fourth leak
    # of the retention family). They feed the instance timeline in the
    # UI, so the window is much longer than the chunk one: purging a job
    # trims visible history to this many days.
    job_retention_days = fields.Integer(
        string='Job retention (days)',
        default=180,
        help='Number of days to keep terminal cloud.job rows (done, '
             'failed, cancelled). Active jobs are never purged. Purging '
             'a job also removes its remaining log chunks and trims the '
             'instance timeline to this window. Set to 0 to disable '
             'purging entirely (table will grow unbounded).',
    )

    # ── Moves between hosts ───────────────────────────────────────────────

    move_frozen_source_hours = fields.Integer(
        string='Keep the old copy after a move (hours)',
        default=48,
        help='After a move, the copy left on the old host is stopped and '
             'kept, data included, for this many hours, so it can still '
             'be restored from by hand; then it is removed. 0 removes it '
             'at the next hourly pass. It uses disk on the old host until '
             'then.',
    )
    move_cut_alert_seconds = fields.Integer(
        string='Move cut alert (seconds)',
        default=90,
        help='A move that keeps the instance unavailable longer than this, '
             'from the stop of its source to its destination answering, '
             'raises a warning. 0 never raises it.',
    )

    # A staging nobody uses still costs disk on the customer's host,
    # still generates probes and series, and still holds a copy of
    # production data. Nothing pushes anyone to delete it once stagings
    # stopped counting against any allowance, so the window does.
    staging_autopurge_days = fields.Integer(
        string='Staging autopurge (days)',
        default=90,
        help='Delete a staging instance after this many days without '
             'anyone using it — no action on it from the panel and no '
             'login inside it. Two warnings go out first, 14 and 3 days '
             'ahead, and one click on Keep starts the window over. '
             'Production instances are never touched. Set to 0 to '
             'disable the autopurge entirely.',
    )

    # ── GitHub event retention ────────────────────────────────────────────
    # cloud.github.event stores the raw webhook JSON. Push payloads for
    # large monorepos are 100 KB+. Keeping them forever bloats backups
    # and keeps commit metadata (authors, messages, diff refs) around
    # beyond any useful audit window. Two-stage retention:
    #   * After ``github_event_truncate_days``: payload is replaced by a
    #     compact stub (event_type, ref, sha, action, original_size).
    #     Event row is preserved for audit; only the blob is shed.
    #   * After ``github_event_retention_days``: the row itself is
    #     unlinked. Only rows with ``processed=True`` are touched —
    #     unprocessed/error events remain until an operator investigates.

    github_event_retention_days = fields.Integer(
        string='GitHub event retention (days)',
        default=90,
        help='Number of days to keep processed cloud.github.event rows. '
             'Unprocessed/error rows are never auto-deleted. Set to 0 to '
             'disable deletion entirely.',
    )
    gitlab_event_retention_days = fields.Integer(
        string='GitLab event retention (days)',
        default=30,
        help='Number of days to keep processed cloud.gitlab.event rows; the '
             'body is never stored, so nothing is truncated first. '
             'Unprocessed/error rows are never auto-deleted. Set to 0 to '
             'disable deletion entirely.',
    )
    github_webhook_silence_hours = fields.Integer(
        string='GitHub webhook silence alert (hours)',
        default=48,
        help='Raise an alert when no webhook delivery has been recorded '
             'for this many hours while a GitHub App is configured. An '
             'edge allowlist that has fallen behind GitHub\'s published '
             'ranges fails by dropping deliveries silently, so the only '
             'way to notice is to watch for the silence itself. Set to 0 '
             'to disable the check.',
    )
    trusted_proxy_ranges = fields.Text(
        string='Trusted proxy ranges',
        help="CIDR ranges of the proxies in front of this installation, "
             "one per line. Only when a request arrives from one of them "
             "is its X-Forwarded-For header believed, so this is what "
             "makes every per-client control here — rate limits, "
             "allowlists, audit lines — key on the real caller instead of "
             "the proxy. Leave empty when nothing fronts this "
             "installation: the connecting address is then used as-is, "
             "which is over-restrictive but never spoofable.",
    )

    github_event_truncate_days = fields.Integer(
        string='GitHub event payload truncation (days)',
        default=7,
        help='After this many days, the raw JSON payload of a processed '
             'cloud.github.event is replaced by a compact stub (event '
             'type, ref, sha, action). Set to 0 to disable truncation.',
    )

    # ── Doodba template used to scaffold instances ────────────────────────
    # Every deploy runs ``copier copy`` against this template. Left
    # unpinned, each deploy silently picks up whatever is on the
    # template's default branch at that moment, so two instances created
    # a week apart can be built from different upstream revisions and a
    # breaking change upstream lands on the next instance deploy with no
    # signal here. Pinning a tag makes template upgrades a decision.

    copier_template_url = fields.Char(
        string='Doodba template',
        default='gh:Tecnativa/doodba-copier-template',
        help='Copier source for new instances. Change it to deploy from a '
             'fork of the doodba template.',
    )
    # Default pin: v10.0.8, rendered on 2026-10-20 in a sandbox with the
    # answers the deploy executor emits (the first with Odoo 20; its
    # stagings close their egress with a gatekeeper). Bump via the
    # runbook (RB-15): sandbox render first, then staging canary.
    copier_template_ref = fields.Char(
        string='Doodba template version',
        default='v10.0.8',
        help='Git tag/branch/commit to pin the template to (copier '
             "--vcs-ref), e.g. 'v10.0.8'. Empty means the template's "
             'default branch: deploys then track upstream automatically '
             'and an upstream change can alter the next deploy without '
             'warning. The effective ref is recorded in the job log.',
    )

    # ── Archived instances ─────────────────────────────────────────────────
    # An archived instance has no stack left to purge its chain from, so
    # the deletion runs one throwaway container on the host. The image is
    # a setting rather than a constant because the template can move to
    # another tag and this must be able to follow without a code deploy —
    # and because the day it is wrong, the symptom is a purge that cannot
    # start and a record that refuses to be deleted.

    archived_purge_image = fields.Char(
        string="Archived purge image",
        default=ARCHIVED_PURGE_IMAGE_DEFAULT,
        required=True,
        help="Container image run once to empty an archived instance's "
             "backup prefix. Must have boto3 available; the duplicity "
             "image the backup service uses already does.",
    )

    # ── Container log rotation ─────────────────────────────────────────────
    # Emitted per service in the docker-compose.override.yml every deploy
    # and rebuild writes (``_resource_override_content``). doodba logs
    # to stdout and the template sets no ``logging:``, so without this a
    # host that never ran host_hardening keeps json-file logs until the
    # disk is full. Global on purpose: read at render time, outside the
    # per-instance config snapshot, so changing it never marks the fleet
    # as drifted — it lands on each instance's next rebuild.

    container_log_max_size = fields.Char(
        string="Container log max size",
        default=CONTAINER_LOG_MAX_SIZE_DEFAULT,
        required=True,
        help="Size at which Docker rotates each container's log file, as "
             "a positive integer followed by k, m or g (e.g. 10m). Applies "
             "to every service of every instance from its next deploy or "
             "rebuild.",
    )
    container_log_max_file = fields.Integer(
        string="Container log files kept",
        default=CONTAINER_LOG_MAX_FILE_DEFAULT,
        required=True,
        help="Number of rotated log files Docker keeps per container "
             "(the current one included). Older files are deleted; the "
             "retained history is at most size x files per container.",
    )
    log_download_max_mb = fields.Integer(
        string="Log download cap (MB)",
        default=LOG_DOWNLOAD_MAX_MB_DEFAULT,
        required=True,
        help="Largest compressed log download the panel will serve. A "
             "day whose rotation stalled can be gigabytes, and the "
             "whole answer is held in memory while it is sent.",
    )
    log_search_max_files = fields.Integer(
        string="Log search: days swept",
        default=LOG_SEARCH_MAX_FILES_DEFAULT,
        required=True,
        help="How many archived days a cross-day log search reads, "
             "newest first. Each one is decompressed on the instance's "
             "host, so this is a bound on that host's CPU.",
    )
    log_search_timeout_s = fields.Integer(
        string="Log search timeout (s)",
        default=LOG_SEARCH_TIMEOUT_S_DEFAULT,
        required=True,
        help="Seconds a cross-day log search may run on the host "
             "before it is cut short. A search that is cut short says "
             "so instead of reporting no matches.",
    )
    odoo_log_archive_days = fields.Integer(
        string="Odoo log archive (days)",
        default=ODOO_LOG_ARCHIVE_DAYS_DEFAULT,
        required=True,
        help="Days of Odoo's own log kept per instance on its host, as "
             "one dated file per day (compressed from the day before "
             "yesterday). Applied by logrotate on the host; a change "
             "reaches an instance on its next deploy or rebuild.",
    )

    # ── Rate limiting caps ────────────────────────────────────────────────
    # Read by ``cloud.rate.limit._get_cap`` when throttling the two
    # abuse-prone public/auth'd endpoints. Values ending in ``per_min``
    # use minute windows; GitHub import values use hour windows. Setting
    # any of them to 0 falls back to the
    # documented default so a misconfigured 0 can't accidentally
    # lock every request out.

    rate_limit_webhook_per_min = fields.Integer(
        string='GitHub webhook (per IP, per minute)',
        default=300,
        help='Max requests/min the ``/cloud/github/webhook`` endpoint '
             'accepts from a single client IP. GitHub can burst heavily '
             'on a push to a monorepo with many workflow triggers, so '
             'the default is generous; lower it if you see abuse. Set '
             'to 0 to fall back to the documented default (300).',
    )
    rate_limit_terminal_per_min = fields.Integer(
        string='Terminal open (per instance, per minute)',
        default=30,
        help='Max ``/cloud/terminal/open`` calls/min targeting the same '
             'cloud.instance, across all users. Protects the remote '
             'host from a multi-operator debug storm.',
    )
    rate_limit_terminal_user_per_min = fields.Integer(
        string='Terminal open (per user, per minute)',
        default=10,
        help='Max ``/cloud/terminal/open`` calls/min from a single '
             'internal user. Caps both legitimate human operators and '
             'any compromised account running a script. Each opened '
             'session holds an SSH connection + PTY buffer until '
             'SESSION_TIMEOUT.',
    )
    rate_limit_host_console_per_min = fields.Integer(
        string='Host console sessions (req/min)',
        default=3,
        help='Maximum host console sessions per minute, per (user) '
             'and per (host). Defaults to a low value because every '
             'open session is a root-equivalent shell on the host.',
    )
    rate_limit_connect_per_min = fields.Integer(
        string='Connect as user (per instance, per minute)',
        default=20,
        help='Max connect-as calls/min targeting the same cloud.instance, '
             'across all users. Each call opens an SSH connection and runs '
             'a script inside the instance container.',
    )
    rate_limit_logs_per_min = fields.Integer(
        string="Log reads per minute (per user)",
        default=60,
        help="Cap on log reads per user: the live tail, the day "
             "listing and reading one archived day. The viewer polls "
             "the live tail every 4 seconds, so a cap below ~15 breaks "
             "a single open viewer. 0 falls back to the default.",
    )
    rate_limit_log_search_per_min = fields.Integer(
        string="Log searches per minute (per user)",
        default=6,
        help="Cap on cross-day log searches and log downloads per user. "
             "A search decompresses up to the configured number of days "
             "on the instance's host; a download ships a whole day "
             "through the panel. 0 falls back to the default.",
    )
    rate_limit_mail_reads_per_min = fields.Integer(
        string="Mail reads per minute (per user)",
        default=30,
        help="Cap on reads of a staging's captured mailbox per user: "
             "the listing, one message and emptying the box. Each one "
             "costs a docker exec plus an HTTP call inside the "
             "instance's stack, which is dearer than a log tail. "
             "0 falls back to the default.",
    )
    rate_limit_connect_user_per_min = fields.Integer(
        string='Connect as user (per user, per minute)',
        default=10,
        help='Max connect-as calls/min from a single panel user. Caps a '
             'compromised account from enumerating instance users or minting '
             'impersonation tokens in bulk.',
    )
    rate_limit_github_previews_per_hour = fields.Integer(
        string='GitHub import previews (per user, per hour)',
        default=10,
        help='Max bounded GitHub preview operations per panel user and hour. '
             'A busy global import lock is rejected before consuming this '
             'quota. 0 falls back to the default (10).',
    )
    rate_limit_github_imports_per_hour = fields.Integer(
        string='GitHub project imports (per user, per hour)',
        default=5,
        help='Max bounded GitHub project imports per panel user and hour. '
             'An admitted attempt counts even if GitHub or parsing fails '
             'later. 0 falls back to the default (5).',
    )
    rate_limit_git_reads_per_hour = fields.Integer(
        string='Git provider reads (per user, per hour)',
        default=120,
        help='Max reads of a Git provider repository (branches, head, '
             'modules, requirements) per panel user and hour. Each one '
             'runs git on the panel. 0 falls back to the default (120).',
    )

    # ── Backup usage alert default ────────────────────────────────────────
    # Per-backend ``alert_threshold_pct`` can opt in (1–100), opt out
    # (-1 = disabled), or inherit (0 = use this default). This system-wide
    # default keeps the per-backend form short for the common case while
    # still letting an operator dial it per backend when needed.

    default_backup_alert_threshold_pct = fields.Integer(
        string='Default backup alert threshold (%)',
        default=80,
        help='Default percentage at which an email alert is sent when a '
             "backup backend's usage approaches its quota. Per-backend "
             "overrides win — set the backend's own threshold to 0 to "
             'inherit this value, -1 to disable, or 1–100 for an explicit '
             'value.',
    )

    # ── Observability ──────────────────────────────────────────────────────
    # Agents push metrics outbound to a central VictoriaMetrics; the panel
    # reads it back over PromQL to evaluate ``cloud.metric.rule``. All of
    # it is inert until ``metrics_enabled`` is set, so the feature can ship
    # and be deployed before anyone turns it on.

    metrics_enabled = fields.Boolean(
        string='Enable observability',
        default=False,
        help='Master switch. When off, no agent is installed, the metric '
             'rules are not evaluated and no metrics alert is raised.',
    )
    metrics_central_url = fields.Char(
        string='Metrics backend URL',
        help='Base URL the PANEL uses to query metrics (PromQL). It must '
             'resolve from inside the panel container — e.g. '
             'http://host.docker.internal:8428 for a co-located backend, '
             'or its public URL when it lives on another server.',
    )
    metrics_remote_write_url = fields.Char(
        string='Remote-write URL',
        help='Endpoint each host AGENT pushes to. It must resolve from '
             'inside the agent container on every host — so 127.0.0.1 is '
             'almost never right: that is the agent itself. Use '
             'http://host.docker.internal:8428/api/v1/write only for '
             'agents on the same server as the backend, and a public '
             'HTTPS URL for every other host. Outbound only: no inbound '
             'port is opened, which is what keeps BYOH and NAT hosts '
             'working.',
    )
    metrics_account = fields.Char(
        string='Metrics account',
        readonly=True,
        copy=False,
        help='Identity this panel authenticates as against the central, '
             'and the label the central FORCES on every series it writes '
             'for us. It is what keeps one panel from reading or poisoning '
             "another's data, so it is never accepted from the agent: the "
             'central derives it from the authenticated user. A '
             'panel generates its own, unless a module that extends the '
             'panel assigns it.',
    )
    # Where the central runs, and who it currently lets in. Both are
    # written by the deployment from what it actually did, never from
    # intent: they exist precisely so that a later job can tell the state
    # of the gateway apart from the state of this database.
    metrics_central_host_id = fields.Many2one(
        'cloud.host',
        string='Metrics central host',
        readonly=True,
        copy=False,
        # The host holds the metrics history and the security logs: it
        # cannot leave the fleet while the central runs on it
        # (``cloud.host._check_not_metrics_central``); this is the last
        # line under that check.
        ondelete='restrict',
        help='Host carrying the central stack, recorded when a '
             'deployment of it succeeds. The account sync targets it. '
             'Empty means no central was ever deployed from this panel, '
             'or it was retired, and accounts cannot be granted until one '
             'is deployed.',
    )
    metrics_accounts_deployed = fields.Text(
        string='Accounts on the central',
        readonly=True,
        copy=False,
        help='Metrics accounts the last successful deployment or sync '
             "wrote into the gateway's access-control list, one per "
             'line. An account minted but absent from here cannot '
             'authenticate yet, so handing out its credential would '
             'produce a permanent 401 rather than observability.',
    )
    def _desired_metrics_accounts(self):
        """Return ``[(user, token), ...]`` that should exist on the central.

        The single source of the access-control list. Core knows exactly
        one account: this panel's own. A layer that serves other panels
        overrides this to append one entry per panel, because only it
        knows they exist.

        Deliberately on the model rather than on the executor that writes
        the list: the reconciliation has to compare the same set without
        being an executor, and two places assembling "who should have
        access" is precisely how the gateway and the database drifted
        apart in the first place.
        """
        user, token = self._get_system()._metrics_auth()
        return [(user, token)] if (user and token) else []

    def _metrics_accounts_shipped(self):
        """React to a new access-control list being in force on the gateway.

        Called by the central's deployment and by the account sync right
        after they record ``metrics_accounts_deployed``, on the job's
        durable cursor. Core's one account is handed out by that same
        deployment, so core has nothing left to do here. A layer that
        serves other panels overrides it to give the panels whose accounts were granted just
        now their settings, rather than leaving them to its next
        reconciliation.
        """

    @api.model
    def _live_central_host(self):
        """Return the host the metrics central runs on, or an empty record.

        The host the last good deployment recorded, while it is still in
        the fleet. A central recorded on an archived host (before 1.0.228
        nothing stopped archiving it) counts as none: there is no host
        left to protect or to deploy beside.

        :return: a ``cloud.host`` record, empty when there is no central
        """
        host = self._get_system().metrics_central_host_id.sudo()
        return host if host and host.active else self.env["cloud.host"]

    @api.model
    def _central_host_refusal(self, host):
        """Say why *host* cannot be deleted or archived, and the ways out.

        One wording for the model, the route and the job, so the person
        reads the same whichever of them refused.

        :param host: the ``cloud.host`` the central runs on
        :return: the translated message
        :rtype: str
        """
        return _(
            '"%(host)s" runs the metrics central, with the metrics history '
            "and the security logs. Before deleting it, go to Settings › "
            "Monitoring › Where the central runs and either start an empty "
            "central on another server or retire the central. Either way "
            "the history stays behind on this server; to keep it, copy the "
            'central\'s data first (operations guide, "Where the central '
            'runs").',
            host=host.name,
        )

    @api.model
    def _central_elsewhere_refusal(self, central):
        """Say why the central cannot simply be deployed on another host.

        :param central: the ``cloud.host`` the central runs on now
        :return: the translated message
        :rtype: str
        """
        return _(
            'The central runs on "%(host)s", with the metrics history and '
            "the security logs. Deploying it on another server would start "
            "an empty one there and leave the history behind: if that is "
            "what you want, use Start an empty central.",
            host=central.name,
        )

    @api.model
    def _central_retire_refusal(self):
        """Say why the central may not be retired from this panel, or ''.

        Core: it may. A layer whose central serves more than this panel
        (the platform's master, whose central every tenant writes to)
        answers why not; the settings then offer no *Retire the central*.

        :rtype: str
        """
        return ""

    def retire_metrics_central(self):
        """Forget the metrics central, its history with it.

        The way out for a central whose server is lost, or for a panel
        whose only server is going away: the panel stops recording a
        host, stops reading and switches observability off, so the host
        can be deleted. Nothing is done on the server: the central's
        stack and its data stay there until the server goes. Agents
        already installed keep sending to the old address until
        observability is enabled again on another host.

        Refused while a deployment or an account sync of the central
        waits or runs: it would record the host again when it ends; and
        where a layer refuses it (``_central_retire_refusal``).

        :return: the name of the host the central ran on
        :rtype: str
        :raises UserError: when there is no central to retire, a job of
            the central is still to end, or this panel may not retire it
        """
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        refusal = self._central_retire_refusal()
        if refusal:
            raise UserError(refusal)
        settings = self._get_system()
        host = settings.metrics_central_host_id.sudo()
        if not host:
            raise UserError(_("There is no metrics central to retire."))
        pending = self.env["cloud.job"].sudo().search_count([
            ("host_id", "=", host.id),
            ("job_type_id.code", "in", (
                "deploy_metrics_central", "sync_metrics_accounts",
            )),
            ("state", "in", (
                "queued", "pending", "enqueued", "wait_dependencies", "started",
            )),
        ])
        if pending:
            raise UserError(_(
                'A job of the central waits or runs on "%(host)s": wait for '
                "it to end, or cancel it, and retire the central then.",
                host=host.name,
            ))
        return self._forget_metrics_central()

    @api.model
    def _forget_metrics_central(self):
        """Forget the metrics central and what the panel said about it.

        What *Retire the central* does once its checks passed, and what a
        layer calls when the central's server leaves the fleet without a
        person asking (a forced removal). Besides the host and the URLs:
        the public name, so that a central deployed later on another host
        does not publish itself under a name whose DNS still points at
        this one; the list of accounts on the central, which is the old
        central's; and the central's open alerts, which nothing would
        close any more. The direct name stays: it is probed before it is
        used.

        :return: the name of the host the central ran on, '' when there
            was none
        :rtype: str
        """
        settings = self._get_system()
        host = settings.metrics_central_host_id.sudo()
        if not host:
            return ""
        # The key its alerts were raised with (``_direct_alerts``).
        direct = settings.metrics_central_direct_host or ""
        settings.write({
            "metrics_central_host_id": False,
            "metrics_central_url": False,
            "metrics_remote_write_url": False,
            "metrics_enabled": False,
            "metrics_central_public_host": False,
            "metrics_accounts_deployed": False,
        })
        Alert = self.env["cloud.alert"].sudo()
        for code in ("metrics_central_failed", "metrics_acl_sync_failed"):
            Alert.resolve_alert(code, host=host)
        if direct:
            settings._direct_forget_alerts(direct)
        self.env["cloud.audit.log"].sudo().create({
            "action": "Metrics central retired",
            "details": host.name,
        })
        return host.name

    # The central's credentials are for system administrators only: the
    # panel's code reads and mints them elevated (``_get_system``), and the
    # settings page only says whether the account's token is set. Without
    # a group every Manager read all four through ``/web/dataset/call_kw``.
    metrics_remote_write_token = EncryptedChar(
        string='Metrics credential',
        groups='base.group_system',
        help='Password half of this panel\'s metrics account (the user '
             'half is ``metrics_account``). Presented as HTTP basic auth '
             'both when the agents push and when the panel queries. '
             'Scoped: it can only write series labelled with this account '
             'and only read those same series, so a host that leaks it '
             'exposes nothing beyond what that panel could already see. '
             'Stored encrypted; written to each host as root-only 0600.',
    )
    metrics_operator_token = EncryptedChar(
        string='Operator read credential',
        groups='base.group_system',
        help='Second credential, for the UNFILTERED read of every '
             "account's series (the fleet's board) and for deleting "
             'series. Only meaningful on the panel that owns the central, '
             'and deliberately never written to any host — that is the '
             'whole point of keeping it apart from the account credential '
             'above.',
    )
    metrics_delete_auth_key = EncryptedChar(
        string='Metrics deletion key',
        groups='base.group_system',
        help="VictoriaMetrics' own key for deleting series. The central "
             'refuses every deletion that does not carry it, and the '
             'gateway adds it on the operator\'s deletion route only, so '
             'no account can delete anything — not even its own series. '
             'Minted by the central deployment; empty means the central '
             'was deployed before deletions existed and cannot delete yet.',
    )
    metrics_retention_days = fields.Integer(
        string='Metrics retention (days)',
        default=90,
        help='How long the central keeps series. Passed to the central '
             'deployment playbook.',
    )
    metrics_collect_pg = fields.Boolean(
        string="Collect database figures",
        default=True,
        help="Every minute, each instance's database connections, running "
             "queries, lock waits and cache hits, read on its server by a "
             "small program run as root. Off removes it from every server "
             "on the agents' next install, which the reconciliation cron "
             "runs a few servers at a time.",
    )
    metrics_central_revision = fields.Integer(
        string='Central revision',
        default=0,
        readonly=True,
        copy=False,
        help='Which version of the central\'s deployment last ended well '
             'on its server. The reconciliation cron deploys the central '
             'again while it is behind the panel\'s.',
    )
    metrics_central_public_host = fields.Char(
        string='Public host name of the central',
        help='Host name under which the central publishes itself through '
             'the Traefik of the host it runs on, with a certificate, e.g. '
             'metrics.example.com; its DNS record must point at that host. '
             'The panel then reaches the central, and every agent writes '
             'to it, under that name. Empty: nothing is published, so only '
             "agents on the central's own server can write to it and the "
             'panel cannot read it from another server.',
    )
    metrics_central_operator_sources = fields.Text(
        string='Who may delete series and read the fleet on the central',
        help='Address ranges, one per line (e.g. 198.51.100.7/32), allowed '
             "to reach the central's series-deletion route under its "
             "public name: the panel's own public address. They may also "
             "read every account's series there, through the operator's "
             "read-only route. Empty: only the central's host itself, "
             'which is right while the panel runs there.',
    )

    # ── Metrics account helpers ────────────────────────────────────────────

    def _ensure_metrics_account(self):
        """Return this panel's metrics account, generating one if absent.

        The identifier must be unique across every panel that shares a
        central, so it is random rather than derived from anything local:
        two self-hosted panels would otherwise both call themselves
        ``acct_1``. Generated once and then stable — it is the label every
        historical series carries, and series cannot be relabelled after
        ingestion.

        :return: the account identifier, e.g. ``acct_9f2c1ab4d0e7``.
        """
        settings = self._get_system()
        if settings.metrics_account:
            return settings.metrics_account
        account = 'acct_%s' % uuid.uuid4().hex[:12]
        settings.sudo().write({'metrics_account': account})
        return account

    def _ensure_operator_credential(self):
        """Return the unfiltered read credential, generating it if absent.

        Deliberately separate from the account credential: this one grants
        a view across every account, so it must never be written to a
        host. Only the panel that owns the central holds it.

        :return: the plaintext operator password.
        """
        settings = self._get_system()
        if settings.metrics_operator_token:
            return settings.metrics_operator_token
        token = generate_password(32)
        settings.sudo().write({'metrics_operator_token': token})
        return token

    def _ensure_metrics_delete_key(self):
        """Return VictoriaMetrics' deletion key, generating it once.

        Only the central deployment calls this: the key reaches the
        VictoriaMetrics container through a file only that deployment
        writes, so it only exists on the central once a deployment has
        written it. The account sync reads the field without minting it
        for the same reason — a key the backend was never given would be
        a route that answers 401.

        The deployment mints it before its playbook runs, on a cursor
        that outlives the job. One that fails afterwards leaves a key the
        backend may not have yet: purges then fail with
        ``metrics_purge_failed`` until the next good deployment, which
        hands the backend this same key.

        :return: the plaintext key.
        """
        settings = self._get_system()
        if not settings.metrics_delete_auth_key:
            settings.sudo().write({
                'metrics_delete_auth_key': generate_password(32),
            })
        return settings.metrics_delete_auth_key

    def _ensure_metrics_credential(self):
        """Return ``(account, password)``, generating both if absent.

        Called when observability is switched on so the operator never
        has to invent either half. A panel that already has them keeps
        them: the account is the label on all of its historical series,
        and series cannot be relabelled after ingestion.
        """
        settings = self._get_system()
        account = self._ensure_metrics_account()
        if not settings.metrics_remote_write_token:
            settings.sudo().write({
                'metrics_remote_write_token': generate_password(32),
            })
        return account, settings.metrics_remote_write_token

    def _observability_capabilities(self):
        """Return what this panel can do with observability, as three axes.

        The single definition of "observability is configured", because
        four screens each deciding it for themselves is how a panel ended
        up offering a Monitoring section whose only possible message was
        an instruction to visit a Settings tab that panel does not have.
        They are three independent facts and collapsing them into one
        boolean is what made the contradiction expressible:

        ``collect``
            The data layer is on: agents enrol, rules evaluate, the panel
            queries the backend. Everything else is inert without it.
        ``dashboards``
            There is something to *look at* — collection plus a central
            to read the panel's own charts from. Collecting without a
            central URL is a valid state for a moment (the central is
            being deployed); offering charts in it is not.
        ``configure``
            This panel's own operator edits these settings. False where
            they are injected from above, which core never does itself:
            it publishes ``True`` and lets the layer that injects them
            say otherwise, exactly as it does for every other capability
            it cannot know about.

        Every UI surface consumes this and none re-derives it. A new
        surface that needs a different question answered adds an axis
        here rather than reading a field directly.

        :return: dict with the ``collect``, ``dashboards`` and
            ``configure`` booleans.
        """
        settings = self._get_system()
        collect = bool(settings.metrics_enabled)
        return {
            'collect': collect,
            'dashboards': collect and bool(
                (settings.metrics_central_url or '').strip()
            ),
            'configure': True,
        }

    def _metrics_auth(self, operator=False):
        """Return ``(user, password)`` for talking to the metrics central.

        Replaces the fixed ``incubacloud`` user that predated per-account
        credentials: the user half is now the account, because the central
        derives the forced label from whoever authenticated.

        :param operator: when True, return the unfiltered operator
            credential instead of this panel's own account. Only the panel
            that owns the central has one.
        :return: ``(user, password)``, or ``('', '')`` when unset — callers
            must treat that as "no credentials" and not as anonymous
            access being acceptable.
        """
        settings = self._get_system()
        if operator:
            token = settings.metrics_operator_token or ''
            return ('operator', token) if token else ('', '')
        token = settings.metrics_remote_write_token or ''
        account = settings.metrics_account or ''
        if not (token and account):
            return ('', '')
        return (account, token)

    # ── Container log rotation ─────────────────────────────────────────────

    @api.constrains(
        "container_log_max_size",
        "container_log_max_file",
        "odoo_log_archive_days",
        "log_download_max_mb",
        "log_search_max_files",
        "log_search_timeout_s",
    )
    def _check_container_log_rotation(self):
        """Refuse log-rotation values Docker would reject or ignore.

        A malformed ``max-size`` makes ``docker compose up`` fail on
        every instance at its next rebuild, and ``max-file`` below 1
        is not a valid json-file option — better to stop the write here
        than to discover it stack by stack.
        """
        for rec in self:
            size = rec.container_log_max_size or ""
            if not _CONTAINER_LOG_MAX_SIZE_RE.fullmatch(size):
                raise ValidationError(
                    _(
                        "Container log max size must be a positive integer "
                        "followed by k, m or g (for example 10m), got %s.",
                        size or "''",
                    )
                )
            if rec.container_log_max_file < 1:
                raise ValidationError(
                    _("Container log files kept must be at least 1.")
                )
            if rec.odoo_log_archive_days < 1:
                raise ValidationError(
                    _("The Odoo log archive must keep at least 1 day.")
                )
            if rec.log_download_max_mb < 1:
                raise ValidationError(
                    _("The log download cap must be at least 1 MB.")
                )
            if rec.log_search_max_files < 1:
                raise ValidationError(
                    _("A log search must sweep at least 1 day.")
                )
            if rec.log_search_timeout_s < 5:
                raise ValidationError(
                    _(
                        "The log search timeout must be at least 5 "
                        "seconds, or no search can finish."
                    )
                )

    # ── Singleton constraint ───────────────────────────────────────────────

    @api.constrains("github_pat")
    def _check_singleton(self):
        # Odoo 19 runs constraints as superuser; 18 as the writer.
        if self.env["cloud.settings"].sudo().search_count([]) > 1:
            raise UserError(
                _("Only one IncubaCloud settings record is allowed.")
            )

    def write(self, vals):
        """Have every server re-apply its agents when the collector's switch moves.

        The switch reaches a server only through the agents' install, so a
        change asks the reconciliation cron to run it again on every
        installed host, at its pace.
        """
        switched = "metrics_collect_pg" in vals and any(
            rec.metrics_collect_pg != bool(vals["metrics_collect_pg"])
            for rec in self
        )
        result = super().write(vals)
        if switched:
            self.env["cloud.host"]._reapply_observability_everywhere()
        return result

    # ── Helpers ────────────────────────────────────────────────────────────

    @api.model
    def _get(self):
        """Return the settings singleton — requires manager ACL.

        The model's ``ir.model.access`` is restricted to
        ``group_cloud_manager``. Use this accessor from controllers that
        are gated to manager (or higher) so the ACL acts as a
        defense-in-depth check on top of the role gate. For trusted
        non-manager flows (cron jobs, queue_job workers, providers,
        webhooks, public endpoints, or any inheriting module that
        legitimately needs settings access without a manager gate) use
        :meth:`_get_system` instead.
        """
        rec = self.search([], limit=1)
        if not rec:
            # Created with the fields' own defaults, none of the caller's.
            rec = self.with_context(clean_context(self.env.context)).create({})
        return rec

    @api.model
    def _get_system(self):
        """Return the settings singleton, bypassing ACL via sudo.

        Reserved for system contexts where the caller is *not* a manager
        but legitimately needs to read settings: cron jobs, queue
        workers, provider integrations, public webhooks, controllers
        exposed to portal users that read non-secret feature flags,
        and any inheriting module operating in a system context. Never
        use from an admin-write path — those must go through
        :meth:`_get` after a manager gate.
        """
        rec = self.sudo().search([], limit=1)
        if not rec:
            # Created with the fields' own defaults, none of the caller's.
            rec = self.sudo().with_context(
                clean_context(self.env.context),
            ).create({})
        return rec

    @api.constrains('staging_autopurge_days')
    def _check_staging_autopurge_days(self):
        """Refuse a window the warning ladder cannot fit inside.

        The two warnings are fixed at 14 and 3 days before the deadline.
        A window shorter than the first one would send both warnings on
        the day the instance was created — technically "never without
        notice", and useless as notice. The floor keeps the promise
        meaningful instead of merely literal.

        :raise ValidationError: when the window is negative, or positive
            but shorter than the first warning needs.
        """
        floor = AUTOPURGE_WARN_DAYS * 2
        for settings in self:
            days = settings.staging_autopurge_days
            if days == 0:
                continue
            if days < 0:
                raise ValidationError(_(
                    "The staging autopurge window cannot be negative. "
                    "Use 0 to turn the autopurge off."
                ))
            if days < floor:
                raise ValidationError(_(
                    "The staging autopurge window must be at least "
                    "%(floor)s days, so the warnings at %(warn)s and "
                    "%(final)s days before deletion still arrive in time "
                    "to act on. Use 0 to turn the autopurge off.",
                    floor=floor,
                    warn=AUTOPURGE_WARN_DAYS,
                    final=AUTOPURGE_FINAL_DAYS,
                ))

    @api.constrains('metrics_central_public_host')
    def _check_metrics_central_public_host(self):
        """Refuse a public name that is not a plain DNS host name.

        It ends up in Traefik rules and in URLs the panel and every agent
        call, so a scheme, a path or a stray character would break both.

        :raise ValidationError: naming the value
        """
        for settings in self:
            name = settings.metrics_central_public_host or ''
            if name and ('.' not in name or not _HOSTNAME_RE.fullmatch(name)):
                raise ValidationError(_(
                    "%(name)s is not a host name. Write the name alone, for "
                    "example metrics.example.com: no https:// and no path.",
                    name=name,
                ))

    @api.constrains('metrics_central_operator_sources')
    def _check_metrics_central_operator_sources(self):
        """Refuse an entry that is not an address range.

        A typo would silently leave the panel unable to delete series.

        :raise ValidationError: naming the unusable entries
        """
        for settings in self:
            bad = invalid_ranges(settings.metrics_central_operator_sources)
            if bad:
                raise ValidationError(_(
                    "These are not address ranges: %(entries)s. Write one "
                    "CIDR range per line, for example 198.51.100.7/32.",
                    entries=", ".join(bad),
                ))

    @api.constrains('trusted_proxy_ranges')
    def _check_trusted_proxy_ranges(self):
        """Refuse a range list carrying an entry that is not a network.

        Same reasoning as the host-level check: a typo narrows who we
        believe without saying so, and the request path skips what it
        cannot read.

        :raise ValidationError: naming the unusable entries
        """
        for settings in self:
            bad = invalid_ranges(settings.trusted_proxy_ranges)
            if bad:
                raise ValidationError(_(
                    "These are not address ranges: %(entries)s. Write one "
                    "CIDR range per line, for example 198.51.100.0/24 or "
                    "2001:db8::/32.",
                    entries=", ".join(bad),
                ))

    @api.model
    def _trusted_proxy_source(self):
        """Return where the effective proxy ranges come from.

        :return: ``'settings'`` when the field here is set, ``'none'``
            when nothing applies
        :rtype: str
        """
        raw = self._get_system().trusted_proxy_ranges
        return 'settings' if parse_ranges(raw) else 'none'

    @api.model
    def _effective_trusted_proxy_ranges(self):
        """Return the CIDR ranges whose ``X-Forwarded-For`` is believed.

        Reads the operator-entered field. Modules that know the proxy in
        front of this installation — for instance, one that knows the
        panel answers behind a CDN — override this and supply the ranges
        themselves, so operators never have to keep a moving list of
        somebody else's addresses up to date by hand.

        :return: CIDR strings; empty means "believe nothing", which is
            the pre-existing behaviour
        :rtype: list
        """
        return parse_ranges(self._get_system().trusted_proxy_ranges)

    # ── Agents' auto-approval switch (incubacloud_mcp) ────────────────────

    @api.model
    def _mcp_auto_approval_allowed(self):
        """Whether an Administrator may let an agent's credential act without
        asking (``incubacloud.mcp.auto.approval``, in ``incubacloud_mcp``).

        True here. Read when a rule is granted and at every call it would
        approve: a panel that answers False leaves the rules it has paused,
        so this is also the switch that stops them all at once.

        Defined in this module and nowhere else in core, so a layer that
        answers False needs only this module below it: its answer comes
        after this one whatever the order the MCP and the layer load in.
        """
        return True

    # ── Startup check: the encryption key ─────────────────────────────────

    def _register_hook(self):
        """Warn loudly at registry load about a misconfigured server.

        Two checks. The Odoo limits of a server that runs the panel's
        jobs, which may cut them short (``_warn_job_server_limits``).
        And the encryption key: the actual fail-loud behaviour lives in
        ``password_utils``, where any attempt to encrypt/decrypt raises;
        this hook only surfaces the misconfiguration at startup so
        operators don't discover it at the first write, and is skipped
        behind ``INCUBACLOUD_ALLOW_NO_KEY=1`` for ad-hoc dev scripts that
        deliberately avoid the key. Both are suppressed during tests
        (the key is patched per-test).
        """
        super()._register_hook()
        if tools.config.get('test_enable'):
            return
        self._warn_job_server_limits(tools.config)
        if os.environ.get('INCUBACLOUD_ALLOW_NO_KEY'):
            _logger.warning(
                "INCUBACLOUD_ALLOW_NO_KEY=1 — any write to an encrypted "
                "field will still raise at runtime."
            )
            return
        if not key_is_configured():
            _logger.critical(
                "The panel's encryption key is missing or invalid (the "
                "error above says where it was looked for). Any write to "
                "cloud.host.password, cloud.instance.*_password, "
                "cloud.backup.backend.passphrase, cloud.settings.github_pat "
                "and similar fields will raise. Set INCUBACLOUD_SECRET_KEY, "
                "INCUBACLOUD_SECRET_KEY_FILE, or secret_key_file or "
                "secret_key in odoo.conf's [incubacloud] section before "
                "going to production."
            )
            return
        # The right key usually arrives with a restart: look again now
        # rather than at the next hourly pass.
        cron = self.env.ref(
            "incubacloud.cron_resolve_readable_secrets",
            raise_if_not_found=False,
        )
        if cron:
            try:
                with self.env.cr.savepoint():
                    cron._trigger()
            except Exception:  # noqa: BLE001 — never block the startup
                _logger.warning(
                    "Could not schedule the readable-secrets pass at "
                    "startup.", exc_info=True,
                )

    @api.model
    def _warn_job_server_limits(self, options):
        """Say at startup when this server's limits cut the panel's jobs short.

        Logs one warning per problem and keeps the panel's alert
        ``job_server_limits_too_low`` in step: raised with the problems,
        resolved once a restart brings limits that let jobs finish. A
        server that runs no jobs (``job_server_limit_problems`` returns
        None) leaves the alert to the one that does. The alert is a
        provider's: the server's configuration belongs to whoever runs
        the panel, not to the people working in it. Never blocks the
        startup.

        What in the channels may make a parked job wait for others
        (``channel_problems``) is only logged: it kills no job, and it
        matters only once a job parks and finds no slot when it comes
        back, which raises the slow job warning by itself, naming the
        channel (``cloud.job._parked_overdue``). Most servers have it
        and never park: a dedicated runner with an hour's limit and
        other modules' jobs in the queue would carry the alert for good.

        Only a server started by ``odoo server`` is judged. ``click-odoo``
        tools (``click-odoo-update`` in every deploy) and scripts load
        the registry without starting a server, and ``odoo shell``
        starts one and stops it before it loads the registry; both read
        the configuration file, whose ``limit_time_real`` is the web
        service's when a dedicated runner raises it on its command line,
        and would raise the alert on every deploy.

        :param options: Odoo's configuration, ``tools.config`` at startup
        """
        if odoo_server.server is None or odoo_cli.COMMAND != "server":
            return
        problems = job_server_limit_problems(options)
        if problems is None:
            return
        budget = attempt_budget_seconds(options)
        _logger.info(
            "A job's attempt runs for %s before it leaves a long step to its"
            " server (limit_time_real %s s).",
            f"{budget} s" if budget else "as long as it needs",
            options.get("limit_time_real") or 0,
        )
        try:
            channels = channel_problems(
                configured_channels(), self._foreign_job_channels(),
            )
        except Exception:  # noqa: BLE001 — never block the startup
            channels = []
            _logger.warning(
                "Could not read the job queue's channels.", exc_info=True,
            )
        for problem in problems + channels:
            _logger.warning(problem)
        Alert = self.env["cloud.alert"].sudo()
        try:
            with self.env.cr.savepoint():
                if problems:
                    Alert.raise_alert(
                        JOB_LIMITS_ALERT_CODE,
                        " ".join(problems),
                        audience="provider",
                    )
                else:
                    Alert.resolve_alert(JOB_LIMITS_ALERT_CODE)
        except Exception:  # noqa: BLE001 — never block the startup
            _logger.warning(
                "Could not update the alert about the job server's "
                "limits.", exc_info=True,
            )

    @api.model
    def _foreign_job_channels(self):
        """Return the channels other modules' queue jobs ran in lately.

        The last 30 days of queue_job, but the jobs of the panel's own
        models (``cloud.*``, ``incubacloud.*``): a job parked between two
        attempts keeps its slot against the panel's jobs only, and may
        wait for these.

        :rtype: set[str]
        """
        self.env.cr.execute(
            "SELECT DISTINCT channel FROM queue_job"
            " WHERE date_created > (now() at time zone 'UTC') - interval '30 days'"
            "   AND channel IS NOT NULL"
            "   AND model_name NOT LIKE 'cloud.%%'"
            "   AND model_name NOT LIKE 'incubacloud.%%'"
        )
        return {row[0] for row in self.env.cr.fetchall()}

    # ── Secret rotation ────────────────────────────────────────────────────
    # Rotation workflow (operator-driven, not time-based):
    #   1. Generate a new key and put "<new>,<old>" where the encryption
    #      key is configured (the environment variable, a key file or
    #      odoo.conf: see ``password_utils``).
    #   2. Restart every Odoo process. New writes already use the new
    #      key; old rows still decrypt via the trailing old key in the
    #      MultiFernet.
    #   3. Activate ``cron_rotate_encrypted_secrets`` (or call
    #      ``_rotate_all_secrets`` manually) to re-encrypt existing rows.
    #   4. When every field reports zero rotated rows, remove the old
    #      key from the chain and restart.
    # The cron ships with active=False — turning it on is a deliberate
    # operator action, so it can't run mid-rotation on a half-configured
    # key chain and corrupt ciphertext.

    @api.model
    def _rotate_all_secrets(self, batch_size=500):
        """Re-encrypt every encrypted value with the primary key.

        Reads ciphertext with raw SQL so plain text is never materialised
        in Python memory. Iterates every model in the registry, picks up
        every stored field built on ``EncryptedFieldMixin`` (so both
        ``EncryptedChar`` and ``EncryptedText``, and any shape added
        later), and rotates row-by-row.
        Invalidates the ORM cache per field so subsequent reads in the
        same transaction return the freshly rotated value.

        **Tolerance**: rotation of one row failing (typically because
        the ciphertext was encrypted with a key no longer present in
        the current MultiFernet chain) MUST NOT abort the whole run.
        We catch per-row exceptions, log them, and keep going so the
        remaining rotatable values still make it to the new key. Every
        stranded row is surfaced as a ``cloud.alert`` linked to the
        owning host / instance / project (or just logged when the model
        has no such relation, e.g. ``cloud.settings``).

        Returns ``{table.field: {'rotated': n, 'failed': [id, ...]}}``.
        """
        stats = {}
        for Model in self.env.registry.values():
            # Abstract mixins (e.g. cloud.terminal.route.mixin) can carry
            # an encrypted field but have no table of their own — the
            # concrete models that inherit them are rotated instead. Skip
            # them so we never query a non-existent relation.
            if Model._abstract or Model._transient:
                continue
            enc_fields = [
                name for name, f in Model._fields.items()
                if isinstance(f, EncryptedFieldMixin) and f.store
            ]
            if not enc_fields:
                continue
            table = Model._table
            for field_name in enc_fields:
                rotated, failed = self._rotate_encrypted_column(
                    table, field_name, batch_size,
                )
                if rotated or failed:
                    stats[f"{table}.{field_name}"] = {
                        'rotated': rotated, 'failed': failed,
                    }
                if rotated:
                    self.env[Model._name].invalidate_model([field_name])
                if failed:
                    self._raise_rotation_alerts(
                        Model._name, field_name, failed,
                    )
        if stats:
            _logger.info("Rotated encrypted secrets: %s", stats)
        else:
            _logger.info("Rotate pass complete: nothing to rotate.")
        pending = self.rotation_pending_count()
        _logger.info(
            "Rotation status: %d value(s) still not on the primary key. "
            "The old key may only be removed from the encryption key chain "
            "once this is 0.",
            pending['total'],
        )
        return stats

    def rotation_pending_count(self):
        """Return how many stored secrets are not on the primary key yet.

        This is the number RB-01 needs and the ``rotated`` tally cannot
        give: a rotation pass rewrites every row every time, because
        Fernet tokens carry a timestamp and a random IV, so "how many
        did I rewrite" never falls to zero no matter how many passes
        run. Asking which key opens each ciphertext does converge, and
        it is what says whether the old key can be retired safely.

        Reads ciphertext with raw SQL, so no plaintext is materialised
        and no ORM cache is disturbed. A value that cannot be read at
        all counts as pending, which is the safe direction: it keeps
        the old key in the chain rather than stranding the row.

        :return: ``{'total': n, 'by_column': {'table.field': n, ...}}``,
            with only the non-zero columns listed.
        :rtype: dict
        """
        by_column = {}
        total = 0
        for Model in self.env.registry.values():
            if Model._abstract or Model._transient:
                continue
            enc_fields = [
                name for name, f in Model._fields.items()
                if isinstance(f, EncryptedFieldMixin) and f.store
            ]
            if not enc_fields:
                continue
            for field_name in enc_fields:
                self.env.cr.execute(
                    sql.SQL(
                        "SELECT id, {f} FROM {t} WHERE {f} LIKE 'enc:%%'"
                    ).format(
                        t=sql.Identifier(Model._table),
                        f=sql.Identifier(field_name),
                    )
                )
                stale = 0
                for _rid, value in self.env.cr.fetchall():
                    try:
                        on_primary = is_on_primary_key(value, self.env)
                    except Exception:  # noqa: BLE001
                        on_primary = False
                    if not on_primary:
                        stale += 1
                if stale:
                    by_column[f"{Model._table}.{field_name}"] = stale
                    total += stale
        return {'total': total, 'by_column': by_column}

    def _rotate_encrypted_column(self, table, field_name, batch_size):
        """Rotate every ``enc:`` value in *table.field_name*.

        ``psycopg2.sql.Identifier`` keeps identifiers safe even though
        they come from model metadata (never user input) — belt-and-
        braces in case a model developer ever picks a weird column name.

        Returns ``(rotated_count, failed_ids)``. A row lands in
        ``failed_ids`` when ``rotate_value`` raises — typically because
        its ciphertext was encrypted with a key no longer in the
        ``INCUBACLOUD_SECRET_KEY`` chain. We swallow the exception,
        log it, and move on so one stranded value never blocks the
        rotation of the rest.
        """
        cr = self.env.cr
        cr.execute(
            sql.SQL(
                "SELECT id, {f} FROM {t} "
                "WHERE {f} LIKE 'enc:%%' ORDER BY id"
            ).format(
                t=sql.Identifier(table),
                f=sql.Identifier(field_name),
            )
        )
        rows = cr.fetchall()
        rotated = 0
        failed = []
        update_stmt = sql.SQL(
            "UPDATE {t} SET {f} = %s WHERE id = %s"
        ).format(
            t=sql.Identifier(table),
            f=sql.Identifier(field_name),
        )
        for start in range(0, len(rows), batch_size):
            for rid, old_value in rows[start:start + batch_size]:
                try:
                    new_value = rotate_value(old_value)
                except Exception as e:  # noqa: BLE001
                    failed.append(rid)
                    _logger.warning(
                        "[rotate] %s.%s id=%s could not rotate: %s "
                        "(likely encrypted with a retired key; "
                        "regenerate this value manually)",
                        table, field_name, rid, e,
                    )
                    continue
                if new_value != old_value:
                    cr.execute(update_stmt, (new_value, rid))
                    rotated += 1
        return rotated, failed

    # ── Unreadable-secret alerts close themselves ─────────────────────────

    #: Codes, by prefix, of the alerts about a secret the key chain
    #: cannot open (``<prefix><model>.<field>``).
    _UNREADABLE_SECRET_PREFIXES = (
        f"{ALERT_CODE_UNREADABLE}:",
        "secret_rotation_stranded:",
    )

    @api.model
    def _cron_resolve_readable_secrets(self):
        """Close every unreadable-secret alert whose values read again.

        The alert is opened by a failed read (``encrypted_char``) or a
        failed rotation; nothing used to close it. Installing the missing
        key and restarting fixed the panel and left the alert lit, and
        the operator had to dismiss it by hand (two panels, 18-oct-2026).
        A read in the hot path cannot close it either: the process that
        failed keeps its cached key chain, and the one with the right
        key never saw the failure.

        So this pass reads again, hourly and once at startup: for every
        active alert, each value of that field on the rows the alert
        points at is decrypted from its raw column. When all of them
        open — or the rows are gone — the alert is dismissed as the
        platform and announced as resolved (which, for a provider
        alert, reaches the provider too).

        :return: how many alerts were closed
        :rtype: int
        """
        if not key_is_configured():
            return 0
        Alert = as_platform(self.env["cloud.alert"])
        domain = ["|"] * (len(self._UNREADABLE_SECRET_PREFIXES) - 1) + [
            ("code", "=like", f"{prefix}%")
            for prefix in self._UNREADABLE_SECRET_PREFIXES
        ]
        closed = 0
        for alert in Alert.search([("state", "=", "active")] + domain):
            target = alert.code.partition(":")[2]
            model_name, _dot, field_name = target.rpartition(".")
            Model = self.env.get(model_name)
            field = Model._fields.get(field_name) if Model is not None else None
            if not (isinstance(field, EncryptedFieldMixin) and field.store):
                continue
            row_ids = self._unreadable_alert_rows(Model, alert)
            if row_ids is None:
                continue
            if not self._encrypted_rows_readable(Model, field_name, row_ids):
                continue
            alert.write({"state": "dismissed", "resolution": "auto"})
            alert._notify_alert_resolved()
            closed += 1
            _logger.info(
                "Alert %s (%s) closed: the value reads again.",
                alert.id, alert.code,
            )
        return closed

    def _unreadable_alert_rows(self, Model, alert):
        """Return the ids of *Model*'s rows an unreadable-secret alert covers.

        An alert on a host, instance or project secret points at that
        record. One on another model points at the row's host, instance
        or project instead (``encrypted_char._alert_link``), and dedups
        per target: it stands for every row of the model linked to it.

        :param Model: the model whose field the alert is about
        :param alert: the ``cloud.alert`` record
        :return: the row ids, or None when the alert's target cannot be
            told (left alone)
        :rtype: list or None
        """
        self_target = self._ROTATION_SELF_ALERT.get(Model._name)
        if self_target:
            target = alert[self_target]
            return [target.id] if target else None
        for link in ("host_id", "instance_id", "project_id"):
            if link in Model._fields and alert[link]:
                return Model.sudo().with_context(active_test=False).search(
                    [(link, "=", alert[link].id)],
                ).ids
        return None

    def _encrypted_rows_readable(self, Model, field_name, row_ids):
        """Return whether the current key chain opens every value of the rows.

        Read from the raw column, like the rotation: no plaintext enters
        the ORM cache, and a failure here opens no new alert.

        :param Model: the model
        :param str field_name: an encrypted, stored field of it
        :param list row_ids: the rows to check; none left counts as readable
        :rtype: bool
        """
        if not row_ids:
            return True
        self.env.cr.execute(
            sql.SQL(
                "SELECT {f} FROM {t} WHERE id IN %s AND {f} LIKE 'enc:%%'"
            ).format(
                t=sql.Identifier(Model._table),
                f=sql.Identifier(field_name),
            ),
            (tuple(row_ids),),
        )
        for (value,) in self.env.cr.fetchall():
            try:
                decrypt_value(value)
            except Exception:  # noqa: BLE001 — any failure: still unreadable
                return False
        return True

    # Models that ARE the alert target — an alert on a host points to
    # the host itself via ``host_id``.
    _ROTATION_SELF_ALERT = {
        'cloud.host': 'host_id',
        'cloud.instance': 'instance_id',
        'cloud.project': 'project_id',
    }

    def _rotation_alert_link(self, Model, record):
        """Return ``(link_field, link_id)`` pointing to the cloud.alert
        target for *record*, or ``(None, None)`` when the model has no
        host/instance/project relation and can't carry an alert.
        """
        self_target = self._ROTATION_SELF_ALERT.get(Model._name)
        if self_target:
            return self_target, record.id
        for candidate in ('host_id', 'instance_id', 'project_id'):
            if candidate in Model._fields:
                ref = record[candidate]
                if ref:
                    return candidate, ref.id
        return None, None

    def _raise_rotation_alerts(self, model_name, field_name, failed_ids):
        """Open a ``cloud.alert`` per stranded row so operators see the
        list in the dashboard without digging through logs.

        When the model is not linked to a host/instance/project (e.g.
        ``cloud.settings``, ``cloud.github.app``) we skip the alert —
        a targetless row would be invisible to the member-scoped record
        rule and impossible to trace back to a record. Those cases stay
        log-only, which is acceptable because the same warning was
        already emitted in ``_rotate_encrypted_column``.
        """
        Alert = self.env['cloud.alert'].sudo()
        Model = self.env[model_name]
        code = f"secret_rotation_stranded:{model_name}.{field_name}"
        log_only = []

        for rid in failed_ids:
            record = Model.sudo().browse(rid)
            if not record.exists():
                continue
            link_field, link_id = self._rotation_alert_link(Model, record)
            if not link_field:
                log_only.append(rid)
                continue
            # One active alert per (code, target) at a time.
            Alert.raise_alert(
                code,
                f"Could not rotate {model_name}.{field_name} "
                f"(id={rid}) — ciphertext was encrypted with a key "
                f"no longer in the encryption key chain. Regenerate "
                f"the value manually to resolve.",
                **Alert._link_target(link_field, link_id),
            )

        if log_only:
            _logger.warning(
                "[rotate] %s.%s has %d stranded row(s) with no "
                "host/instance/project to link an alert to: %s",
                model_name, field_name, len(log_only), log_only,
            )
