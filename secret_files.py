"""
Read a secret either from an environment variable or from a file.

Every secret setting NAME can instead be given as NAME_FILE, the absolute
path of a file holding the value. This lets a production service receive
its secrets through systemd's LoadCredential= (a private, read-only file
under $CREDENTIALS_DIRECTORY) rather than through its environment, which
is easier to leak: child processes inherit it, and "systemctl show" or a
crash report can display it.

    SECRET_KEY=...                      either this
    SECRET_KEY_FILE=/run/credentials/helpdesk-web.service/secret-key
                                        or this, never both

The file's content is used as it is, except for one trailing line break,
which editors add. Problems are reported by variable name only; neither
the value nor the file's path is ever placed in an error message.
"""

import errno
import os
import stat
import struct

# A secret file larger than this is certainly a mistake (wrong file)
MAX_SECRET_FILE_BYTES = 4096

# POSIX access ACLs, as Linux stores them in the system.posix_acl_access
# extended attribute: a 4-byte version (2), then 8-byte entries
# (tag, permissions, id), little-endian
ACL_XATTR = "system.posix_acl_access"
_ACL_VERSION = 2
_ACL_ENTRY = struct.Struct("<HHI")
_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ = 0x01, 0x02, 0x04
_ACL_GROUP, _ACL_MASK, _ACL_OTHER = 0x08, 0x10, 0x20


class SecretFileError(ValueError):
    """The secret could not be read. The message names variables only."""


def file_variable(name):
    return f"{name}_FILE"


def _access_acl(descriptor):
    """The file's ACL entries as (tag, permissions, id), or None if none."""

    try:
        data = os.getxattr(descriptor, ACL_XATTR)
    except OSError as error:
        if error.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return None
        raise
    except AttributeError:  # no extended attributes on this platform
        return None

    body = data[4:]
    if (len(data) < 4 or struct.unpack_from("<I", data)[0] != _ACL_VERSION
            or len(body) % _ACL_ENTRY.size):
        raise ValueError("unreadable ACL")

    return list(_ACL_ENTRY.iter_unpack(body))


def only_this_account_can_read(descriptor, info):
    """
    True if no account other than the file's owner and this process's own
    account (and root) can read or change the file.

    Without an ACL that means no group or other permission bits, as
    "chmod 600" or "chmod 400" give. systemd's LoadCredential= instead
    gives a service its secrets as root-owned, mode 0400 files with one
    ACL entry granting read access to the service's account; the group
    bits then show the ACL mask (0440). Such a file is accepted when the
    ACL grants nothing to "other", nothing to any group, and access only
    to this process's account. Anything unexpected (including an ACL that
    cannot be read) counts as not private.
    """

    if not info.st_mode & 0o077:
        return True  # chmod 600/400; with an ACL, its mask is then empty

    if info.st_mode & 0o007:
        return False

    try:
        entries = _access_acl(descriptor)
    except (OSError, ValueError):
        return False

    if entries is None:
        return False  # plainly group-readable

    own = os.geteuid()
    for tag, permissions, identifier in entries:
        if tag in (_ACL_USER_OBJ, _ACL_MASK):
            continue
        if tag == _ACL_USER and identifier == own:
            continue
        if tag in (_ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_OTHER):
            if permissions:
                return False
            continue
        return False  # an entry type this check does not know

    return True


def read_secret(environ, name, *, strip=False, require_private=False):
    """
    Return the value of secret NAME ("" if neither form is set).

    strip            remove surrounding whitespace (for keys and URLs,
                     never for passwords, which may contain spaces)
    require_private  refuse a file that other accounts can read, which
                     production uses: a group- or world-readable secret
                     is not a secret on a shared server. systemd
                     credentials (read access by ACL for this account
                     only) count as private; see
                     only_this_account_can_read()
    """

    file_name = file_variable(name)
    direct = environ.get(name, "")
    path = environ.get(file_name, "").strip()

    if direct and path:
        raise SecretFileError(f"Set only one of {name} or {file_name}.")

    if not path:
        return direct.strip() if strip else direct

    if not os.path.isabs(path):
        raise SecretFileError(f"{file_name} must be an absolute path.")

    try:
        # O_NONBLOCK: opening a FIFO by mistake must not hang startup.
        # O_NOFOLLOW is not used: systemd credentials may be symlinks.
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
        )

        try:
            info = os.fstat(descriptor)

            if not stat.S_ISREG(info.st_mode):
                raise SecretFileError(f"{file_name} must name a regular file.")

            if require_private and not only_this_account_can_read(
                    descriptor, info):
                raise SecretFileError(
                    f"{file_name} must not be readable by other accounts "
                    "(use chmod 600 or 400)."
                )

            data = os.read(descriptor, MAX_SECRET_FILE_BYTES + 1)

        finally:
            os.close(descriptor)

    except OSError:
        raise SecretFileError(f"{file_name} could not be read.") from None

    if len(data) > MAX_SECRET_FILE_BYTES:
        raise SecretFileError(f"{file_name} is too large to be a secret.")

    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretFileError(f"{file_name} must contain UTF-8 text.") from None

    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]

    return value.strip() if strip else value
