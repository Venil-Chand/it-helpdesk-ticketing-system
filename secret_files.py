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

import os
import stat

# A secret file larger than this is certainly a mistake (wrong file)
MAX_SECRET_FILE_BYTES = 4096


class SecretFileError(ValueError):
    """The secret could not be read. The message names variables only."""


def file_variable(name):
    return f"{name}_FILE"


def read_secret(environ, name, *, strip=False, require_private=False):
    """
    Return the value of secret NAME ("" if neither form is set).

    strip            remove surrounding whitespace (for keys and URLs,
                     never for passwords, which may contain spaces)
    require_private  refuse a file that other accounts can read, which
                     production uses: a group- or world-readable secret
                     is not a secret on a shared server
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

            if require_private and info.st_mode & 0o077:
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
