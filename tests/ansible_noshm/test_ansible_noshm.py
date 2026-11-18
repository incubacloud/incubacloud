"""Tests of ``incubacloud/ansible_noshm.py``: Ansible without ``/dev/shm``.

Plain pytest, without Odoo, since the launcher runs outside it, and outside
the addon, whose package pytest would import: the CI's
job ``ansible-noshm`` runs them with the ansible-core and ansible-runner
of ``requirements.txt``, and ci-local replays that job in the full suites.
Nothing is skipped: without Ansible installed they fail.

A server whose ``/dev/shm`` is read-only, as Odoo.sh's, is made by a
``sitecustomize`` in ``PYTHONPATH`` (``SITECUSTOMIZE``): in every Python it
starts, a semaphore and a shared memory block fail to be created with
``EROFS``, as ``sem_open`` and ``shm_open`` do there. Ansible without the
launcher then fails with the error Odoo.sh shows.
"""
import ast
import gc
import importlib.metadata
import importlib.util
import json
import multiprocessing.context
import multiprocessing.queues
import os
import re
import resource
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

import _multiprocessing
import ansible
import ansible_runner
import pytest
import yaml

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent.parent / "incubacloud" / "ansible_noshm.py"
#: What ansible-core and ansible-runner use of ``multiprocessing``, per
#: reviewed version (``test_the_multiprocessing_uses_are_the_reviewed_ones``).
RECORDED_USES = HERE / "multiprocessing_uses.json"

SITECUSTOMIZE = '''\
"""A server whose /dev/shm is read-only: sem_open and shm_open fail."""
import errno
import multiprocessing.synchronize

import _posixshmem


def _read_only(*args, **kwargs):
    raise OSError(errno.EROFS, "Read-only file system")


multiprocessing.synchronize.SemLock.__init__ = _read_only
_posixshmem.shm_open = _read_only
'''


def _load_launcher():
    """Import ``ansible_noshm.py`` by its path, outside the addon's package."""
    spec = importlib.util.spec_from_file_location("ansible_noshm", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


noshm = _load_launcher()


@pytest.fixture
def read_only_shm(tmp_path):
    """Return a ``PYTHONPATH`` entry whose ``sitecustomize`` breaks /dev/shm."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    return str(site)


@pytest.fixture
def ctx():
    """Return a fork context of its own, on pipe locks."""
    context = multiprocessing.context.ForkContext()
    noshm.use_pipe_locks(context)
    return context


def _finalizer(obj):
    """Return the ``weakref.finalize`` registered for *obj*."""
    for finalizer in list(weakref.finalize._registry):
        info = finalizer.peek()
        if info and info[0] is obj:
            return finalizer
    raise AssertionError(f"no finalizer for {obj!r}")


# ── PipeLock ──


def _in_child(work, *args):
    """Run *work* in a forked child and leave, whatever happens: 0 if it ran."""
    status = 1
    try:
        work(*args)
        status = 0
    finally:
        os._exit(status)


def _wait_and_tell(lock, write_fd):
    """Take *lock* and write how long that took to *write_fd*."""
    started = time.monotonic()
    lock.acquire()
    os.write(write_fd, f"{time.monotonic() - started:.3f}".encode())
    lock.release()


def _hold_for_a_while(lock, write_fd):
    """Take *lock*, say so on *write_fd*, and give it back half a second later."""
    lock.acquire()
    os.write(write_fd, b"held")
    time.sleep(0.5)
    lock.release()


def test_a_free_lock_is_taken_once_until_given_back():
    lock = noshm.PipeLock()
    assert lock.acquire() is True
    assert lock.acquire(False) is False
    lock.release()
    assert lock.acquire(block=False) is True
    lock.release()


def test_a_timed_acquire_of_a_held_lock_gives_up_in_time():
    lock = noshm.PipeLock()
    lock.acquire()
    started = time.monotonic()
    assert lock.acquire(timeout=0.3) is False
    assert 0.25 <= time.monotonic() - started < 2
    lock.release()
    assert lock.acquire(timeout=0.3) is True
    lock.release()


def test_a_timed_acquire_returns_once_another_thread_releases():
    lock = noshm.PipeLock()
    lock.acquire()
    threading.Timer(0.2, lock.release).start()
    assert lock.acquire(timeout=5) is True
    lock.release()


def test_threads_hold_the_lock_one_at_a_time():
    lock = noshm.PipeLock()
    inside, most = [0], [0]

    def work():
        for _ in range(50):
            with lock:
                inside[0] += 1
                most[0] = max(most[0], inside[0])
                time.sleep(0.0005)
                inside[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert most[0] == 1


def test_a_forked_child_waits_for_the_lock_its_parent_holds():
    lock = noshm.PipeLock()
    lock.acquire()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        _in_child(_wait_and_tell, lock, write_fd)
    os.close(write_fd)
    time.sleep(0.5)
    lock.release()
    waited = float(os.read(read_fd, 64))
    os.close(read_fd)
    assert os.waitpid(pid, 0)[1] == 0
    assert waited >= 0.4
    assert lock.acquire(False) is True
    lock.release()


def test_the_parent_waits_for_the_lock_a_forked_child_holds():
    lock = noshm.PipeLock()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        _in_child(_hold_for_a_while, lock, write_fd)
    os.close(write_fd)
    assert os.read(read_fd, 4) == b"held"
    os.close(read_fd)
    assert lock.acquire(False) is False
    assert lock.acquire(timeout=10) is True
    lock.release()
    assert os.waitpid(pid, 0)[1] == 0


def test_releasing_a_free_lock_is_refused():
    lock = noshm.PipeLock()
    with pytest.raises(ValueError, match="released too many times"):
        lock.release()
    lock.acquire()
    lock.release()
    with pytest.raises(ValueError, match="released too many times"):
        lock.release()


def test_a_read_of_nothing_is_an_error_and_not_an_acquire():
    lock = noshm.PipeLock()
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    os.set_blocking(read_fd, False)
    real = lock._read_fd
    lock._read_fd = read_fd
    try:
        with pytest.raises(OSError, match="no writer left"):
            lock.acquire()
    finally:
        lock._read_fd = real
        os.close(read_fd)


def test_the_pipe_is_closed_with_the_lock_and_not_at_exit():
    lock = noshm.PipeLock()
    assert _finalizer(lock).atexit is False
    fds = (lock._read_fd, lock._write_fd)
    del lock
    gc.collect()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_the_pipe_is_not_inherited_by_what_ansible_executes():
    lock = noshm.PipeLock()
    assert not os.get_inheritable(lock._read_fd)
    assert not os.get_inheritable(lock._write_fd)


def test_a_lock_above_descriptor_1024_waits_with_poll():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 2048 if hard == resource.RLIM_INFINITY else min(hard, 2048)
    assert wanted > 1100, f"RLIMIT_NOFILE's hard limit is {hard}"
    resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    padding = []
    try:
        while len(padding) < 1030:
            padding.append(os.open(os.devnull, os.O_RDONLY))
        lock = noshm.PipeLock()
        assert lock._read_fd > 1024
        lock.acquire()
        assert lock.acquire(timeout=0.2) is False
        threading.Timer(0.2, lock.release).start()
        assert lock.acquire(timeout=5) is True
        lock.release()
        del lock
        gc.collect()
    finally:
        for fd in padding:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# ── The fork context on pipe locks ──


def _produce(queue, simple):
    """Put 200 items on each queue (run in a forked child)."""
    for i in range(200):
        queue.put(("queue", i))
        simple.put(("simple", i))
    queue.close()
    queue.join_thread()


def test_queues_carry_items_from_a_forked_child(ctx):
    queue = multiprocessing.queues.Queue(ctx=ctx)
    simple = multiprocessing.queues.SimpleQueue(ctx=ctx)
    assert isinstance(queue._rlock, noshm.PipeLock)
    assert isinstance(queue._sem, noshm.SemaphoreAtMax)
    assert isinstance(simple._wlock, noshm.PipeLock)
    child = ctx.Process(target=_produce, args=(queue, simple))
    child.start()
    got_queue = [queue.get(timeout=30) for _ in range(200)]
    got_simple = [simple.get() for _ in range(200)]
    child.join(30)
    assert child.exitcode == 0
    assert got_queue == [("queue", i) for i in range(200)]
    assert got_simple == [("simple", i) for i in range(200)]


def test_a_semaphore_at_its_maximum_never_blocks(ctx):
    semaphore = ctx.BoundedSemaphore(_multiprocessing.SemLock.SEM_VALUE_MAX)
    assert all(semaphore.acquire(timeout=0) for _ in range(1000))
    semaphore.release()


def test_what_has_no_substitute_is_refused_by_name(ctx):
    for name in noshm.UNSUPPORTED:
        with pytest.raises(noshm.SemaphoreUnavailable, match=name):
            getattr(ctx, name)()
    with pytest.raises(noshm.SemaphoreUnavailable, match=r"BoundedSemaphore\(2\)"):
        ctx.BoundedSemaphore(2)


def test_the_global_fork_context_is_left_alone():
    assert "Lock" not in vars(multiprocessing.get_context("fork"))


# ── The probe and the launcher ──


def test_the_probe_finds_semaphores_working_here():
    assert noshm.semaphore_error() is None


def test_the_probe_names_a_read_only_dev_shm(read_only_shm):
    script = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('n', {str(LAUNCHER)!r})\n"
        "n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)\n"
        "print(n.describe(n.semaphore_error()))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        env=os.environ | {"PYTHONPATH": read_only_shm},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.stdout.strip() == "EROFS", done.stderr


def test_the_launcher_says_when_its_python_cannot_import_ansible(tmp_path):
    # -S: no site-packages, so this Python has no Ansible.
    done = subprocess.run(
        [sys.executable, "-S", str(LAUNCHER), "site.yml"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode != 0
    assert "cannot import Ansible" in done.stderr, done.stderr
    assert os.path.normpath(sys.executable) in done.stderr


def test_the_launch_copies_the_launcher_privately_and_starts_python(tmp_path):
    launch = noshm.launch_arguments(str(tmp_path), "playbooks/x.yml", ["--check"])
    launcher = tmp_path / "bin" / "ansible-playbook"
    assert launch["binary"] == sys.executable
    assert launch["cmdline"] == f"{launcher} playbooks/x.yml --check"
    assert launcher.read_bytes() == LAUNCHER.read_bytes()
    assert launcher.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "bin").stat().st_mode & 0o777 == 0o700


# ── Ansible through ansible-runner ──

#: How many tasks the playbook repeats: each forks one worker per host,
#: whose queue holds two locks and a pipe, six descriptors.
REPEATED_TASKS = 20
#: The most descriptors the controller may gain over the repeated tasks:
#: those it holds depend on the version (2.17 keeps a lock for each
#: module it knows), but a leak of each worker's would be 240.
MOST_NEW_FDS = 16

INCLUDED = [
    {"name": "Included task", "ansible.builtin.set_fact": {"ic_included": True}},
]


def _count_fds(key, **data):
    """A task that exports, as *key*, how many descriptors the controller holds.

    The worker that runs it is forked from the controller, so it holds
    what the controller held; the lookup's shell is the worker's child.
    """
    return {
        "name": f"Export {key}",
        "run_once": True,
        "ansible.builtin.set_stats": {
            "aggregate": False,
            "data": {key: "{{ lookup('pipe', 'ls /proc/$PPID/fd | wc -l') | int }}"} | data,
        },
    }


def _playbook():
    """The playbook the launcher runs: two hosts, every kind of task it needs."""
    tasks = [
        {"name": "Loop", "ansible.builtin.command": "echo {{ item }}",
         "loop": [1, 2, 3], "changed_when": False},
        {"name": "Include", "ansible.builtin.include_tasks": "included.yml"},
        {"name": "Run past its timeout", "ansible.builtin.command": "sleep 10",
         "timeout": 1, "ignore_errors": True},
    ]
    tasks.append(_count_fds("ic_fds_before"))
    tasks += [
        {"name": f"Repeat {i}", "ansible.builtin.set_fact": {f"ic_n{i}": i}}
        for i in range(REPEATED_TASKS)
    ]
    tasks.append(_count_fds("ic_fds_after", ic_hosts="{{ ansible_play_hosts | sort }}"))
    return [{"name": "Without /dev/shm", "hosts": "all", "gather_facts": False,
             "tasks": tasks}]


def _run(root, site, launcher=True):
    """Run ``_playbook`` on two local hosts with *site* in ``PYTHONPATH``.

    :return: the runner and its events
    """
    project = root / "project"
    project.mkdir(parents=True)
    (project / "site.yml").write_text(yaml.safe_dump(_playbook(), sort_keys=False))
    (project / "included.yml").write_text(yaml.safe_dump(INCLUDED, sort_keys=False))
    inventory = root / "inventory"
    inventory.mkdir()
    local = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
    (inventory / "hosts.yml").write_text(yaml.safe_dump(
        {"all": {"hosts": {"alpha": local, "beta": local}}}
    ))
    if launcher:
        launch = noshm.launch_arguments(str(root), "site.yml", ["--forks", "5"])
    else:
        launch = {"playbook": "site.yml", "cmdline": "--forks 5"}
    events = []
    runner = ansible_runner.run(
        private_data_dir=str(root),
        project_dir=str(project),
        inventory=str(inventory / "hosts.yml"),
        envvars={"PYTHONPATH": site, "ANSIBLE_NOCOLOR": "True"},
        event_handler=lambda event: events.append(event) or True,
        quiet=True,
        timeout=600,
        **launch,
    )
    return runner, events


def _stdout(events):
    """The run's output, as the panel's job log gets it."""
    return "\n".join(event.get("stdout") or "" for event in events)


def _task_events(events, kind, task):
    """The events of *kind* of the task named *task*, by host."""
    return sorted(
        event["event_data"].get("host")
        for event in events
        if event.get("event") == kind and event["event_data"].get("task") == task
    )


def test_without_the_launcher_ansible_fails_as_on_odoo_sh(tmp_path, read_only_shm):
    runner, events = _run(tmp_path, read_only_shm, launcher=False)
    assert runner.status == "failed"
    output = _stdout(events)
    assert "lack of access to /dev/shm" in output, output
    assert "Read-only file system" in output, output


def test_a_playbook_runs_through_the_launcher_without_dev_shm(tmp_path, read_only_shm):
    runner, events = _run(tmp_path, read_only_shm)
    output = _stdout(events)
    assert (runner.status, runner.rc) == ("successful", 0), output
    assert len([
        e for e in events
        if e.get("event") == "runner_item_on_ok" and e["event_data"].get("task") == "Loop"
    ]) == 6, output
    assert _task_events(events, "runner_on_ok", "Included task") == ["alpha", "beta"], output
    timed_out = [
        e for e in events
        if e.get("event") == "runner_on_failed"
        and e["event_data"].get("task") == "Run past its timeout"
    ]
    assert len(timed_out) == 2, output
    for event in timed_out:
        assert event["event_data"]["ignore_errors"], event
        # 2.21's words, then those before 2.19.
        assert re.search(
            r"Timed out after 1 second|expected time frame \(1\)",
            event["event_data"]["res"]["msg"],
        ), event
    for i in range(REPEATED_TASKS):
        assert _task_events(events, "runner_on_ok", f"Repeat {i}") == ["alpha", "beta"]
    stats = [e for e in events if e.get("event") == "playbook_on_stats"]
    assert len(stats) == 1, output
    artifact = stats[0]["event_data"]["artifact_data"]
    assert artifact["ic_hosts"] == ["alpha", "beta"], artifact
    assert int(artifact["ic_fds_after"]) - int(artifact["ic_fds_before"]) < MOST_NEW_FDS, artifact


# ── The tripwire on upgrades ──


def _dotted(node):
    """Return an attribute chain as ``a.b.c``, or None when it is not a name's."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_multiprocessing(module):
    """Whether *module* is ``multiprocessing``'s, or ansible-core's context of it."""
    return (
        module == "multiprocessing"
        or module.startswith("multiprocessing.")
        or module.endswith("utils.multiprocessing")
    )


def multiprocessing_uses(package_dir, base):
    """Return what each file under *package_dir* uses of ``multiprocessing``.

    Its imports of ``multiprocessing`` (and of ansible-core's context,
    ``ansible.utils.multiprocessing``), and every attribute read on a name
    they bind: ``multiprocessing_context.Lock``, for one.

    :param Path package_dir: where the files are
    :param Path base: what their names are relative to
    :return: ``{file: sorted uses}``, only the files with any
    :rtype: dict
    """
    uses = {}
    for path in sorted(Path(package_dir).rglob("*.py")):
        tree = ast.parse(path.read_bytes(), filename=str(path))
        names, found = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_multiprocessing(alias.name):
                        found.add(f"import {alias.name}"
                                  + (f" as {alias.asname}" if alias.asname else ""))
                        names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and _is_multiprocessing(node.module or ""):
                for alias in node.names:
                    found.add(f"from {'.' * node.level}{node.module} import {alias.name}"
                              + (f" as {alias.asname}" if alias.asname else ""))
                    names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                chain = _dotted(node)
                if chain and chain.split(".")[0] in names:
                    found.add(chain)
        if found:
            uses[path.relative_to(base).as_posix()] = sorted(found)
    return uses


def test_the_installed_versions_are_the_pins_of_requirements_txt():
    """Bumping ``requirements.txt`` alone cannot leave this job green.

    The panel's image installs ``requirements.txt``; the versions tested
    here must be the ones it pins, whatever installed them.
    """
    pins = {}
    for line in (HERE.parent.parent / "requirements.txt").read_text().splitlines():
        match = re.fullmatch(r"(ansible-core|ansible-runner)==(\S+)", line.strip())
        if match:
            pins[match.group(1)] = match.group(2)
    assert sorted(pins) == ["ansible-core", "ansible-runner"], pins
    installed = {package: importlib.metadata.version(package) for package in pins}
    assert installed == pins, "install the pins of requirements.txt, or update them"


def test_the_multiprocessing_uses_are_the_reviewed_ones():
    """A new ansible-core or ansible-runner fails here until it is reviewed.

    The launcher stands in for what these versions use; another version
    may use a primitive it has no substitute for, and fail on Odoo.sh
    only. Review its uses against ``ansible_noshm.py`` and record them in
    ``multiprocessing_uses.json``.
    """
    recorded = json.loads(RECORDED_USES.read_text())
    ansible_dir = Path(ansible.__file__).parent
    runner_dir = Path(ansible_runner.__file__).parent
    for package, scope, base in (
        ("ansible-core", ansible_dir, ansible_dir.parent),
        ("ansible-runner", runner_dir / "display_callback", runner_dir.parent),
    ):
        key = f"{package} {importlib.metadata.version(package)}"
        assert key in recorded, (
            f"{key} is not reviewed: compare what it uses of multiprocessing "
            f"with ansible_noshm.py and record it in {RECORDED_USES.name}"
        )
        assert multiprocessing_uses(scope, base) == recorded[key], (
            f"{key} uses multiprocessing otherwise than recorded"
        )
