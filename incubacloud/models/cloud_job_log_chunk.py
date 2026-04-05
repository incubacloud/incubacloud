from odoo import fields, models


class CloudJobLogChunk(models.Model):
    _name = "cloud.job.log.chunk"
    _description = "Job Log Chunk"
    _order = "id asc"

    job_id = fields.Many2one(
        "cloud.job",
        required=True,
        ondelete="cascade",
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
