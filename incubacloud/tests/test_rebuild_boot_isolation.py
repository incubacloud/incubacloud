"""Structural guards on the rebuild boot test, in ``scripts/rebuild.sh``.

The boot test twice took every tenant on a host down (2026-08-13, 09:06
and 13:03). Both times the mechanism was the same: its throwaway
Postgres held an endpoint on the project's compose network, so when
``docker compose run`` decided to reconcile that network it could not
remove it — ``has active endpoints`` — and the step died leaving ``db``
and ``smtp`` stopped under a live ``odoo``.

Shell is not reachable from the Python suite, so these tests pin the
three properties of the fixed script that the incident turned into
invariants. They are cheap and they fail loudly the day someone
reintroduces ``--network "${project}_default"`` because it reads like
the obvious way to let the two containers talk.
"""
from pathlib import Path

from odoo.tests.common import BaseCase

REBUILD_SH = (
    Path(__file__).resolve().parents[1] / "scripts" / "rebuild.sh"
)


def _boot_test_block():
    """Return just the ``boot-test)`` case arm of the script.

    Scoping the assertions to the arm keeps them honest: a match
    anywhere else in the file would not tell us anything about the step
    that caused the outage.
    """
    body = REBUILD_SH.read_text()
    start = body.index("    boot-test)")
    end = body.index("\n    *)", start)
    return body[start:end]


class TestRebuildBootTestIsolation(BaseCase):

    def setUp(self):
        super().setUp()
        self.block = _boot_test_block()

    def test_the_throwaway_postgres_is_not_on_the_project_network(self):
        """The regression itself: an endpoint there blocks reconciliation."""
        self.assertNotIn(
            '--network "${project}_default"', self.block,
            "the boot test's Postgres must not hold an endpoint on the "
            "project network: docker compose cannot then recreate that "
            "network, the step dies with 'has active endpoints' and the "
            "tenant is left half stopped.",
        )

    def test_the_throwaway_postgres_is_published_on_the_gateway(self):
        """Reachable from that bridge, and from nowhere else."""
        self.assertIn('-p "$gw::5432"', self.block)
        self.assertIn("(index .IPAM.Config 0).Gateway", self.block)

    def test_the_boot_run_points_libpq_at_the_gateway(self):
        """Both halves are needed: the port is whatever docker picked."""
        self.assertIn('-e "PGHOST=$gw"', self.block)
        self.assertIn('-e "PGPORT=$pg_port"', self.block)

    def test_an_unresolvable_gateway_aborts_instead_of_falling_back(self):
        """Silently reverting to the old behaviour would hide the outage."""
        self.assertIn(
            'cannot resolve the gateway of ${project}_default', self.block,
        )

    def test_the_stack_is_restored_from_a_trap(self):
        """``set -e`` must not be able to skip the restore.

        A cleanup written at the tail of the arm is skipped whenever an
        intermediate command fails, which is exactly when the stack most
        needs putting back.
        """
        self.assertIn("trap ic_boot_restore EXIT", self.block)
        self.assertIn("--status running", self.block)

    def test_the_restore_starts_rather_than_ups_the_services(self):
        """``up`` would deploy the very image the boot test just rejected.

        It would also reconcile networks, so the restore could trip over
        the same drift as the failure it is cleaning up after.
        """
        self.assertIn("docker compose start $running_before", self.block)
        self.assertNotIn("docker compose up", self.block)
