"""Run ``ansible-playbook`` where POSIX semaphores cannot be created.

ansible-core's controller passes its tasks' results through
``multiprocessing`` queues (``FinalQueue``, each worker's
``WorkerQueue``) and, before 2.19, guards its module cache with
``multiprocessing`` locks. CPython makes each lock a POSIX semaphore with
``sem_open(3)``, which glibc keeps in ``/dev/shm``. Where ``/dev/shm`` is
read-only, as on Odoo.sh, the first queue fails with *Unable to use
multiprocessing, this is normally caused by lack of access to /dev/shm*
and no playbook runs. No setting of Ansible avoids it: the queue is made
before ``forks`` is read, and every task forks a worker with its own.

``AnsibleExecutor`` runs a playbook through this file only when its own
probe (``semaphore_error``) finds that a lock cannot be made: it copies
the file into the run's private directory and has ansible-runner start
``<the panel's Python> <copy> <playbook> <options>`` (``launch_arguments``).
The file is read by Python, never executed, so a ``/tmp`` mounted
``noexec`` does not stop it. The file checks again for itself and, only
when a lock still cannot be made, replaces in the ``fork`` context, the
one ansible-core uses (``ansible.utils.multiprocessing``):

- ``Lock`` with ``PipeLock``: one byte in a pipe, which excludes between
  threads and between processes forked from one another, as a semaphore
  does;
- ``BoundedSemaphore`` at ``SEM_VALUE_MAX``, the bound of a ``Queue``
  without ``maxsize``, with one that never blocks: nothing can reach that
  bound. Any other value is refused;
- every other primitive that needs a semaphore with a function that
  raises ``SemaphoreUnavailable`` naming it: nothing fails silently.

Then it runs ansible-playbook's ``main`` in its own process, so the
signals of a cancel reach Ansible as they do without it.

It patches the controller's process only, and the workers forked from it.
A task that runs on the panel itself (``delegate_to: localhost``, or a
play on ``localhost``) executes its module in a new interpreter, which has
none of this: a module that needs a semaphore there would fail on Odoo.sh
as the controller did. The panel's playbooks work on the hosts through
SSH only, and the tripwire test does not cover modules.

A test of the panel (``tests/ansible_noshm``) records which ``multiprocessing``
primitives each reviewed ansible-core and ansible-runner use, and fails
on a version that uses others until someone reviews them here.

Only the standard library is imported: the file runs outside Odoo.
"""
import contextlib
import errno
import fcntl
import math
import multiprocessing
import os
import select
import shlex
import shutil
import struct
import sys
import termios
import time
import weakref
from pathlib import Path

import _multiprocessing

#: The primitives of a ``multiprocessing`` context that need a semaphore
#: and have no substitute here.
UNSUPPORTED = ("RLock", "Semaphore", "Condition", "Event", "Barrier", "Value", "Array")

#: The name of the launcher's copy in a run's private directory: the name
#: ansible-playbook is started under.
LAUNCHER_NAME = "ansible-playbook"


class SemaphoreUnavailable(RuntimeError):
    """A ``multiprocessing`` primitive this file has no substitute for."""


def semaphore_error():
    """Return why a lock of the ``fork`` context cannot be made, or None.

    The ``fork`` context unlinks a semaphore's name as soon as it is
    created, so the check leaves nothing behind in ``/dev/shm``.

    :return: the error ``sem_open`` raised, or None when locks work
    :rtype: OSError | None
    """
    try:
        multiprocessing.get_context("fork").Lock()
    except OSError as exc:
        return exc
    return None


def describe(error):
    """Return *error*'s errno as a short name, e.g. ``EROFS``.

    :param OSError error: what ``semaphore_error`` returned
    :rtype: str
    """
    return errno.errorcode.get(error.errno, str(error.errno))


def _close_fds(*fds):
    """Close *fds*, ignoring those already closed.

    :param int fds: file descriptors
    """
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def _pipe_bytes(fd):
    """Return how many bytes the pipe of *fd* holds, unread.

    :param int fd: an end of a pipe
    :rtype: int
    """
    raw = fcntl.ioctl(fd, termios.FIONREAD, struct.pack("i", 0))
    return struct.unpack("i", raw)[0]


class PipeLock:
    """A ``multiprocessing.Lock`` without a semaphore: one byte in a pipe.

    The pipe holds the byte while the lock is free; ``acquire`` reads it
    and ``release`` writes it back. A process forked while the lock is
    held shares the pipe, so it waits for the same byte, as it would for
    the same semaphore. The read end does not block and ``acquire`` waits
    on it with ``poll``, which, unlike ``select``, takes a descriptor
    above 1024. Both descriptors are closed when the lock is collected,
    in each process that holds a copy, and never at the interpreter's
    exit, when a queue's feeder thread may still be using them. They are
    not inherited by what Ansible executes (``O_CLOEXEC``).

    Known limit: a signal handler that raises, between ``acquire``'s read
    of the byte and its return, loses the byte, and the lock stays held
    for ever; a semaphore has no such window. ansible-core's task
    ``timeout:`` is one such handler (it raises from a ``SIGALRM`` in the
    worker). The window is a few bytecodes wide, and the playbook's
    time limit (``PLAYBOOK_LIMIT_MARGIN_S`` of ``AnsibleExecutor``) ends
    a run that a lost byte leaves blocked.
    """

    def __init__(self):
        """Create the pipe and put the lock's byte in it."""
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        self._read_fd = read_fd
        self._write_fd = write_fd
        closer = weakref.finalize(self, _close_fds, read_fd, write_fd)
        closer.atexit = False
        os.write(write_fd, b"\0")

    def acquire(self, block=True, timeout=None):
        """Take the lock, as ``multiprocessing.Lock.acquire`` does.

        :param bool block: wait for the lock; False returns at once
        :param timeout: the longest to wait, in seconds; None waits for
            as long as it takes
        :return: whether the lock was taken
        :rtype: bool
        :raise OSError: when the pipe has no writer left, which a held
            lock's never lacks: a read of nothing is not an acquire
        """
        if not block:
            deadline = time.monotonic()
        elif timeout is None:
            deadline = None
        else:
            deadline = time.monotonic() + max(timeout, 0)
        poller = select.poll()
        poller.register(self._read_fd, select.POLLIN)
        while True:
            try:
                data = os.read(self._read_fd, 1)
            except BlockingIOError:
                pass
            else:
                if not data:
                    raise OSError(errno.EPIPE, "the lock's pipe has no writer left")
                return True
            if deadline is None:
                poller.poll()
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            poller.poll(math.ceil(left * 1000))

    def release(self):
        """Give the lock back.

        :raise ValueError: when the lock is not held, as a semaphore's
            ``release`` past its bound does
        """
        if _pipe_bytes(self._read_fd):
            raise ValueError("semaphore or lock released too many times")
        os.write(self._write_fd, b"\0")

    def __enter__(self):
        """Take the lock for a ``with`` block."""
        return self.acquire()

    def __exit__(self, *exc):
        """Give the lock back at the end of a ``with`` block."""
        self.release()


class SemaphoreAtMax:
    """A ``BoundedSemaphore(SEM_VALUE_MAX)`` that never blocks.

    A ``multiprocessing.Queue`` without ``maxsize`` counts its items with
    one: ``put`` takes it and ``get`` gives it back. Its bound is
    ``SEM_VALUE_MAX`` (2,147,483,647 on Linux), which a queue never
    reaches, so taking it never waits.
    """

    def acquire(self, block=True, timeout=None):
        """Take the semaphore: always at once.

        :param bool block: unused, as there is never anything to wait for
        :param timeout: unused, for the same reason
        :return: True
        """
        return True

    def release(self):
        """Give the semaphore back: nothing to do."""

    def __enter__(self):
        """Take the semaphore for a ``with`` block."""
        return self.acquire()

    def __exit__(self, *exc):
        """Give the semaphore back at the end of a ``with`` block."""
        self.release()


def _bounded_semaphore(value=1):
    """Stand in for ``BoundedSemaphore``: only at ``SEM_VALUE_MAX``.

    :param int value: the semaphore's bound
    :rtype: SemaphoreAtMax
    :raise SemaphoreUnavailable: for any other bound
    """
    if value == _multiprocessing.SemLock.SEM_VALUE_MAX:
        return SemaphoreAtMax()
    raise SemaphoreUnavailable(_refusal(f"BoundedSemaphore({value})"))


def _refusal(name):
    """Return the message that refuses the primitive *name*.

    :param str name: e.g. ``RLock``
    :rtype: str
    """
    return (
        f"Ansible asked for multiprocessing's {name}, which needs a POSIX "
        f"semaphore, and this server cannot create one (/dev/shm is not "
        f"writable). The panel has no substitute for it: review "
        f"incubacloud/ansible_noshm.py for this version of Ansible."
    )


def _refuser(name):
    """Return a function that refuses the primitive *name*.

    :param str name: e.g. ``RLock``
    :rtype: callable
    """

    def refuse(*args, **kwargs):
        """Raise ``SemaphoreUnavailable`` naming the primitive."""
        raise SemaphoreUnavailable(_refusal(name))

    return refuse


def use_pipe_locks(ctx):
    """Replace on *ctx* the primitives that need a semaphore.

    :param ctx: a ``multiprocessing`` context; the instance is changed,
        not its class
    """
    ctx.Lock = PipeLock
    ctx.BoundedSemaphore = _bounded_semaphore
    for name in UNSUPPORTED:
        setattr(ctx, name, _refuser(name))


def launch_arguments(root, playbook, options=()):
    """Return what starts *playbook* through this file in ansible-runner.

    Copies this file to ``<root>/bin/ansible-playbook`` (0600: Python
    reads it, nothing executes it) and names the panel's own Python as the
    binary, so nothing is looked up in ``PATH`` and a ``noexec`` ``/tmp``
    does not matter. ansible-runner then starts the binary with the
    command line given here and still adds the inventory and the extra
    variables, as it does for ``ansible-playbook``; only the playbook is
    left to the command line.

    :param str root: the run's private data directory, 0700
    :param str playbook: the playbook, relative to the project directory
    :param options: further options for ansible-playbook, e.g. ``--check``
    :return: ``binary`` and ``cmdline`` for ``ansible_runner.run``
    :rtype: dict
    :raise RuntimeError: when the panel's Python is unknown
    """
    if not sys.executable:
        raise RuntimeError(
            "The panel's Python does not know its own executable "
            "(sys.executable is empty), so Ansible cannot be started "
            "without /dev/shm."
        )
    bin_dir = os.path.join(root, "bin")
    Path(bin_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    launcher = os.path.join(bin_dir, LAUNCHER_NAME)
    fd = os.open(launcher, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as target, open(__file__, "rb") as source:
        shutil.copyfileobj(source, target)
    return {
        "binary": sys.executable,
        "cmdline": shlex.join([launcher, playbook, *options]),
    }


def main():
    """Run ansible-playbook with ``sys.argv``, on pipe locks when needed.

    Exits with ansible-playbook's exit status, or with a message when
    this Python cannot import Ansible.
    """
    if semaphore_error() is not None:
        use_pipe_locks(multiprocessing.get_context("fork"))
    try:
        from ansible.cli.playbook import main as playbook_main
    except ImportError as exc:
        sys.exit(
            f"ansible-playbook cannot start: {sys.executable} cannot import "
            f"Ansible ({exc}). Install the panel's requirements.txt in the "
            f"Python that runs Odoo."
        )
    playbook_main()


if __name__ == "__main__":
    main()
