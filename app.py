"""
IT Helpdesk Ticketing System
Run locally:
    pip install -r requirements.txt
    python app.py
Then open:  http://127.0.0.1:5000

Default logins:
  admin  / admin123   (full access)
  venil  / venil123   (technician)
  staff1 / staff123   (regular user)
"""

import os
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash
)
from werkzeug.security import check_password_hash
from dotenv import load_dotenv
import database as db

load_dotenv()

app = Flask(__name__)

_secret = os.environ.get('SECRET_KEY')
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. "
        "Copy .env.example to .env and add a real key: "
        "python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )
app.secret_key = _secret

PRIORITIES = ['Low', 'Medium', 'High', 'Critical']
STATUSES   = ['Open', 'In Progress', 'Resolved', 'Closed']


# ── Auth helpers ──────────────────────────────────────────────────────────────

def logged_in():
    return 'username' in session

def require_login():
    if not logged_in():
        flash('Please log in first.', 'warning')
        return redirect(url_for('login'))

def is_tech_or_admin():
    return session.get('role') in ('technician', 'admin')


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if logged_in():
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = db.get_user(username)

        if user and check_password_hash(user['password_hash'], password):
            session['username'] = user['username']
            session['role']     = user['role']
            flash(f"Welcome back, {user['username']}!", 'success')
            return redirect(url_for('index'))

        flash('Invalid username or password.', 'danger')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/')
def index():
    redir = require_login()
    if redir: return redir

    status_filter   = request.args.get('status', 'All')
    priority_filter = request.args.get('priority', 'All')

    tickets = db.get_all_tickets(status_filter, priority_filter)
    stats   = db.get_stats()

    return render_template(
        'index.html',
        tickets         = tickets,
        stats           = stats,
        status_filter   = status_filter,
        priority_filter = priority_filter,
        statuses        = ['All'] + STATUSES,
        priorities      = ['All'] + PRIORITIES,
    )


@app.route('/tickets/new', methods=['GET', 'POST'])
def new_ticket():
    redir = require_login()
    if redir: return redir

    if request.method == 'POST':
        title       = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        priority    = request.form.get('priority', 'Medium')

        if not title or not description:
            flash('Title and description are required.', 'danger')
        elif priority not in PRIORITIES:
            flash('Invalid priority.', 'danger')
        else:
            ticket_id = db.create_ticket(
                title, description, priority, session['username']
            )
            flash(f'Ticket #{ticket_id} created successfully!', 'success')
            return redirect(url_for('ticket_detail', ticket_id=ticket_id))

    return render_template('new_ticket.html', priorities=PRIORITIES)


@app.route('/tickets/<int:ticket_id>')
def ticket_detail(ticket_id):
    redir = require_login()
    if redir: return redir

    ticket = db.get_ticket(ticket_id)
    if not ticket:
        flash(f'Ticket #{ticket_id} not found.', 'danger')
        return redirect(url_for('index'))

    comments  = db.get_comments(ticket_id)
    all_users = db.get_all_usernames()

    return render_template(
        'ticket_detail.html',
        ticket    = ticket,
        comments  = comments,
        statuses  = STATUSES,
        all_users = all_users,
    )


@app.route('/tickets/<int:ticket_id>/update', methods=['POST'])
def update_ticket(ticket_id):
    redir = require_login()
    if redir: return redir

    if not is_tech_or_admin():
        flash('Only technicians and admins can update ticket status.', 'danger')
        return redirect(url_for('ticket_detail', ticket_id=ticket_id))

    status      = request.form.get('status')
    assigned_to = request.form.get('assigned_to', '').strip() or None

    if status not in STATUSES:
        flash('Invalid status.', 'danger')
    else:
        db.update_ticket(ticket_id, status, assigned_to)
        flash('Ticket updated.', 'success')

    return redirect(url_for('ticket_detail', ticket_id=ticket_id))


@app.route('/tickets/<int:ticket_id>/comment', methods=['POST'])
def add_comment(ticket_id):
    redir = require_login()
    if redir: return redir

    content = request.form.get('content', '').strip()
    if not content:
        flash('Comment cannot be empty.', 'danger')
    else:
        db.add_comment(ticket_id, session['username'], content)
        flash('Note added.', 'success')

    return redirect(url_for('ticket_detail', ticket_id=ticket_id))


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    db.init_db()
    print("\n  IT Helpdesk running at http://127.0.0.1:5000")
    print("  Logins:  admin/admin123   venil/venil123   staff1/staff123\n")
    app.run(debug=True)
