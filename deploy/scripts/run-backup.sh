#!/bin/bash
# Nightly backup job, run by helpdesk-backup.service.
#
#   1. make an encrypted, verified backup in $HELPDESK_BACKUP_DIR
#   2. copy it to $HELPDESK_BACKUP_OFFSITE_DIR and verify the copy there
#      (skipped, with a warning, if no off-server location is configured)
#   3. apply the retention policy to $HELPDESK_BACKUP_DIR
#
# Any failure stops the job with a non-zero exit status, which makes
# systemd start the OnFailure= alert unit. Nothing here prints secrets.

set -euo pipefail

FLASK="${HELPDESK_FLASK:-/opt/helpdesk/current/.venv/bin/flask}"

"$FLASK" --app app backup-create

if [[ -n "${HELPDESK_BACKUP_OFFSITE_DIR:-}" ]]; then
    "$FLASK" --app app backup-transfer
else
    echo "Warning: HELPDESK_BACKUP_OFFSITE_DIR is not set; the backup" \
         "exists on this server only." >&2
fi

"$FLASK" --app app backup-prune --apply
