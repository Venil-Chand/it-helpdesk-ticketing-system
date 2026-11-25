"""
Build security_data/common_passwords.txt.gz from public password lists.

The application rejects any new password found in that file. The check is
entirely offline: submitted passwords are never sent anywhere.

Usage (from the project folder):

    python scripts/build_password_blocklist.py SOURCE.txt [SOURCE.txt ...]

Only entries at least validation.MIN_PASSWORD_LENGTH characters long are
kept: shorter passwords are refused by the length rule anyway. Entries are
case-folded, de-duplicated, sorted and gzipped with a fixed timestamp, so
the same inputs always produce a byte-identical file. Lines that are not
valid UTF-8 are skipped.

The sources used for the bundled file, and their SHA-256 checksums, are
listed in security_data/README.md. Rebuild the file if the minimum length
is ever lowered.
"""

import gzip
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import validation  # noqa: E402

OUTPUT = PROJECT_ROOT / "security_data" / "common_passwords.txt.gz"


def read_entries(path, min_length, max_length):
    entries = set()

    with open(path, "rb") as source:
        for raw in source:
            try:
                text = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                continue

            if min_length <= len(text) <= max_length and "\n" not in text:
                entries.add(validation.normalize_for_blocklist(text))

    return entries


def build(sources, output=OUTPUT):
    entries = set()

    for source in sources:
        entries |= read_entries(
            source,
            validation.MIN_PASSWORD_LENGTH,
            validation.MAX_PASSWORD_LENGTH
        )

    data = "".join(f"{entry}\n" for entry in sorted(entries)).encode()

    output.parent.mkdir(exist_ok=True)

    with open(output, "wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9
    ) as compressed:
        compressed.write(data)

    return len(entries)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    count = build(sys.argv[1:])
    print(f"Wrote {count} entries to {OUTPUT}")
