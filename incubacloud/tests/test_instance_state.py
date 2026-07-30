"""
Tier 2 — the cloud.instance lifecycle state machine.

``state`` replaces the old pair of "is it deployed?" booleans, so these
tests pin down the whole contract: which moves are legal, which are
refused, that ``deployed`` stays a faithful derivation of it, and that
the ways into and out of the machine (deploy, teardown, import, unlink)
go through ``_transition`` instead of writing fields by hand.

The transition matrix is exhaustive on purpose: every ordered pair of
states is either asserted legal or asserted refused, so adding a state
without deciding its edges fails here.
"""

from itertools import product

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

ALL_STATES = ("draft", "deploying", "deployed", "deleting")

# The one true map, mirrored from cloud.instance._STATE_TRANSITIONS. It
# is duplicated rather than imported so a silent edit to the model does
# not silently edit its own test.
LEGAL = {
    ("draft", "deploying"),
    ("draft", "deployed"),
    ("deploying", "deployed"),
    ("deploying", "draft"),
    ("deployed", "deleting"),
    ("deleting", "draft"),
    ("deleting", "deployed"),
}


class InstanceStateCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "P"})
        self._seq = 0

    def _instance(self, **kw):
        # Names are unique per project, and the matrix tests build many
        # instances inside one transaction.
        self._seq += 1
        return self.env["cloud.instance"].create(
            {
                "name": f"inst{self._seq}",
                "project_id": self.project.id,
                "environment": "staging",
            }
            | kw
        )

    def _in_state(self, state):
        """Return an instance parked in *state* via legal moves only."""
        inst = self._instance()
        if state == "draft":
            return inst
        if state == "deploying":
            inst._transition("deploying")
            return inst
        inst._transition("deployed")
        if state == "deleting":
            inst._transition("deleting")
        return inst


class TestDefaults(InstanceStateCase):

    def test_new_instance_is_a_draft(self):
        self.assertEqual(self._instance().state, "draft")

    def test_new_instance_is_not_deployed(self):
        self.assertFalse(self._instance().deployed)

    def test_status_is_health_only(self):
        """'provisioning' is gone: lifecycle lives in ``state``."""
        selection = dict(
            self.env["cloud.instance"]._fields["status"].selection,
        )
        self.assertEqual(set(selection), {"ok", "warning", "error"})


class TestTransitionMatrix(InstanceStateCase):

    def test_every_ordered_pair_is_decided(self):
        for origin, target in product(ALL_STATES, ALL_STATES):
            inst = self._in_state(origin)
            with self.subTest(origin=origin, target=target):
                if (origin, target) in LEGAL:
                    inst._transition(target)
                    self.assertEqual(inst.state, target)
                else:
                    with self.assertRaises(UserError):
                        inst._transition(target)
                    self.assertEqual(inst.state, origin)

    def test_unknown_target_is_refused(self):
        inst = self._instance()
        with self.assertRaises(UserError):
            inst._transition("archived")

    def test_error_names_both_ends(self):
        inst = self._in_state("deployed")
        with self.assertRaises(UserError) as caught:
            inst._transition("deploying")
        message = str(caught.exception)
        self.assertIn("deployed", message)
        self.assertIn("deploying", message)


class TestDerivedDeployed(InstanceStateCase):

    def test_true_only_while_the_stack_exists_on_the_host(self):
        expected = {
            "draft": False,
            "deploying": False,
            "deployed": True,
            # Teardown has not finished: the stack is still up there.
            "deleting": True,
        }
        for state, deployed in expected.items():
            with self.subTest(state=state):
                self.assertEqual(self._in_state(state).deployed, deployed)

    def test_follows_state_without_an_extra_write(self):
        inst = self._in_state("deployed")
        self.assertTrue(inst.deployed)
        inst._transition("deleting")
        self.assertTrue(inst.deployed)
        inst._transition("draft")
        self.assertFalse(inst.deployed)

    def test_searchable(self):
        """Stored, because crons search on it."""
        inst = self._in_state("deployed")
        found = self.env["cloud.instance"].search(
            [("id", "=", inst.id), ("deployed", "=", True)],
        )
        self.assertEqual(found, inst)


class TestWriteGuards(InstanceStateCase):

    def test_writing_state_directly_is_refused(self):
        inst = self._instance()
        with self.assertRaises(UserError):
            inst.write({"state": "deployed"})
        self.assertEqual(inst.state, "draft")

    def test_writing_deployed_is_refused(self):
        """Odoo would accept and silently discard it; we refuse it."""
        inst = self._instance()
        with self.assertRaises(UserError):
            inst.write({"deployed": True})

    def test_other_writes_still_work(self):
        inst = self._instance()
        inst.write({"status": "warning"})
        self.assertEqual(inst.status, "warning")

    def test_create_may_seed_deployed_for_an_import(self):
        """Importing an already-running instance is the one legal entry."""
        self.assertTrue(self._instance(state="deployed").deployed)

    def test_a_copy_starts_as_a_draft(self):
        inst = self._in_state("deployed")
        self.assertEqual(inst.copy({"name": "inst-copy"}).state, "draft")


class TestLifecycleEntryPoints(InstanceStateCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "H",
                "ip_address": "10.0.0.1",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
            }
        )

    def test_deploy_enters_deploying(self):
        inst = self._instance(host_id=self.host.id)
        inst.deploy()
        self.assertEqual(inst.state, "deploying")

    def test_deploy_twice_is_refused(self):
        inst = self._instance(host_id=self.host.id)
        inst.deploy()
        with self.assertRaises(UserError):
            inst.deploy()

    def test_deploy_of_a_deployed_instance_is_refused(self):
        inst = self._in_state("deployed")
        inst.write({"host_id": self.host.id})
        with self.assertRaises(UserError) as caught:
            inst.deploy()
        self.assertIn("already deployed", str(caught.exception))

    def test_finalize_removal_keeping_the_record(self):
        inst = self._in_state("deleting")
        inst.write({"running": True})
        inst._finalize_removal(keep_in_panel=True)
        self.assertEqual(inst.state, "draft")
        self.assertFalse(inst.running)

    def test_finalize_removal_dropping_the_record(self):
        inst = self._in_state("deleting")
        inst._finalize_removal(keep_in_panel=False)
        self.assertFalse(inst.exists())

    def test_finalize_removal_of_an_already_draft_record(self):
        """Tearing down a never-deployed instance must not raise.

        A deploy that fails rolls the instance back to 'draft', and the
        teardown job that cleans it up skips its own 'deleting' step for
        the same reason — so the record arrives here already 'draft'.
        ``draft → draft`` is not a legal move, so finalising must treat
        it as a no-op instead of raising inside the job hook's cursor
        (which rolled back the unlink and stranded the record).
        """
        inst = self._instance()
        self.assertEqual(inst.state, "draft")
        inst._finalize_removal(keep_in_panel=False)
        self.assertFalse(inst.exists())

    def test_finalize_removal_of_an_already_draft_record_kept(self):
        """Same path with ``keep_in_panel``: stays a draft, stays alive."""
        inst = self._instance()
        inst.write({"running": True})
        inst._finalize_removal(keep_in_panel=True)
        self.assertTrue(inst.exists())
        self.assertEqual(inst.state, "draft")
        self.assertFalse(inst.running)


class TestUnlinkGuard(InstanceStateCase):

    def test_a_draft_can_be_dropped(self):
        inst = self._instance()
        inst.unlink()
        self.assertFalse(inst.exists())

    def test_a_deployed_instance_cannot(self):
        inst = self._in_state("deployed")
        with self.assertRaises(UserError) as caught:
            inst.unlink()
        self.assertIn("still deployed", str(caught.exception))

    def test_an_instance_with_a_job_in_flight_cannot(self):
        for state in ("deploying", "deleting"):
            with self.subTest(state=state):
                inst = self._in_state(state)
                with self.assertRaises(UserError) as caught:
                    inst.unlink()
                self.assertIn("job in flight", str(caught.exception))
