class ExecutorRegistry:

    def __init__(self):
        self._executors = {}

    def register(self, job_type, executor_cls):
        if job_type in self._executors:
            raise ValueError(f"Executor already registered for '{job_type}'")
        self._executors[job_type] = executor_cls

    def get(self, job_type):
        return self._executors.get(job_type)

    def all(self):
        return self._executors.copy()


executor_registry = ExecutorRegistry()
