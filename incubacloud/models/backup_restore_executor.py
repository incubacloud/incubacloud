from .abstract_executor import AbstractSSHExecutor, validate_dup_time


class BackupRestoreExecutor(AbstractSSHExecutor):
    """Restore a production instance from a duplicity backup.

    Follows the documented doodba restore flow:
    1. Stop Odoo
    2. duplicity restore --time TIME --force
    3. dropdb
    4. createdb
    5. psql -f $SRC/$PGDATABASE.sql
    6. docker compose up -d

    Payload:
        time: duplicity timestamp (e.g. '2026-03-19T02:00:00')
    """

    _job_type = "backup_restore"

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

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        payload = self.job.payload or {}
        time = payload['time']
        dbname = inst.postgres_dbname or 'prod'

        return [
            (
                "Stop Odoo",
                f"cd {d} && docker compose stop odoo",
            ),
            (
                "Restore from backup",
                f"cd {d} && docker compose exec -T backup"
                f" sh -c 'dup restore --time \"{time}\" --force"
                f" \"$DST\" \"$SRC\"'",
            ),
            (
                "Drop database",
                f"cd {d} && docker compose exec -T backup"
                f" dropdb --if-exists {dbname}",
                {"stop_on_failure": True},
            ),
            (
                "Create database",
                f"cd {d} && docker compose exec -T backup"
                f" createdb {dbname}",
                {"stop_on_failure": True},
            ),
            (
                "Import SQL",
                f"cd {d} && docker compose exec -T backup"
                f" sh -c 'psql -d {dbname} -f $SRC/{dbname}.sql'",
                {"stop_on_failure": True},
            ),
            (
                "Start instance",
                f"cd {d} && docker compose up -d",
            ),
        ]

    def parse_results(self, results):
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
            'status': 'ok', 'running': True,
        })

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._sys(
            "⚠ Restore failed. The instance may be in an"
            " inconsistent state. Check the logs and try"
            " starting the instance manually."
        )
        self._inst().write({'status': 'error'})
