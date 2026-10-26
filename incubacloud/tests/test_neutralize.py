"""What ``neutralize.sql`` must clear, checked against the registry.

A copy of production restored for development still holds every
credential production holds: SSH keys that open the fleet, API tokens
that spend money, passwords to customer databases. Odoo neutralizes what
it knows about — mail servers, crons, webhooks — and leaves the rest to
each module's ``data/neutralize.sql``.

That file is a hand-written list, so it goes stale the moment a new
secret field is added and nobody remembers it. It did, in two ways at
once. Six fields were storing secrets nothing cleared. And the script
itself named a column that had been removed, which mattered far more
than it looks: neutralization runs as a single transaction, so that one
line rolled back everything — including Odoo's own — and left databases
believed to be neutralized fully armed.

So the list stops being hand-checked. The tests read the registry rather
than the file, and check both directions: nothing secret is left behind,
and nothing named has gone missing.

Scope is every installed module at once, which is how neutralization
actually runs. A module absent from the database contributes neither
fields nor a script, so a core-only install is checked against core
alone without pretending saas columns exist.

Secrets are not all a copy must lose. A field that makes it reach
outside — a user's webhook — and a cron that the copy's next module
update switches back on act on the real world with no secret at all.
``NeutralizeInventory`` checks those per repository: each one checks its
own modules and names its own exemptions, where its fields live.
"""
import os
import re

from lxml import etree

from odoo.modules.module import get_manifest, get_module_path
from odoo.modules.neutralize import neutralize_database
from odoo.tests.common import TransactionCase, tagged
from odoo.tools.misc import file_open

from odoo.addons.incubacloud.models.encrypted_char import EncryptedFieldMixin


#: A field whose name says it points somewhere: the copy would call it.
OUTBOUND_RE = re.compile(r"(^|_)(url|uri|endpoint|webhook|hook)(_|$)")


class NeutralizeInventory:
    """What a repository checks of its own modules' neutralization.

    Mixed into a ``TransactionCase``: core checks its modules with it, and
    a layer above checks its own the same way, naming its exemptions
    where its fields live. "Its modules" are the installed modules of the
    repository ``_inventory_anchor`` belongs to. The class mixing it in
    sets ``allow_inherited_tests_method``, or Odoo runs none of these.
    """

    #: A module of the repository whose modules are checked.
    _inventory_anchor = None
    #: ``{"table.column": why}``: fields that point outside the copy, that
    #: no script clears, and why that is safe.
    _outbound_exempt = {}

    def _scripts(self):
        """Return the statements of every installed module's script.

        Comments are stripped rather than skipped: every statement in
        these files is preceded by one, and dropping commented parts
        would hide most of the script from the checks. A statement
        guarded by ``DO $$ BEGIN ... EXCEPTION`` (a column newer than
        the copies it must neutralize) counts as the statement it guards.
        """
        statements = []
        installed = self.env["ir.module.module"].search([
            ("state", "in", ("installed", "to upgrade")),
        ])
        for module in installed:
            try:
                with file_open(f"{module.name}/data/neutralize.sql") as handle:
                    body = re.sub(r"--[^\n]*", "", handle.read())
            except FileNotFoundError:
                continue
            statements.extend(
                (module.name, re.sub(
                    r"^do \$\$ begin ", "", " ".join(part.lower().split())
                ))
                for part in body.split(";")
                if part.strip()
            )
        return statements

    def _clears(self, statements, table, column):
        """Return whether any script empties ``table.column``.

        A deleted row counts as cleared: several of these tables hold
        nothing but ephemeral session state and are dropped wholesale.
        """
        for _module, statement in statements:
            if re.match(rf"delete from {table}\b", statement):
                return True
            if re.match(rf"update {table}\s+set\b", statement) and re.search(
                rf"\b{column}\s*=", statement.split(" where ")[0]
            ):
                return True
        return False

    def _inventory_modules(self):
        """Return the installed modules of the anchor's repository.

        Paths are resolved: modules are linked into one addons directory,
        and their repository is where the links lead.

        :rtype: set[str]
        """
        root = os.path.dirname(os.path.realpath(
            get_module_path(self._inventory_anchor)
        ))
        installed = self.env["ir.module.module"].search([
            ("state", "in", ("installed", "to upgrade")),
        ]).mapped("name")
        return {
            name for name in installed
            if (path := get_module_path(name, display_warning=False))
            and os.path.dirname(os.path.realpath(path)) == root
        }

    def test_every_outbound_field_is_neutralized_or_exempt(self):
        """A field the copy would call is cleared, or says why it is safe.

        Encrypted fields are left to ``test_every_secret_field_is_neutralized``.
        """
        modules = self._inventory_modules()
        statements = self._scripts()
        found = set()
        for name in self.env.registry:
            model = self.env[name]
            if model._abstract or model._transient:
                continue
            found.update(
                (model._table, field.name)
                for field in model._fields.values()
                if field.store and field.type in ("char", "text")
                and field._module in modules
                and not isinstance(field, EncryptedFieldMixin)
                and OUTBOUND_RE.search(field.name)
            )
        self.assertTrue(found, "no outbound fields found — check the filter")
        missed = sorted(
            f"{table}.{column}"
            for table, column in found
            if f"{table}.{column}" not in self._outbound_exempt
            and not self._clears(statements, table, column)
        )
        self.assertFalse(
            missed,
            "a neutralized copy would still reach out through these fields; "
            "clear them in data/neutralize.sql or say why they are safe: %s"
            % ", ".join(missed),
        )
        stale = sorted(
            key for key in self._outbound_exempt
            if tuple(key.split(".", 1)) not in found
        )
        self.assertFalse(stale, "exemptions for fields that are gone: %s" % stale)

    def test_no_updatable_cron_switches_itself_on(self):
        """A copy's crons, switched off, stay off through its next update.

        Odoo's neutralization switches every cron off; a record outside
        ``noupdate`` that declares ``active`` writes it again on each
        update. Left out, a new cron is still born active.
        """
        offending = []
        for module in sorted(self._inventory_modules()):
            for path in get_manifest(module).get("data", ()):
                if not path.endswith(".xml"):
                    continue
                with file_open(f"{module}/{path}", "rb") as handle:
                    root = etree.parse(handle).getroot()
                for record in root.iter("record"):
                    if record.get("model") != "ir.cron" or any(
                        node.get("noupdate") in ("1", "True", "true")
                        for node in (record, *record.iterancestors())
                    ):
                        continue
                    offending.extend(
                        f"{module}/{path}: {record.get('id')}"
                        for field in record.iterfind("field[@name='active']")
                        if (field.get("eval") or field.text or "").strip()
                        in ("True", "1", "true")
                    )
        self.assertFalse(
            offending,
            "these crons switch themselves back on when a neutralized copy "
            "is updated; leave 'active' out: %s" % ", ".join(offending),
        )


@tagged("post_install", "-at_install")
class TestNeutralize(NeutralizeInventory, TransactionCase):

    #: Odoo collects only the class's own methods otherwise.
    allow_inherited_tests_method = True
    _inventory_anchor = "incubacloud"
    _outbound_exempt = {
        "cloud_backup_backend.s3_endpoint_url":
            "its credentials are cleared: nothing reaches it",
        "cloud_instance_repo.url": "a repository the copy reads, never writes",
        "cloud_project_repo.url": "a repository the copy reads, never writes",
        "cloud_settings.copier_template_url": "a public template the copy reads",
        "cloud_settings.grafana_base_url": "read only while metrics are on",
        "cloud_settings.metrics_central_url": "read only while metrics are on",
        "cloud_settings.metrics_remote_write_url": "read only while metrics are on",
    }

    def _secret_columns(self):
        """Return ``(table, column)`` for every encrypted field stored."""
        found = set()
        for name in self.env.registry:
            model = self.env[name]
            if model._abstract or model._transient:
                continue
            found.update(
                (model._table, field.name)
                for field in model._fields.values()
                if isinstance(field, EncryptedFieldMixin) and field.store
            )
        return found

    def test_every_secret_field_is_neutralized(self):
        secrets = self._secret_columns()
        self.assertTrue(secrets, "no encrypted fields found — check the filter")
        statements = self._scripts()
        missed = sorted(
            f"{table}.{column}"
            for table, column in secrets
            if not self._clears(statements, table, column)
        )
        self.assertFalse(
            missed,
            "neutralization leaves these secrets in a restored copy of "
            "production: %s" % ", ".join(missed),
        )

    def test_no_script_names_a_column_that_is_gone(self):
        """One stale column aborts neutralization entirely, not its line."""
        stale = []
        for module, statement in self._scripts():
            update = re.match(r"update (\w+)\s+set (.*)", statement)
            delete = re.match(r"delete from (\w+)", statement)
            if delete:
                self.env.cr.execute("SELECT to_regclass(%s)", (delete.group(1),))
                if not self.env.cr.fetchone()[0]:
                    stale.append(f"{module}: {delete.group(1)} (table)")
                continue
            if not update:
                continue
            table = update.group(1)
            self.env.cr.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = %s
                """,
                (table,),
            )
            present = {row[0] for row in self.env.cr.fetchall()}
            if not present:
                stale.append(f"{module}: {table} (table)")
                continue
            stale.extend(
                f"{module}: {table}.{column}"
                for column in re.findall(
                    r"([\w]+)\s*=", update.group(2).split(" where ")[0]
                )
                if column not in present
            )
        self.assertFalse(
            stale,
            "these scripts name columns that no longer exist, which aborts "
            "neutralization and leaves the database armed: %s"
            % ", ".join(sorted(stale)),
        )

    def test_a_copy_from_before_the_instance_key_is_neutralized(self):
        """A copy taken before 1.0.189 has no ``cloud_instance.secret_key``.

        qa-refresh, a neutralized download and a restore into devel all
        neutralize a copy before upgrading it; naming that column aborted
        the whole neutralization, and the QA refresh of 19-oct-2026 with it.
        """
        host = self.env["cloud.host"].create({
            "name": "neutralize-old-copy",
            "ip_address": "192.0.2.10",
            "user": "root",
            "wildcard_domain": "old-copy.example.com",
            "password": "kept-by-production",
        })
        self.env.flush_all()
        self.env.cr.execute("ALTER TABLE cloud_instance DROP COLUMN secret_key")
        neutralize_database(self.env.cr)
        self.env.cr.execute(
            "SELECT active, password FROM cloud_host WHERE id = %s", (host.id,)
        )
        self.assertEqual(self.env.cr.fetchone(), (False, ""))
        self.env.cr.execute(
            "SELECT value FROM ir_config_parameter"
            " WHERE key = 'database.is_neutralized'"
        )
        self.assertEqual(self.env.cr.fetchone(), ("true",))

    def test_a_neutralized_copy_reaches_nothing_outside(self):
        """No webhook, no metrics, no job of production's, no open console.

        A job of production's is one dispatched and not finished, or one
        waiting in its target's queue.
        """
        host = self.env["cloud.host"].create({
            "name": "neutralize-outside",
            "ip_address": "192.0.2.11",
            "user": "root",
            "wildcard_domain": "outside.example.com",
        })
        self.env.user.cloud_webhook_url = "https://hooks.example.com/incubacloud"
        self.env["cloud.settings"]._get_system().metrics_enabled = True
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1,
        ) or self.env["cloud.job.type"].create({
            "name": "host_probe", "code": "host_probe", "apply_to": "host",
        })
        jobs = {}
        for state in ("pending", "started", "done"):
            qjob = self.env["queue.job"].sudo().create({
                "uuid": f"neutralize-{state}",
                "name": f"qj-{state}",
                "state": state,
                "method_name": "noop",
                "model_name": "cloud.job",
                "func_string": "noop()",
            })
            jobs[state] = self.env["cloud.job"].sudo().create({
                "host_id": host.id,
                "job_type_id": job_type.id,
                "name": f"Job {state}",
                "queue_job_uuid": qjob.uuid,
                "queue_job_id": qjob.id,
            })
        # Waiting in the host's queue, with no queue.job yet; and waiting
        # for an alert, with no state at all.
        waiting = self.env["cloud.job"].sudo().create({
            "host_id": host.id,
            "job_type_id": job_type.id,
            "name": "Job queued",
            "state": "queued",
        })
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "pip_conflict", "conflict", host=host,
        )
        blocked = self.env["cloud.job"].sudo().create({
            "host_id": host.id,
            "job_type_id": job_type.id,
            "name": "Job blocked",
            "state": False,
            "blocked_alert_id": alert.id,
        })
        console = self.env["cloud.host.session"].sudo().create({
            "host_id": host.id,
            "user_id": self.env.user.id,
            "session_id": "neutralize-console",
        })
        self.env.flush_all()
        neutralize_database(self.env.cr)
        cr = self.env.cr
        cr.execute("SELECT cloud_webhook_url FROM res_users WHERE id = %s",
                   (self.env.user.id,))
        self.assertEqual(cr.fetchone(), (None,))
        cr.execute("SELECT bool_or(metrics_enabled) FROM cloud_settings")
        self.assertEqual(cr.fetchone(), (False,))
        cr.execute(
            "SELECT c.name, c.state, q.state FROM cloud_job c"
            " JOIN queue_job q ON q.id = c.queue_job_id WHERE c.id IN %s"
            " ORDER BY c.id",
            (tuple(job.id for job in jobs.values()),),
        )
        self.assertEqual(cr.fetchall(), [
            ("Job pending", "cancelled", "cancelled"),
            ("Job started", "cancelled", "cancelled"),
            # What production finished stays as it ended.
            ("Job done", "done", "done"),
        ])
        cr.execute(
            "SELECT name, state FROM cloud_job WHERE id IN %s ORDER BY id",
            ((waiting.id, blocked.id),),
        )
        self.assertEqual(cr.fetchall(), [
            ("Job queued", "cancelled"),
            # Only resolving its alert would start it.
            ("Job blocked", None),
        ])
        cr.execute(
            "SELECT state, closed_at IS NOT NULL FROM cloud_host_session"
            " WHERE id = %s", (console.id,),
        )
        self.assertEqual(cr.fetchone(), ("closed", True))
