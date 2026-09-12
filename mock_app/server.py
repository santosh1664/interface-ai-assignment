"""
Mock Legacy Bank Back-Office System
-------------------------------------
Simulates the kind of hostile surface described in the assignment:
- Table-based layouts, no semantic HTML, no test-IDs
- Nested framesets / iframes
- Session timeouts
- Validation errors on bad input
- "Record not found" business outcomes
- Confirmation dialogs before irreversible actions

Member data is in-memory (no real DB). Credentials are fake.

Run:  python -m mock_app.server  (default port 5001)
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime
from functools import wraps
from typing import Dict, Optional

from flask import (Flask, jsonify, redirect, render_template,
                   request, session, url_for)

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ---------------------------------------------------------------------------
# In-memory "database"
# ---------------------------------------------------------------------------

MEMBERS: Dict[str, Dict] = {
    "10001": {
        "id": "10001", "name": "Alice Harrington", "status": "Active",
        "accounts": {
            "SAV-10001": {"type": "Savings",  "balance": 12_450.00, "status": "Open"},
            "CHK-10001": {"type": "Checking", "balance":  3_210.75, "status": "Open"},
        }
    },
    "10002": {
        "id": "10002", "name": "Bob Tran", "status": "Active",
        "accounts": {
            "SAV-10002": {"type": "Savings",  "balance":   875.22, "status": "Open"},
        }
    },
    "10003": {
        "id": "10003", "name": "Carol McNamara", "status": "Suspended",
        "accounts": {
            "CHK-10003": {"type": "Checking", "balance":    0.00, "status": "Frozen"},
        }
    },
}

VALID_CREDENTIALS = {"admin": "password123"}

# ---------------------------------------------------------------------------
# Session / auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        # Simulate session timeout after 30 min idle (skipped for demo; just check flag)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if VALID_CREDENTIALS.get(username) == password:
            session["logged_in"] = True
            session["user"] = username
            session["login_time"] = time.time()
            return redirect(url_for("dashboard"))
        error = "Invalid credentials. Please try again."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=session.get("user"))


@app.route("/members", methods=["GET"])
@login_required
def member_search():
    query = request.args.get("q", "").strip()
    results = []
    error = None

    if query:
        if not query.isdigit() and len(query) < 2:
            error = "Search term must be a member ID or at least 2 characters."
        else:
            for mid, m in MEMBERS.items():
                if query.isdigit() and mid == query:
                    results.append(m)
                elif not query.isdigit() and query.lower() in m["name"].lower():
                    results.append(m)

    return render_template(
        "member_search.html",
        query=query,
        results=results,
        error=error
    )


@app.route("/members/<member_id>")
@login_required
def member_detail(member_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("member_detail.html", member=None, not_found=True,
                               member_id=member_id)
    return render_template("member_detail.html", member=member, not_found=False)


@app.route("/members/<member_id>/account/<account_id>/action",
           methods=["GET", "POST"])
@login_required
def account_action(member_id: str, account_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        return redirect(url_for("member_search"))
    account = member["accounts"].get(account_id)
    if not account:
        return redirect(url_for("member_detail", member_id=member_id))

    confirmation = None
    error = None
    success = None

    if request.method == "POST":
        action_type = request.form.get("action_type", "")
        amount_str  = request.form.get("amount", "").strip()
        confirmed   = request.form.get("confirmed", "")

        if action_type == "open_sub_account":
            if confirmed == "yes":
                new_id = f"SUB-{member_id}-{len(member['accounts'])+1:02d}"
                member["accounts"][new_id] = {
                    "type": "Sub-Savings",
                    "balance": 0.00,
                    "status": "Open",
                    "opened_at": datetime.utcnow().isoformat(),
                }
                success = f"Sub-account {new_id} opened successfully."
            else:
                confirmation = {
                    "message": "You are about to open a new sub-savings account. "
                               "This action will be recorded. Continue?",
                    "action_type": "open_sub_account",
                }
        elif action_type == "deposit":
            try:
                amount = float(amount_str)
                if amount <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                error = "Invalid amount. Enter a positive number."
            else:
                if confirmed == "yes":
                    account["balance"] += amount
                    success = f"Deposited ${amount:,.2f} into {account_id}."
                else:
                    confirmation = {
                        "message": f"Deposit ${amount:,.2f} into {account_id}?",
                        "action_type": "deposit",
                        "amount": amount_str,
                    }

    return render_template(
        "account_action.html",
        member=member,
        account_id=account_id,
        account=account,
        confirmation=confirmation,
        error=error,
        success=success,
    )


# ---------------------------------------------------------------------------
# API endpoint for operator handoff (HITL)
# ---------------------------------------------------------------------------

@app.route("/api/session-url")
@login_required
def session_url():
    """Returns the current page URL the automation was on — used by operator console."""
    return jsonify({"url": request.referrer or url_for("dashboard")})


if __name__ == "__main__":
    app.run(port=5001, debug=False)
