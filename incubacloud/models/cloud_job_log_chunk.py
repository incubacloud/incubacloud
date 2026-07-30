import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# Batch size used by the purge cron. Big enough to make material progress
# per pass, small enough that a single ``unlink`` never materialises a
# multi-million-row recordset in memory.
_PURGE_BATCH = 50000
# Ceiling on batches per cron tick. Bounds the worst-case run the first
# time a retention window is enabled on a database with years of history;
# whatever is left over is picked up by the next tick.
_PURGE_MAX_BATCHES = 40


class CloudJobLogChunk(models.Model):
    _name = "cloud.job.log.chunk"
    _description = "Job Log Chunk"
    _order = "id asc"

    job_id = fields.Many2one(
        "cloud.job",
        required=True,
        ondelete="cascade",
        # Index: every read path filters by job_id (terminal page, log
        # download, streaming append). Without an index this becomes a
        # seq scan once the table grows past a few hundred thousand rows.
        index=True,
    )
    source = fields.Selection([
        ('stdout', 'STDOUT'),
        ('stderr', 'STDERR'),
        ('system', 'System'),
    ], required=True)
    content = fields.Text(
        required=True,
    )

    def _format(self):
        return [{
            "id": chunk.id,
            "source": chunk.source,
            "content": chunk.content,
            "ts": chunk.create_date.strftime('%Y-%m-%d %H:%M:%SZ')
                  if chunk.create_date else "",
        } for chunk in self]

    @api.model
    def _purge_old(self, days):
        """Delete chunks older than *days* belonging to terminal jobs.

        Active jobs (started/pending/enqueued) keep their full log even if
        the rows are old, so a long-running deploy that's been streaming
        for a week still shows complete output.

        Deletes in batches until the backlog is exhausted or
        ``_PURGE_MAX_BATCHES`` is reached. Looping is the whole point: the
        cron fires once a day, so a single batch capped the purge at
        ``_PURGE_BATCH`` rows per day. Any panel producing chunks faster
        than that would grow without bound *while showing retention as
        configured and working*.

        Returns the total number of rows deleted in this run.
        """
        if not days or days <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=days)
        domain = [
            ('create_date', '<', cutoff),
            ('job_id.state', 'in', ('done', 'failed', 'cancelled')),
        ]
        total = 0
        for _pass in range(_PURGE_MAX_BATCHES):
            chunks = self.sudo().search(domain, order='id', limit=_PURGE_BATCH)
            if not chunks:
                return total
            total += len(chunks)
            chunks.unlink()
        # Ceiling reached with matching rows still on the table. Say so:
        # silence here would read exactly like "retention is keeping up"
        # while the backlog quietly outlives every run.
        _logger.warning(
            "cloud.job.log.chunk: purge stopped at the %d-batch ceiling "
            "after deleting %d row(s); chunks older than %d day(s) remain "
            "and will be picked up by the next run.",
            _PURGE_MAX_BATCHES, total, days,
        )
        return total
