import logging

_logger = logging.getLogger(__name__)


class ExecutorRegistry:
    """In-process registry of cloud.job executors keyed by ``_job_type``.

    A given Python process can host multiple Odoo databases that each
    install a different combination of modules. Two cases require
    care here:

    * **Same module imported twice** (e.g. on a worker reload). The
      class object is the same; we silently ignore the second
      registration.
    * **Two modules registering separate classes for the same
      _job_type** — can happen when two dependent modules in the addons
      path each ship an executor with the same ``_job_type`` and their
      own recipe. The Python process can only hold one binding at a time,
      so we log a warning and keep the FIRST registration (whichever
      module loaded first wins). In a real-world deployment such modules
      live in different databases anyway, so the in-process binding is
      mostly used by whichever DB the worker is currently serving.
    """

    def __init__(self):
        self._executors = {}

    def register(self, job_type, executor_cls):
        existing = self._executors.get(job_type)
        if existing is executor_cls:
            return  # Same class re-registering on reload — silent no-op.
        if existing is not None:
            _logger.warning(
                "ExecutorRegistry: '%s' already bound to %s; ignoring "
                "duplicate registration from %s. Two modules ship the "
                "same _job_type — this is OK if they target different "
                "databases.",
                job_type, existing.__name__, executor_cls.__name__,
            )
            return
        self._executors[job_type] = executor_cls

    def get(self, job_type):
        return self._executors.get(job_type)

    def all(self):
        return self._executors.copy()


executor_registry = ExecutorRegistry()
