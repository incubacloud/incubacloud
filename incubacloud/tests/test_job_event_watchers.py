"""Every page that refetches on a ``cloud_jobs`` event filters it first.

A running job publishes a ``progress`` event every half second it wrote
output, and a page that answers each with a refetch reloads itself twice
a second for the whole job (the host page did, through a deploy of an
instance it does not even list: QA, 20-oct-2026). ``refreshesOn`` is the
one place that says which events count; this is what keeps a subscriber
added later from forgetting it.

Static: it reads the sources, as the front end's own tests cannot mount
these components. The SaaS manager has the same test for its pages.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static" / "src"
_SUBSCRIBES = re.compile(r'subscribe\(\s*"cloud_jobs"')
#: Subscribers that do not refetch, or want every chunk: the app shell
#: only toasts a terminal state, which ``refreshesOn`` is not about.
_NOT_A_WATCHER = {"app/app.js"}


def watchers(static_src, not_a_watcher=frozenset()):
    """Return ``(path, filters)`` for each source subscribing to the events.

    :param static_src: the module's ``static/src`` directory.
    :param not_a_watcher: paths, relative to it, that need no filter.
    :return: list of ``(relative path, whether it calls refreshesOn)``.
    """
    found = []
    for source in sorted(static_src.rglob("*.js")):
        text = source.read_text()
        if not _SUBSCRIBES.search(text):
            continue
        rel = source.relative_to(static_src).as_posix()
        if rel in not_a_watcher:
            continue
        found.append((rel, "refreshesOn(" in text))
    return found


class TestJobEventWatchers(BaseCase):

    def test_the_pages_that_watch_jobs_are_found(self):
        """The check below must have something to check."""
        paths = {path for path, _filters in watchers(_STATIC, _NOT_A_WATCHER)}
        self.assertIn("components/host_detail/host_detail.js", paths)
        self.assertIn("store/project_store.js", paths)

    def test_every_watcher_filters_the_events_it_refetches_on(self):
        unfiltered = [
            path for path, filters in watchers(_STATIC, _NOT_A_WATCHER)
            if not filters
        ]
        self.assertFalse(
            unfiltered,
            "these subscribe to cloud_jobs and refetch on every event, "
            "a job's output included; filter with refreshesOn: %s"
            % ", ".join(unfiltered),
        )
