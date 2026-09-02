"""Publish a host's GitHub webhook allowlist into its Traefik."""

from ..github.edge import EDGE_CONFIG_FILENAME
from .abstract_executor import AbstractSSHExecutor

_TMP = '/tmp/.incubacloud-github-webhook.yml'


class PushGitHubWebhookEdgeExecutor(AbstractSSHExecutor):
    """Write ``~/traefik/dynamic/github-webhook.yml`` and let Traefik reload.

    Uploading to a temporary path and moving it into place keeps the
    watched directory from ever holding a half-written document, which
    Traefik would parse, reject, and leave the routers gone until the
    next run.

    Removal comes first and always runs. A host that should no longer
    carry an allowlist — the feature turned off, its last instance gone,
    the published ranges unreadable — must end this job with the file
    absent, because a stale allowlist rejects deliveries silently while
    no allowlist at all merely leaves them unfiltered.
    """

    _job_type = 'push_github_webhook_edge'

    def _document(self):
        """Return the rendered allowlist for this host, cached per run.

        :rtype: str
        """
        if not hasattr(self, '_edge_document'):
            self._edge_document = self.job.host_id._github_webhook_document()
        return self._edge_document

    async def before_execute(self, transport):
        """Upload the rendered document next to the command installing it."""
        document = self._document()
        if not document:
            self._sys(
                'ℹ Nothing to protect on this host — removing any '
                'allowlist it still carries.'
            )
            return
        await transport.upload_text_files({_TMP: document})
        self._sys('✓ GitHub webhook allowlist prepared.')

    def get_commands(self):
        commands = [(
            'Remove stale GitHub webhook allowlist',
            f'rm -f ~/traefik/dynamic/{EDGE_CONFIG_FILENAME}',
        )]
        if self._document():
            commands.append((
                'Install GitHub webhook allowlist',
                'mkdir -p ~/traefik/dynamic'
                f' && mv {_TMP} ~/traefik/dynamic/{EDGE_CONFIG_FILENAME}',
            ))
        return commands

    async def on_success(self, results):
        """Record what was published so the refresh can skip an unchanged host."""
        host = self.job.host_id
        host.write({
            'github_webhook_edge_hash':
                host._github_webhook_digest(self._document()),
        })
        self._sys(
            '✓ GitHub webhook allowlist published.' if self._document()
            else '✓ GitHub webhook allowlist removed.'
        )

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f'✗ {err}')
