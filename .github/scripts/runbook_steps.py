"""
Print the automated steps of docs/STAGING_RUNBOOK.md.

    python3 runbook_steps.py RUNBOOK --list       step names, in order
    python3 runbook_steps.py RUNBOOK NAME         that step's commands

A step is a ```bash block directly preceded by a line
"<!-- runbook: NAME -->". Unmarked blocks are for a person (or a real
server) and are never run by CI. Used by staging-runbook-check.sh.
"""

import re
import sys

MARKER = re.compile(r"^<!-- runbook: ([a-z0-9-]+) -->$")


def steps(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    found = {}
    index = 0
    while index < len(lines):
        match = MARKER.match(lines[index])
        index += 1
        if not match:
            continue
        name = match.group(1)
        if name in found:
            sys.exit(f"duplicate runbook step {name!r}")
        if index >= len(lines) or lines[index] != "```bash":
            sys.exit(f"runbook step {name!r} is not followed by a bash block")
        body = []
        index += 1
        while index < len(lines) and lines[index] != "```":
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            sys.exit(f"runbook step {name!r} has no closing fence")
        found[name] = "\n".join(body) + "\n"
    return found


def main():
    path, what = sys.argv[1], sys.argv[2]
    found = steps(path)
    if what == "--list":
        print("\n".join(found))
    elif what in found:
        sys.stdout.write(found[what])
    else:
        sys.exit(f"no runbook step {what!r}")


if __name__ == "__main__":
    main()
