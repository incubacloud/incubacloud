"""Post-migrate for 1.0.128 — re-anchor the fleet after two new answers.

``config_dirty`` compares a hash of the whole config snapshot against
the one the last deploy stamped, and the snapshot carries
``_render_copier_answers()`` entire. This release answers two questions
the template had been defaulting for us (``whitelisted_hosts_test`` and
``whitelisted_hosts_devel``), so every instance's hash moves at once and
the "Changes not deployed" pill lights up fleet-wide with nobody having
edited a thing. It has happened twice before — ``5fcc2c5`` and
``1f3b23e`` — and both times the pill stayed on until each instance was
rebuilt or re-anchored by hand.

**Binary gate, so this can never mask real drift.** A record is
re-stamped only when the snapshot it renders *today, minus exactly the
two new answers*, still hashes to the anchor it already carries. That
proves nothing else moved since its last deploy. Anything else — a repo
pin, a domain, a backup backend, a resource limit — changes that reduced
hash too, and the record is left dirty, which is what it is.

**Production only, on purpose.** For a staging the two answers are not
cosmetic: an empty ``whitelisted_hosts_test`` removes the NAT gateway
and the privileged sidecar the template had been rendering, and this
release also puts the mail catcher behind ``traefik.enable=false``. A
staging's compose really does change, so it stays dirty until it is
redeployed. Production's compose is byte-for-byte identical (verified by
rendering the pinned template both ways).

Hosts are untouched: their snapshot has no copier answers.
"""
import hashlib
import json
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# The keys this release adds. Removing them reconstructs the snapshot as
# the previous release rendered it.
_NEW_ANSWER_KEYS = ("whitelisted_hosts_test", "whitelisted_hosts_devel")


def _hash_without_new_keys(inst):
    """Return the config hash this instance would have had before 1.0.128.

    Mirrors ``_config_snapshot_hash`` exactly — same canonical JSON dump,
    same digest — over a snapshot whose answers lack the new keys.

    :param inst: a ``cloud.instance`` record
    :return: hex digest as :class:`str`
    """
    snapshot = inst._render_config_snapshot()
    answers = dict(snapshot.get("answers") or {})
    for key in _NEW_ANSWER_KEYS:
        answers.pop(key, None)
    snapshot = {**snapshot, "answers": answers}
    raw = json.dumps(snapshot, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def migrate(cr, version):
    """Re-stamp production instances whose only change is the new answers."""
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})

    instances = env["cloud.instance"].sudo().with_context(
        active_test=False,
    ).search([
        ("environment", "=", "production"),
        ("state", "in", ("deployed", "deleting")),
        ("applied_config_hash", "!=", False),
    ])

    reanchored = skipped = 0
    for inst in instances:
        try:
            if _hash_without_new_keys(inst) != inst.applied_config_hash:
                skipped += 1
                continue
            inst.write(inst._applied_config_vals())
            reanchored += 1
        except Exception:
            # A snapshot that cannot render (an undecryptable secret, a
            # legacy row) must not block the upgrade. The record simply
            # keeps its old anchor and reads as dirty, which is the safe
            # direction.
            _logger.warning(
                "1.0.128: could not re-anchor instance %s", inst.id,
                exc_info=True,
            )
            skipped += 1

    _logger.info(
        "1.0.128: re-anchored %s production instance(s); %s left dirty "
        "(real drift, or unrenderable snapshot). Stagings are dirty on "
        "purpose — their compose changed.",
        reanchored, skipped,
    )
