import json
import logging
from datetime import timedelta

from odoo import fields, models

from ._repo_requirements import _normalize_url

_logger = logging.getLogger(__name__)

_AUTO_REBUILD_COOLDOWN = timedelta(seconds=60)


class CloudGitHubEvent(models.Model):
    """Log of received GitHub App webhook events.

    Events are persisted by the webhook controller immediately after HMAC
    validation.  Push events are processed to trigger auto-rebuilds.
    """

    _name = "cloud.github.event"
    _description = "GitHub Webhook Event"
    _order = "create_date desc"

    event_type = fields.Char(
        string="Event Type",
        readonly=True,
        help="Value of the X-GitHub-Event header (e.g. push, pull_request).",
    )
    action = fields.Char(
        string="Action",
        readonly=True,
        help="The 'action' field from the webhook payload (e.g. opened, closed).",
    )
    delivery_id = fields.Char(
        string="Delivery ID",
        readonly=True,
        index=True,
        help="Value of the X-GitHub-Delivery header — unique per delivery.",
    )
    payload = fields.Text(
        string="Payload",
        readonly=True,
        help="Raw JSON payload received from GitHub.",
    )
    processed = fields.Boolean(
        string="Processed",
        default=False,
        help="Set to True once business logic has handled this event.",
    )
    error = fields.Text(
        string="Processing Error",
        readonly=True,
        help="Error message if processing failed.",
    )

    def _process_push_event(self):
        """Process a push webhook — trigger auto-rebuild for matching instances."""
        self.ensure_one()
        payload = json.loads(self.payload or '{}')

        # Ignore branch deletions and tag pushes
        if payload.get('deleted'):
            self.write({'processed': True})
            return

        ref = payload.get('ref', '')
        branch = ref.removeprefix('refs/heads/')
        if not branch or branch == ref:
            # Not a branch push (e.g. tag)
            self.write({'processed': True})
            return

        repo_data = payload.get('repository', {})
        clone_url = repo_data.get('clone_url', '')
        repo_norm = _normalize_url(clone_url)
        if not repo_norm:
            self.write({'processed': True})
            return

        # Push context for job payload
        pusher = payload.get('pusher', {}).get('name', '')
        head_sha = payload.get('after', '')[:7]
        head_msg = payload.get('head_commit', {}).get('message', '').split('\n')[0][:80]
        repo_short = repo_data.get('full_name', repo_norm.split('github.com/')[-1])

        # Find instance repos matching repo+branch, not frozen
        matched = self.env['cloud.instance.repo'].search([
            ('branch', '=', branch),
            ('commit_sha', 'in', (False, '')),
        ]).filtered(
            lambda r: _normalize_url(r.url) == repo_norm
        )

        now = fields.Datetime.now()
        triggered = []

        for repo in matched:
            inst = repo.instance_id
            if not (inst.auto_rebuild and inst.deployed and inst.host_id):
                continue
            if (inst.last_auto_rebuild
                    and (now - inst.last_auto_rebuild) < _AUTO_REBUILD_COOLDOWN):
                _logger.info(
                    "Auto-rebuild skipped for %s (cooldown)", inst.name,
                )
                continue
            # Skip if there's already a running/pending job for this instance
            active_job = self.env['cloud.job'].search([
                ('instance_id', '=', inst.id),
                ('job_type_id.code', 'in',
                 ('deploy_instance', 'rebuild_instance')),
                ('state', 'in', ('pending', 'started')),
            ], limit=1)
            if active_job:
                _logger.info(
                    "Auto-rebuild skipped for %s (job %d already %s)",
                    inst.name, active_job.id, active_job.state,
                )
                continue
            try:
                self.env['cloud.job'].enqueue(
                    inst.host_id.id, inst.id, 'rebuild_instance',
                    payload={
                        'trigger': 'webhook',
                        'push_repo': repo_short,
                        'push_branch': branch,
                        'push_sha': head_sha,
                        'push_message': head_msg,
                        'push_by': pusher,
                    },
                )
                inst.write({'last_auto_rebuild': now})
                triggered.append(inst.name)
            except Exception:
                _logger.exception(
                    "Failed to enqueue auto-rebuild for %s", inst.name,
                )

        self.write({'processed': True})
        if triggered:
            _logger.info(
                "Auto-rebuild triggered for: %s (push %s@%s)",
                ', '.join(triggered), repo_norm.split('/')[-1], branch,
            )

    def _process_pull_request_event(self):
        """Process a pull_request webhook — create/rebuild/destroy PR preview instances."""
        self.ensure_one()
        payload   = json.loads(self.payload or '{}')
        action    = payload.get('action')
        pr        = payload.get('pull_request', {})
        pr_number = pr.get('number')
        head_ref  = pr.get('head', {}).get('ref')
        clone_url = pr.get('head', {}).get('repo', {}).get('clone_url', '')
        repo_full = payload.get('repository', {}).get('full_name', '')
        repo_norm = _normalize_url(clone_url)

        if not pr_number or not head_ref or not repo_norm:
            self.write({'processed': True})
            return

        if action in ('opened', 'reopened'):
            # Find production instances with this repo configured
            prod_repos = self.env['cloud.instance.repo'].search([
                ('commit_sha', 'in', (False, '')),
                ('instance_id.environment', '=', 'production'),
                ('instance_id.deployed', '=', True),
                ('instance_id.project_id.pr_reviews_enabled', '=', True),
            ]).filtered(lambda r: _normalize_url(r.url) == repo_norm)

            for prod in prod_repos.mapped('instance_id'):
                exists = self.env['cloud.instance'].search([
                    ('pr_number', '=', pr_number),
                    ('pr_repo', '=', repo_full),
                    ('project_id', '=', prod.project_id.id),
                ], limit=1)
                if exists:
                    continue
                try:
                    prod.clone_to_staging(
                        f'pr-{pr_number}',
                        pr_number=pr_number,
                        pr_repo=repo_full,
                        pr_head_branch=head_ref,
                    )
                    _logger.info(
                        "PR preview instance created for PR #%s (%s) → project %s",
                        pr_number, repo_full, prod.project_id.name,
                    )
                except Exception:
                    _logger.exception(
                        "Failed to create PR preview for PR #%s", pr_number,
                    )

        elif action == 'synchronize':
            # New commit pushed to the PR branch → rebuild existing preview instances
            instances = self.env['cloud.instance'].search([
                ('pr_number', '=', pr_number),
                ('pr_repo', '=', repo_full),
            ])
            for inst in instances:
                if not (inst.deployed and inst.host_id):
                    continue
                for repo in inst.repo_ids:
                    if _normalize_url(repo.url) == repo_norm and repo.branch != head_ref:
                        repo.write({'branch': head_ref, 'commit_sha': False})
                try:
                    self.env['cloud.job'].enqueue(
                        inst.host_id.id, inst.id, 'rebuild_instance'
                    )
                    _logger.info(
                        "PR preview rebuild triggered for %s (PR #%s)",
                        inst.name, pr_number,
                    )
                except Exception:
                    _logger.exception(
                        "Failed to enqueue PR rebuild for %s", inst.name,
                    )

        elif action == 'closed':
            # PR merged or closed → destroy preview instances
            instances = self.env['cloud.instance'].search([
                ('pr_number', '=', pr_number),
                ('pr_repo', '=', repo_full),
            ])
            for inst in instances:
                inst_name = inst.name
                if inst.pr_comment_id:
                    inst._delete_pr_comment()
                if inst.deployed and inst.host_id:
                    try:
                        self.env['cloud.job'].enqueue(
                            inst.host_id.id, inst.id, 'delete_instance'
                        )
                    except Exception:
                        _logger.exception(
                            "Failed to enqueue delete for PR instance %s", inst_name,
                        )
                else:
                    inst.unlink()
                _logger.info(
                    "PR preview instance %s queued for deletion (PR #%s closed)",
                    inst_name, pr_number,
                )

        self.write({'processed': True})
