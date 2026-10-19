"""Pinned versions and names shared by the model, executors and scripts.

wal-g is pinned with the sha256 its release publishes: the host refuses a
download that does not match. Its builds are for glibc and the images it
runs in (the agent's rclone image, the database's for a restore) are
Alpine (musl), so ``pitr.sh install`` copies the host's glibc loader and
libraries next to the binary and runs it through them (checked on Alpine
PostgreSQL 15-18 images and the rclone image, 18/19-oct-2026).
"""
import base64
import hashlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

WALG_VERSION = "v3.0.9"
#: ``wal-g-pg-20.04-<arch>``: built against glibc 2.29, which every
#: supported host OS (Ubuntu 22.04+, Debian 12+) exceeds.
WALG_SHA256 = {
    "amd64": "965a7147852435bc4b54f272799b63df826b54ef65cf7c4fc688c82b96c0c4eb",
    "aarch64": "89f0cea535eed8c5b742358bff5151e65b302eab90689d0afb38750f8d505936",
}
#: The agent's image: rclone, by digest.
RCLONE_IMAGE = (
    "rclone/rclone:1.71.1@sha256:"
    "d5971950c2b370fb04dd3292541b5bda6d9103143fd7e345aeb435a399388afc"
)

#: Where wal-g and the scripts live, relative to the instance directory,
#: and where the containers see them. The spool is the one part the agent
#: writes: the marks the database's archive command waits for.
WALG_DIR = ".walg"
WALG_MOUNT = "/opt/walg"
SPOOL_DIR = ".walg/spool"
#: The agent's environment: the archive's credentials and key. Only the
#: agent reads it; the database never does.
ENV_FILE = ".docker/pitr.env"
#: The service that uploads the log and the filestore.
AGENT_SERVICE = "pitr"
#: The database's data directory, as the db image and the agent see it.
PGDATA = "/var/lib/postgresql/data"

#: Allowed recovery objectives, in minutes: how much the instance may
#: lose at most (``archive_timeout`` and the filestore copy interval).
OBJECTIVES = [
    ("1", "1 minute"),
    ("5", "5 minutes"),
    ("15", "15 minutes"),
    ("60", "1 hour"),
]

#: The fixed key rclone obscures configuration passwords with
#: (``fs/config/obscure``). Obscuring is not encryption, it only keeps a
#: password from being read at a glance; rclone requires it.
_RCLONE_OBSCURE_KEY = bytes.fromhex(
    "9c935b48730a554d6bfd7c63c886a92bd390198eb8128afbf4de162b8b95f638"
)


def rclone_obscure(password):
    """Return *password* in the form rclone expects in its configuration.

    The IV is derived from the password rather than drawn at random, so
    the same password always renders the same file: a container whose
    environment did not change is not recreated by the next rebuild.

    :param str password: the clear password
    :return: base64url of the IV and the AES-CTR ciphertext
    :rtype: str
    """
    iv = hashlib.sha256(password.encode()).digest()[:16]
    encryptor = Cipher(
        algorithms.AES(_RCLONE_OBSCURE_KEY), modes.CTR(iv),
    ).encryptor()
    data = iv + encryptor.update(password.encode()) + encryptor.finalize()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def filestore_password(pitr_key):
    """Derive the filestore's encryption password from the instance's key.

    One secret per instance encrypts both halves: the database archive
    (wal-g, libsodium) with the key itself, the filestore (rclone crypt)
    with this derivation of it.

    :param str pitr_key: the instance's ``pitr_key`` (64 hex characters)
    :rtype: str
    """
    return hashlib.sha256(
        b"incubacloud-pitr-filestore:" + pitr_key.encode(),
    ).hexdigest()
