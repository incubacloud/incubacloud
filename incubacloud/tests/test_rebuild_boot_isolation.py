"""Structural guards on the rebuild boot test, in ``scripts/rebuild.sh``.

The boot test twice took every tenant on a host down (2026-08-13, 09:06
and 13:03). Both times the mechanism was the same: its throwaway
Postgres held an endpoint on the project's compose network, so when
``docker compose run`` decided to reconcile that network it could not
remove it — ``has active endpoints`` — and the step died leaving ``db``
and ``smtp`` stopped under a live ``odoo``.

Shell is not reachable from the Python suite, so these tests pin the
properties of the fixed script that the incident turned into
invariants. They are cheap and they fail loudly the day someone
reintroduces ``--network "${project}_default"`` because it reads like
the obvious way to let the two containers talk.

The first fix published the Postgres on the project network's gateway,
which a staging cannot reach — test.yaml makes that network
``internal`` — and doodba waits for its database forever: the first
staging rebuild in production hung for good (2026-10-03). The Postgres
now has a network of its own, which the boot container joins, and the
wait has a deadline.
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

    def test_the_throwaway_postgres_has_a_network_of_its_own(self):
        """Not the project's, and not published on any host address.

        Publishing it on the project network's gateway was the previous
        answer, and it cannot reach a staging: test.yaml makes that
        network ``internal`` and Docker drops what leaves it. The first
        staging rebuild in production waited forever (2026-10-03).
        """
        self.assertIn('docker network create "$boot_net"', self.block)
        self.assertIn('--network "$boot_net"', self.block)
        self.assertNotIn("-p ", self.block)
        self.assertNotIn(".Gateway", self.block)

    def test_the_boot_run_joins_that_network_and_points_libpq_at_it(self):
        """``compose run`` cannot attach outside the project, so it joins."""
        self.assertIn(
            'docker network connect "$boot_net" "$odoo_container"',
            self.block,
        )
        self.assertIn('-e "PGHOST=$pg_container"', self.block)

    def test_the_wait_for_postgres_has_a_deadline(self):
        """Doodba's own wait loops forever; an unreachable DB hung the job."""
        self.assertIn("-e WAIT_DB=false", self.block)
        self.assertIn("$(seq 120)", self.block)
        self.assertIn("the throwaway postgres never answered", self.block)

    def test_the_trap_removes_what_the_test_created(self):
        """The boot container and the network go with the throwaway PG."""
        restore = self.block[
            self.block.index("ic_boot_restore() {"):
            self.block.index("trap ic_boot_restore EXIT")
        ]
        self.assertIn('docker rm -f "$odoo_container"', restore)
        self.assertIn('docker rm -f "$pg_container"', restore)
        self.assertIn('docker network rm "$boot_net"', restore)

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
