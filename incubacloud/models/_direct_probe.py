"""The state of a direct name's probe, and what the hosts' scripts print.

Free of Odoo on purpose: these rules are the whole of when the platform's
own hosts and panels leave the CDN and when they come back
(plan-trafico-interno-directo, piece 3), and they are worth reading, and
testing, on their own.

A name starts ``unknown``; one good probe makes it ``ok``. Two failed
probes in a row (connection, TLS, or an answer that is not the expected
one) make it ``failing``; one failed DNS check does at once, and only a
complete probe -- the job's, which reads DNS -- clears that. From
``failing`` it takes two good probes in a row and at least 30 minutes, so
a flapping name changes state at most twice in half an hour.
"""
import ipaddress
import json
from datetime import datetime, timedelta

#: The states, as ``cloud.settings`` stores them.
STATES = [
    ("unknown", "Not checked yet"),
    ("ok", "Working"),
    ("failing", "Failing"),
]

#: What ``advance`` moves, each kept as ``<prefix>_<suffix>``. The time of
#: the last probe is not here: it moves at every probe and lives in a
#: system parameter (``cloud.settings._direct_checked_at``).
SUFFIXES = ("state", "failures", "successes", "failing_since", "dns_bad")

FAILING_AFTER = 2
OK_AFTER = 2
MIN_FAILING = timedelta(minutes=30)
CERT_WARNING = timedelta(days=21)


def advance(current, outcome, now):
    """Return the probe state after *outcome*.

    :param dict current: ``state``, ``failures``, ``successes``,
        ``failing_since`` and ``dns_bad`` as stored.
    :param dict outcome: ``ok`` (bool), ``layer`` (``dns``, ``connect``,
        ``tls``, ``route`` or None) and ``source`` (``job`` or ``light``).
    :param datetime now: when the probe ran.
    :return: the same keys with their new values. The counters stop at
        what the rules need, so a state that holds changes nothing.
    :rtype: dict
    """
    state = current.get("state") or "unknown"
    failures = current.get("failures") or 0
    successes = current.get("successes") or 0
    failing_since = current.get("failing_since") or False
    dns_bad = bool(current.get("dns_bad"))
    if outcome["ok"]:
        if outcome.get("source") == "job":
            dns_bad = False
        elif dns_bad:
            # A light probe reads no DNS: it cannot clear a DNS failure.
            return dict(current)
        failures = 0
        successes = min(successes + 1, OK_AFTER)
        if state == "unknown":
            state = "ok"
        elif (
            state == "failing"
            and successes >= OK_AFTER
            and failing_since
            and now - failing_since >= MIN_FAILING
        ):
            state, failing_since = "ok", False
    else:
        successes = 0
        failures = min(failures + 1, FAILING_AFTER)
        dns = outcome.get("layer") == "dns"
        dns_bad = dns_bad or dns
        if state != "failing" and (dns or failures >= FAILING_AFTER):
            state, failing_since = "failing", now
    return {
        "state": state,
        "failures": failures,
        "successes": successes,
        "failing_since": failing_since,
        "dns_bad": dns_bad,
    }


def reset_values(prefix):
    """Return the ``cloud.settings`` values that start a direct name over.

    :param str prefix: ``metrics_central_direct`` or, in the SaaS build,
        ``saas_backchannel``.
    :rtype: dict
    """
    return {
        f"{prefix}_state": "unknown",
        f"{prefix}_failures": 0,
        f"{prefix}_successes": 0,
        f"{prefix}_failing_since": False,
        f"{prefix}_dns_bad": False,
        f"{prefix}_cert_expires": False,
        f"{prefix}_error": False,
    }


def parse_probe(output):
    """Read what ``direct_probe.sh`` printed into an outcome.

    :param str output: the step's standard output.
    :return: ``ok``, ``layer``, ``detail``, ``cert_expires`` (naive UTC
        datetime or None) and ``source`` (``job``).
    :rtype: dict
    """
    dns = tls = cert = None
    routes = []
    for line in (output or "").splitlines():
        parts = line.strip().split(" ")
        tag = parts[0]
        if tag == "IC_DIRECT_DNS" and len(parts) >= 2:
            dns = (parts[1] == "ok", " ".join(parts[2:]))
        elif tag == "IC_DIRECT_TLS" and len(parts) >= 2:
            tls = (
                parts[1] == "ok",
                parts[2] if len(parts) > 2 else "",
                " ".join(parts[3:]),
            )
        elif tag == "IC_DIRECT_CERT" and len(parts) == 2:
            try:
                cert = datetime.strptime(parts[1], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                cert = None
        elif tag == "IC_DIRECT_ROUTE" and len(parts) == 5:
            routes.append((parts[1], parts[2], parts[3], parts[4] == "ok"))
    outcome = {
        "ok": True, "layer": None, "detail": "",
        "cert_expires": cert, "source": "job",
    }
    if dns is None or tls is None:
        outcome.update(ok=False, layer="connect", detail="the probe printed nothing")
    elif not dns[0]:
        outcome.update(ok=False, layer="dns", detail=dns[1])
    elif not tls[0]:
        outcome.update(ok=False, layer=tls[1] or "connect", detail=tls[2])
    else:
        bad = [route for route in routes if not route[3]]
        if bad or not routes:
            method, path, status, _ok = bad[0] if bad else ("", "", "", False)
            outcome.update(
                ok=False, layer="route",
                detail=f"{method} {path} answered {status}".strip(),
            )
    return outcome


def allow_entries(output):
    """Return the entries of ``direct_allowlist.sh``'s ``IC_DIRECT_ALLOW`` line.

    :param str output: the step's standard output.
    :return: the entries as the host printed them (CIDR, plain addresses
        or ``a-b`` ranges, depending on its nft), or None when the line
        is missing.
    :rtype: list | None
    """
    for line in (output or "").splitlines():
        if line.startswith("IC_DIRECT_ALLOW"):
            rest = line[len("IC_DIRECT_ALLOW"):]
            return [entry.strip() for entry in rest.split(",") if entry.strip()]
    return None


def covered(entries):
    """Return the addresses *entries* cover, as collapsed networks.

    The set merges neighbouring addresses (``auto-merge``), and what the
    host prints back depends on its nft (1.0.2 and 1.1.3 print CIDR, such
    as ``46.4.0.20/31``; others a range), so it is compared by what it
    covers, not by its text.

    :param entries: CIDR strings, plain addresses or ``a-b`` ranges.
    :raises ValueError: on an entry that is none of those.
    :rtype: list
    """
    networks = []
    for entry in entries:
        if "-" in entry:
            first, last = (part.strip() for part in entry.split("-", 1))
            networks.extend(ipaddress.summarize_address_range(
                ipaddress.ip_address(first), ipaddress.ip_address(last),
            ))
        else:
            networks.append(ipaddress.ip_network(entry.strip(), strict=False))
    try:
        return list(ipaddress.collapse_addresses(networks))
    except TypeError as exc:
        # IPv4 and IPv6 in one list: the set is IPv4 only.
        raise ValueError("entries of different address families") from exc


def reach_report(raw, probes):
    """Pair what the host's try answered with the checks the panel asked for.

    :param raw: the playbook's ``ic_direct_report``, a JSON list (or the
        list itself) of ``{"url", "status", "msg"}``.
    :param list probes: ``cloud.host._direct_reach_probes`` as sent.
    :return: one ``{"url", "prefix", "expected", "status", "msg"}`` per
        probe; a probe with no answer counts as status -1.
    :rtype: list
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    answers = raw if isinstance(raw, list) else []
    report = []
    for index, probe in enumerate(probes):
        answer = answers[index] if index < len(answers) else {}
        if not isinstance(answer, dict):
            answer = {}
        try:
            status = int(answer.get("status", -1))
        except (TypeError, ValueError):
            status = -1
        report.append({
            "url": probe["url"],
            "prefix": probe["prefix"],
            "expected": int(probe["status"]),
            "status": status,
            "msg": str(answer.get("msg") or ""),
        })
    return report


def reach_reason(answer):
    """Say in a word why one of a host's tries of the direct path failed.

    :param dict answer: one entry of ``reach_report``.
    :return: ``dns``, ``tls``, ``connection`` or ``answered <status>``.
    :rtype: str
    """
    message = answer.get("msg", "").lower()
    if any(text in message for text in (
        "name or service not known", "temporary failure in name resolution",
        "nodename nor servname",
    )):
        return "dns"
    if "certificate" in message or "ssl" in message:
        return "tls"
    if answer.get("status", -1) == -1:
        return "connection"
    return f"answered {answer['status']}"
