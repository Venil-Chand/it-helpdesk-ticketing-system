# Administrator Guide

For people with the **admin** role. Server operations (backups, services,
monitoring) are in the [production operations guide](PRODUCTION_OPERATIONS.md).

## 1. Roles

| Role | Can do |
|---|---|
| **user** | Create tickets, see and comment on **their own** tickets, export their own tickets, change their own password. |
| **technician** | Everything a user can, on **all** tickets, plus change status and assignment. Can receive assignments. |
| **admin** | Everything a technician can, plus account administration and the audit log. Can receive assignments. |

The role is read from the database on every request, so a change applies
on the user's next page load.

## 2. Managing accounts (Users)

### Create an account

1. **Users → New account.**
2. Enter a username (it is permanent and cannot be renamed), a role and a
   temporary password, twice.
3. Give the temporary password to the person through a trusted channel
   (in person or by phone, not in the ticket system).

At first sign-in the person **must** choose their own password; until
they do, only the password page and Logout work.

### Change a role

Choose the role in the account's row and press **Save**. Demoting a
technician does not unassign their tickets; the ticket page shows such an
assignee as "no longer eligible" until someone reassigns the ticket.

### Deactivate or reactivate

**Deactivate** (confirm the dialog) signs the person out on every device
immediately and refuses their sign-ins. Their tickets and history are
kept. **Reactivate** allows sign-in again; old sessions do not come back.

Prefer deactivation to deletion. Accounts cannot be deleted from the web
interface, so a deleted user's name can never be reused by a new person
who would then see the old tickets.

You cannot deactivate your own account or change your own role. A second
administrator must do it.

### Reset a forgotten password

**Reset password** in the account's row, enter a temporary password twice,
**Set password**. All of the person's sessions end, and they must choose a
new password at their next sign-in.

There is **no self-service "forgot password" email**. Recovery is always
done by an administrator.

### If every administrator is locked out

On the server, as the service account:

```bash
flask --app app reset-password admin
```

The password is typed at a hidden prompt (twice) and is never accepted
from a pipe or an argument. The audit log records the operating-system
account that ran the command (`cli:<name>`). Deactivated accounts are
refused; this command never reactivates anyone.

## 3. Email addresses for notifications

Addresses are set on the server, not in the web interface:

```bash
flask --app app set-email alice alice@example.org
flask --app app set-email alice --clear
```

Accounts without an address are simply not notified (their queued
notifications are cancelled). See [NOTIFICATIONS.md](NOTIFICATIONS.md) for
who is notified about what.

## 4. The audit log (Audit log)

Recorded, append-only, newest first:

| Category | Events |
|---|---|
| Accounts | account created, role changed, deactivated, reactivated, password reset, password changed, email changed, audit archived |
| Sign-ins | successful and failed sign-ins (the reason is shown only here, never on the login page) |
| Operations | notification queueing failures, notification requeues |

Each event records who did it, to whom, when, and the client address.
Old events can be archived to a verified file
([AUDIT_RETENTION.md](AUDIT_RETENTION.md)); removing them is not
implemented.

## 5. Working with tickets

Administrators work tickets exactly like technicians (see the
[user guide](USER_GUIDE.md#3-for-technicians-and-administrators)).
Useful administrator tasks:

* **Reassigning** tickets from someone who left: filter the dashboard,
  open each ticket, choose a new assignee.
* **Checking assignments that are no longer valid:**
  `flask --app app assignment-audit` lists tickets assigned to accounts
  that are deactivated, demoted or do not exist.
* **Exports:** the dashboard's **Export CSV** button exports exactly the
  current search and filters. Treat exported files as confidential; they
  contain ticket descriptions.

## 6. Sign-in protection

* 5 sign-in attempts per minute per client address. The 6th is refused
  (HTTP 429), even with the right password; wait a minute.
* If the rate-limit store (Redis) is down, sign-in and password changes
  are refused with "temporarily unavailable" rather than allowed without
  limits. Everything else keeps working for people already signed in.
* Sessions last at most 8 hours from sign-in. Logout ends the session on
  **every** device of that account.

## 7. Notification problems

```bash
flask --app app notifications-status          # counts and recent failures
flask --app app notifications-requeue 812 --reason "SMTP password fixed"
```

Requeue only after fixing the cause; each requeue is audited. Details in
[NOTIFICATIONS.md](NOTIFICATIONS.md#queue-processing-commands).

## 8. Before the pilot starts

Work through the [UAT checklist](UAT_CHECKLIST.md) on the pilot server
and run one restoration drill ([BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md)).
