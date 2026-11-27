"""
Release rehearsals: slow, end-to-end checks that run the real commands
and servers against disposable data.

    python -m rehearsals backup        backup, corruption, key and restore
    python -m rehearsals production    Gunicorn with production settings
    python -m rehearsals reliability   Redis outage, crashes, restarts,
                                       SQLite concurrency
    python -m rehearsals mutation      deliberate security regressions
    python -m rehearsals all           all four, one after another

See docs/REHEARSALS.md for what each one checks, what it needs and how to
read the results.

Every rehearsal works in a new temporary directory holding a copy of the
committed source code, its own SQLite database, random secrets and (where
needed) its own Redis server on 127.0.0.1. The repository's helpdesk.db
and .env are never opened: rehearsals/common.py refuses to start a
command whose environment could reach them.
"""
