"""SEC-013 — a browser-uploaded restore must reach the job that sends it.

The upload is served by ``odoo`` and consumed by ``odoo_runner``. Since
the runner was split out those are separate containers sharing exactly
one mount, the data directory, so ``/tmp`` is private to each. Staging an
upload in ``/tmp`` and passing the path in a job payload named a file the
executor could not open: it answered "Backup file not found on Odoo
server. Please re-upload and try again." — advice that produces the same
failure forever, leaving up to 2 GiB behind each time, because the only
``unlink`` for that file lived on the side that could not see it.

Nothing here proves the containers are separate; that is a deployment
fact. What these pin is the consequence: the writer and the validator
agree on one location, that location is under the data dir rather than
``/tmp``, and no path outside it survives validation — the payload
reaches ``restore_db`` over JSON-RPC, so the validator is what stands
between a caller and having an arbitrary file uploaded to a host and
then deleted.
"""
import asyncio
import os
import re
import time
from pathlib import Path
from unittest.mock import create_autospec, patch

from odoo.tests.common import BaseCase, TransactionCase
from odoo.tools import config

from .. import restore_staging
from ..models.transport import SSHTransport
from ..restore_staging import (
    is_staged_upload,
    new_upload_path,
    purge_stale,
    staging_dir,
)
from .test_host_handoff import _RestoreExecutorMixin


class TestStagingLocation(BaseCase):
    """Where the archive lands, and why it is not ``/tmp``."""

    def test_staging_lives_under_the_data_dir(self):
        """The data dir is the one mount both containers share.

        If this ever moves back under ``tempfile.gettempdir()``, the
        handoff silently stops working again — the executor simply
        reports the file as missing.
        """
        self.assertEqual(
            staging_dir().parent.resolve(),
            Path(config['data_dir']).resolve(),
        )

    def test_staging_is_not_the_system_temp_dir(self):
        import tempfile
        self.assertNotEqual(
            staging_dir().resolve(),
            Path(tempfile.gettempdir()).resolve(),
        )

    def test_staging_is_outside_the_filestore(self):
        """Odoo's filestore GC walks ``filestore/`` and deletes whatever
        no ``ir.attachment`` points at. These files have no attachment,
        so they must not live there."""
        self.assertNotIn('filestore', staging_dir().parts)

    def test_the_directory_is_private(self):
        self.assertEqual(staging_dir().stat().st_mode & 0o777, 0o700)


class TestStagedUploadValidation(BaseCase):
    """What ``local_path`` is allowed to name.

    ``restore_db`` is reachable over JSON-RPC, so a caller controls this
    value. The executor uploads the file it names to a host and then
    unlinks it — both things it must never do to a file it was not given.
    """

    def setUp(self):
        super().setUp()
        self.instance_id = 42

    def test_a_path_this_module_produced_is_accepted(self):
        path = new_upload_path(self.instance_id)
        self.assertTrue(is_staged_upload(str(path), self.instance_id))

    def test_another_instances_upload_is_refused(self):
        """Scoping, not just location: one instance's restore must not
        consume an archive staged for a different one."""
        path = new_upload_path(self.instance_id + 1)
        self.assertFalse(is_staged_upload(str(path), self.instance_id))

    def test_paths_outside_the_staging_directory_are_refused(self):
        for bad in (
            '/etc/odoo/odoo.conf',
            '/tmp/cloud_restore_42_deadbeef.zip',
            str(Path(config['data_dir']) / 'filestore' / 'x.zip'),
            '',
            None,
        ):
            with self.subTest(path=bad):
                self.assertFalse(is_staged_upload(bad, self.instance_id))

    def test_traversal_out_of_the_staging_directory_is_refused(self):
        """``resolve()`` runs before the comparison, so ``..`` cannot
        walk out and still look like it belongs."""
        escape = staging_dir() / f'cloud_restore_{self.instance_id}_x' \
            / '..' / '..' / 'passwd'
        self.assertFalse(is_staged_upload(str(escape), self.instance_id))

    def test_a_sibling_directory_with_the_same_prefix_is_refused(self):
        """The parent is compared exactly. A string-prefix check would
        accept ``<staging>-evil/`` because it starts the same way."""
        sibling = Path(f'{staging_dir()}-evil')
        candidate = sibling / f'cloud_restore_{self.instance_id}_x.zip'
        self.assertFalse(is_staged_upload(str(candidate), self.instance_id))

    def test_two_uploads_for_one_instance_do_not_collide(self):
        """Two operators, or one retrying, must not overwrite each other
        mid-transfer."""
        first = new_upload_path(self.instance_id)
        second = new_upload_path(self.instance_id)
        self.assertNotEqual(first, second)


class TestWriterAndValidatorAgree(BaseCase):
    """The two halves live in different files and different containers.

    Every other test here would still pass if the controller stopped
    calling ``new_upload_path`` and went back to ``tempfile.mkstemp``.
    This is the one that would not.
    """

    def test_the_controller_stages_through_this_module(self):
        from ..controllers import main
        self.assertIs(main.new_upload_path, new_upload_path)

    def test_the_executor_validates_through_this_module(self):
        from ..models import restore_instance_executor as ex
        self.assertIs(ex.is_staged_upload, is_staged_upload)

    def test_the_controller_no_longer_reaches_for_the_temp_dir(self):
        """``tempfile`` was how the archive ended up in the wrong
        container; the import going away is what keeps it out."""
        from ..controllers import main
        self.assertFalse(hasattr(main, 'tempfile'))


class TestPurgeStale(BaseCase):
    """The upload whose job never runs.

    The executor's ``finally`` covers a transfer that failed. It cannot
    cover a job that was cancelled before it started, or enqueued against
    an instance that has since been removed — nothing runs, so nothing
    deletes. At up to 2 GiB each, those accumulate until the container is
    recreated, which is the only reason this has not bitten yet.
    """

    def setUp(self):
        super().setUp()
        self.dir = staging_dir()

    def _staged(self, name, age_hours=0):
        path = self.dir / name
        path.write_bytes(b'zip')
        self.addCleanup(lambda p=path: p.unlink(missing_ok=True))
        if age_hours:
            old = time.time() - age_hours * 3600
            os.utime(path, (old, old))
        return path

    def test_an_abandoned_upload_is_removed(self):
        old = self._staged('cloud_restore_1_old.zip', age_hours=48)
        purge_stale()
        self.assertFalse(old.exists())

    def test_an_upload_still_waiting_its_turn_is_kept(self):
        """Production forces a full backup of the target before the
        restore, so a legitimate archive can wait hours. Reaping one mid
        flight would fail a restore that was going to work."""
        fresh = self._staged('cloud_restore_1_fresh.zip')
        recent = self._staged('cloud_restore_1_recent.zip', age_hours=6)
        purge_stale()
        self.assertTrue(fresh.exists())
        self.assertTrue(recent.exists())

    def test_the_cutoff_is_what_decides(self):
        borderline = self._staged('cloud_restore_1_border.zip', age_hours=2)
        self.assertEqual(purge_stale(max_age_hours=1), 1)
        self.assertFalse(borderline.exists())

    def test_an_unreadable_entry_does_not_abort_the_sweep(self):
        """One bad file must not leave every later one behind."""
        first = self._staged('cloud_restore_1_a.zip', age_hours=48)
        second = self._staged('cloud_restore_1_b.zip', age_hours=48)
        real_unlink = Path.unlink

        def flaky(self_path, *args, **kwargs):
            if self_path == first:
                raise OSError('permission denied')
            return real_unlink(self_path, *args, **kwargs)

        with patch.object(Path, 'unlink', flaky):
            purge_stale()
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())


class TestGcCronIsWired(BaseCase):
    """A sweep nothing schedules is dead code.

    The cron names its entry point in a string inside an XML file, which
    no import or call graph can check. Renaming the method would leave
    the cron raising once a day into the log while abandoned uploads
    quietly piled up.
    """

    def _cron_xml(self):
        path = (
            Path(__file__).resolve().parent.parent
            / 'data' / 'restore_staging_gc_cron.xml'
        )
        return path.read_text(encoding='utf-8')

    def test_the_cron_calls_a_method_that_exists(self):
        from ..models.cloud_instance import CloudInstance
        match = re.search(
            r'<field name="code">model\.(\w+)\(\)</field>', self._cron_xml(),
        )
        self.assertTrue(match, 'the cron declares no model method call')
        self.assertTrue(
            hasattr(CloudInstance, match.group(1)),
            f'the cron calls {match.group(1)}(), which does not exist',
        )


class TestExecutorConsumesTheStagedUpload(_RestoreExecutorMixin,
                                          TransactionCase):
    """What the runner does with the file the web worker staged."""

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'rs-proj'})
        self.host = self.env['cloud.host'].create({
            'name': 'rs-host', 'ip_address': '10.0.9.1', 'port': 22,
            'user': 'root', 'login_type': 'ssh_key',
            'wildcard_domain': 'rs.example.com',
            'status': 'compatible', 'traefik_deployed': True,
        })
        self.instance = self.env['cloud.instance'].create({
            'name': 'rs-inst', 'project_id': self.project.id,
            'environment': 'staging', 'host_id': self.host.id,
            'state': 'deployed',
        })

    def _executor(self, local_path):
        job = self._create_job('restore_instance', self.host, self.instance)
        job.payload = {'mode': 'browser', 'local_path': str(local_path)}
        return self._make_restore_executor(job)

    def _staged_archive(self):
        path = new_upload_path(self.instance.id)
        path.write_bytes(b'PK\x03\x04 pretend this is a backup')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_a_staged_upload_is_sent_and_then_removed(self):
        path = self._staged_archive()
        ex = self._executor(path)
        transport = create_autospec(SSHTransport, instance=True)

        asyncio.run(ex.before_execute(transport))

        transport.upload_file.assert_awaited_once()
        sent = transport.upload_file.await_args.args[0]
        self.assertEqual(Path(sent), path.resolve())
        self.assertFalse(
            path.exists(), 'the archive must not outlive the transfer',
        )

    def test_a_failed_transfer_still_removes_the_archive(self):
        """The leak this fix exists to close.

        Every reason a transfer fails — host down, disk full on the far
        side, connection dropped mid-stream — used to leave up to 2 GiB
        behind, and the operator's natural response is to upload again.
        """
        path = self._staged_archive()
        ex = self._executor(path)
        transport = create_autospec(SSHTransport, instance=True)
        transport.upload_file.side_effect = OSError('host unreachable')

        with self.assertRaises(OSError):
            asyncio.run(ex.before_execute(transport))

        self.assertFalse(path.exists())

    def test_a_path_outside_the_staging_area_is_refused_untouched(self):
        """The payload arrives over JSON-RPC. Naming someone else's file
        must not get it uploaded to a host — nor deleted."""
        outsider = Path(config['data_dir']) / 'rs-not-an-upload.zip'
        outsider.write_bytes(b'precious')
        self.addCleanup(lambda: outsider.unlink(missing_ok=True))
        ex = self._executor(outsider)
        transport = create_autospec(SSHTransport, instance=True)

        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(transport))

        transport.upload_file.assert_not_awaited()
        self.assertTrue(
            outsider.exists(),
            'a refused path must not be unlinked either',
        )

    def test_an_upload_staged_for_another_instance_is_refused(self):
        other = new_upload_path(self.instance.id + 1000)
        other.write_bytes(b'not yours')
        self.addCleanup(lambda: other.unlink(missing_ok=True))
        ex = self._executor(other)

        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(
                create_autospec(SSHTransport, instance=True),
            ))

        self.assertTrue(other.exists())


class TestGcCronEntryPoint(TransactionCase):
    """The method the cron names must actually sweep."""

    def test_calling_it_removes_an_abandoned_upload(self):
        path = staging_dir() / 'cloud_restore_9_abandoned.zip'
        path.write_bytes(b'zip')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        old = time.time() - restore_staging.STALE_AFTER_HOURS * 3600 - 60
        os.utime(path, (old, old))

        self.env['cloud.instance']._gc_restore_uploads()

        self.assertFalse(path.exists())
