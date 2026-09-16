#!/usr/bin/env python3
"""
StayGrid Branch Monitor (GitHub Actions edition)
--------------------------------------------------
Checks each branch's front-desk login page and emails an alert when a
branch goes down, sends periodic reminders while it stays down, and
emails again when it recovers.

Meant to be run by the GitHub Actions workflow in
.github/workflows/monitor.yml on a schedule. State is written to
state.json at the repo root, which that workflow commits back to the
repo after each run - that committed file is what the dashboard reads.

Graph API credentials are read from environment variables (set as
GitHub Actions secrets) rather than from config.json, so nothing
secret ever gets committed to the repo:

    GRAPH_TENANT_ID
    GRAPH_CLIENT_ID
    GRAPH_CLIENT_SECRET

Usage:
    python monitor.py                # run one monitoring pass
    python monitor.py --test-email   # send a test email and exit
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = REPO_ROOT / "state.json"          # committed back by the workflow
LOG_PATH = SCRIPT_DIR / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("staygrid-monitor")


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Site check
# --------------------------------------------------------------------------

def check_url(url, timeout, retries, retry_delay, expect_text=None):
    """Try the URL up to (retries + 1) times. Return (is_up, detail)."""
    last_error = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
            elif expect_text and expect_text.lower() not in resp.text.lower():
                last_error = f"HTTP {resp.status_code} but page did not contain '{expect_text}'"
            else:
                return True, f"HTTP {resp.status_code}"
        except requests.exceptions.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"

        if attempt < retries + 1:
            log.info("  attempt %s/%s failed for %s (%s) - retrying in %ss",
                      attempt, retries + 1, url, last_error, retry_delay)
            time.sleep(retry_delay)

    return False, last_error or "unknown error"


# --------------------------------------------------------------------------
# Email sending
# --------------------------------------------------------------------------

def send_via_graph(cfg, subject, body):
    """Send mail through Microsoft Graph using an app registration
    (client credentials flow). Requires Mail.Send *application*
    permission with admin consent, granted to the app in Azure AD."""
    tenant_id = os.environ.get("GRAPH_TENANT_ID") or cfg["email"]["graph"].get("tenant_id")
    client_id = os.environ.get("GRAPH_CLIENT_ID") or cfg["email"]["graph"].get("client_id")
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET") or cfg["email"]["graph"].get("client_secret")

    if not all([tenant_id, client_id, client_secret]):
        raise RuntimeError(
            "Missing Graph credentials. Set GRAPH_TENANT_ID, GRAPH_CLIENT_ID, "
            "GRAPH_CLIENT_SECRET as environment variables (GitHub Actions secrets)."
        )

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    token_resp = requests.post(
        token_url,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]

    sender = cfg["email"]["sender"]
    recipients = cfg["email"]["recipients"]
    send_url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        },
        "saveToSentItems": "false",
    }
    resp = requests.post(
        send_url,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=message,
        timeout=15,
    )
    resp.raise_for_status()


def send_via_smtp(cfg, subject, body):
    """Fallback: plain SMTP with an app password. Note: Microsoft has been
    disabling basic-auth SMTP tenant-wide during 2026 - only use this if
    your tenant admin has confirmed SMTP AUTH is still enabled."""
    s = cfg["email"]["smtp"]
    password = os.environ.get("SMTP_PASSWORD", "")
    if not password:
        raise RuntimeError("Environment variable SMTP_PASSWORD is not set")
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg["email"]["sender"]
    msg["To"] = ", ".join(cfg["email"]["recipients"])
    with smtplib.SMTP(s["server"], s["port"], timeout=15) as server:
        server.starttls()
        server.login(s["username"], password)
        server.sendmail(cfg["email"]["sender"], cfg["email"]["recipients"], msg.as_string())


def send_alert(cfg, subject, body):
    method = cfg["email"].get("method", "graph")
    try:
        if method == "graph":
            send_via_graph(cfg, subject, body)
        else:
            send_via_smtp(cfg, subject, body)
        log.info("Alert email sent: %s", subject)
    except Exception as e:
        log.error("FAILED to send alert email (%s): %s", subject, e)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-email", action="store_true",
                         help="Send one test email using config.json settings, then exit.")
    args = parser.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log.error("config.json not found next to monitor.py")
        sys.exit(1)

    if args.test_email:
        log.info("Sending test email...")
        send_alert(cfg, "[TEST] StayGrid monitor test email",
                   f"This is a test email from the StayGrid branch monitor.\nSent at: {now_iso()}")
        return

    state = load_json(STATE_PATH, {"sites": {}, "generated_at": None})
    sites_state = state.get("sites", {})

    timeout = cfg.get("timeout_seconds", 10)
    retries = cfg.get("retries", 2)
    retry_delay = cfg.get("retry_delay_seconds", 15)
    repeat_minutes = cfg.get("alert_repeat_minutes", 60)
    expect_text = cfg.get("expect_text")  # optional content check, e.g. "password"

    for site in cfg["urls"]:
        name, url = site["name"], site["url"]
        log.info("Checking %s (%s)", name, url)
        is_up, detail = check_url(url, timeout, retries, retry_delay, expect_text)

        prev = sites_state.get(url, {"status": "up", "down_since": None, "last_alert": None})
        prev_status = prev.get("status", "up")

        if is_up:
            if prev_status == "down":
                down_since = prev.get("down_since")
                duration = "unknown"
                if down_since:
                    try:
                        delta = datetime.now(timezone.utc) - datetime.fromisoformat(down_since)
                        duration = str(delta).split(".")[0]
                    except Exception:
                        pass
                send_alert(
                    cfg,
                    f"[RECOVERED] {name} is back up",
                    f"{name}\n{url}\n\nStatus: back online ({detail})\n"
                    f"Was down for: {duration}\nRecovered at: {now_iso()}",
                )
            sites_state[url] = {"status": "up", "detail": detail, "down_since": None,
                                 "last_alert": None, "name": name, "url": url,
                                 "last_checked": now_iso()}
        else:
            if prev_status == "up":
                log.warning("%s is DOWN: %s", name, detail)
                sites_state[url] = {"status": "down", "detail": detail, "down_since": now_iso(),
                                     "last_alert": now_iso(), "name": name, "url": url,
                                     "last_checked": now_iso()}
                send_alert(
                    cfg,
                    f"[DOWN] {name} is unreachable",
                    f"{name}\n{url}\n\nError: {detail}\nDetected at: {now_iso()}",
                )
            else:
                last_alert = prev.get("last_alert")
                should_repeat = True
                if last_alert:
                    try:
                        elapsed = (datetime.now(timezone.utc) -
                                   datetime.fromisoformat(last_alert)).total_seconds() / 60
                        should_repeat = elapsed >= repeat_minutes
                    except Exception:
                        should_repeat = True
                prev["detail"] = detail
                prev["last_checked"] = now_iso()
                prev["url"] = url
                prev["name"] = name
                if should_repeat:
                    log.warning("%s still DOWN: %s (repeat reminder)", name, detail)
                    prev["last_alert"] = now_iso()
                    send_alert(
                        cfg,
                        f"[STILL DOWN] {name} is unreachable",
                        f"{name}\n{url}\n\nError: {detail}\n"
                        f"Down since: {prev.get('down_since')}\nChecked at: {now_iso()}",
                    )
                sites_state[url] = prev

    state["sites"] = sites_state
    state["generated_at"] = now_iso()
    save_json(STATE_PATH, state)
    log.info("state.json updated")


if __name__ == "__main__":
    main()
