#!/usr/bin/env python3
"""
CS Bug Zendesk Ticket Weekly Report
====================================
Fetches all customer-support-filed bugs from Jira, extracts Zendesk ticket
links from descriptions, compares against a persisted snapshot from the
previous week, and emails a diff report.

Setup:
    pip install requests python-dotenv

Environment variables (put in a .env file next to this script):
    JIRA_BASE_URL=https://scribdjira.atlassian.net
    JIRA_EMAIL=your-email@scribd.com
    JIRA_API_TOKEN=your-jira-api-token      # https://id.atlassian.com/manage-profile/security/api-tokens
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=your-email@scribd.com
    SMTP_PASSWORD=your-app-password
    REPORT_TO=recipient@scribd.com          # comma-separated for multiple
    SNAPSHOT_FILE=./zendesk_snapshot.json   # where state is persisted between runs

Schedule (cron example — every Monday at 9am):
    0 9 * * 1 /usr/bin/python3 /path/to/cs_bug_zendesk_report.py
"""

import os
import re
import json
import smtplib
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

JIRA_BASE_URL   = os.getenv("JIRA_BASE_URL", "https://scribdjira.atlassian.net")
JIRA_EMAIL      = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN  = os.getenv("JIRA_API_TOKEN")
SMTP_HOST       = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", 587))
SMTP_USER       = os.getenv("SMTP_USER")
SMTP_PASS       = os.getenv("SMTP_PASSWORD")
REPORT_TO       = [e.strip() for e in os.getenv("REPORT_TO", "").split(",") if e.strip()]
SNAPSHOT_FILE   = os.getenv("SNAPSHOT_FILE", "./zendesk_snapshot.json")

# JQL that mirrors your dashboard filter — adjust if your dashboard uses different criteria
JQL = 'labels = "customer-support-filed" AND issuetype = Bug ORDER BY created DESC'

# Zendesk URL pattern (matches both /tickets/NNN and /agent/tickets/NNN)
ZD_RE = re.compile(r'https?://[a-zA-Z0-9-]+\.zendesk\.com/(?:agent/)?tickets/(\d+)', re.IGNORECASE)

# ── Jira helpers ──────────────────────────────────────────────────────────────

def jira_get(path, params=None):
    url = f"{JIRA_BASE_URL}/rest/api/3{path}"
    resp = requests.get(
        url,
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_all_bugs():
    """Page through JQL results and return all matching issues."""
    issues = []
    start = 0
    page_size = 100
    fields = "summary,status,labels,description,issuelinks,project,created,updated"

    while True:
        data = jira_get("/search/jql", {
            "jql": JQL,
            "startAt": start,
            "maxResults": page_size,
            "fields": fields,
        })
        batch = data.get("issues", [])
        issues.extend(batch)
        log.info(f"  Fetched {len(issues)}/{data['total']} issues…")
        if start + page_size >= data["total"]:
            break
        start += page_size

    return issues


def extract_text(description_field):
    """Recursively extract plain text from Jira ADF (Atlassian Document Format) or plain strings."""
    if description_field is None:
        return ""
    if isinstance(description_field, str):
        return description_field
    # ADF node
    node_type = description_field.get("type", "")
    text_parts = []
    if node_type == "text":
        text_parts.append(description_field.get("text", ""))
    for child in description_field.get("content", []):
        text_parts.append(extract_text(child))
    return " ".join(text_parts)


def get_zendesk_tickets(issue):
    """Return a set of Zendesk ticket IDs mentioned in an issue's description."""
    raw = extract_text(issue["fields"].get("description"))
    return set(ZD_RE.findall(raw))


def build_snapshot(issues):
    """
    Returns a dict:  { issue_key: {"summary": ..., "status": ..., "zendesk_ids": [...], "url": ...} }
    """
    snapshot = {}
    for issue in issues:
        key = issue["key"]
        zd_ids = get_zendesk_tickets(issue)
        snapshot[key] = {
            "summary": issue["fields"]["summary"],
            "status": issue["fields"]["status"]["name"],
            "zendesk_ids": sorted(zd_ids),
            "url": f"{JIRA_BASE_URL}/browse/{key}",
            "project": issue["fields"]["project"]["key"],
        }
    return snapshot


# ── Snapshot persistence ──────────────────────────────────────────────────────

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}


def save_snapshot(snapshot):
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshot, f, indent=2)
    log.info(f"Snapshot saved to {SNAPSHOT_FILE}")


# ── Diff logic ────────────────────────────────────────────────────────────────

def diff_snapshots(old, new):
    """
    Returns:
      new_issues      – issues that appeared this week
      removed_issues  – issues that disappeared (closed/deleted)
      ticket_changes  – issues where the zendesk ticket set changed
                         each entry: {key, summary, url, added, removed}
      status_changes  – issues whose Jira status changed
    """
    old_keys = set(old)
    new_keys = set(new)

    new_issues      = {k: new[k] for k in new_keys - old_keys}
    removed_issues  = {k: old[k] for k in old_keys - new_keys}
    ticket_changes  = []
    status_changes  = []

    for key in old_keys & new_keys:
        o, n = old[key], new[key]

        old_zd = set(o["zendesk_ids"])
        new_zd = set(n["zendesk_ids"])
        added   = new_zd - old_zd
        removed = old_zd - new_zd
        if added or removed:
            ticket_changes.append({
                "key": key, "summary": n["summary"], "url": n["url"],
                "status": n["status"], "added": sorted(added), "removed": sorted(removed),
            })

        if o["status"] != n["status"]:
            status_changes.append({
                "key": key, "summary": n["summary"], "url": n["url"],
                "old_status": o["status"], "new_status": n["status"],
            })

    # Sort for stable output
    ticket_changes.sort(key=lambda x: x["key"])
    status_changes.sort(key=lambda x: x["key"])
    return new_issues, removed_issues, ticket_changes, status_changes


# ── Stats summary ─────────────────────────────────────────────────────────────

def compute_stats(snapshot):
    total_zd = sum(len(v["zendesk_ids"]) for v in snapshot.values())
    by_status = defaultdict(int)
    for v in snapshot.values():
        by_status[v["status"]] += 1
    by_project = defaultdict(int)
    for v in snapshot.values():
        by_project[v["project"]] += 1
    return {
        "total_bugs": len(snapshot),
        "total_zendesk_tickets": total_zd,
        "bugs_with_no_tickets": sum(1 for v in snapshot.values() if not v["zendesk_ids"]),
        "by_status": dict(sorted(by_status.items())),
        "by_project": dict(sorted(by_project.items())),
    }


# ── Email report ──────────────────────────────────────────────────────────────

def zendesk_link(ticket_id):
    return f'<a href="https://scribdjira.zendesk.com/agent/tickets/{ticket_id}">#{ticket_id}</a>'


def build_html_report(new_issues, removed_issues, ticket_changes, status_changes, stats, run_date):
    def status_badge(s):
        colors = {
            "Open": "#0052CC", "In Progress": "#FF8B00", "Done": "#00875A",
            "Closed": "#00875A", "On Hold": "#97A0AF", "Resolved": "#00875A",
        }
        c = colors.get(s, "#42526E")
        return f'<span style="background:{c};color:#fff;padding:2px 7px;border-radius:3px;font-size:12px">{s}</span>'

    sections = []

    # ── Header
    sections.append(f"""
    <div style="background:#0052CC;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0">
      <h2 style="margin:0;font-size:20px">📋 CS Bug Zendesk Ticket Weekly Report</h2>
      <p style="margin:4px 0 0;opacity:.8;font-size:13px">Week ending {run_date} &nbsp;·&nbsp; {JIRA_BASE_URL}/jira/dashboards/19420</p>
    </div>
    """)

    # ── Stats bar
    sp = stats["by_status"]
    status_pills = " &nbsp; ".join(f'<b>{v}</b> {status_badge(k)}' for k, v in sp.items())
    sections.append(f"""
    <div style="background:#F4F5F7;padding:14px 24px;border-bottom:1px solid #DFE1E6">
      <table width="100%"><tr>
        <td><b style="font-size:22px">{stats['total_bugs']}</b><br><span style="font-size:12px;color:#6B778C">Total Bugs</span></td>
        <td><b style="font-size:22px">{stats['total_zendesk_tickets']}</b><br><span style="font-size:12px;color:#6B778C">Zendesk Tickets Linked</span></td>
        <td><b style="font-size:22px">{stats['bugs_with_no_tickets']}</b><br><span style="font-size:12px;color:#6B778C">Bugs With No ZD Ticket</span></td>
        <td style="font-size:13px">{status_pills}</td>
      </tr></table>
    </div>
    """)

    def issue_row(key, data, extras=""):
        zd_links = " ".join(zendesk_link(t) for t in data.get("zendesk_ids", []))
        return f"""
        <tr style="border-bottom:1px solid #F4F5F7">
          <td style="padding:8px 4px"><a href="{data['url']}" style="color:#0052CC;font-weight:600">{key}</a></td>
          <td style="padding:8px 4px;font-size:13px">{data['summary'][:80]}{'…' if len(data['summary'])>80 else ''}</td>
          <td style="padding:8px 4px">{status_badge(data.get('status',''))}</td>
          <td style="padding:8px 4px;font-size:12px">{zd_links or '<span style="color:#97A0AF">none</span>'}</td>
          {extras}
        </tr>"""

    def section_header(title, count, color="#0052CC"):
        return f'<h3 style="color:{color};margin:24px 0 8px">{title} <span style="font-size:14px;font-weight:normal;color:#6B778C">({count})</span></h3>'

    table_head = """
    <table width="100%" style="border-collapse:collapse;font-size:13px">
      <tr style="background:#F4F5F7;font-size:12px;color:#6B778C">
        <th style="padding:6px 4px;text-align:left">Key</th>
        <th style="padding:6px 4px;text-align:left">Summary</th>
        <th style="padding:6px 4px;text-align:left">Status</th>
        <th style="padding:6px 4px;text-align:left">Zendesk Tickets</th>
      </tr>"""

    body = '<div style="padding:16px 24px;font-family:Arial,sans-serif">'

    # ── Ticket changes
    body += section_header("🔗 Zendesk Ticket Changes", len(ticket_changes), "#FF8B00")
    if ticket_changes:
        body += table_head
        for ch in ticket_changes:
            added_str   = " ".join(f'<span style="color:#00875A">+{zendesk_link(t)}</span>' for t in ch["added"])
            removed_str = " ".join(f'<span style="color:#DE350B">−#{t}</span>' for t in ch["removed"])
            change_td = f'<td style="padding:8px 4px;font-size:12px">{added_str} {removed_str}</td>'
            body += f"""
            <tr style="border-bottom:1px solid #F4F5F7">
              <td style="padding:8px 4px"><a href="{ch['url']}" style="color:#0052CC;font-weight:600">{ch['key']}</a></td>
              <td style="padding:8px 4px;font-size:13px">{ch['summary'][:80]}</td>
              <td style="padding:8px 4px">{status_badge(ch['status'])}</td>
              {change_td}
            </tr>"""
        body += "</table>"
    else:
        body += '<p style="color:#6B778C;font-size:13px">No Zendesk ticket changes this week.</p>'

    # ── New bugs
    body += section_header("🆕 New Bugs This Week", len(new_issues), "#00875A")
    if new_issues:
        body += table_head
        for key, data in sorted(new_issues.items()):
            body += issue_row(key, data)
        body += "</table>"
    else:
        body += '<p style="color:#6B778C;font-size:13px">No new bugs filed this week.</p>'

    # ── Status changes
    body += section_header("🔄 Status Changes", len(status_changes))
    if status_changes:
        body += f"""
        <table width="100%" style="border-collapse:collapse;font-size:13px">
          <tr style="background:#F4F5F7;font-size:12px;color:#6B778C">
            <th style="padding:6px 4px;text-align:left">Key</th>
            <th style="padding:6px 4px;text-align:left">Summary</th>
            <th style="padding:6px 4px;text-align:left">Before</th>
            <th style="padding:6px 4px;text-align:left">After</th>
          </tr>"""
        for ch in status_changes:
            body += f"""
            <tr style="border-bottom:1px solid #F4F5F7">
              <td style="padding:8px 4px"><a href="{ch['url']}" style="color:#0052CC;font-weight:600">{ch['key']}</a></td>
              <td style="padding:8px 4px;font-size:13px">{ch['summary'][:80]}</td>
              <td style="padding:8px 4px">{status_badge(ch['old_status'])}</td>
              <td style="padding:8px 4px">{status_badge(ch['new_status'])}</td>
            </tr>"""
        body += "</table>"
    else:
        body += '<p style="color:#6B778C;font-size:13px">No status changes this week.</p>'

    # ── Removed bugs
    if removed_issues:
        body += section_header("✅ Resolved/Removed Bugs", len(removed_issues), "#6B778C")
        body += table_head
        for key, data in sorted(removed_issues.items()):
            body += issue_row(key, data)
        body += "</table>"

    body += "</div>"
    sections.append(body)

    footer = f'<div style="background:#F4F5F7;padding:10px 24px;border-radius:0 0 8px 8px;font-size:11px;color:#6B778C">Generated by cs_bug_zendesk_report.py · {run_date}</div>'
    sections.append(footer)

    return f'<div style="max-width:900px;margin:0 auto;border:1px solid #DFE1E6;border-radius:8px;font-family:Arial,sans-serif">{"".join(sections)}</div>'


def send_email(subject, html_body):
    if not REPORT_TO:
        log.warning("REPORT_TO is not set — printing report to stdout instead.")
        print(html_body)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = ", ".join(REPORT_TO)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, REPORT_TO, msg.as_string())
    log.info(f"Report sent to {REPORT_TO}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    run_date = datetime.now(timezone.utc).strftime("%B %d, %Y")
    log.info("Fetching issues from Jira…")
    issues = fetch_all_bugs()
    log.info(f"Found {len(issues)} issues total.")

    new_snapshot = build_snapshot(issues)
    old_snapshot = load_snapshot()

    new_issues, removed_issues, ticket_changes, status_changes = diff_snapshots(old_snapshot, new_snapshot)
    stats = compute_stats(new_snapshot)

    log.info(f"Diff: {len(new_issues)} new, {len(removed_issues)} removed, "
             f"{len(ticket_changes)} ZD changes, {len(status_changes)} status changes")

    subject = (
        f"[Weekly] CS Bug ZD Ticket Report — "
        f"{len(ticket_changes)} ticket changes, {len(new_issues)} new bugs ({run_date})"
    )
    html = build_html_report(new_issues, removed_issues, ticket_changes, status_changes, stats, run_date)

    send_email(subject, html)
    save_snapshot(new_snapshot)
    log.info("Done.")


if __name__ == "__main__":
    main()
