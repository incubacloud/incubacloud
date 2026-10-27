"""Tests for deriving ``running`` from metrics (Fase 4 / A8).

The fail-safes are the point of this file: an unreachable backend must
change nothing, and an instance the backend has never reported on must
not be declared stopped. They protect the same chain — ``running``
feeds ``sleeping`` feeds ``last_activity_at`` feeds the 14-day
auto-suspend, so a false "stopped" eventually has a billing consequence.

The other half is which container the flag is about, which the fail-safes
say nothing about: a false "running" is just as wrong and far quieter,
because everything downstream simply never fires. See
``TestWhichContainerDecides``.
"""
import asyncio
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_instance_metrics import (
    _CONFIRMED_GRACE_SECONDS,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.start_instance_executor import (
    StartInstanceExecutor,
)
from odoo.addons.incubacloud.models.stop_instance_executor import (
    StopInstanceExecutor,
)

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"

#: The outer aggregation of a liveness query, and the label it groups by.
_AGGREGATION_RE = re.compile(r"^(min|max) by \((\w+)\)")
#: A query's exclusion of containers by name — the ``name`` label, not
#: one whose name ends in it — and its regex.
_NAME_EXCLUDED_RE = re.compile(r'[{,]\s*name!~"([^"]+)"')


def _samples(*pairs):
    """Build a PromQL payload of (instance_id, seconds-since-seen)."""
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"instance_id": str(iid)}, "value": [0, str(age)]}
                for iid, age in pairs
            ],
        },
    }


def _host_samples(*pairs):
    """Build a PromQL payload of (host_id, seconds-since-seen)."""
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"host_id": str(hid)}, "value": [0, str(age)]}
                for hid, age in pairs
            ],
        },
    }


def _aggregated(query, series):
    """Answer *query* over raw *series* the way the backend would.

    Only the outer ``min by`` or ``max by`` is evaluated, and the
    query's exclusion of containers by name if it has one: which age
    the backend keeps when one instance or host has several series is
    what the tests calling this are about. Like PromQL, the exclusion
    is anchored at both ends.

    :param str query: the PromQL the cron sent
    :param list series: ``(label value, seconds since seen)``, or the
        same plus the container's name, one per raw series
    :return: a PromQL payload with one sample per label value
    :rtype: dict
    """
    operator, label = _AGGREGATION_RE.match(query).groups()
    pick = min if operator == "min" else max
    excluded = _NAME_EXCLUDED_RE.search(query)
    ages = {}
    for key, age, *name in series:
        if excluded and re.fullmatch(excluded.group(1), "".join(name)):
            continue
        ages[key] = pick(age, ages.get(key, age))
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {label: str(key)}, "value": [0, str(age)]}
                for key, age in ages.items()
            ],
        },
    }


class InstanceLivenessCase(TransactionCase):

    def setUp(self):
        super().setUp()
        # The cron stamps on a cursor of its own so it can run at READ
        # COMMITTED (see ``models/_concurrency``). Without test mode
        # that cursor is a real one: its writes would commit for good
        # and leak into the next test.
        self.registry_enter_test_mode()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
        })
        self.project = self.env["cloud.project"].create({"name": "P"})
        self.inst = self.env["cloud.instance"].create({
            "name": "i1", "project_id": self.project.id,
            "environment": "staging",
        })

    def _run(self, payload=None, exc=None, odoo_payload=None,
             host_payload=None):
        # The cron stamps on a cursor of its own, so it reads what is in
        # the database rather than what is pending in this env's cache —
        # which is exactly what happens in production, where the cron
        # opens a clean transaction of its own. Flush first, or the cron
        # grades a row whose ``running`` the test only *intends* to set.
        self.env.flush_all()
        # Two queries now: who the backend covers, then how long since
        # each one's ``odoo`` container. Passing only ``payload`` answers
        # both the same way, which is the shape of a fleet where every
        # instance's odoo container is the freshest thing it reports —
        # what every test written before the split assumed. The third
        # answers which hosts report at all; none, unless a test says.
        coverage = payload or _samples()
        bodies = [
            coverage,
            coverage if odoo_payload is None else odoo_payload,
            host_payload or _host_samples(),
        ]
        responses = []
        for body in bodies:
            resp = MagicMock(spec=requests.Response)
            resp.json.return_value = body
            resp.raise_for_status.return_value = None
            responses.append(resp)
        with patch(f"{_MODULE}.requests.get",
                   side_effect=exc or responses):
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()


class TestLivenessSendsCredentials(InstanceLivenessCase):
    """The liveness query must authenticate as this panel's account.

    ``promql_query`` attaches credentials only when given BOTH halves —
    ``auth=(user, token) if (token and user) else None`` — so a caller
    that unpacks the pair and forwards just the token queries
    anonymously. The central answers 401, the cron logs a warning and
    returns, and liveness silently stops being refreshed: every
    instance keeps whatever ``running`` it last had, which then feeds
    ``sleeping`` and the auto-suspend clock.

    That is precisely what this call site did while the other two
    passed both halves, so the property is pinned on the wire rather
    than on the call.
    """

    def setUp(self):
        super().setUp()
        self.settings.write({
            "metrics_account": "acct_test",
            "metrics_remote_write_token": "s3cr3t",
        })

    def test_the_query_carries_both_halves_of_the_credential(self):
        resp = MagicMock(spec=requests.Response)
        resp.json.return_value = _samples()
        resp.raise_for_status.return_value = None
        with patch(f"{_MODULE}.requests.get", return_value=resp) as get:
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()
        self.assertEqual(
            get.call_args.kwargs["auth"], ("acct_test", "s3cr3t"),
        )


class TestLiveness(InstanceLivenessCase):

    def test_a_recently_seen_container_marks_running(self):
        self.inst.write({"running": False})
        self._run(_samples((self.inst.id, 20)))
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)

    def test_a_stale_container_marks_not_running(self):
        self.inst.write({"running": True})
        self._run(_samples((self.inst.id, 9999)))
        self.inst.invalidate_recordset()
        self.assertFalse(self.inst.running)

    def test_the_window_is_forgiving_of_one_missed_scrape(self):
        """Just over the scrape interval must not flap the flag."""
        window = self.env["cloud.instance"]._metrics_liveness_window()
        self.assertGreaterEqual(window, 90)
        self.inst.write({"running": False})
        self._run(_samples((self.inst.id, 45)))
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)


class TestLivenessFailSafes(InstanceLivenessCase):

    def test_a_timeout_absorbed_by_the_retry_still_refreshes(self):
        """One slow answer is asked again; the cron then runs as usual."""
        self.inst.write({"running": False})
        self.env.flush_all()
        responses = [requests.Timeout("slow")]
        for body in (
            _samples((self.inst.id, 20)),
            _samples((self.inst.id, 20)),
            _host_samples(),
        ):
            resp = MagicMock(spec=requests.Response)
            resp.json.return_value = body
            resp.raise_for_status.return_value = None
            responses.append(resp)
        with patch(f"{_MODULE}.requests.get", side_effect=responses) as get:
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()
        self.assertEqual(get.call_count, 4)
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)

    def test_unreachable_backend_changes_nothing(self):
        self.inst.write({"running": True})
        self._run(exc=requests.ConnectionError("down"))
        self.inst.invalidate_recordset()
        self.assertTrue(
            self.inst.running,
            "an unreachable backend must not mark instances stopped",
        )

    def test_an_uncovered_instance_is_left_alone(self):
        """No metrics for this instance = unknown, not stopped."""
        other = self.env["cloud.instance"].create({
            "name": "i2", "project_id": self.project.id,
            "environment": "staging",
        })
        self.inst.write({"running": True})
        other.write({"running": True})
        # Only i1 is reported on, and it is stale.
        self._run(_samples((self.inst.id, 9999)))
        self.inst.invalidate_recordset()
        other.invalidate_recordset()
        self.assertFalse(self.inst.running)
        self.assertTrue(
            other.running,
            "an instance the backend never reported on was clobbered",
        )

    def test_disabled_master_switch_changes_nothing(self):
        self.settings.metrics_enabled = False
        self.inst.write({"running": True})
        self._run(_samples((self.inst.id, 9999)))
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)

    def test_empty_result_changes_nothing(self):
        """A backend with no data yet must not stop the whole fleet."""
        self.inst.write({"running": True})
        self._run(_samples())
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)


class TestWhichContainerDecides(InstanceLivenessCase):
    """``running`` is about the ``odoo`` container, not about the stack.

    An instance whose app alone is stopped keeps its database and
    backup containers up. Asked about any container of the stack, the
    backend therefore answers "seen seconds ago" all night,
    the flag never falls, and everything hanging off it never happens:
    the tenant is never marked sleeping, its activity clock is refreshed
    as though somebody had visited, and the panel shows a sleeping
    tenant as running.

    Nothing fails while that is wrong, which is why it is pinned here
    rather than left to the fail-safes above.
    """

    def setUp(self):
        super().setUp()
        self.awake = self.env["cloud.instance"].create({
            "name": "i2", "project_id": self.project.id,
            "environment": "staging",
        })

    def _queries(self):
        """Return the PromQL of each query the cron sent, in order."""
        self.env.flush_all()
        responses = []
        for _ in range(3):
            resp = MagicMock(spec=requests.Response)
            resp.json.return_value = _samples((self.inst.id, 5))
            resp.raise_for_status.return_value = None
            responses.append(resp)
        with patch(f"{_MODULE}.requests.get", side_effect=responses) as get:
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()
        return [
            call.kwargs["params"]["query"] for call in get.call_args_list
        ]

    def test_three_questions_are_asked(self):
        """Who reports, whose ``odoo`` reports, and which hosts report —
        the third being what tells a stack stopped whole from one whose
        telemetry never arrived."""
        self.assertEqual(len(self._queries()), 3)

    def test_the_third_asks_which_hosts_report(self):
        """Any container at all, the agents' own included: a host whose
        every instance is stopped must still count as reporting."""
        third = self._queries()[2]
        self.assertIn("by (host_id)", third)
        self.assertNotIn("instance_id", third)

    def test_the_first_asks_who_reports_at_all(self):
        """Coverage, not liveness: it decides whose flag may be written,
        so narrowing it to ``odoo`` would make a sleeping instance
        indistinguishable from one with no agent installed."""
        self.assertNotIn("compose_service", self._queries()[0])

    def test_the_second_asks_about_the_odoo_container(self):
        """The label the agents are started with. Get it wrong and the
        query matches nothing — silently, and forever."""
        second = self._queries()[1]
        self.assertIn("container_label_com_docker_compose_service", second)
        self.assertIn('"odoo"', second)

    def test_a_stack_whose_odoo_is_gone_is_not_running(self):
        """The sleeping tenant, exactly: still reporting containers, but
        not that one."""
        self.inst.write({"running": True})
        self.awake.write({"running": True})
        self._run(
            _samples((self.inst.id, 5), (self.awake.id, 5)),
            odoo_payload=_samples((self.awake.id, 5)),
        )
        self.inst.invalidate_recordset()
        self.awake.invalidate_recordset()
        self.assertFalse(
            self.inst.running,
            "an instance whose odoo container is gone read as running",
        )
        self.assertTrue(self.awake.running)

    def test_a_stale_odoo_is_not_running_either(self):
        """Reported, but too long ago — the container went away between
        scrapes and the series has not expired yet."""
        self.inst.write({"running": True})
        self._run(
            _samples((self.inst.id, 5), (self.awake.id, 5)),
            odoo_payload=_samples((self.inst.id, 9999), (self.awake.id, 5)),
        )
        self.inst.invalidate_recordset()
        self.assertFalse(self.inst.running)


class TestWhenTheSecondQuestionCannotBeTrusted(InstanceLivenessCase):

    def test_a_fleet_with_no_odoo_container_at_all_changes_nothing(self):
        """A missing label or a broken query looks exactly like every
        instance stopping at once, and the two need opposite reactions.

        Believing it would mark the whole fleet stopped, which flips
        every sleep-eligible tenant to sleeping and hands a false answer
        to the clock behind the 14-day auto-suspend. Refusing to believe
        it costs, at worst, the flag staying where it already was.
        """
        self.inst.write({"running": True})
        self._run(_samples((self.inst.id, 5)), odoo_payload=_samples())
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)

    def test_a_second_query_that_fails_changes_nothing(self):
        """Coverage answered, liveness did not. Silence is not an
        answer, and the first query alone cannot decide this."""
        self.env.flush_all()
        self.inst.write({"running": True})
        self.env.flush_all()
        first = MagicMock(spec=requests.Response)
        first.json.return_value = _samples((self.inst.id, 5))
        first.raise_for_status.return_value = None
        with patch(
            f"{_MODULE}.requests.get",
            side_effect=[first, requests.ConnectionError("down")],
        ):
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)


class TestParkedStacks(InstanceLivenessCase):
    """A stack stopped whole reports nothing at all.

    Its containers leave the backend within a scrape of stopping, so the
    instance simply drops out of the coverage query, and "not covered"
    used to mean "leave the flag alone". The flag then stayed True for
    up to twenty minutes, until the SSH probe took it back — long enough
    for a nightly job keyed on it to start the stack behind the back of
    whatever had parked it.
    """

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HPark", "ip_address": "10.0.0.51", "user": "root",
            "wildcard_domain": "hpark.example.com",
        })
        self.inst.write({
            "host_id": self.host.id,
            "running": True,
            "metrics_last_seen": fields.Datetime.now(),
        })
        # A neighbour that still reports, so the fleet-wide guards
        # (something covered, some odoo seen) are satisfied.
        other_project = self.env["cloud.project"].create({"name": "P2"})
        self.other = self.env["cloud.instance"].create({
            "name": "i2", "project_id": other_project.id,
            "environment": "staging", "host_id": self.host.id,
        })

    def _run_parked(self, host_payload):
        """Run the cron with only the neighbour reporting containers."""
        self._run(_samples((self.other.id, 10)), host_payload=host_payload)
        self.inst.invalidate_recordset()

    def test_a_silent_instance_on_a_reporting_host_is_stopped(self):
        self._run_parked(_host_samples((self.host.id, 10)))
        self.assertFalse(self.inst.running)

    def test_it_keeps_its_last_reading(self):
        """``metrics_last_seen`` still means the last time the backend
        reported on this instance, which silence is not."""
        before = self.inst.metrics_last_seen
        self._run_parked(_host_samples((self.host.id, 10)))
        self.assertEqual(self.inst.metrics_last_seen, before)

    def test_an_instance_never_reported_on_is_left_alone(self):
        """The label map may never have reached it: silence proves nothing."""
        self.inst.write({"metrics_last_seen": False})
        self._run_parked(_host_samples((self.host.id, 10)))
        self.assertTrue(self.inst.running)

    def test_a_host_that_reports_nothing_says_nothing(self):
        self._run_parked(_host_samples())
        self.assertTrue(self.inst.running)

    def test_a_host_gone_quiet_says_nothing(self):
        self._run_parked(_host_samples((self.host.id, 9999)))
        self.assertTrue(self.inst.running)


class TestSeveralSeriesOfOneSubject(InstanceLivenessCase):
    """A recreated container leaves its old series behind for a while.

    The old container's series stays reported for minutes, ageing, next
    to the new one's: one service, two series. Read by its oldest, an
    odoo that had just come back read as unseen — on 22-oct-2026 (QA) a
    production that answered showed "Stopped", without its Connect
    button, for four minutes after a backup. And a host read by its
    oldest series went quiet whenever any container on it had just gone.
    Containers that run odoo without being the instance's own — the
    one-offs of compose and the rebuild's boot test — are left out.
    """

    def setUp(self):
        """The instance under test, on a host of its own."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HSeries", "ip_address": "10.0.0.52", "user": "root",
            "wildcard_domain": "hseries.example.com",
        })
        self.inst.write({"host_id": self.host.id})

    def _run_series(self, odoo, containers=None, hosts=()):
        """Run the cron against a backend holding these raw series.

        :param list odoo: ``(instance id, age)`` per ``odoo`` series
        :param list containers: ``(instance id, age)`` per series of any
            container of an instance; the ``odoo`` ones when omitted
        :param hosts: ``(host id, age)`` per series of any container
        """
        self.env.flush_all()

        def answer(url, params=None, **kwargs):
            """Answer one query of the cron from the raw series above.

            :param str url: the backend's query endpoint
            :param dict params: carries the PromQL as ``query``
            :return: the backend's response to it
            :rtype: requests.Response
            """
            query = params["query"]
            if "compose_service" in query:
                series = odoo
            elif "by (host_id)" in query:
                series = hosts
            else:
                series = odoo if containers is None else containers
            resp = MagicMock(spec=requests.Response)
            resp.json.return_value = _aggregated(query, series)
            resp.raise_for_status.return_value = None
            return resp

        with patch(f"{_MODULE}.requests.get", autospec=True,
                   side_effect=answer):
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()
        self.inst.invalidate_recordset()

    def test_the_freshest_odoo_series_decides(self):
        """The old container's series, 600 s old, beside the new one's."""
        self.inst.write({"running": False})
        self._run_series(odoo=[(self.inst.id, 600), (self.inst.id, 10)])
        self.assertTrue(
            self.inst.running,
            "an odoo seen 10 s ago read as unseen because of its old series",
        )

    def test_odoo_series_all_stale_is_not_running(self):
        """The freshest decides, so a stop still reads as one."""
        self.inst.write({"running": True})
        self._run_series(odoo=[(self.inst.id, 600), (self.inst.id, 400)])
        self.assertFalse(self.inst.running)

    def test_a_one_off_odoo_does_not_pass_for_the_service(self):
        """``docker compose run odoo …`` while the service's own container
        is stopped — a restore, say — reports a fresh series under the
        ``odoo`` service, named ``<project>-odoo-run-<hash>``. It is still
        the instance's telemetry: covered, and stamped."""
        self.inst.write({"running": True})
        self._run_series(odoo=[
            (self.inst.id, 600, "p1-odoo-1"),
            (self.inst.id, 5, "p1-odoo-run-0123456789ab"),
        ])
        self.assertFalse(
            self.inst.running, "a one-off odoo passed for the instance's own",
        )
        self.assertTrue(self.inst.metrics_last_seen)

    def test_the_rebuild_boot_test_does_not_pass_for_the_service(self):
        """The rebuild boots the new image as ``ic_boot_odoo_<id>``, a
        ``docker compose run`` of ``odoo`` under a name of its own, while
        the instance's odoo may be stopped — a Free tenant asleep."""
        self.inst.write({"running": True})
        self._run_series(odoo=[
            (self.inst.id, 600, "p1-odoo-1"),
            (self.inst.id, 5, "ic_boot_odoo_42"),
        ])
        self.assertFalse(
            self.inst.running, "the boot test passed for the instance's odoo",
        )

    def test_a_one_off_is_heard_from_its_host(self):
        """Whether anything is heard is not about which container."""
        other = self.env["cloud.instance"].create({
            "name": "i2", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
        })
        self.inst.write({
            "running": True, "metrics_last_seen": fields.Datetime.now(),
        })
        self._run_series(
            odoo=[(other.id, 10)],
            hosts=[
                (self.host.id, 600),
                (self.host.id, 5, "p2-odoo-run-0123456789ab"),
            ],
        )
        self.assertFalse(self.inst.running)

    def test_a_host_is_heard_through_its_freshest_series(self):
        """A container gone minutes ago does not silence its whole host."""
        other = self.env["cloud.instance"].create({
            "name": "i2", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
        })
        self.inst.write({
            "running": True, "metrics_last_seen": fields.Datetime.now(),
        })
        self._run_series(
            odoo=[(other.id, 10)],
            hosts=[(self.host.id, 600), (self.host.id, 10)],
        )
        self.assertFalse(
            self.inst.running,
            "a parked stack read as running because its host had one "
            "stale series",
        )

    def test_a_host_with_every_series_stale_has_gone_quiet(self):
        """The fleet still reports, through another host."""
        elsewhere = self.env["cloud.host"].create({
            "name": "HElsewhere", "ip_address": "10.0.0.53", "user": "root",
            "wildcard_domain": "helsewhere.example.com",
        })
        other = self.env["cloud.instance"].create({
            "name": "i2", "project_id": self.project.id,
            "environment": "staging", "host_id": elsewhere.id,
        })
        self.inst.write({
            "running": True, "metrics_last_seen": fields.Datetime.now(),
        })
        self._run_series(
            odoo=[(other.id, 10)],
            hosts=[
                (self.host.id, 600), (self.host.id, 400), (elsewhere.id, 10),
            ],
        )
        self.assertTrue(self.inst.running)


class TestGraceAfterAConfirmation(InstanceLivenessCase):
    """A job that has just brought odoo up is believed for a while.

    A job that starts or recreates the ``odoo`` container takes its
    series away at once, and the backend answers with the new one about
    a minute later. Read as a stop, that gap showed a restored instance
    "Stopped", without its Restart and Shell buttons, for over two
    minutes (QA d76c, 26-oct-2026). The grace is a job's alone: a rise
    the cron read itself opens none, and a stop closes it.
    """

    def setUp(self):
        """The instance on a host, beside a neighbour whose odoo reports."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HGrace", "ip_address": "10.0.0.54", "user": "root",
            "wildcard_domain": "hgrace.example.com",
        })
        self.inst.write({
            "host_id": self.host.id,
            "metrics_last_seen": fields.Datetime.now(),
        })
        # The fleet-wide guards (something covered, some odoo seen)
        # stay satisfied whatever the instance under test reports.
        other_project = self.env["cloud.project"].create({"name": "P3"})
        self.other = self.env["cloud.instance"].create({
            "name": "i3", "project_id": other_project.id,
            "environment": "staging", "host_id": self.host.id,
        })

    def _executor(self, cls, code):
        """Bind *cls* to a fresh job of type *code* on the instance.

        :param type cls: the executor class
        :param str code: the ``cloud.job.type`` code it runs
        :return: the executor, ready for its hooks
        """
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        ) or self.env["cloud.job.type"].create(
            {"name": code, "code": code, "apply_to": "instance"},
        )
        job = self.env["cloud.job"].create({
            "host_id": self.host.id, "instance_id": self.inst.id,
            "job_type_id": job_type.id, "name": code,
        })
        return cls(job, self.host)

    def _confirm(self, seconds_ago=0):
        """Have a start job bring the instance up *seconds_ago* ago.

        :param int seconds_ago: how long since the job confirmed it
        """
        self.inst.write({"running": False})
        executor = self._executor(StartInstanceExecutor, "start_instance")
        asyncio.run(executor.on_success({}))
        self.assertTrue(self.inst.running)
        if seconds_ago:
            self.inst.write({
                "running_confirmed_at": (
                    self.inst.running_confirmed_at
                    - timedelta(seconds=seconds_ago)
                ),
            })

    def _tick(self, odoo_age=None, covered=True):
        """Run the cron once, with the neighbour's odoo reporting.

        :param odoo_age: seconds since the instance's ``odoo`` was seen,
            or ``None`` when the backend holds no such series
        :param bool covered: whether the backend still reports any
            container of the instance; False is a stack stopped whole
        """
        coverage = [(self.other.id, 5)]
        if covered:
            coverage.append((self.inst.id, 5))
        odoo = [(self.other.id, 5)]
        if odoo_age is not None:
            odoo.append((self.inst.id, odoo_age))
        self._run(
            _samples(*coverage),
            odoo_payload=_samples(*odoo),
            host_payload=_host_samples((self.host.id, 5)),
        )
        self.inst.invalidate_recordset()

    def test_an_odoo_missing_right_after_a_confirmation_is_still_running(self):
        """The QA d76c case: the series the job took away is not back yet."""
        self._confirm()
        self._tick()
        self.assertTrue(
            self.inst.running,
            "a job's confirmation was undone by the series it had just "
            "taken away",
        )

    def test_a_stack_silent_right_after_a_confirmation_is_still_running(self):
        """The restore stops ``db`` too: every container leaves at once."""
        self._confirm()
        self._tick(covered=False)
        self.assertTrue(self.inst.running)

    def test_past_the_grace_a_missing_odoo_is_a_stop(self):
        """The grace ends: an odoo still unseen is stopped, as before it."""
        self._confirm(seconds_ago=_CONFIRMED_GRACE_SECONDS + 1)
        self._tick()
        self.assertFalse(self.inst.running)

    def test_past_the_grace_a_silent_stack_is_a_stop(self):
        """The same for a stack the backend no longer reports at all."""
        self._confirm(seconds_ago=_CONFIRMED_GRACE_SECONDS + 1)
        self._tick(covered=False)
        self.assertFalse(self.inst.running)

    def test_a_rise_the_cron_read_opens_no_grace(self):
        """Or an odoo that keeps going away would never read as stopped."""
        self.inst.write({"running": False})
        self._tick(odoo_age=5)
        self.assertTrue(self.inst.running)
        self._tick()
        self.assertFalse(self.inst.running)

    def test_a_stop_closes_the_grace(self):
        """A Stop after the confirmation leaves nothing to hold back."""
        self._confirm()
        executor = self._executor(StopInstanceExecutor, "stop_instance")
        asyncio.run(executor.on_success({}))
        self.assertFalse(self.inst.running)
        # Its series ages out: inside the window, the cron reads it up…
        self._tick(odoo_age=100)
        self.assertTrue(self.inst.running)
        # …and once it is gone, down at once.
        self._tick()
        self.assertFalse(
            self.inst.running,
            "a confirmation from before the stop held the stop back",
        )

    def _confirm_meanwhile(self):
        """Write a job's confirmation of the instance straight to its row.

        What a job committing as the cron runs looks like to the cron:
        the row changes, and what the cron read of it before does not.
        """
        self.env.cr.execute(
            "UPDATE cloud_instance SET running_confirmed_at = %s"
            " WHERE id = %s",
            (fields.Datetime.now(), self.inst.id),
        )

    def test_a_confirmation_committed_after_the_cron_read_is_kept(self):
        """The cron reads every row it covers, then decides on each in
        turn. A job confirming the instance in between is read again,
        under the row's lock, before a stop is written over it."""
        self.inst.write({"running": True})
        Instance = type(self.env["cloud.instance"])
        original = Instance.write
        neighbour = self.other.id

        def write(records, vals):
            """Write; the neighbour's, decided first, lets the job in.

            :param records: the instances written
            :param dict vals: the values written
            :return: what ``write`` returns
            """
            if neighbour in records.ids:
                self._confirm_meanwhile()
            return original(records, vals)

        with patch.object(Instance, "write", autospec=True, side_effect=write):
            self._tick()
        self.assertTrue(
            self.inst.running, "the cron wrote a stop over a confirmation",
        )

    def test_a_confirmation_committed_after_the_search_keeps_a_silent_stack(
        self,
    ):
        """The stack stopped whole: found by a search, then written. A
        job confirming the instance in between is read again too."""
        self.inst.write({"running": True})
        Instance = type(self.env["cloud.instance"])
        original = Instance.search
        silent = self.inst.id

        def search(model, domain, *args, **kwargs):
            """Search; a result holding the instance lets the job in.

            :param model: the model searched
            :param list domain: the search domain
            :return: what ``search`` returns
            """
            found = original(model, domain, *args, **kwargs)
            if silent in found.ids:
                self._confirm_meanwhile()
            return found

        with patch.object(
            Instance, "search", autospec=True, side_effect=search,
        ):
            self._tick(covered=False)
        self.assertTrue(
            self.inst.running, "the cron wrote a stop over a confirmation",
        )

    def test_without_a_recent_job_a_missing_odoo_is_a_stop_at_once(self):
        """What the grace leaves as it was: the first tick decides."""
        self.inst.write({"running": True})
        self._tick()
        self.assertFalse(self.inst.running)

    def test_a_rebuild_leaves_the_instance_running(self):
        """Its restart step is ``docker compose up -d``. It wrote nothing
        about ``running``, so a stop the cron read during it stayed until
        another job came by (QA d76c)."""
        self.inst.write({"running": False})
        executor = self._executor(RebuildInstanceExecutor, "rebuild_instance")
        asyncio.run(executor.on_success({}))
        self.assertTrue(self.inst.running)
        self._tick()
        self.assertTrue(self.inst.running)

    def test_a_rebuild_whose_stack_may_be_down_confirms_nothing(self):
        """A layer above may hand the rebuilt stack back stopped — a plan
        that sleeps an idle instance does — and says so through
        ``_stop_is_expected``."""
        self.inst.write({"running": False})
        executor = self._executor(RebuildInstanceExecutor, "rebuild_instance")
        with patch.object(
            type(self.inst), "_stop_is_expected", autospec=True,
            return_value=True,
        ):
            asyncio.run(executor.on_success({}))
        self.assertFalse(self.inst.running)
        self.assertFalse(self.inst.running_confirmed_at)
