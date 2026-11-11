from .abstract_executor import (
    COMPOSE_OP_EXIT_UNCHECKED,
    AbstractSSHExecutor,
    validate_dup_time,
)


class BackupRestoreExecutor(AbstractSSHExecutor):
    """Restore a production instance from a duplicity backup.

    Follows the documented doodba restore flow:
    1. Stop Odoo
    2. duplicity restore --time TIME --force
    3. dropdb
    4. createdb
    5. psql -f $SRC/$PGDATABASE.sql
    6. click-odoo-update, unless the instance's ``auto_update`` is off
    7. docker compose up -d

    Payload:
        time: duplicity timestamp (e.g. '2026-03-19T02:00:00')
    """

    _job_type = "backup_restore"

    #: From the drop of the database on a stop is refused: stopped there
    #: the database is left missing, half loaded or half updated, and the
    #: stop kills the ``docker compose exec`` client while ``psql`` goes on
    #: in the container (``cloud.job._no_return_refusal``).
    _no_return_from = "Drop database"

    def _inst(self):
        return self.job.instance_id

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError(
                "backup_restore job has no instance_id"
            )
        if inst.environment != 'production':
            raise ValueError(
                "Backup restore is only supported for production"
                " instances. Use Restore Database for non-production."
            )
        payload = self.job.payload or {}
        if not payload.get('time'):
            raise ValueError("Missing 'time' in job payload.")
        # The value flows into
        # ``sh -c 'dup restore --time "<value>" --force ...'``;
        # validating the format up-front is the only defense against
        # shell injection through this field.
        validate_dup_time(payload['time'])
        if inst.auto_update:
            self._say_update_target(inst)

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        payload = self.job.payload or {}
        time = payload['time']
        # ``validate_dup_time`` admits 'latest' for the download jobs,
        # which leave the flag out for it. ``dup restore --time`` takes
        # the value as it comes and only knows 'now': the revive of an
        # archived copy asks for 'latest' and failed here on every try.
        if time == 'latest':
            time = 'now'
        dbname = inst.postgres_dbname or 'prod'

        cmds = [
            (
                "Stop Odoo",
                self.run_script("compose_op.sh", [d, "stop", "odoo"]),
            ),
            (
                "Restore from backup",
                self.run_script("backup_restore.sh", ["restore", d, time]),
                # Abort before dropdb/createdb: a failed restore must never
                # fall through to destroying the production database and
                # then importing a stale/absent SQL dump.
                {"stop_on_failure": True},
            ),
            (
                "Drop database",
                self.run_script("backup_restore.sh", ["dropdb", d, dbname]),
                {"stop_on_failure": True},
            ),
            (
                "Create database",
                self.run_script("backup_restore.sh", ["createdb", d, dbname]),
                {"stop_on_failure": True},
            ),
            (
                "Import SQL",
                self.run_script("backup_restore.sh", ["import-sql", d, dbname]),
                {"stop_on_failure": True},
            ),
        ]
        # The backup was taken under the code of its day; the instance
        # runs today's, and a revive rebuilds it from its branch. Its
        # modules are brought up to that code before Odoo serves it, as
        # a rebuild would — unless this instance's rebuilds leave them
        # alone (``auto_update`` off).
        if inst.auto_update:
            cmds.append(self._update_restored_modules_step(d, inst))
        cmds.append((
            "Start instance",
            self.run_script("compose_op.sh", [d, "up"]),
            {"continue_on": (COMPOSE_OP_EXIT_UNCHECKED,)},
        ))
        return cmds

    def parse_results(self, results):
        # The database is restored and the stack up when only the check
        # after ``up`` failed: a warning, not a failed restore.
        results = self._accept_unchecked_compose_op(results, "Start instance")
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get('exit_status', 1) != 0
        ]

    async def on_success(self, results):
        self._sys(
            "✓ Database restored successfully from backup."
        )
        self._inst().write({
            'status': 'ok', **self._inst()._running_confirmed_vals(),
        })
        self._call_addon_hook(
            self._inst(), '_after_restore_hook', 'backup_restore',
        )

    async def on_abort(self, exc):
        """A restore cut off halfway fails as one whose step failed.

        A connection to the host that dropped, or a step out of its time
        (``DEFAULT_SSH_COMMAND_TIMEOUT``), ends the run with no outcome:
        Odoo stayed stopped, the database as the cut left it, and the
        instance said nothing of it. Once a step ran the restore closes
        as ``on_failure`` closes it, the step that never answered counted
        as failed. Before its steps nothing changed on the host and the
        instance keeps its status.

        :param Exception exc: what stopped the run
        """
        await self._fail_cut_off_run(exc)

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        if self._report_failed_module_update(results):
            self._inst().write({'status': 'error'})
            return
        self._sys(
            "⚠ Restore failed. The instance may be in an"
            " inconsistent state. Check the logs and try"
            " starting the instance manually."
        )
        self._inst().write({'status': 'error'})
