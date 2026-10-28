"""Re-ship a host's proxy trust declaration without a full setup run.

``forwardedHeaders.trustedIPs`` is static configuration: Traefik reads it
once at start-up, so unlike the documents it watches, a change here only
takes effect when the proxy restarts. That makes this a job of its own
rather than a file drop — and it is why the ranges are re-shipped only
when they actually moved, since every push costs the host a proxy
restart and the connections in flight through it.
"""

import shlex

from . import _config_snapshot_diff as _snapshot_diff
from . import acme_store
from .abstract_executor import AbstractSSHExecutor
from .full_setup_executor import ProxyStagingMixin

#: Snapshot keys this job is allowed to declare applied.
_PROXY_KEYS = {'trusted_proxy_ranges', 'block_direct_access', 'cdn_client_ca'}


class PushTrustedProxiesExecutor(
    ProxyStagingMixin, acme_store.AcmeStorePruneMixin, AbstractSSHExecutor,
):
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
        await self._stage_proxy_documents(transport, host)
        self._sys(
            f'✓ Trusted proxy configuration prepared ({len(ranges)} '
            f'range{"" if len(ranges) == 1 else "s"}'
            f'{", direct access refused" if host.block_direct_access else ""}).'
        )

    def _refresh_firewall_sets(self):
        """Return the command that puts today's ranges in the firewall.

        Traefik and the firewall have to believe the same list. Traefik
        gets it from the documents this job uploads; the firewall gets
        it from the hardening playbook, which runs on Ansible and only
        at host creation or a deliberate re-hardening. So the list the
        firewall enforced was whatever it was handed the day the host
        was hardened, and a range the CDN added afterwards was accepted
        by the proxy and dropped before reaching it. This closes that
        gap without needing Ansible.

        Written as one ``nft -f`` document because nftables applies a
        file as a single transaction: flushing and refilling in two
        commands leaves a window, however short, in which the set is
        empty and the rule referring to it matches nobody.

        Skips on a host whose ruleset has no such set — one hardened
        before the sets existed, or one not filtering at all. The next
        hardening run creates them; until then the inline ranges it
        already has keep working.

        The IPv6 set is refreshed only where it exists: a host hardened
        while the ranges had no IPv6 has neither the set nor the rules
        naming it, and nft refuses a whole document that names a set the
        host lacks -- the IPv4 refresh went down with it. Its IPv6
        ranges wait for the next hardening run, which renders the set
        and its rules, and the step says so. Where the set exists and
        the ranges no longer have IPv6, it is emptied: the proxy stopped
        trusting them, and a hardening run would render no IPv6 set.

        :return: a ``(label, command)`` pair, or ``None`` when this host
            has no allowlist to refresh
        :rtype: tuple | None
        """
        host = self.job.host_id
        if not (host.behind_cdn and host.block_direct_access):
            return None
        ranges = host._effective_trusted_proxy_ranges()
        v4 = [r for r in ranges if ':' not in r]
        v6 = [r for r in ranges if ':' in r]
        # An empty v4 list is what the hardening playbook treats as "no
        # allowlist at all", so there is no set to refresh either, and
        # flushing to empty would take the host off the network.
        if not v4:
            return None
        lines4 = [
            'flush set inet filter ic_cdn_v4',
            'add element inet filter ic_cdn_v4 { %s }' % ', '.join(v4),
        ]
        lines6 = ['flush set inet filter ic_cdn_v6']
        if v6:
            lines6.append(
                'add element inet filter ic_cdn_v6 { %s }' % ', '.join(v6),
            )
        document4 = shlex.quote('\n'.join(lines4) + '\n')
        document6 = shlex.quote('\n'.join(lines6) + '\n')
        no_v6_set = (
            'echo "no IPv6 allowlist set on this host: its IPv6 ranges wait'
            ' for the next hardening run"' if v6 else ':'
        )
        # One document, so one transaction: the IPv6 lines join it only
        # when the host has the set they name.
        return (
            'Refresh the firewall allowlist',
            'if sudo nft list set inet filter ic_cdn_v4 >/dev/null 2>&1;'
            f' then doc={document4};'
            ' if sudo nft list set inet filter ic_cdn_v6 >/dev/null 2>&1;'
            f' then doc="$doc"{document6}; else {no_v6_set}; fi;'
            ' printf %s "$doc" | sudo nft -f -'
            ' && echo "firewall allowlist refreshed";'
            ' else echo "no allowlist set on this host; the next'
            ' hardening run creates it"; fi',
        )

    def get_commands(self):
        stage = self._job_path
        ships_cert, ships_ca = self._proxy_ships()
        put = 'ic_put() { mv "$1" "$2" 2>/dev/null || sudo mv "$1" "$2"; };'
        if ships_cert:
            install_cert = (
                f'{put}'
                ' mkdir -p ~/traefik/certs 2>/dev/null'
                ' || sudo mkdir -p ~/traefik/certs;'
                f' ic_put {stage("default.crt")} ~/traefik/certs/default.crt'
                f' && ic_put {stage("default.key")} ~/traefik/certs/default.key'
                ' && { chmod 600 ~/traefik/certs/default.key 2>/dev/null'
                ' || sudo chmod 600 ~/traefik/certs/default.key; }'
                f' && ic_put {stage("tls.yml")} ~/traefik/dynamic/tls-default.yml'
            )
        else:
            install_cert = (
                'rm -f ~/traefik/dynamic/tls-default.yml;'
                ' rm -f ~/traefik/certs/default.crt'
                ' ~/traefik/certs/default.key 2>/dev/null'
                ' || sudo rm -f ~/traefik/certs/default.crt'
                ' ~/traefik/certs/default.key'
            )
        if ships_ca:
            require_ca = (
                f'{put}'
                f' ic_put {stage("cdn-ca.pem")} ~/traefik/certs/cdn-client-ca.pem'
                f' && ic_put {stage("client-auth.yml")}'
                ' ~/traefik/dynamic/tls-client-auth.yml'
            )
        else:
            require_ca = (
                'rm -f ~/traefik/dynamic/tls-client-auth.yml'
                ' ~/traefik/certs/cdn-client-ca.pem 2>/dev/null'
                ' || sudo rm -f ~/traefik/dynamic/tls-client-auth.yml'
                ' ~/traefik/certs/cdn-client-ca.pem'
            )
        commands = [
            (
                # The static file names the dynamic *directory*, which the
                # container only has once a Full Setup mounted it. This job
                # does not ship the compose file, so on a host set up
                # before the directory form existed the file is put back
                # to the single-file form it is running with: a provider
                # pointing at a directory the container does not have
                # loses every middleware the entrypoints name, and the
                # proxy answers nothing. Such a host keeps its proxy as it
                # was and takes the directory at its next Full Setup.
                'Move Traefik configuration',
                'if ! docker inspect inverseproxy-proxy-1'
                ' --format "{{range .Mounts}}{{.Destination}} {{end}}"'
                ' 2>/dev/null | grep -q "/etc/traefik/dynamic "; then'
                f' sed -i "s#^\\(\\s*\\)directory: /etc/traefik/dynamic#'
                f'\\1filename: /etc/traefik/config.yml#" {stage("traefik.yml")}'
                ' && echo "proxy not yet set up for the dynamic directory;'
                ' keeping the single-file provider until its next Full Setup";'
                ' fi;'
                f' mv {stage("traefik.yml")} ~/traefik/traefik.yml'
                f' && mv {stage("config.yml")} ~/traefik/config.yml',
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
                # A host given its own certificate serves it as the
                # default; one without gets the document removed, since a
                # store naming files that are not there makes Traefik
                # answer every handshake with a throwaway certificate.
                'Install the default certificate',
                # ``sudo`` only as a fallback, and only for this
                # directory. Traefik runs as root inside its container
                # and owns ``certs/`` on any host it has already
                # written an ACME store into, so a host whose jobs run
                # as the unprivileged operator the hardening playbook
                # creates cannot move a file in there — measured on
                # Tenants1, where every other step of this job
                # succeeded. Trying the plain move first keeps a host
                # without sudo working exactly as it did. Which branch
                # runs is decided by what this job staged.
                install_cert,
            ),
        ]
        commands.append((
            # Same interlock as the default certificate: an authority
            # makes the proxy require the CDN's client certificate; none
            # removes the requirement. Applied with the restart below.
            'Require the CDN client certificate',
            require_ca,
        ))
        prune = self._acme_prune_step()
        if prune:
            commands.append(prune)
        commands.append((
            'Restart Traefik',
            'cd ~/traefik && docker compose -p inverseproxy'
            ' -f inverseproxy.yaml restart proxy',
        ))
        confirm = self._acme_confirm_step()
        if confirm:
            commands.append(confirm)
        firewall = self._refresh_firewall_sets()
        if firewall:
            commands.append(firewall)
        return commands

    def parse_results(self, results):
        """Fail the job unless the retirement is confirmed on the host.

        The write step reports the exit status of a redirection, which
        succeeds over a store that ends up truncated and says nothing
        about the proxy having come back. Without this the job would
        report success while the host went on serving the certificates
        it was told to stop serving — the exact silence this whole
        change exists to end.
        """
        return (
            super().parse_results(results)
            + self._acme_prune_errors(results)
        )

    async def on_success(self, results):
        """Record what the host is now running, so nothing publishes early.

        Everything downstream that has to agree with the host's proxy
        posture — an edge allowlist above all — reads this rather than
        what the panel intends, because the two differ for as long as a
        change is queued.
        """
        host = self.job.host_id
        vals = {
            'trusted_proxies_shipped':
                '\n'.join(host._effective_trusted_proxy_ranges()),
        }
        # Re-anchor the drift indicator, but only when the proxy fields
        # are the *only* thing that moved since the last full setup.
        # This job ships two documents; a full setup ships those plus the
        # compose file, the panel password and the whitelist, so claiming
        # all of it was applied would hide a real pending change.
        moved = set(_snapshot_diff.diff_keys(
            host.applied_config_snapshot or {}, host._render_config_snapshot(),
        ))
        if moved and not moved - _PROXY_KEYS:
            vals.update(host._applied_config_vals())
        host.write(vals)
        self._sys('✓ Traefik restarted with the new proxy trust settings.')

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f'✗ {err}')
