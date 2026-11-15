"""What writes the first steps' marks as it happens (``cloud.first.steps``).

The creation of a server, a backup destination, a project or a domain,
a change to an instance's mail relay and the end of the jobs that achieve
a mark reconcile the marks it may write, in the same request: a server
created and deleted before anyone looks at the home page still leaves its
mark. Each one in a savepoint, logged (``_touch``): a mark never makes the
operation that triggers it fail.
"""
from odoo import api, models


class CloudHost(models.Model):
    """Creating a server tries the ``server`` mark."""

    _inherit = "cloud.host"

    @api.model_create_multi
    def create(self, vals_list):
        """Create the servers, then reconcile the ``server`` mark."""
        records = super().create(vals_list)
        self.env["cloud.first.steps"]._touch(["server"])
        return records


class CloudBackupBackend(models.Model):
    """Creating a backup destination tries the ``backups`` mark."""

    _inherit = "cloud.backup.backend"

    @api.model_create_multi
    def create(self, vals_list):
        """Create the destinations, then reconcile the ``backups`` mark."""
        records = super().create(vals_list)
        self.env["cloud.first.steps"]._touch(["backups"])
        return records


class CloudProject(models.Model):
    """Creating a project achieves the ``project`` mark."""

    _inherit = "cloud.project"

    @api.model_create_multi
    def create(self, vals_list):
        """Create the projects, then reconcile the ``project`` mark."""
        records = super().create(vals_list)
        self.env["cloud.first.steps"]._touch(["project"])
        return records


class CloudInstanceDomain(models.Model):
    """Adding a domain of one's own tries the ``domain`` mark."""

    _inherit = "cloud.instance.domain"

    @api.model_create_multi
    def create(self, vals_list):
        """Create the domains, then reconcile the ``domain`` mark."""
        records = super().create(vals_list)
        self.env["cloud.first.steps"]._touch(["domain"])
        return records


class CloudInstance(models.Model):
    """Setting up an instance's mail relay tries, and its delivering achieves, ``mail``."""

    _inherit = "cloud.instance"

    def write(self, vals):
        """Write, then reconcile ``mail`` when the relay's server or state changed."""
        result = super().write(vals)
        if "smtp_relay_host" in vals or vals.get("smtp_relay_state") == "ok":
            self.env["cloud.first.steps"]._touch(["mail"])
        return result


class CloudJob(models.Model):
    """The end of a job reconciles the marks its type writes."""

    _inherit = "cloud.job"

    def _after_job_terminal(self, new_state):
        """Run core's and the layers' hooks, then reconcile the marks of *self*'s types.

        Reads the job and writes marks; when they are already achieved it
        does nothing. Never raises (``cloud.first.steps._on_job_terminal``).

        :param str new_state: ``done``, ``failed`` or ``cancelled``
        """
        result = super()._after_job_terminal(new_state)
        self.env["cloud.first.steps"]._on_job_terminal(self, new_state)
        return result
