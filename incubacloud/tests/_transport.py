"""Transport mocks that take a job's scripts the way a host does: as one tar.

``_upload_scripts`` hands a host the scripts on the input of one command
(``run_with_stdin``) and falls back to SFTP file by file when that fails.
A ``MagicMock(spec=SSHTransport)`` left as it is answers that command with
a mock, never 0, so every test went the fallback way and a tar that does
not work would not show. The helpers here make a mock take the tar, and
read back what it carried.
"""
import io
import tarfile
from unittest.mock import AsyncMock

from odoo.addons.incubacloud.models.transport import CommandResult


def take_the_tar(transport):
    """Make *transport* unpack the scripts' tar, as a host does (exit 0).

    :param transport: a mock spec'd against ``SSHTransport``
    :return: the same mock
    """
    transport.run_with_stdin = AsyncMock(return_value=CommandResult("", 0))
    return transport


def uploaded_scripts(transport):
    """Return the scripts the tar calls of *transport* carried.

    :param transport: a mock :func:`take_the_tar` prepared
    :return: ``{remote path: text}``, the paths under the root each
        command unpacked into
    :rtype: dict
    """
    files = {}
    for call in transport.run_with_stdin.await_args_list:
        command, data = call.args[0], call.args[1]
        root = command.rsplit(" -C ", 1)[-1].strip("'")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    files[f"{root}/{member.name}"] = (
                        archive.extractfile(member).read().decode("utf-8")
                    )
    return files
