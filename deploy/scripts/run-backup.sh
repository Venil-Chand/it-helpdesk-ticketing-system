#!/bin/bash
# Nightly backup job, run by helpdesk-backup.service.
#
#   1. make an encrypted, verified backup in $HELPDESK_BACKUP_DIR
#   2. copy it to $HELPDESK_BACKUP_OFFSITE_DIR and verify the copy there
#      (skipped, with a warning, if no off-server location is configured;
#      refused if that folder is not on a separately mounted filesystem)
#   3. apply the retention policy to $HELPDESK_BACKUP_DIR
#
# Any failure stops the job with a non-zero exit status, which makes
# systemd start the OnFailure= alert unit. Nothing here prints secrets.

set -euo pipefail

FLASK="${HELPDESK_FLASK:-/opt/helpdesk/current/.venv/bin/flask}"

"$FLASK" --app app backup-create

if [[ -n "${HELPDESK_BACKUP_OFFSITE_DIR:-}" ]]; then
    # An unmounted share leaves an empty folder on the local disk; copying
    # there would look like success while nothing left the server. The
    # off-server folder must be on a different filesystem than the local
    # backups, or the job fails (and alerts) after the local backup.
    local_fs=$(stat -c %d -- "$HELPDESK_BACKUP_DIR")
    offsite_fs=$(stat -c %d -- "$HELPDESK_BACKUP_OFFSITE_DIR") || {
        echo "Error: $HELPDESK_BACKUP_OFFSITE_DIR does not exist; is the" \
             "off-server storage mounted?" >&2
        exit 1
    }
    if [[ $local_fs == "$offsite_fs" ]]; then
        echo "Error: $HELPDESK_BACKUP_OFFSITE_DIR is on the same filesystem" \
             "as $HELPDESK_BACKUP_DIR; the off-server storage is not" \
             "mounted. The local backup was made; nothing was copied." >&2
        exit 1
    fi
    "$FLASK" --app app backup-transfer
else
    echo "Warning: HELPDESK_BACKUP_OFFSITE_DIR is not set; the backup" \
         "exists on this server only." >&2
fi

"$FLASK" --app app backup-prune --apply
