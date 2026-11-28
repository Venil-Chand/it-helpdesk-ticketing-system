"""
Secret files handed over by systemd's LoadCredential= (root-owned, mode
0400, plus an ACL entry granting read access to the service's account)
are private, while every other kind of group or world access is still
refused. See secret_files.only_this_account_can_read().

Most tests replace the ACL lookup with exact ACL bytes, so they run on
any file system; one test uses a real ACL (setfacl) where available.
"""

import errno
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backups
import secret_files
import security_config as config

from security_helpers import PRODUCTION_ENV, TEST_SECRET_KEY

NO_ID = 0xFFFFFFFF
USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 1, 2, 4, 8, 16, 32
READ = 4


def acl(*entries):
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identifier)
        for tag, permissions, identifier in entries)


def systemd_acl(uid=None):
    """What systemd writes: owner r, one named user r, mask r, nothing else."""

    return acl((USER_OBJ, READ, NO_ID),
               (USER, READ, os.geteuid() if uid is None else uid),
               (GROUP_OBJ, 0, NO_ID),
               (MASK, READ, NO_ID),
               (OTHER, 0, NO_ID))


class AclTestCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def write(self, name, text, mode):
        path = os.path.join(self.directory, name)
        if os.path.exists(path):  # an earlier subtest left it read-only
            os.unlink(path)
        with open(path, "w") as handle:
            handle.write(text)
        os.chmod(path, mode)
        return path

    def with_acl(self, value):
        """Make every ACL lookup return `value` (bytes) or raise it."""

        def getxattr(target, name, *args, **kwargs):
            self.assertEqual(name, secret_files.ACL_XATTR)
            if isinstance(value, BaseException):
                raise value
            return value

        patcher = patch.object(secret_files.os, "getxattr", getxattr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def private(self, path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            return secret_files.only_this_account_can_read(
                descriptor, os.fstat(descriptor))
        finally:
            os.close(descriptor)


class TestOnlyThisAccountCanRead(AclTestCase):

    def test_owner_only_modes_need_no_acl_lookup(self):
        self.with_acl(AssertionError("the ACL must not be consulted"))
        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                self.assertTrue(self.private(self.write("s", "x", mode)))

    def test_systemd_credential_is_private(self):
        self.with_acl(systemd_acl())
        self.assertTrue(self.private(self.write("s", "x", 0o440)))

    def test_read_access_for_another_account_is_refused(self):
        self.with_acl(systemd_acl(uid=os.geteuid() + 1))
        self.assertFalse(self.private(self.write("s", "x", 0o440)))

    def test_read_access_for_the_owning_group_is_refused(self):
        self.with_acl(acl((USER_OBJ, READ, NO_ID), (GROUP_OBJ, READ, NO_ID),
                          (MASK, READ, NO_ID), (OTHER, 0, NO_ID)))
        self.assertFalse(self.private(self.write("s", "x", 0o440)))

    def test_read_access_for_a_named_group_is_refused(self):
        self.with_acl(acl((USER_OBJ, READ, NO_ID), (GROUP_OBJ, 0, NO_ID),
                          (GROUP, READ, 1234), (MASK, READ, NO_ID),
                          (OTHER, 0, NO_ID)))
        self.assertFalse(self.private(self.write("s", "x", 0o440)))

    def test_world_access_is_refused_whatever_the_acl(self):
        self.with_acl(systemd_acl())
        for mode in (0o444, 0o404, 0o401):
            with self.subTest(mode=oct(mode)):
                self.assertFalse(self.private(self.write("s", "x", mode)))

    def test_group_readable_without_an_acl_is_refused(self):
        self.with_acl(OSError(errno.ENODATA, "no ACL"))
        for mode in (0o640, 0o440, 0o620):
            with self.subTest(mode=oct(mode)):
                self.assertFalse(self.private(self.write("s", "x", mode)))

    def test_file_systems_without_acls_use_the_mode_bits(self):
        self.with_acl(OSError(errno.EOPNOTSUPP, "not supported"))
        self.assertFalse(self.private(self.write("s", "x", 0o440)))
        self.assertTrue(self.private(self.write("t", "x", 0o400)))

    def test_unreadable_or_unexpected_acls_are_refused(self):
        cases = {
            "error reading it": OSError(errno.EACCES, "denied"),
            "wrong version": struct.pack("<I", 1) + systemd_acl()[4:],
            "truncated": systemd_acl()[:-3],
            "too short": b"\x02\x00",
            "unknown entry type": acl((USER_OBJ, READ, NO_ID),
                                      (0x40, READ, NO_ID)),
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                self.with_acl(value)
                self.assertFalse(self.private(self.write("s", "x", 0o440)))


class TestCallersAcceptSystemdCredentials(AclTestCase):

    def test_production_secret_key_from_a_systemd_credential(self):
        path = self.write("secret-key", TEST_SECRET_KEY + "\n", 0o440)
        self.with_acl(systemd_acl())
        environ = dict(PRODUCTION_ENV, SECRET_KEY_FILE=path)
        del environ["SECRET_KEY"]

        self.assertEqual(config.load_settings(environ).secret_key,
                         TEST_SECRET_KEY)

    def test_production_refuses_a_credential_shared_with_another_account(self):
        path = self.write("secret-key", TEST_SECRET_KEY + "\n", 0o440)
        self.with_acl(systemd_acl(uid=os.geteuid() + 1))
        environ = dict(PRODUCTION_ENV, SECRET_KEY_FILE=path)
        del environ["SECRET_KEY"]

        with self.assertRaisesRegex(config.ConfigurationError,
                                    "must not be readable by other accounts"):
            config.load_settings(environ)

    def test_backup_key_from_a_systemd_credential(self):
        path = os.path.join(self.directory, "backup-key")
        written = backups.write_new_key(path)
        os.chmod(path, 0o440)

        self.with_acl(systemd_acl())
        self.assertEqual(backups.load_key(path).key_id, written.key_id)

        self.with_acl(systemd_acl(uid=os.geteuid() + 1))
        with self.assertRaisesRegex(backups.BackupError, "chmod 600"):
            backups.load_key(path)


@unittest.skipUnless(shutil.which("setfacl"), "setfacl is not installed")
class TestRealAcl(AclTestCase):

    def test_a_real_acl_for_this_account_only(self):
        path = self.write("secret", "x", 0o400)
        result = subprocess.run(
            ["setfacl", "-m", f"u:{os.geteuid()}:r", path],
            capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("this file system has no ACL support")

        # The group bits now show the ACL mask, as for a systemd credential
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o440)
        self.assertTrue(self.private(path))

        subprocess.run(["setfacl", "-m", "g::r", path], check=True)
        self.assertFalse(self.private(path))


if __name__ == "__main__":
    unittest.main()
