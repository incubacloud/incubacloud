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
"""
import re

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.misc import file_open

from odoo.addons.incubacloud.models.encrypted_char import EncryptedFieldMixin


@tagged("post_install", "-at_install")
class TestNeutralize(TransactionCase):

    def _scripts(self):
        """Return the statements of every installed module's script.

        Comments are stripped rather than skipped: every statement in
        these files is preceded by one, and dropping commented parts
        would hide most of the script from the checks.
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
                (module.name, " ".join(part.lower().split()))
                for part in body.split(";")
                if part.strip()
            )
        return statements

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
