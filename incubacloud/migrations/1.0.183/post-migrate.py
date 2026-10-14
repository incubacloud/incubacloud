"""Turn the connection-rate cap on for the hosts that had it off.

The per-source cap on new connections to 80/443 shipped inert (0) until
it had been rehearsed, and its rule weighed the containers' own outbound
traffic as well. 1.0.183 restricts it to connections redirected to the
proxy and makes 50 per second the default, so hosts still at 0 take the
default too. Nothing changes on a host until its next hardening run,
which is what renders the rule; behind a CDN the CDN's allowlist
replaces it and the rate is not used.
"""


def migrate(cr, version):
    """Give the hosts still at 0 the new default of 50."""
    if not version:
        return
    cr.execute(
        "UPDATE cloud_host SET http_conn_rate = 50"
        " WHERE http_conn_rate = 0 OR http_conn_rate IS NULL"
    )
