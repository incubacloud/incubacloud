from datetime import timedelta

from odoo import api, fields, models


# Batch size used by the purge cron. Big enough to make material progress
# in one tick, small enough to keep the DELETE transaction short and let
# concurrent inserts (live SSH streams) acquire row locks quickly.
_PURGE_BATCH = 50000


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
        for a week still shows complete output. Returns the row count
        deleted in this batch — re-run until 0 for full cleanup.
        """
        if not days or days <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=days)
        chunks = self.sudo().search(
            [
                ('create_date', '<', cutoff),
                ('job_id.state', 'in', ('done', 'failed', 'cancelled')),
            ],
            order='id',
            limit=_PURGE_BATCH,
        )
        n = len(chunks)
        if n:
            chunks.unlink()
        return n
