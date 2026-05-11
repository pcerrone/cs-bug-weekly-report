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
    JIRA_API_TOKEN=your-jira-api-token
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=your-email@scribd.com
    SMTP_PASSWORD=your-app-password
    REPORT_TO=recipient@scribd.com
    SNAPSHOT_FILE=./zendesk_snapshot.json
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

JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "https://scribdjira.atlassian.net")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", 587))
SMTP_USER      = os.getenv("SMTP_USER")
SMTP_PASS      = os.getenv("SMTP_PASSWORD")
REPORT_TO      = [e.strip() for e in os.getenv("REPORT_TO", "").split(",") if e.strip()]
SNAPSHOT_FILE  = os.getenv("SNAPSHOT_FILE", "./zendesk_snapshot.json")

JQL = 'labels = "customer-support-filed" AND issuetype = Bug ORDER BY created DESC'
ZD_RE = re.compile(r'https?://[a-zA-Z0-9-]+\.zendesk\.com/(?:agent/)?tickets/(\d+)', re.IGNORECASE)

WEEKLY_ALERT_THRESHOLD = 10   # flag if 10+ tickets added since last run
ALERT_THRESHOLD        = 100  # [ALERT] label
BREACH_THRESHOLD       = 200  # [BREACH] label

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
        total = data.get("total", data.get("totalCount", 0))
        log.info(f"  Fetched {len(issues)}/{total} issues...")
        if start + page_size >= total:
            break
        start += page_size
    return issues


def extract_text(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    parts = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    for child in node.get("content", []):
        parts.append(extract_text(child))
    return " ".join(parts)


def get_zendesk_tickets(issue):
    raw = extract_text(issue["fields"].get("description"))
    return set(ZD_RE.findall(raw))


def build_snapshot(issues):
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
            "total_zd_ever": len(zd_ids),
        }
    return snapshot


def enrich_snapshot(new_snapshot, old_snapshot):
    """Carry forward running all-time ZD ticket count — never goes down."""
    for key, data in new_snapshot.items():
        old_ever = old_snapshot.get(key, {}).get("total_zd_ever", 0)
        data["total_zd_ever"] = max(old_ever, len(data["zendesk_ids"]))


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

def is_done(status):
    return status.lower() in ("done", "closed", "resolved")


def diff_snapshots(old, new):
    old_keys = set(old)
    new_keys = set(new)

    # New bugs filed this week (skip if already Done)
    new_issues = {k: new[k] for k in new_keys - old_keys if not is_done(new[k]["status"])}

    # Resolved = transitioned to Done this week
    resolved_issues = {
        k: new[k] for k in old_keys & new_keys
        if is_done(new[k]["status"]) and not is_done(old[k]["status"])
    }

    # Disappeared without being Done (unexpected — worth flagging)
    removed_issues = {k: old[k] for k in old_keys - new_keys if not is_done(old[k]["status"])}

    ticket_changes = []
    status_changes = []
    weekly_alerts  = []

    for key in old_keys & new_keys:
        o, n = old[key], new[key]

        old_zd = set(o["zendesk_ids"])
        new_zd = set(n["zendesk_ids"])
        added   = new_zd - old_zd
        removed = old_zd - new_zd

        if added or removed:
            ticket_changes.append({
                "key": key, "summary": n["summary"], "url": n["url"],
                "status": n["status"], "added": sorted(added),
                "removed": sorted(removed), "total_zd_ever": n["total_zd_ever"],
            })

        # Weekly high-volume alert
        if len(added) >= WEEKLY_ALERT_THRESHOLD:
            weekly_alerts.append({
                "key": key, "summary": n["summary"], "url": n["url"],
                "status": n["status"], "added_count": len(added),
                "total_zd_ever": n["total_zd_ever"],
            })

        # Status changes — exclude transitions TO Done (those go in resolved table)
        if o["status"] != n["status"] and not is_done(n["status"]):
            status_changes.append({
                "key": key, "summary": n["summary"], "url": n["url"],
                "old_status": o["status"], "new_status": n["status"],
                "total_zd_ever": n["total_zd_ever"],
            })

    ticket_changes.sort(key=lambda x: -x["total_zd_ever"])
    status_changes.sort(key=lambda x: x["key"])
    weekly_alerts.sort(key=lambda x: -x["added_count"])

    return new_issues, resolved_issues, removed_issues, ticket_changes, status_changes, weekly_alerts


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(snapshot, old_snapshot):
    total_zd     = sum(len(v["zendesk_ids"]) for v in snapshot.values())
    old_total_zd = sum(len(v["zendesk_ids"]) for v in old_snapshot.values())
    by_status = defaultdict(int)
    for v in snapshot.values():
        by_status[v["status"]] += 1
    return {
        "total_bugs": len(snapshot),
        "total_zendesk_tickets": total_zd,
        "tickets_added_this_week": total_zd - old_total_zd,
        "bugs_with_no_tickets": sum(1 for v in snapshot.values() if not v["zendesk_ids"]),
        "by_status": dict(sorted(by_status.items())),
    }


# ── Email HTML builder ────────────────────────────────────────────────────────

def zd_link(tid):
    return f'<a href="https://scribd.zendesk.com/agent/tickets/{tid}">#{tid}</a>'


def alert_badge(total):
    if total >= BREACH_THRESHOLD:
        return '<span style="background:#DE350B;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:bold;margin-right:4px">[BREACH]</span>'
    if total >= ALERT_THRESHOLD:
        return '<span style="background:#FF8B00;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:bold;margin-right:4px">[ALERT]</span>'
    return ""


def row_bg(total):
    if total >= BREACH_THRESHOLD:
        return "background:#FFF0F0;"
    if total >= ALERT_THRESHOLD:
        return "background:#FFF8F0;"
    return ""


def status_badge(s):
    colors = {
        "Open": "#0052CC", "In Progress": "#FF8B00",
        "Done": "#00875A", "Closed": "#00875A",
        "On Hold": "#97A0AF", "Resolved": "#00875A",
    }
    c = colors.get(s, "#42526E")
    return f'<span style="background:{c};color:#fff;padding:2px 7px;border-radius:3px;font-size:12px">{s}</span>'


def section_hdr(title, count, color="#0052CC"):
    return f'<h3 style="color:{color};margin:24px 0 8px">{title} <span style="font-size:14px;font-weight:normal;color:#6B778C">({count})</span></h3>'


BASE_THEAD = """
<table width="100%" style="border-collapse:collapse;font-size:13px">
  <tr style="background:#F4F5F7;font-size:12px;color:#6B778C">
    <th style="padding:6px 4px;text-align:left">Key</th>
    <th style="padding:6px 4px;text-align:left">Summary</th>
    <th style="padding:6px 4px;text-align:left">Status</th>
    <th style="padding:6px 4px;text-align:left">ZD Tickets (running total)</th>
  </tr>"""


def simple_row(key, data):
    al = alert_badge(data["total_zd_ever"])
    bg = row_bg(data["total_zd_ever"])
    zd = " ".join(zd_link(t) for t in data.get("zendesk_ids", []))
    zd_str = f'{zd} <b>({data["total_zd_ever"]})</b>' if zd else f'<span style="color:#97A0AF">none</span> (0)'
    summary = data["summary"][:80] + ("..." if len(data["summary"]) > 80 else "")
    return f"""
    <tr style="border-bottom:1px solid #F4F5F7;{bg}">
      <td style="padding:8px 4px"><a href="{data['url']}" style="color:#0052CC;font-weight:600">{key}</a></td>
      <td style="padding:8px 4px;font-size:13px">{al}{summary}</td>
      <td style="padding:8px 4px">{status_badge(data.get('status',''))}</td>
      <td style="padding:8px 4px;font-size:12px">{zd_str}</td>
    </tr>"""


def build_html_report(new_issues, resolved_issues, removed_issues,
                      ticket_changes, status_changes, weekly_alerts,
                      stats, run_date):
    s = []

    # Header
    s.append(f"""
    <div style="background:#0052CC;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0">
      <h2 style="margin:0;font-size:20px">CS Bug Zendesk Ticket Weekly Report</h2>
      <p style="margin:4px 0 0;opacity:.8;font-size:13px">Week ending {run_date} &nbsp;
        <a href="{JIRA_BASE_URL}/jira/dashboards/19420" style="color:#fff">View Dashboard</a></p>
    </div>""")

    # Stats bar
    added = stats["tickets_added_this_week"]
    added_color = "#DE350B" if added >= WEEKLY_ALERT_THRESHOLD else ("#00875A" if added >= 0 else "#FF8B00")
    arrow = "+" if added > 0 else ""
    sp_pills = " &nbsp; ".join(f'<b>{v}</b> {status_badge(k)}' for k, v in stats["by_status"].items())
    s.append(f"""
    <div style="background:#F4F5F7;padding:14px 24px;border-bottom:1px solid #DFE1E6">
      <table width="100%"><tr>
        <td style="padding:4px 16px 4px 0"><b style="font-size:22px">{stats['total_bugs']}</b><br><span style="font-size:12px;color:#6B778C">Total Bugs</span></td>
        <td style="padding:4px 16px"><b style="font-size:22px">{stats['total_zendesk_tickets']}</b><br><span style="font-size:12px;color:#6B778C">ZD Tickets (total)</span></td>
        <td style="padding:4px 16px"><b style="font-size:22px;color:{added_color}">{arrow}{added}</b><br><span style="font-size:12px;color:#6B778C">Added This Week</span></td>
        <td style="padding:4px 16px"><b style="font-size:22px">{stats['bugs_with_no_tickets']}</b><br><span style="font-size:12px;color:#6B778C">Bugs With No ZD Ticket</span></td>
        <td style="font-size:13px">{sp_pills}</td>
      </tr></table>
    </div>""")

    body = '<div style="padding:16px 24px;font-family:Arial,sans-serif">'

    # High-volume alert banner
    if weekly_alerts:
        items = "".join(
            f'<li><a href="{a["url"]}" style="color:#fff">{a["key"]}</a> — '
            f'{a["summary"][:60]}... ({a["added_count"]} new this week, {a["total_zd_ever"]} total)</li>'
            for a in weekly_alerts
        )
        body += f"""
        <div style="background:#DE350B;color:#fff;padding:14px 16px;border-radius:6px;margin-bottom:20px">
          <b style="font-size:15px">ALERT — {len(weekly_alerts)} bug(s) received 10+ new Zendesk tickets this week</b>
          <ul style="margin:8px 0 0;padding-left:20px;font-size:13px">{items}</ul>
        </div>"""

    # Ticket changes
    body += section_hdr("Zendesk Ticket Changes This Week", len(ticket_changes), "#FF8B00")
    if ticket_changes:
        body += """
        <table width="100%" style="border-collapse:collapse;font-size:13px">
          <tr style="background:#F4F5F7;font-size:12px;color:#6B778C">
            <th style="padding:6px 4px;text-align:left">Key</th>
            <th style="padding:6px 4px;text-align:left">Summary</th>
            <th style="padding:6px 4px;text-align:left">Status</th>
            <th style="padding:6px 4px;text-align:left">Changes</th>
            <th style="padding:6px 4px;text-align:left">Running Total</th>
          </tr>"""
        for ch in ticket_changes:
            added_str   = " ".join(f'<span style="color:#00875A">+{zd_link(t)}</span>' for t in ch["added"])
            removed_str = " ".join(f'<span style="color:#DE350B">-#{t}</span>' for t in ch["removed"])
            al = alert_badge(ch["total_zd_ever"])
            bg = row_bg(ch["total_zd_ever"])
            summary = ch["summary"][:70] + ("..." if len(ch["summary"]) > 70 else "")
            body += f"""
            <tr style="border-bottom:1px solid #F4F5F7;{bg}">
              <td style="padding:8px 4px"><a href="{ch['url']}" style="color:#0052CC;font-weight:600">{ch['key']}</a></td>
              <td style="padding:8px 4px;font-size:13px">{al}{summary}</td>
              <td style="padding:8px 4px">{status_badge(ch['status'])}</td>
              <td style="padding:8px 4px;font-size:12px">{added_str} {removed_str}</td>
              <td style="padding:8px 4px;font-size:14px;font-weight:bold">{ch['total_zd_ever']}</td>
            </tr>"""
        body += "</table>"
    else:
        body += '<p style="color:#6B778C;font-size:13px">No Zendesk ticket changes this week.</p>'

    # New bugs
    body += section_hdr("New Bugs This Week", len(new_issues), "#00875A")
    if new_issues:
        body += BASE_THEAD
        for key, data in sorted(new_issues.items()):
            body += simple_row(key, data)
        body += "</table>"
    else:
        body += '<p style="color:#6B778C;font-size:13px">No new bugs filed this week.</p>'

    # Status changes (Done excluded)
    body += section_hdr("Status Changes (excluding Done)", len(status_changes))
    if status_changes:
        body += """
        <table width="100%" style="border-collapse:collapse;font-size:13px">
          <tr style="background:#F4F5F7;font-size:12px;color:#6B778C">
            <th style="padding:6px 4px;text-align:left">Key</th>
            <th style="padding:6px 4px;text-align:left">Summary</th>
            <th style="padding:6px 4px;text-align:left">Before</th>
            <th style="padding:6px 4px;text-align:left">After</th>
            <th style="padding:6px 4px;text-align:left">ZD Total</th>
          </tr>"""
        for ch in status_changes:
            al = alert_badge(ch["total_zd_ever"])
            bg = row_bg(ch["total_zd_ever"])
            summary = ch["summary"][:70] + ("..." if len(ch["summary"]) > 70 else "")
            body += f"""
            <tr style="border-bottom:1px solid #F4F5F7;{bg}">
              <td style="padding:8px 4px"><a href="{ch['url']}" style="color:#0052CC;font-weight:600">{ch['key']}</a></td>
              <td style="padding:8px 4px;font-size:13px">{al}{summary}</td>
              <td style="padding:8px 4px">{status_badge(ch['old_status'])}</td>
              <td style="padding:8px 4px">{status_badge(ch['new_status'])}</td>
              <td style="padding:8px 4px;font-size:14px;font-weight:bold">{ch['total_zd_ever']}</td>
            </tr>"""
        body += "</table>"
    else:
        body += '<p style="color:#6B778C;font-size:13px">No status changes this week.</p>'

    # Resolved bugs (moved to Done)
    body += section_hdr("Resolved This Week (moved to Done)", len(resolved_issues), "#00875A")
    if resolved_issues:
        body += BASE_THEAD
        for key, data in sorted(resolved_issues.items()):
            body += simple_row(key, data)
        body += "</table>"
    else:
        body += '<p style="color:#6B778C;font-size:13px">No bugs resolved this week.</p>'

    # Disappeared without Done (unexpected)
    if removed_issues:
        body += section_hdr("Disappeared Without Being Resolved", len(removed_issues), "#97A0AF")
        body += BASE_THEAD
        for key, data in sorted(removed_issues.items()):
            body += simple_row(key, data)
        body += "</table>"

    body += "</div>"
    s.append(body)
    s.append(f'<div style="background:#F4F5F7;padding:10px 24px;border-radius:0 0 8px 8px;font-size:11px;color:#6B778C">Generated by cs_bug_zendesk_report.py &nbsp;·&nbsp; {run_date}</div>')

    return f'<div style="max-width:960px;margin:0 auto;border:1px solid #DFE1E6;border-radius:8px;font-family:Arial,sans-serif">{"".join(s)}</div>'


def send_email(subject, html_body):
    if not REPORT_TO:
        log.warning("REPORT_TO not set — printing to stdout.")
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
    log.info("Fetching issues from Jira...")
    issues = fetch_all_bugs()
    log.info(f"Found {len(issues)} issues total.")

    new_snapshot = build_snapshot(issues)
    old_snapshot = load_snapshot()
    enrich_snapshot(new_snapshot, old_snapshot)

    new_issues, resolved_issues, removed_issues, ticket_changes, status_changes, weekly_alerts = diff_snapshots(old_snapshot, new_snapshot)
    stats = compute_stats(new_snapshot, old_snapshot)

    log.info(
        f"Diff: {len(new_issues)} new, {len(resolved_issues)} resolved, "
        f"{len(removed_issues)} disappeared, {len(ticket_changes)} ZD changes, "
        f"{len(status_changes)} status changes, {len(weekly_alerts)} weekly alerts"
    )

    alert_flag = " ALERT" if weekly_alerts else ""
    subject = (
        f"[Weekly{alert_flag}] CS Bug ZD Report — "
        f"+{stats['tickets_added_this_week']} tickets this week, "
        f"{stats['total_zendesk_tickets']} total ({run_date})"
    )

    html = build_html_report(
        new_issues, resolved_issues, removed_issues,
        ticket_changes, status_changes, weekly_alerts,
        stats, run_date
    )

    send_email(subject, html)
    save_snapshot(new_snapshot)
    log.info("Done.")


if __name__ == "__main__":
    main()
