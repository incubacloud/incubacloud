"""Run a job once, twice, and cut off halfway, and compare what it leaves.

queue_job runs a ``cloud.job`` again from the start in two cases
(``cloud.job.outcome`` has the history): the store of ``done`` lost a
serialization race after the success hook had committed on its own
cursor, or the worker died somewhere in the run. Either way the second
attempt starts from ``cloud.job.execute`` in a world the first one
changed. A job type is idempotent when that second attempt leaves the
world as one attempt alone would have left it, and makes no call that
must happen once (a server bought, a charge, a mail, the removal of a
file somebody else still needs) a second time.

This module is importable, not a test: core runs it over its job types
(``test_job_idempotency``) and a repository layered on top over its own, with
specifications of its own. For each specified type, three scenarios:

* **once** — one attempt, stored done, committed: the reference.
* **twice** — one attempt stored done whose commit is lost (its
  ``postcommit`` is dropped, the runner's pending writes cleared), then a
  second, committed.
* **cut at k** — the worker dies right after the k-th call to a host
  returned (``HarnessCut``, a ``BaseException`` of its own that no
  ``except Exception`` of the code under test catches), or at the end of
  ``on_success`` before its cursor commits; then a complete attempt.

Every attempt enters through ``cloud.job.execute``, as queue_job does,
never ``executor.run``: what ``execute`` decides before the run is part
of what is tested. After each scenario the world is photographed
(:meth:`IdempotencyCase.world`) and the external calls are in the
:class:`Ledger`.

**What a ``TransactionCase`` cannot reproduce.** In registry test mode
the durable cursor of the hooks (``AbstractExecutor._durable_env``) is
not independent of the job's transaction: it is a pseudo-cursor on the
test's own transaction, a savepoint that its "commit" releases. Two
consequences. A rollback of the runner cannot be told apart from what
the hooks committed, so the writes the runner flushed before a lost
commit stay here (they would be gone for real); only its ``postcommit``
is dropped and its pending writes cleared. And no REPEATABLE READ
snapshot hides anything, so a serialization failure never happens. The
tests with real cursors (``test_job_outcome_real_cursors``) cover both;
here the scenarios pin the behaviour.

Each scenario runs in a savepoint rolled back afterwards, on fixtures
built once (``JobSpec.setup``), so their ids are the same in every
scenario and the photos compare.
"""
import contextlib
import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from odoo.tools.misc import Callbacks

from odoo.addons.queue_job.exception import RetryableJobError

from ..models.abstract_executor import AbstractExecutor
from ..models.cloud_host import CloudHost
from ..models.registry import executor_registry
from ..models.transport import CommandResult, SSHTransport

#: What the photo leaves out of a row: who and when, which differ from
#: one scenario to the next and say nothing about the world, and
#: ``return_wait_s``, a measure every attempt that starts adds to (0 when
#: it was not late), so a job run twice keeps 0 where one run once keeps
#: nothing.
VOLATILE_COLUMNS = frozenset({
    "create_uid", "create_date", "write_uid", "write_date", "return_wait_s",
})

#: The models whose rows created during a scenario are part of the photo,
#: with how each row is summed up (SQL over the table, ``%s`` the id the
#: scenario began after). The job log is not among them: an attempt that
#: finds its outcome saved says so in the log, on purpose.
CREATED_ROWS = {
    "cloud.job": (
        "SELECT t.code FROM cloud_job j"
        " JOIN cloud_job_type t ON t.id = j.job_type_id"
        " WHERE j.id > %s ORDER BY t.code"
    ),
    "cloud.alert": (
        "SELECT code || ':' || state FROM cloud_alert WHERE id > %s ORDER BY 1"
    ),
    "mail.mail": "SELECT 'mail' FROM mail_mail WHERE id > %s",
    "ir.attachment": (
        "SELECT res_model FROM ir_attachment"
        " WHERE id > %s AND res_model LIKE 'cloud.%%' ORDER BY 1"
    ),
    "cloud.instance.backup": (
        "SELECT 'backup' FROM cloud_instance_backup WHERE id > %s"
    ),
    "cloud.tenant.log": "SELECT 'log' FROM cloud_tenant_log WHERE id > %s",
    "payment.transaction": "SELECT 'tx' FROM payment_transaction WHERE id > %s",
}


class HarnessCut(BaseException):
    """The worker dying at the chosen point of a run.

    A ``BaseException``: nothing the code under test catches with
    ``except Exception`` may survive it, as nothing survives a killed
    process. The transport keeps raising it once it was raised, so the
    cleanup on the way out (``_discard_staging``) reaches no host either.
    """


class Call(NamedTuple):
    """One call the code under test made outside the database."""

    #: ``ssh`` (a host), ``notify`` (mail, Telegram, webhook) or what a
    #: specification's own patches record
    kind: str
    #: the host for ``ssh``, the job for ``notify``
    target: int
    #: the transport method, or the notification channel
    method: str
    #: the command, or the state announced
    detail: str


@dataclass
class Ledger:
    """The external calls of one scenario, and the remote state they change.

    ``remote`` is a small picture of the hosts — the paths present on
    each, ``(host_id, path)`` — for the specifications whose steps test
    for a file or remove one; a ``JobSpec.answer`` reads and changes it.
    """

    calls: list = field(default_factory=list)
    remote: set = field(default_factory=set)

    def of(self, kind):
        """Return the calls of *kind*, in order.

        :param str kind: ``ssh``, ``notify``, …
        :rtype: list[Call]
        """
        return [call for call in self.calls if call.kind == kind]


@dataclass
class JobSpec:
    """How to run one job type in the harness, and what it may not repeat.

    :ivar code: the job type, whose registered executor runs
    :ivar setup: ``setup(case)`` builds the fixtures and returns the
        ``cloud.job`` to run, dispatched: linked to a ``queue.job`` in
        ``started``, as the runner hands it over
        (:meth:`IdempotencyCase.dispatched_job`)
    :ivar once: ``once(call)``: whether *call* must happen once in all,
        whatever the scenario (``notify`` calls always do)
    :ivar answer: ``answer(ledger, host, method, detail)``: the
        ``CommandResult`` of a host call, or None for a clean exit 0
    :ivar commands: the steps run instead of ``get_commands()``, for a
        type whose real recipe needs a world the harness does not build
        (the hooks stay real), or None for the real ones
    :ivar before: ``await before(executor, transport)`` run instead of
        ``before_execute``, or None for the real one
    :ivar remote: the paths present on the hosts before the first
        attempt, ``{(host_id, path)}``, from ``remote(case, job)``
    :ivar tracked: ``tracked(case, job)``: the rows photographed, as
        ``{label: (model, id)}``; by default the job, its instance and
        its host
    :ivar patches: ``patches(case, ledger)``: more context managers for
        what the type reaches outside (DNS, a provider), recording into
        the ledger
    :ivar divergent: scenarios known not to converge yet, each with the
        reason: ``{key: why}``, the key ``"twice"``, ``"hook"`` or the
        number of the host call a cut follows. One listed here that
        converges fails the check, so the entry is dropped when the fix
        lands.
    :ivar divergent_after: ``((predicate, why), …)``: the cuts right
        after a host call *predicate* matches are known not to converge,
        for the reason given; same rule as ``divergent``
    """

    code: str
    setup: Callable
    once: Callable = None
    answer: Callable = None
    commands: list = None
    before: Callable = None
    remote: Callable = None
    tracked: Callable = None
    patches: Callable = None
    divergent: dict = field(default_factory=dict)
    divergent_after: tuple = ()


#: Scenario plans for one attempt: run it and commit, lose its commit,
#: or cut it at the k-th host call (an int) or at the end of its hook.
COMMIT, LOST_COMMIT, HOOK = "commit", "lost_commit", "hook"


class _Rollback(Exception):
    """Ends a scenario's savepoint so it rolls back."""


class IdempotencyCase(TransactionCase):
    """Run :class:`JobSpec` scenarios through ``cloud.job.execute``.

    In registry test mode, so the hooks' durable cursor and the
    ``postcommit`` callbacks reach this transaction (see the module's
    docstring for what that does not reproduce).
    """

    def setUp(self):
        """Registry test mode, as the durable cursors need it here."""
        super().setUp()
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)

    # ── The check ─────────────────────────────────────────────────────

    def check_spec(self, spec):
        """Run *spec*'s scenarios and fail on the first that diverges.

        :param JobSpec spec: the type's specification
        """
        job = spec.setup(self)
        self.env.flush_all()
        tracked = (spec.tracked or self._default_tracked)(self, job)
        reference, ref_ledger = self.scenario(spec, job, tracked, [COMMIT])
        once_calls = self._once_calls(spec, ref_ledger)
        plans = [("twice", "twice", [LOST_COMMIT, COMMIT])]
        for k, call in enumerate(ref_ledger.of("ssh"), start=1):
            name = f"cut after host call {k} ({call.detail[:60]})"
            plans.append((k, name, [k, COMMIT]))
        plans.append((HOOK, "cut at the end of on_success", [HOOK, COMMIT]))
        for key, name, plan in plans:
            with self.subTest(job_type=spec.code, scenario=name):
                world, ledger = self.scenario(spec, job, tracked, plan)
                same = world == reference and (
                    self._once_calls(spec, ledger) == once_calls
                )
                if key in spec.divergent or (isinstance(key, int) and any(
                    known(ref_ledger.of("ssh")[key - 1])
                    for known, _why in spec.divergent_after
                )):
                    self.assertFalse(
                        same,
                        f"{name} converges now: drop it from the "
                        f"specification's divergent points",
                    )
                    continue
                self.assertEqual(world, reference, f"{name}: another world")
                self.assertEqual(
                    self._once_calls(spec, ledger), once_calls,
                    f"{name}: a call that must happen once did not",
                )

    def _once_calls(self, spec, ledger):
        """The calls of *ledger* that must happen once, in order.

        :rtype: list[Call]
        """
        once = spec.once or (lambda call: False)
        return [
            call for call in ledger.calls
            if call.kind == "notify" or once(call)
        ]

    # ── One scenario ──────────────────────────────────────────────────

    def scenario(self, spec, job, tracked, plan):
        """Run the attempts of *plan* on *job* and photograph the world.

        Inside a savepoint rolled back at the end: the next scenario
        starts from the same fixtures.

        :param list plan: one entry per attempt (``COMMIT``,
            ``LOST_COMMIT``, ``HOOK`` or a host call's number)
        :return: ``(world, ledger)``
        """
        ledger = Ledger()
        if spec.remote:
            ledger.remote |= set(spec.remote(self, job))
        with contextlib.suppress(_Rollback):
            with self.env.cr.savepoint():
                baseline = self._baseline(tracked)
                for index, step in enumerate(plan):
                    if index:
                        self._start_again(job)
                    self._attempt(spec, job, ledger, step)
                world = self.world(tracked, baseline)
                raise _Rollback
        self.env.invalidate_all()
        return world, ledger

    def _start_again(self, job):
        """Hand *job* to a worker again, as queue_job does on a retry.

        The state is stored ``started`` through the ORM, as
        ``RunJobController`` stores it, so the mirror on the cloud job
        and what a new attempt clears happen as they would.
        """
        job.queue_job_id.invalidate_recordset()
        job.queue_job_id.write({"state": "started"})
        self.env.flush_all()

    def _attempt(self, spec, job, ledger, step):
        """One attempt of *job*, ended as *step* says.

        Its ``postcommit`` gets a queue of its own: run once the attempt
        is stored and "commits", dropped when its commit is lost or the
        worker is cut off. The runner's pending writes are cleared
        either way (``end_job``).
        """
        cr = self.env.cr
        original = cr.postcommit
        cr.postcommit = armed = Callbacks()
        committed = False
        try:
            with self._outside(spec, ledger, step):
                try:
                    result = job.execute()
                except HarnessCut:
                    pass
                except RetryableJobError as exc:
                    self._store(job, "pending", str(exc))
                    committed = True
                except Exception as exc:  # noqa: BLE001 — queue_job's failure
                    self._store(job, "failed", str(exc))
                    committed = True
                else:
                    self._store(job, "done", result)
                    committed = step != LOST_COMMIT
            # Run while the queue is the cursor's: a callback finds its
            # data there (the bus's channels).
            if committed:
                self.env.flush_all()
                armed.run()
            else:
                armed.clear()
        finally:
            cr.postcommit = original
        self.env.clear()

    def _store(self, job, state, result):
        """Store *job*'s queue job as the runner would, through the ORM."""
        qjob = job.queue_job_id
        vals = {"state": state}
        if state == "done":
            vals.update(date_done=datetime.datetime.now(), result=result)
        else:
            vals.update(exc_message=result)
        qjob.write(vals)
        self.env.flush_all()

    @contextlib.contextmanager
    def _outside(self, spec, ledger, step):
        """Patch everything an attempt reaches outside the database.

        The hosts (``CloudHost.get_transport``, a ``MagicMock`` with
        ``spec=SSHTransport``), the notifications of a job's end, the
        read of the host's disk from metrics, and what *spec* patches.
        """
        cut = {"dead": False, "at": step if isinstance(step, int) else None}

        def connect(host):
            """Open a connection to *host* that writes into the ledger."""
            return self._connection(spec, ledger, cut, host)

        def notified(channel):
            """Record a notification of *channel* in the ledger."""
            def record(_model, job, state):
                ledger.calls.append(Call("notify", job.id, channel, state))
            return record

        Job = type(self.env["cloud.job"])
        patches = [
            patch.object(
                CloudHost, "get_transport", autospec=True, side_effect=connect,
            ),
            patch.object(
                Job, "_notify_by_email", autospec=True,
                side_effect=notified("email"),
            ),
            patch.object(
                Job, "_notify_external", autospec=True,
                side_effect=notified("external"),
            ),
            patch.object(AbstractExecutor, "_refresh_host_disk", autospec=True),
        ]
        executor_cls = executor_registry.get(spec.code)
        if spec.commands is not None:
            patches.append(patch.object(
                executor_cls, "get_commands", autospec=True,
                return_value=list(spec.commands),
            ))
        if spec.before is not None:
            patches.append(patch.object(
                executor_cls, "before_execute", autospec=True,
                side_effect=spec.before,
            ))
        if step == HOOK:
            success = executor_cls.on_success

            async def cut_after_success(executor, results):
                """Run ``on_success``, then die before its cursor commits."""
                await success(executor, results)
                cut["dead"] = True
                raise HarnessCut("cut at the end of on_success")

            patches.append(patch.object(
                executor_cls, "on_success", autospec=True,
                side_effect=cut_after_success,
            ))
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            for item in (spec.patches(self, ledger) if spec.patches else ()):
                stack.enter_context(item)
            yield

    def _connection(self, spec, ledger, cut, host):
        """An async context manager yielding a transport to *host*."""
        transport = MagicMock(spec=SSHTransport)

        def method(name):
            """The side effect of transport method *name*."""
            async def call(*args, **_kwargs):
                if cut["dead"]:
                    raise HarnessCut("the worker is gone")
                detail = str(args[0]) if args else ""
                if name.startswith("upload"):
                    detail = json.dumps(sorted(args[0])) if args and isinstance(
                        args[0], dict,
                    ) else detail
                ledger.calls.append(Call("ssh", host.id, name, detail))
                answer = None
                if spec.answer:
                    answer = spec.answer(ledger, host, name, detail)
                if cut["at"] is not None and len(ledger.of("ssh")) == cut["at"]:
                    cut["dead"] = True
                    raise HarnessCut(f"cut after host call {cut['at']}")
                if name in ("execute", "run"):
                    return answer or CommandResult(stdout="", exit_status=0)
                return None
            return call

        for name in ("execute", "run", "upload_text_files", "upload_file",
                     "upload_dir", "download_file"):
            getattr(transport, name).side_effect = method(name)

        @contextlib.asynccontextmanager
        async def opened():
            if cut["dead"]:
                raise HarnessCut("the worker is gone")
            yield transport

        return opened()

    # ── The photo ─────────────────────────────────────────────────────

    def _default_tracked(self, _case, job):
        """The job, its instance and its host."""
        tracked = {"job": ("cloud.job", job.id), "host": ("cloud.host", job.host_id.id)}
        if job.instance_id:
            tracked["instance"] = ("cloud.instance", job.instance_id.id)
        return tracked

    def _last_id(self, table):
        """The highest id of *table* now, 0 when it is empty."""
        self.env.cr.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
        return self.env.cr.fetchone()[0]

    def _baseline(self, tracked):
        """Where each table stood when a scenario began.

        :param dict tracked: ``{label: (model, id)}``
        :return: ``{"created": {model: last id}, "linked": {table: last id}}``
            — the models of ``CREATED_ROWS``, and the tables the tracked
            rows' many2one fields point to
        """
        created = {
            model: self._last_id(self.env[model]._table)
            for model in CREATED_ROWS if model in self.env
        }
        linked = {}
        for model, _record_id in tracked.values():
            for fld in self.env[model]._fields.values():
                if fld.type != "many2one" or not fld.store:
                    continue
                if fld.comodel_name in self.env:
                    table = self.env[fld.comodel_name]._table
                    if table not in linked:
                        linked[table] = self._last_id(table)
        return {"created": created, "linked": linked}

    def world(self, tracked, baseline):
        """Photograph what a scenario left, comparably.

        Each tracked row as the database holds it (None once deleted),
        without ``VOLATILE_COLUMNS`` and without dates — a stamp of now
        differs between two runs that did the same —, its links to rows
        created during the scenario shown as ``"<created>"`` (their ids
        differ from one scenario to the next); the rows of
        ``CREATED_ROWS`` created since *baseline*, summed up; and the
        alerts on the tracked records, by code and state.

        :param dict tracked: ``{label: (model, id)}``
        :param dict baseline: from ``_baseline``
        :rtype: dict
        """
        self.env.flush_all()
        photo = {}
        for label, (model, record_id) in tracked.items():
            self.env.cr.execute(
                f'SELECT * FROM "{self.env[model]._table}" WHERE id = %s',
                (record_id,),
            )
            row = self.env.cr.dictfetchone()
            photo[label] = row and {
                key: self._comparable(model, key, value, baseline["linked"])
                for key, value in row.items()
                if key not in VOLATILE_COLUMNS
                and not isinstance(value, (datetime.date, datetime.datetime))
            }
        for model, since in baseline["created"].items():
            self.env.cr.execute(CREATED_ROWS[model], (since,))
            photo[f"created {model}"] = sorted(
                row[0] for row in self.env.cr.fetchall()
            )
        ids = {model: record_id for model, record_id in tracked.values()}
        self.env.cr.execute(
            "SELECT code, state FROM cloud_alert"
            " WHERE instance_id = %s OR host_id = %s ORDER BY code, state",
            (ids.get("cloud.instance") or 0, ids.get("cloud.host") or 0),
        )
        photo["alerts"] = self.env.cr.fetchall()
        return photo

    def _comparable(self, model, column, value, linked):
        """*value* of *column*, or ``"<created>"`` for a link to a new row.

        A serialized column is compared by the values it holds: the
        replay of a committed outcome (``cloud.job.outcome``) writes the
        same dict back from ``jsonb``, whose keys come in another order.
        """
        fld = self.env[model]._fields.get(column)
        if value and fld and fld.type == "serialized":
            return json.loads(value)
        if not value or not fld or fld.type != "many2one":
            return value
        table = self.env[fld.comodel_name]._table
        if table in linked and value > linked[table]:
            return "<created>"
        return value

    # ── Fixtures shared by the specifications ─────────────────────────

    def dispatched_job(self, code, host, instance=None, payload=None, name=None):
        """Return a ``cloud.job`` of *code* as the runner hands it over.

        Linked to a ``queue.job`` in ``started``, by uuid and id, with
        the retry budget every ``cloud.job`` gets.

        :param str code: the job type
        :param host: the ``cloud.host`` it runs on
        :param instance: its ``cloud.instance``, if any
        :param dict payload: its payload
        """
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        self.assertTrue(job_type, f"no job type {code}")
        uuid = f"idem-{code}-{host.id}-{instance.id if instance else 0}"
        qjob = self.env["queue.job"].sudo().create({
            "uuid": uuid,
            "name": code,
            "state": "started",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
            "max_retries": 1,
        })
        job = self.env["cloud.job"].sudo().create({
            "host_id": host.id,
            "instance_id": instance.id if instance else False,
            "job_type_id": job_type.id,
            "name": name or code,
            "payload": payload or {},
            "queue_job_uuid": qjob.uuid,
            "queue_job_id": qjob.id,
        })
        return job


# ── Coverage of the job types ─────────────────────────────────────────


def _defining_class(cls, code):
    """The class of *cls*'s hierarchy that first declares *code*."""
    for klass in reversed(cls.__mro__):
        if vars(klass).get("_job_type") == code:
            return klass
    return cls


def uncovered_job_types(env, root, specs, exempt):
    """Return what the coverage test reports for the types under *root*.

    A job type belongs to the repository whose files declare it first
    (an override in another repository changes its recipe, not its
    owner), and counts only when that module is installed in *env*.

    :param env: an environment of the database under test
    :param root: the repository's directory; its addons' types are
        checked
    :param specs: the codes with a :class:`JobSpec`
    :param dict exempt: ``{code: reason}`` for the types left out on
        purpose; a reason must say why
    :return: ``(missing, unreasoned)``: the codes with neither, and the
        exemptions with an empty reason
    :rtype: tuple[list, list]
    """
    root = Path(root).resolve()
    installed = set(env["ir.module.module"].sudo().search(
        [("state", "=", "installed")],
    ).mapped("name"))
    missing = []
    for code, cls in sorted(executor_registry.all().items()):
        owner = _defining_class(cls, code)
        addon = owner.__module__.split(".")[2] if owner.__module__.startswith(
            "odoo.addons.",
        ) else None
        if addon not in installed:
            continue
        source = Path(__import__(owner.__module__, fromlist=["_"]).__file__)
        if not source.resolve().is_relative_to(root):
            continue
        if code not in specs and code not in exempt:
            missing.append(code)
    unreasoned = sorted(code for code, why in exempt.items() if not (why or "").strip())
    return missing, unreasoned
