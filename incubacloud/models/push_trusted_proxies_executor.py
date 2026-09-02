"""Re-ship a host's proxy trust declaration without a full setup run.

``forwardedHeaders.trustedIPs`` is static configuration: Traefik reads it
once at start-up, so unlike the documents it watches, a change here only
takes effect when the proxy restarts. That makes this a job of its own
rather than a file drop — and it is why the ranges are re-shipped only
when they actually moved, since every push costs the host a proxy
restart and the connections in flight through it.
"""

from .abstract_executor import AbstractSSHExecutor
from .full_setup_executor import _TMP


class PushTrustedProxiesExecutor(AbstractSSHExecutor):
    """Upload the patched Traefik configuration and restart the proxy.

    Ships both documents together because they are interlocked: the
    static file names a middleware the dynamic file has to define, and a
    host whose entrypoint references a middleware its file provider does
    not declare answers 500 on every router it serves.
    """

    _job_type = 'push_trusted_proxies'

    async def before_execute(self, transport):
        """Upload the rendered static and dynamic Traefik documents."""
        host = self.job.host_id
        ranges = host._effective_trusted_proxy_ranges()
        await transport.upload_text_files({
            f'{_TMP}-traefik.yml': host._shipped_traefik_yml(),
            f'{_TMP}-config.yml': host._shipped_config_yml(),
        })
        self._sys(
            f'✓ Trusted proxy configuration prepared ({len(ranges)} '
            f'range{"" if len(ranges) == 1 else "s"}'
            f'{", direct access refused" if host.block_direct_access else ""}).'
        )

    def get_commands(self):
        return [
            (
                'Move Traefik configuration',
                f'mv {_TMP}-traefik.yml ~/traefik/traefik.yml'
                f' && mv {_TMP}-config.yml ~/traefik/config.yml',
            ),
            (
                # The file provider reads config.yml from the dynamic
                # directory on a host that has been through full setup;
                # the copy is what makes the edit visible there.
                'Refresh the dynamic configuration',
                'mkdir -p ~/traefik/dynamic'
                ' && cp ~/traefik/config.yml ~/traefik/dynamic/config.yml',
            ),
            (
                'Restart Traefik',
                'cd ~/traefik && docker compose -p inverseproxy'
                ' -f inverseproxy.yaml restart proxy',
            ),
        ]

    async def on_success(self, results):
        self._sys('✓ Traefik restarted with the new proxy trust settings.')

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f'✗ {err}')
