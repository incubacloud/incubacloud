"""Room in queue_job's channels, counted as its runner counts it.

queue_job's runner starts a job when its channel, and every channel above
it, has fewer jobs running than its capacity. A job that parks between two
attempts (``JobParked``) goes back to ``pending`` with an ``eta``, and the
runner stops counting it: in the 5 s before it comes back, the runner may
give its place to another job waiting in the channel, and the parked job
then waits for that one to end. With ``root:1``, the usual on Odoo.sh, a
parked rebuild would wait for a second rebuild whole.

So the panel keeps the place for it: its dispatcher hands a job to
queue_job only while the job's channel has room, counting the panel's
parked jobs as running (``cloud.job._channel_admits``). This module reads
the channels as the runner does and counts the load on them; the
dispatcher reads what runs.

The capacity comes from one source, the runner's: ``ODOO_QUEUE_JOB_CHANNELS``
in the environment, else ``channels`` in odoo.conf's ``[queue_job]``
section, else ``root:1``. The process that dispatches and the runner must
see the same string (``INSTALL.md``): on Odoo.sh they are one process, in
Doodba the service's environment, in production the odoo.conf.
"""
import os
from collections import Counter

from odoo.addons.queue_job.jobrunner import queue_job_config
from odoo.addons.queue_job.jobrunner.channels import ChannelManager

#: What queue_job's runner runs with when nothing configures its channels.
DEFAULT_CHANNELS = "root:1"

#: The odoo.conf option, in the ``[incubacloud]`` section, that turns the
#: channel gate off (10-nov-2026): ``channel_gate = off`` dispatches as
#: before it, without counting the room of the channels, should a wrong
#: count hold jobs back; no deploy needed to lift it.
CHANNEL_GATE_OPTION = "channel_gate"

#: The channel the panel's jobs of people run in, which a parked job keeps
#: its place in: it needs a capacity of its own.
USER_CHANNEL = "root.user"

#: Configured channel managers, by configuration string.
_MANAGERS = {}


def configured_channels():
    """Return the channels string queue_job's runner runs with.

    The same sources, in the same order, as the runner's ``_channels()``.

    :rtype: str
    """
    return (
        os.environ.get("ODOO_QUEUE_JOB_CHANNELS")
        or queue_job_config.get("channels")
        or DEFAULT_CHANNELS
    )


def channel_manager(config):
    """Return a ``ChannelManager`` configured with *config*, kept for reuse.

    Only its channels are used — names, capacities, parents — never its
    jobs: the load is counted from the database.

    :param str config: a channels string, as ``configured_channels``
    :rtype: ChannelManager
    """
    manager = _MANAGERS.get(config)
    if manager is None:
        manager = ChannelManager()
        manager.simple_configure(config)
        _MANAGERS[config] = manager
    return manager


def resolve_channel(manager, name):
    """Return the channel a job of channel *name* runs in, as the runner does.

    A channel not configured is created under its nearest configured
    ancestor, without capacity of its own (``parent_fallback``).

    :param manager: a ``ChannelManager``
    :param str name: a queue job's channel
    """
    return manager.get_channel_by_name(name or "root", parent_fallback=True)


def channel_loads(manager, names):
    """Return the load on every channel, given the channels jobs run in.

    A channel's load is its own jobs plus, from each channel under it, as
    many as that channel lets through: its load, or its capacity when that
    is lower — what waits in a full channel does not reach the one above.

    :param manager: a ``ChannelManager``
    :param names: the channel of each job that takes a place, one per job
    :return: ``{channel: load}``, for every channel of the manager
    :rtype: dict
    """
    direct = Counter(resolve_channel(manager, name) for name in names)
    loads = {}

    def _load(channel):
        """Count *channel*'s load, and its children's on the way."""
        total = direct.get(channel, 0)
        for child in channel.children.values():
            through = _load(child)
            total += min(through, child.capacity) if child.capacity else through
        loads[channel] = total
        return total

    _load(manager.get_channel_by_name("root"))
    return loads


def full_channel(manager, name, names):
    """Return what keeps one more job of channel *name* from running, if anything.

    The first channel, from the job's own up to ``root``, whose load has
    reached its capacity. A channel without capacity (or 0) never fills.

    :param manager: a ``ChannelManager``
    :param str name: the channel of the job to start
    :param names: the channel of each job that takes a place
    :return: ``(channel name, load, capacity)``, or None when there is room
    """
    channel = resolve_channel(manager, name)
    loads = channel_loads(manager, names)
    while channel is not None:
        load = loads.get(channel, 0)
        if channel.capacity and load >= channel.capacity:
            return channel.fullname, load, channel.capacity
        channel = channel.parent
    return None


def channel_wait(manager, name, names):
    """Return the channel a job of channel *name* waits on, its load and capacity.

    The first full channel on its way to ``root`` (``full_channel``); with
    room everywhere, the first that has a capacity. For what a job held
    back shows: «root.user 1/1».

    :param manager: a ``ChannelManager``
    :param str name: the channel of the job
    :param names: the channel of each job that takes a place
    :return: ``(channel name, load, capacity)``; capacity None when no
        channel on the way has one
    """
    full = full_channel(manager, name, names)
    if full is not None:
        return full
    loads = channel_loads(manager, names)
    channel = resolve_channel(manager, name)
    while channel.parent is not None and not channel.capacity:
        channel = channel.parent
    return channel.fullname, loads.get(channel, 0), channel.capacity or None


def channel_problems(config, foreign=()):
    """Return what in the channels makes a parked job wait for others.

    Informational: nothing is refused. A job parked between two attempts
    keeps its place only against the panel's own jobs; it may wait for
    any other to end.

    :param str config: the channels string the runner runs with
    :param foreign: channels other modules' queue jobs ran in lately
    :return: one message per problem
    :rtype: list[str]
    """
    problems = []
    names = {
        item["name"] if item["name"] == "root" or item["name"].startswith("root.")
        else f"root.{item['name']}"
        for item in ChannelManager.parse_simple_config(config)
    }
    if USER_CHANNEL not in names:
        problems.append(
            f"queue_job's channels ({config}) give {USER_CHANNEL} no capacity"
            " of its own: a job parked between two attempts may wait for the"
            f" jobs of other channels to end. Give it one, for example"
            f" channels = root:3,{USER_CHANNEL}:1,root.bg:1."
        )
    if foreign:
        problems.append(
            "Other modules use the job queue in "
            f"{', '.join(sorted(foreign))}: a job parked between two attempts"
            " may wait for theirs to end."
        )
    return problems
