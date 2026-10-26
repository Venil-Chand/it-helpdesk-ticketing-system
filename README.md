
# IT Helpdesk Ticketing System

A Python-based web application for managing IT support requests.

## Technologies
- Python
- Flask
- SQLite
- HTML and Jinja2
- Linux (Ubuntu WSL2)

## Features Completed
- Create support tickets using a web form
- Store tickets in SQLite
- Display tickets on a dashboard
- Update ticket status using Python

## Features in Progress
- Update ticket status from the dashboard
- Assign tickets to technicians
- Add comments
- User authentication
- Ticket filtering
- Deployment to a Linux server

## Project Status
Work in progress — started October 2026.

## How to Run Locally

Create and activate a virtual environment, then install dependencies:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Initialize the database:

    python database.py

Start the application:

    python -m flask --app app run --debug

Visit http://127.0.0.1:5000

For local development only. Not ready for public deployment.
