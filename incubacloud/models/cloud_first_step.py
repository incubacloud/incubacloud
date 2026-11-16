"""The first steps of a panel: when each was first tried and first done.

One row per mark (``cloud.first.step``): the first time a step was tried
and the first time it was done, kept once written. Deleting the server,
the destination or the instance afterwards takes nothing back, which is
why the dates are kept rather than computed on every read.

``cloud.first.steps`` is the one source of what a mark means and of the
list built on them: the first steps card of the panel's home page, the
assistant's ``get_first_steps`` and whatever a layer sends elsewhere all
read it. Nobody else looks at the models to decide whether a step is done.

A layer turns the whole thing off (``_enabled``), adds marks of its own
(``_marks``, ``_mark``), narrows the servers that count (``_first_step_host_domain``),
adds actions to the steps (``_steps``), hangs data for its own header on
the card (``_header``) and hears about every mark written
(``_on_marks_changed``).
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

#: Where the steps' guides live.
DOCS_URL = "https://docs.incubacloud.io/"

#: The job types whose end can write each mark: ``done`` achieves it (or,
#: for ``domain``, applies a domain added before), ``started`` is tried
#: by being created, ``failed`` is the failure kept while the mark is not
#: achieved. A layer adds its own types through ``_job_types``.
_JOB_TYPES = {
    "server": {"done": ("full_setup",), "failed": ("host_hardening", "full_setup")},
    "backups": {"done": ("backup_create",), "failed": ("backup_create",)},
    "deploy": {
        "started": ("deploy_instance",),
        "done": ("deploy_instance",),
        "failed": ("deploy_instance",),
    },
    "push": {"done": ("rebuild_instance",)},
    "domain": {"done": ("deploy_instance", "rebuild_instance")},
    "mail": {"failed": ("relay_test_mail",)},
}

#: What a push's rebuild carries in its payload (``cloud.repo.event.mixin``).
_PUSH_TRIGGERS = ("webhook", "coalesced")


class CloudFirstStep(models.Model):
    """One mark of the first steps, with the dates it was first tried and done.

    Read and written only by ``cloud.first.steps``, as the system. A date
    once written is never changed nor cleared (``_upsert``).
    """

    _name = "cloud.first.step"
    _description = "First step mark"
    _order = "code"

    code = fields.Char(required=True, readonly=True)
    attempted_at = fields.Datetime(readonly=True)
    achieved_at = fields.Datetime(readonly=True)
    failed_job_type = fields.Char(
        readonly=True,
        help="Code of the type of the last job that failed while the mark "
        "was not achieved; kept once it is.",
    )
    failed_at = fields.Datetime(readonly=True)
    backfilled = fields.Boolean(
        readonly=True,
        help="A date of this mark was not seen happen: it was filled with "
        "what the database held when the panel started keeping marks.",
    )

    _code_uniq = models.Constraint("unique (code)", "One row per first step mark.")

    @api.model
    def _upsert(self, code, attempted_at=None, achieved_at=None,
                failed_job_type=None, failed_at=None, backfilled=False):
        """Write the dates of mark *code* that are still empty, in one statement.

        ``INSERT … ON CONFLICT (code) DO UPDATE`` with ``COALESCE`` on
        what the row holds: two requests at once neither duplicate the row
        nor overwrite each other, and a date written stays. The failure
        moves to a later one only while the mark is not achieved.
        ``backfilled`` is set on the row when this write fills a date with
        it, and never cleared.

        :param str code: the mark
        :param datetime attempted_at: when it was first tried, or None
        :param datetime achieved_at: when it was first done, or None
        :param str failed_job_type: the type of a job that failed, or None
        :param datetime failed_at: when that job failed, or None
        :param bool backfilled: the dates were not seen happen
        """
        self.env.cr.execute(
            """
            INSERT INTO cloud_first_step AS s (
                code, attempted_at, achieved_at, failed_job_type, failed_at,
                backfilled, create_uid, create_date, write_uid, write_date
            )
            VALUES (%(code)s, %(attempted)s, %(achieved)s, %(ftype)s, %(fat)s,
                    %(backfilled)s, %(uid)s, NOW() AT TIME ZONE 'UTC',
                    %(uid)s, NOW() AT TIME ZONE 'UTC')
            ON CONFLICT (code) DO UPDATE SET
                attempted_at = COALESCE(s.attempted_at, EXCLUDED.attempted_at),
                achieved_at = COALESCE(s.achieved_at, EXCLUDED.achieved_at),
                failed_job_type = CASE
                    WHEN s.achieved_at IS NULL AND EXCLUDED.failed_at IS NOT NULL
                         AND (s.failed_at IS NULL OR EXCLUDED.failed_at > s.failed_at)
                    THEN EXCLUDED.failed_job_type ELSE s.failed_job_type END,
                failed_at = CASE
                    WHEN s.achieved_at IS NULL AND EXCLUDED.failed_at IS NOT NULL
                         AND (s.failed_at IS NULL OR EXCLUDED.failed_at > s.failed_at)
                    THEN EXCLUDED.failed_at ELSE s.failed_at END,
                backfilled = s.backfilled OR (EXCLUDED.backfilled AND (
                    (s.attempted_at IS NULL AND EXCLUDED.attempted_at IS NOT NULL)
                    OR (s.achieved_at IS NULL AND EXCLUDED.achieved_at IS NOT NULL))),
                write_uid = EXCLUDED.write_uid,
                write_date = EXCLUDED.write_date
            """,
            {
                "code": code,
                "attempted": attempted_at,
                "achieved": achieved_at,
                "ftype": failed_job_type if failed_at else None,
                "fat": failed_at if failed_job_type else None,
                "backfilled": bool(backfilled),
                "uid": self.env.uid,
            },
        )
        self.invalidate_model()


class CloudFirstSteps(models.AbstractModel):
    """The one source of the first steps: their marks, the list and its state."""

    _name = "cloud.first.steps"
    _description = "First steps of the panel"

    # ── What a layer overrides ─────────────────────────────────────────────

    @api.model
    def _enabled(self):
        """Whether the panel keeps first steps at all.

        ``True`` in core. A layer returns ``False`` to turn it all off: no
        list, no assistant tool and no mark written.

        :rtype: bool
        """
        return True

    @api.model
    def _first_step_host_domain(self):
        """The domain of the servers that count for the ``server`` mark.

        Every server in core. A layer narrows it to the servers that are
        the user's own.

        :rtype: list
        """
        return []

    @api.model
    def _job_types(self):
        """The job types whose end writes each mark, by mark and outcome.

        :return: ``{mark: {"started"|"done"|"failed": (job type code, …)}}``
        :rtype: dict
        """
        return {code: dict(kinds) for code, kinds in _JOB_TYPES.items()}

    @api.model
    def _marks(self):
        """The marks that exist, with how each is derived from the database.

        :return: ``{code: method}``, the method returning the dates seen now
            (``_derive_*``), or ``None`` for a mark only ``_mark`` writes.
            A layer adds its own.
        :rtype: dict
        """
        return {
            "server": self._derive_server,
            "backups": self._derive_backups,
            "project": self._derive_project,
            "deploy": self._derive_deploy,
            "push": self._derive_push,
            "domain": self._derive_domain,
            "mail": self._derive_mail,
        }

    @api.model
    def _on_marks_changed(self, codes):
        """Called when ``_reconcile`` or ``_mark`` wrote something; empty in core.

        Not called while the marks are filled from what was there before
        (``first_steps_backfill`` in the context).

        :param list codes: the marks that changed
        """

    @api.model
    def _failure_domain(self, code):
        """The jobs whose failure counts for mark *code*.

        For ``server``, those of the servers of ``_first_step_host_domain``;
        any job for the others. A layer adds its own marks'.

        :param str code: the mark
        :rtype: list
        """
        if code == "server":
            return self._server_job_domain()
        return []

    @api.model
    def _started(self, code, row):
        """Whether mark *code*'s *row* makes its step started.

        Tried, in core. A layer leaves out a try that only a click wrote,
        which it keeps all the same.

        :param str code: the mark
        :param row: its ``cloud.first.step``, or an empty recordset
        :rtype: bool
        """
        return bool(row and row.attempted_at)

    @api.model
    def _header(self, user):
        """Data a layer hangs on the card for its own header; empty in core.

        :param user: the ``res.users`` the card is for
        :rtype: dict
        """
        return {}

    # ── Derivations: the one definition of each mark ───────────────────────

    def _now(self):
        """The date of a fact that is true but whose moment is not kept."""
        return fields.Datetime.now()

    def _jobs(self):
        """``cloud.job`` as the system, archived records included."""
        return self.env["cloud.job"].sudo().with_context(active_test=False)

    def _first_job_date(self, types, state=None, domain=(), when="done"):
        """The date of the oldest job of *types*, or None.

        :param tuple types: job type codes
        :param str state: only jobs in this state, or any
        :param domain: more conditions
        :param str when: ``created`` for the job's creation, ``done`` for
            when it ended (``date_done``, else its last write)
        """
        if not types:
            return None
        search = [("job_type_id.code", "in", list(types)), *domain]
        if state:
            search.append(("state", "=", state))
        job = self._jobs().search(search, order="id", limit=1)
        if not job:
            return None
        if when == "created":
            return job.create_date
        return job.date_done or job.write_date

    def _last_failure(self, types, domain=(), before=None):
        """The type and date of the last failed job of *types*, or ``(None, None)``.

        :param tuple types: job type codes
        :param domain: more conditions
        :param datetime before: only a failure up to this moment
        """
        if not types:
            return None, None
        search = [
            ("job_type_id.code", "in", list(types)),
            ("state", "=", "failed"),
            *domain,
        ]
        if before:
            search.append(("write_date", "<=", before))
        job = self._jobs().search(search, order="id desc", limit=1)
        if not job:
            return None, None
        return job.job_type_id.code, job.write_date

    def _oldest(self, model, domain=(), field="create_date"):
        """The smallest *field* of *model*'s records in *domain*, archived included."""
        record = self.env[model].sudo().with_context(active_test=False).search(
            [*domain, (field, "!=", False)], order=f"{field}, id", limit=1,
        )
        return record[field] if record else None

    @staticmethod
    def _earliest(*dates):
        """The earliest of *dates* that are set, or None."""
        dates = [date for date in dates if date]
        return min(dates) if dates else None

    def _failure_vals(self, code, domain=(), achieved_at=None):
        """The ``failed_*`` values of mark *code*'s last failure."""
        ftype, fat = self._last_failure(
            self._job_types().get(code, {}).get("failed", ()),
            domain, before=achieved_at,
        )
        return {"failed_job_type": ftype, "failed_at": fat}

    def _server_job_domain(self):
        """The jobs of the servers of ``_first_step_host_domain``, archived included.

        :rtype: list
        """
        host_domain = self._first_step_host_domain()
        if not host_domain:
            return []
        hosts = self.env["cloud.host"].sudo().with_context(
            active_test=False,
        ).search(host_domain)
        return [("host_id", "in", hosts.ids)]

    def _derive_server(self):
        """Tried: the oldest server; done: the oldest full setup that ended done.

        Only the servers of ``_first_step_host_domain``.
        """
        job_domain = self._server_job_domain()
        types = self._job_types()["server"]
        achieved = self._first_job_date(types.get("done"), "done", job_domain)
        return {
            "attempted_at": self._oldest("cloud.host", self._first_step_host_domain()),
            "achieved_at": achieved,
            **self._failure_vals("server", job_domain, achieved),
        }

    def _derive_backups(self):
        """Tried: the oldest destination; done: the first backup that ended well."""
        types = self._job_types()["backups"]
        achieved = self._earliest(
            self._first_job_date(types.get("done"), "done"),
            self._oldest("cloud.instance.backup", field="backup_time"),
        )
        return {
            "attempted_at": self._oldest("cloud.backup.backend"),
            "achieved_at": achieved,
            **self._failure_vals("backups", achieved_at=achieved),
        }

    def _derive_project(self):
        """Done: the oldest project."""
        return {"achieved_at": self._oldest("cloud.project")}

    def _derive_deploy(self):
        """Tried: the oldest deploy asked for; done: the oldest that ended done."""
        types = self._job_types()["deploy"]
        achieved = self._first_job_date(types.get("done"), "done")
        return {
            "attempted_at": self._first_job_date(
                types.get("started"), when="created",
            ),
            "achieved_at": achieved,
            **self._failure_vals("deploy", achieved_at=achieved),
        }

    def _derive_push(self):
        """Done: the first rebuild a push asked for that ended done, boot test on."""
        types = self._job_types()["push"].get("done")
        if not types:
            return {}
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT COALESCE(j.date_done, j.write_date)
              FROM cloud_job j
              JOIN cloud_job_type t ON t.id = j.job_type_id
              JOIN cloud_instance i ON i.id = j.instance_id
             WHERE t.code IN %s AND j.state = 'done' AND i.boot_test
               AND j.payload IS NOT NULL
               AND (j.payload::jsonb ->> 'trigger') IN %s
             ORDER BY j.id
             LIMIT 1
            """,
            (tuple(types), _PUSH_TRIGGERS),
        )
        row = self.env.cr.fetchone()
        return {"achieved_at": row[0] if row else None}

    @staticmethod
    def _is_own_domain(domain):
        """Whether *domain* is a name of its own, not one under its server's wildcard.

        A domain whose instance has no server left is not counted.

        :param domain: a ``cloud.instance.domain``
        :rtype: bool
        """
        hostname = (domain.hostname or "").strip().lower()
        host = domain.instance_id.host_id
        if not hostname or not host:
            # An archived instance whose server was deleted: its wildcard
            # is gone with it, so whether the name was its own is unknown.
            return False
        suffix = (host._subdomain_suffix() or "").lower()
        if not suffix:
            return True
        return hostname != suffix and not hostname.endswith(f".{suffix}")

    def _derive_domain(self):
        """Tried: the first own domain; done: the first deploy or rebuild after it.

        «Done» means applied by a deploy or a rebuild that ended done and
        started once the domain existed; its DNS and certificate are not
        checked.
        """
        domains = self.env["cloud.instance.domain"].sudo().with_context(
            active_test=False,
        ).search([], order="create_date, id").filtered(self._is_own_domain)
        if not domains:
            return {}
        types = self._job_types()["domain"].get("done")
        achieved = self._earliest(*(
            self._first_job_date(types, "done", [
                ("instance_id", "=", domain.instance_id.id),
                ("create_date", ">=", domain.create_date),
            ])
            for domain in domains
        ))
        return {"attempted_at": domains[0].create_date, "achieved_at": achieved}

    def _derive_mail(self):
        """Tried: an instance with a relay; done: one whose relay delivers."""
        Instance = self.env["cloud.instance"].sudo().with_context(active_test=False)
        attempted = (
            self._now() if Instance.search_count(
                [("smtp_relay_host", "!=", False)], limit=1,
            ) else None
        )
        achieved = None
        delivering = Instance.search(
            [("smtp_relay_state", "=", "ok")],
            order="smtp_relay_checked_at, id", limit=1,
        )
        if delivering:
            achieved = delivering.smtp_relay_checked_at or self._now()
        return {
            "attempted_at": attempted or achieved,
            "achieved_at": achieved,
            **self._failure_vals("mail", achieved_at=achieved),
        }

    # ── Writing the marks ──────────────────────────────────────────────────

    def _rows(self):
        """The marks written so far, by code."""
        return {
            row.code: row for row in self.env["cloud.first.step"].sudo().search([])
        }

    @staticmethod
    def _adds_something(row, vals):
        """Whether writing *vals* over *row* (or no row) would change anything."""
        if not row:
            return bool(vals.get("attempted_at") or vals.get("achieved_at")
                        or vals.get("failed_at"))
        if not row.attempted_at and vals.get("attempted_at"):
            return True
        if not row.achieved_at and vals.get("achieved_at"):
            return True
        failed_at = vals.get("failed_at")
        return bool(
            not row.achieved_at and failed_at and vals.get("failed_job_type")
            and (not row.failed_at or failed_at > row.failed_at)
        )

    @api.model
    def _reconcile(self, codes=None):
        """Derive and write every mark of *codes* that still has a date to fill.

        The only definition of what each date means (``_marks``). A mark
        already achieved is not looked at again: with every mark achieved
        nothing else is read. Each mark is derived and written in a
        savepoint of its own: one that fails is logged and left unwritten,
        and the others are written. With ``first_steps_backfill`` in the context
        what it writes is marked ``backfilled`` and ``_on_marks_changed``
        is not called.

        :param list codes: the marks to look at; all of them by default
        :return: the codes of the marks written
        :rtype: list
        """
        if not self._enabled():
            return []
        marks = self._marks()
        rows = self._rows()
        backfill = bool(self.env.context.get("first_steps_backfill"))
        Step = self.env["cloud.first.step"].sudo()
        changed = []
        for code, derive in marks.items():
            if codes is not None and code not in codes:
                continue
            row = rows.get(code)
            if derive is None or (row and row.achieved_at):
                continue
            try:
                with self.env.cr.savepoint():
                    vals = derive() or {}
                    if not self._adds_something(row, vals):
                        continue
                    Step._upsert(code, backfilled=backfill, **vals)
            except Exception:  # one broken mark leaves the others standing
                _logger.exception("First steps: deriving %s failed", code)
                continue
            changed.append(code)
        if changed and not backfill:
            self._on_marks_changed(changed)
        return changed

    @api.model
    def _mark(self, code, attempted=False, achieved=False):
        """Write now as the first try or success of a mark no record keeps.

        For a fact of a moment (a click), which no derivation can read
        later. Core has none: it is the door for the layers.

        :param str code: the mark
        :param bool attempted: write its first try
        :param bool achieved: write its first success
        :return: whether something was written
        :rtype: bool
        """
        if not self._enabled():
            return False
        now = self._now()
        vals = {
            "attempted_at": now if attempted or achieved else None,
            "achieved_at": now if achieved else None,
        }
        if not self._adds_something(self._rows().get(code), vals):
            return False
        self.env["cloud.first.step"].sudo()._upsert(code, **vals)
        self._on_marks_changed([code])
        return True

    @api.model
    def _touch(self, codes):
        """Reconcile *codes* after a change that may write them; never raises.

        In a savepoint, logged: a mark never makes the operation that
        triggers it fail.

        :param list codes: the marks the change may write
        """
        if not codes:
            return
        try:
            with self.env.cr.savepoint():
                self._reconcile(codes)
        except Exception:  # the operation stands without its mark
            _logger.exception("First steps: reconciling %s failed", codes)

    @api.model
    def _on_job_terminal(self, jobs, new_state):
        """Reconcile the marks the end of *jobs* may write; never raises.

        Reads the jobs and writes marks: nothing of the jobs' life changes.

        :param jobs: the ``cloud.job`` records that just ended
        :param str new_state: ``done``, ``failed`` or ``cancelled``
        """
        if new_state not in ("done", "failed") or not self._enabled():
            return
        kinds = ("done", "started") if new_state == "done" else ("failed", "started")
        job_codes = set(jobs.sudo().job_type_id.mapped("code"))
        codes = [
            code for code, by_kind in self._job_types().items()
            if any(job_codes & set(by_kind.get(kind, ())) for kind in kinds)
        ]
        self._touch(codes)

    @api.model
    def _backfill(self):
        """Fill the marks with what the database holds, for a panel already running.

        Run once, by the migration that brings the marks: every mark the
        derivations find is written ``backfilled``. Old jobs are purged,
        so what has no job left is read from the records: ``server`` done
        when a server has its proxy deployed (``traefik_deployed``), at
        the server's creation; ``deploy`` done when an instance is
        deployed, at the instance's creation; ``backups`` done at the
        oldest backup listed (in its derivation already).

        :return: the codes written
        :rtype: list
        """
        if not self._enabled():
            return []
        self = self.with_context(first_steps_backfill=True)
        changed = set(self._reconcile())
        rows = self._rows()
        Step = self.env["cloud.first.step"].sudo()
        fallbacks = {
            "server": self._oldest("cloud.host", [
                *self._first_step_host_domain(), ("traefik_deployed", "=", True),
            ]),
            "deploy": self._oldest("cloud.instance", [("deployed", "=", True)]),
        }
        for code, date in fallbacks.items():
            row = rows.get(code)
            if not date or (row and row.achieved_at):
                continue
            try:
                with self.env.cr.savepoint():
                    Step._upsert(
                        code, attempted_at=date, achieved_at=date, backfilled=True,
                    )
            except Exception:  # the update goes on without this mark
                _logger.exception("First steps: backfilling %s failed", code)
                continue
            changed.add(code)
        return sorted(changed)

    # ── The list ───────────────────────────────────────────────────────────

    def _first_production(self, deployed=False):
        """The first production instance the user sees, or an empty recordset.

        :param bool deployed: only one deployed
        """
        domain = [("environment", "=", "production")]
        if deployed:
            domain.append(("deployed", "=", True))
        return self.env["cloud.instance"].search(domain, order="id", limit=1)

    def _instance_action(self, code, label, screen, instance, tab=None):
        """An action that opens *instance*'s page, on *tab* when given."""
        params = {"project_id": instance.project_id.id, "instance_id": instance.id}
        if tab:
            params["tab"] = tab
        return {
            "code": code, "label": label, "screen": screen,
            "route": "instance_detail", "params": params,
        }

    def _deploy_actions(self):
        """What the ``deploy`` step offers, by what exists now (navigation only)."""
        _ = self.env._
        project = self.env["cloud.project"].search([], order="id", limit=1)
        if not project:
            return [{
                "code": "new_project", "label": _("Create a project"),
                "screen": _("New project"), "route": "new_project", "params": {},
            }]
        instance = self.env["cloud.instance"].search(
            [("project_id", "=", project.id), ("environment", "=", "production")],
            order="id", limit=1,
        )
        if not instance:
            return [{
                "code": "new_production",
                "label": _("Create the production instance"),
                "screen": _("The project's page"),
                "route": "project_detail", "params": {"project_id": project.id},
            }]
        return [self._instance_action(
            "open_instance", _("Open the instance"), _("The instance's page"),
            instance,
        )]

    @api.model
    def _steps(self):
        """The steps of the list, in order, with their marks, texts, actions and guide.

        Texts are translated here, once, in the context's language: the
        card gets them ready and the assistant asks for them in English.
        An action is ``{code, label, screen, route, params}`` for a page of
        the panel, or ``{code, label, screen, client: True}`` for one a
        layer's JavaScript opens (``registerFirstStepAction``). A layer adds
        actions or changes their order.

        :return: ``[{code, title, help, optional, marks, actions, guide_url,
            failure_route}]``; ``marks`` are those that close the step,
            the first of them the one tried first
        :rtype: list
        """
        _ = self.env._
        production = self._first_production(deployed=True)
        return [
            {
                "code": "server",
                "title": _("Get a server"),
                "help": _("Odoo runs on a server of yours, which the panel sets up."),
                "optional": False,
                "marks": ["server"],
                "actions": [{
                    "code": "connect_server", "label": _("Connect a server"),
                    "screen": _("New Host"), "route": "new_host",
                    "params": {},
                }],
                "guide_url": f"{DOCS_URL}hosts/connect/",
            },
            {
                "code": "backups",
                "title": _("Set up backups"),
                "help": _("Production keeps its backups in a storage destination you choose."),
                "optional": False,
                "marks": ["backups"],
                "actions": [{
                    "code": "new_backup_backend",
                    "label": _("Add a backup destination"),
                    "screen": _("New Backup Destination"),
                    "route": "new_backup_backend", "params": {},
                }],
                "guide_url": f"{DOCS_URL}backups/",
            },
            {
                "code": "deploy",
                "title": _("Deploy Odoo"),
                "help": _("A project holds your instances; its production is the Odoo your team uses."),
                "optional": False,
                "marks": ["deploy"],
                "actions": self._deploy_actions(),
                "guide_url": f"{DOCS_URL}getting-started/first-deploy/",
            },
            {
                "code": "domain",
                "title": _("Use your own domain"),
                "help": _("Point a name of yours at production, such as erp.example.com."),
                "optional": True,
                "marks": ["domain"],
                "actions": [self._instance_action(
                    "add_domain", _("Add a domain"),
                    _("The instance's page, Networking tab"), production,
                    "networking",
                )] if production else [],
                "guide_url": f"{DOCS_URL}instances/#custom-domains-and-certificates",
            },
            {
                "code": "mail",
                "title": _("Send mail from Odoo"),
                "help": _("Production sends its mail through a mail server of yours."),
                "optional": True,
                "marks": ["mail"],
                "actions": [self._instance_action(
                    "set_up_mail", _("Set up outgoing mail"),
                    _("The instance's page, Email tab"), production, "email",
                )] if production else [],
                "guide_url": f"{DOCS_URL}instances/mail-relay/",
            },
        ]

    def _failure_action(self, step, rows):
        """The action to the screen where the failed job of *step* shows, or None.

        The job is the last failed one of the type its mark keeps, among
        the jobs that count for that mark (``_failure_domain``): the
        server step leads to one of the servers it counts.

        :param dict step: a step of ``_steps``
        :param dict rows: the marks written, by code
        """
        _ = self.env._
        failed = [
            (code, rows[code]) for code in step["marks"]
            if rows.get(code) and rows[code].failed_job_type
            and not rows[code].achieved_at
        ]
        if not failed:
            return None
        code, row = max(failed, key=lambda item: item[1].failed_at or item[1].create_date)
        job = self.env["cloud.job"].search([
            ("job_type_id.code", "=", row.failed_job_type), ("state", "=", "failed"),
            *self._failure_domain(code),
        ], order="id desc", limit=1)
        if job.instance_id:
            tab = {"backups": "backups", "mail": "email"}.get(step["code"])
            return self._instance_action(
                "see_failure", _("See what failed"),
                _("The instance's page"), job.instance_id, tab,
            )
        if job.host_id:
            return {
                "code": "see_failure", "label": _("See what failed"),
                "screen": _("The server's page"), "route": "host_detail",
                "params": {"host_id": job.host_id.id},
            }
        return None

    def _user_display(self, user):
        """How *user* chose to see the list: ``auto``, ``hidden`` or ``shown``."""
        return user.sudo().cloud_first_steps_display or "auto"

    def _compute(self):
        """Reconcile the marks, then each step's state, which comes next and the counts.

        :return: ``(steps, rows, next_code)`` with every step carrying
            ``state`` (``done``, ``started``, ``todo``) and ``failed``
        """
        self._touch(list(self._marks()))
        rows = self._rows()
        steps = self._steps()
        deploy_done = False
        for step in steps:
            marks = [rows.get(code) for code in step["marks"]]
            if any(row and row.achieved_at for row in marks):
                state = "done"
            elif any(
                self._started(code, rows.get(code)) for code in step["marks"]
            ) or (
                step["code"] == "deploy" and rows.get("project")
                and rows["project"].achieved_at
            ):
                state = "started"
            else:
                state = "todo"
            step["state"] = state
            step["failed"] = state == "started" and any(
                row and row.failed_job_type for row in marks
            )
            if step["code"] == "deploy":
                deploy_done = state == "done"
        for step in steps:
            step["after_deploy"] = step["optional"] and not deploy_done
            if step["after_deploy"]:
                step["actions"] = []
        required = [s for s in steps if not s["optional"]]
        optional = [s for s in steps if s["optional"]]
        pending = [s for s in required if s["state"] != "done"] or [
            s for s in optional if s["state"] != "done"
        ]
        return steps, rows, pending[0]["code"] if pending else False

    @api.model
    def _state(self, user):
        """What the card shows to *user*: each step's state, which comes next, whether it shows.

        It shows by itself (``visible``) while it is on, the user has not
        closed it and a required step is left, or optional ones are left
        and ``deploy`` was not ``backfilled`` (a panel already running
        does not see it appear for want of a domain of its own). A user
        who opened it again sees it while anything is left. With the
        required steps done it is ``collapsed`` to one line; with every
        step done it is gone (``pending`` false: no menu entry either).

        :param user: the ``res.users`` the card is for
        :return: ``{enabled: False}`` without the Manager role or with
            ``_enabled`` off; else ``{enabled, visible, hidden, pending,
            collapsed, done, required, optional_left, next, steps, header}``
        :rtype: dict
        """
        if not self._enabled() or not user.has_group("incubacloud.group_cloud_manager"):
            return {"enabled": False}
        steps, rows, next_code = self._compute()
        required = [s for s in steps if not s["optional"]]
        done = sum(1 for s in required if s["state"] == "done")
        optional_left = sum(
            1 for s in steps if s["optional"] and s["state"] != "done"
        )
        display = self._user_display(user)
        deploy = rows.get("deploy")
        auto = done < len(required) or bool(
            optional_left and not (deploy and deploy.backfilled)
        )
        pending = bool(next_code)
        visible = pending and display != "hidden" and (auto or display == "shown")
        _ = self.env._
        out_steps = []
        for step in steps:
            actions = step["actions"]
            help_text = step["help"]
            if step["failed"]:
                help_text = _("The last attempt failed.")
                failure = self._failure_action(step, rows)
                if failure:
                    actions = [failure, *actions]
            elif step["after_deploy"] and step["state"] != "done":
                help_text = _("After your first deployment.")
            out_steps.append({
                "code": step["code"],
                "title": step["title"],
                "help": help_text,
                "optional": step["optional"],
                "state": step["state"],
                "failed": step["failed"],
                "actions": [
                    {k: v for k, v in action.items() if k != "screen"}
                    for action in actions
                ],
                "guide_url": step["guide_url"],
            })
        return {
            "enabled": True,
            "visible": visible,
            "hidden": display == "hidden",
            "pending": pending,
            "collapsed": done == len(required),
            "done": done,
            "required": len(required),
            "optional_left": optional_left,
            "next": next_code,
            "steps": out_steps,
            "header": self._header(user),
        }

    @api.model
    def _set_display(self, user, hidden):
        """Close the list for *user*, or open it again.

        :param user: the ``res.users`` who closes or opens it
        :param bool hidden: close it
        """
        user.sudo().cloud_first_steps_display = "hidden" if hidden else "shown"

    @api.model
    def _state_for_assistant(self):
        """The steps for the assistant: in English, without a record's name or id.

        :return: ``{enabled: False}`` when off; else ``{steps, next, done,
            required, can_do_them}``, each step with its state, whether it
            is optional and its last attempt failed, its help, its actions
            (the button's exact label and the screen it is on) and its guide
        :rtype: dict
        """
        if not self._enabled():
            return {"enabled": False}
        self = self.with_context(lang="en_US")
        steps, _rows, next_code = self._compute()
        required = [s for s in steps if not s["optional"]]
        return {
            "enabled": True,
            "next": next_code,
            "done": sum(1 for s in required if s["state"] == "done"),
            "required": len(required),
            "can_do_them": self.env.user.has_group("incubacloud.group_cloud_manager"),
            "steps": [
                {
                    "code": step["code"],
                    "title": step["title"],
                    "help": step["help"],
                    "optional": step["optional"],
                    "state": step["state"],
                    "last_attempt_failed": step["failed"],
                    "only_after_first_deployment": step["after_deploy"],
                    "actions": [
                        {"button": action["label"], "screen": action["screen"]}
                        for action in step["actions"]
                    ],
                    "guide_url": step["guide_url"],
                }
                for step in steps
            ],
        }
