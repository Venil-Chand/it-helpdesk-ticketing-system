"""
Server-side validation for text submitted through forms.

Browser limits such as maxlength are only a convenience, because anyone can
send a request without using the form. These checks are the real limits.

Lengths are counted in Unicode characters (code points), so an accented
letter or an emoji counts as one character however many bytes it uses.
Text that is too long is rejected and never shortened.
"""

import re
import unicodedata
from dataclasses import dataclass

# Whole-request limits. The form limit matches the body limit so that one
# number decides when a form is too large.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_FORM_PARTS = 50

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 10_000
MAX_COMMENT_LENGTH = 10_000

# Sign-in limits. An oversized password is refused before it reaches the
# deliberately slow password hash, so it cannot be used to waste CPU time.
MAX_USERNAME_LENGTH = 150
MAX_PASSWORD_LENGTH = 1024

# The same minimum setup_accounts.py has always required
MIN_PASSWORD_LENGTH = 16

# Names for NEW accounts: 3-64 letters, digits, ".", "_" or "-", starting
# with a letter or digit. Existing accounts keep their names (usernames
# are permanent). Plain ASCII avoids look-alike names such as a Cyrillic
# "а" in "admin", and the names are safe in logs, URLs and CSV files.
NEW_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")


def username_problem(username):
    """Why `username` cannot be used for a new account, or None."""

    if not NEW_USERNAME_PATTERN.fullmatch(username or ""):
        return (
            "Username must be 3 to 64 characters: letters, digits, "
            "'.', '_' or '-', starting with a letter or digit."
        )

    return None


def new_password_problem(password, confirmation):
    """
    Why `password` cannot be set, or None.

    The rules are the ones setup_accounts.py already applies: at least
    MIN_PASSWORD_LENGTH characters, at most MAX_PASSWORD_LENGTH (longer
    would be refused at the login page), and typed the same way twice.
    """

    password = password or ""

    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"Password must contain at least {MIN_PASSWORD_LENGTH} "
            "characters."
        )

    if len(password) > MAX_PASSWORD_LENGTH:
        return (
            f"Password must contain at most {MAX_PASSWORD_LENGTH} "
            "characters."
        )

    if password != (confirmation or ""):
        return "Passwords do not match."

    return None

# Control characters allowed in multi-line text, after newline normalising
_MULTILINE_CONTROLS = frozenset("\n\t")


@dataclass(frozen=True)
class CleanedText:
    """The text to store, and a safe message if it was rejected."""

    value: str
    error: str = None


def normalize_newlines(text):
    """
    Convert Windows and old Mac line endings to a single newline.

    Browsers submit each textarea line break as two characters (CRLF) but
    count it as one for maxlength. Normalising keeps both counts the same.
    """

    return text.replace("\r\n", "\n").replace("\r", "\n")


def _has_forbidden_character(text, *, multiline):
    """Find control characters and invalid (surrogate) code points."""

    for character in text:
        category = unicodedata.category(character)

        if category == "Cs":
            return True

        if category == "Cc" and not (
            multiline and character in _MULTILINE_CONTROLS
        ):
            return True

    return False


def _has_visible_character(text):
    """False for text made only of spaces and invisible format characters."""

    return any(unicodedata.category(c)[0] not in "ZC" for c in text)


def clean_text(value, *, label, max_length, multiline):
    """
    Normalise and check one submitted text field.

    Text with nothing visible becomes "", so the caller's existing
    "required field" check handles it.
    """

    text = normalize_newlines(value or "").strip()

    if _has_forbidden_character(text, multiline=multiline):
        return CleanedText(
            text, f"{label} contains characters that are not allowed."
        )

    if len(text) > max_length:
        return CleanedText(
            text,
            f"{label} must be {max_length:,} characters or fewer "
            f"(you entered {len(text):,})."
        )

    if not _has_visible_character(text):
        return CleanedText("")

    return CleanedText(text)
