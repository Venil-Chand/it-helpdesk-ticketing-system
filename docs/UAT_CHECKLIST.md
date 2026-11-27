# User Acceptance Testing Checklist: 1.0.0-rc1

Run on the **pilot server**, through its real HTTPS address, with
**demo accounts and demo data only**. Record the result (Pass/Fail), who
tested it, and the date. Rows marked **(auto)** are also covered by the
automated browser tests; checking them on the real server confirms the
deployment, not the code.

Tester: ____________  Date: ____________  Server/URL: ____________  Commit: ____________

## A. Access and accounts

| # | Step | Expected | Result |
|---|---|---|---|
| A1 | Open `http://<host>/` | Redirected to `https://`; padlock shown; certificate valid | |
| A2 | Sign in with a wrong password **(auto)** | Refused with a generic message | |
| A3 | Sign in as the administrator; sign out **(auto)** | Dashboard, then the login page | |
| A4 | Create a user account with a temporary password **(auto)** | Account listed, "Must change password" badge | |
| A5 | Sign in as that user **(auto)** | Forced to the password page; other pages unavailable | |
| A6 | Choose a short password, then one containing the username | Both refused with an explanation | |
| A7 | Choose a valid password **(auto)** | Dashboard; temporary password no longer works | |
| A8 | Create a technician; promote, deactivate and reactivate them **(auto)** | Each change takes effect; a deactivated person cannot sign in | |
| A9 | Administrator resets a user's password **(auto)** | User's open session ends; forced change at next sign-in | |
| A10 | On the server: `flask --app app reset-password <user>` **(auto)** | Works only at a terminal; audit log shows `cli:<name>` | |
| A11 | Six wrong sign-ins within a minute **(auto)** | The 6th is refused ("Too Many Requests", HTTP 429), even with the right password; works again after a minute | |
| A12 | Sign in on two browsers; log out in one **(auto)** | The other is signed out too | |
| A13 | Audit log page | Shows the events from A2–A12 | |

## B. Tickets

| # | Step | Expected | Result |
|---|---|---|---|
| B1 | As the user, create a ticket of each priority **(auto)** | Each appears on the dashboard with "Within SLA" | |
| B2 | As a second user, try the first user's ticket URL **(auto)** | "Not found" | |
| B3 | As the administrator, assign a ticket to the technician **(auto)** | Assignee shown; history entry | |
| B4 | As the technician: In Progress, comment, Resolved **(auto)** | History shows each step; SLA "Met" after the comment | |
| B5 | As the user, comment on the resolved ticket **(auto)** | Comment shown to the technician | |
| B6 | As the administrator, close the ticket **(auto)** | Status Closed; history complete | |
| B7 | Search and filter by status, priority and SLA | Correct tickets; pagination keeps the filters | |
| B8 | Export CSV with a filter **(auto)** | Opens in a spreadsheet; only the filtered tickets; no formulas run | |
| B9 | Wait past a Critical ticket's 1-hour target without a staff comment | Shows "Overdue" on the dashboard and the ticket page | |

## C. Operations (server administrator)

| # | Step | Expected | Result |
|---|---|---|---|
| C1 | `curl -fsS https://<host>/healthz` | `ok` | |
| C2 | `/readyz` from outside the server | 404 | |
| C3 | `flask --app app ops-check` | All OK (or only expected warnings, written down) | |
| C4 | `flask --app app notifications-status` after B1–B6 | Messages simulated, none failed | |
| C5 | `backup-create`, `backup-verify`, `backup-transfer` | All succeed; the copy exists off-server | |
| C6 | `backup-drill` | Passes | |
| C7 | Restore the newest backup into a new file and open it read-only | Same ticket count as live | |
| C8 | Stop Redis for one minute | Sign-in refused with "temporarily unavailable"; signed-in users unaffected; recovers on restart | |
| C9 | Restart `helpdesk-web` | Users stay signed in; `/readyz` OK | |
| C10 | Reboot the server | All services return by themselves; C1–C3 pass | |
| C11 | Retrieve the backup key from its offline copy and verify a backup with it | Verifies | |

## Sign-off

| Role | Name | Decision (accept / accept with conditions / reject) | Date |
|---|---|---|---|
| Pilot administrator | | | |
| Sponsor | | | |
