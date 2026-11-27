# IT Helpdesk 1.0 Documentation

Documentation for release **1.0.0-rc1**. Each topic has one authoritative
document. Where an existing guide already covers a topic in depth, this
index points to the right section instead of repeating it.

| # | Document | For | Where |
|---|---|---|---|
| 1 | Installation guide | Developers, evaluators | [INSTALLATION.md](INSTALLATION.md) |
| 2 | Administrator guide | Helpdesk administrators | [ADMINISTRATOR_GUIDE.md](ADMINISTRATOR_GUIDE.md) |
| 3 | User guide | Requesters and technicians | [USER_GUIDE.md](USER_GUIDE.md) |
| 4 | Production deployment guide | System administrators | [PRODUCTION_OPERATIONS.md](PRODUCTION_OPERATIONS.md), sections 1–6 and 10 |
| 5 | Backup and restoration guide | System administrators | [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) (procedures), with the design in [PRODUCTION_OPERATIONS.md §7](PRODUCTION_OPERATIONS.md#7-sqlite-backup-and-recovery) |
| 6 | Security configuration | System administrators, reviewers | [SECURITY_CONFIGURATION.md](SECURITY_CONFIGURATION.md) |
| 7 | Notification configuration | System administrators | [NOTIFICATIONS.md](NOTIFICATIONS.md) |
| 8 | Monitoring and troubleshooting | System administrators | [PRODUCTION_OPERATIONS.md §9 Monitoring](PRODUCTION_OPERATIONS.md#9-monitoring) and [§13 Troubleshooting](PRODUCTION_OPERATIONS.md#13-troubleshooting) |
| 9 | Disaster recovery plan | System administrators, sponsor | [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) |
| 10 | Known limitations | Everyone | [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) |
| 11 | User acceptance testing checklist | Pilot administrator, sponsor | [UAT_CHECKLIST.md](UAT_CHECKLIST.md) |
| 12 | Release notes | Everyone | [RELEASE_NOTES_1.0.0-rc1.md](RELEASE_NOTES_1.0.0-rc1.md) |
| 13 | Release-readiness report | Sponsor, reviewers | [RELEASE_READINESS_1.0.0-rc1.md](RELEASE_READINESS_1.0.0-rc1.md) |
| 14 | Release rehearsals (how to repeat the backup, production, reliability and mutation checks) | Developers, reviewers | [REHEARSALS.md](REHEARSALS.md) |

Other references:

* [AUDIT_RETENTION.md](AUDIT_RETENTION.md): archiving old audit events.
* The repository [README](../README.md): architecture, database schema and
  migrations, SLA definitions, and the design history of each feature.

## What "verified" means in these documents

The documents state plainly whether something was **verified** (by
automated tests, the browser tests, or a rehearsal on disposable files)
or is a **requirement for the production environment** that has not been
carried out yet. No public deployment and no penetration test has taken
place.
