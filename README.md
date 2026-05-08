# CS Bug Zendesk Weekly Report

Automatically sends a weekly email report tracking Zendesk ticket changes on Customer Support Filed bugs in Jira.

## What it does
- Runs every Monday at 9am Phoenix time
- Fetches all Jira bugs labeled `customer-support-filed`
- Extracts Zendesk ticket links from issue descriptions
- Compares against last week to find what changed
- Emails an HTML report showing new tickets linked/unlinked, new bugs, and status changes

## Files
- `cs_bug_zendesk_report.py` — the main Python script
- `.github/workflows/weekly-report.yml` — the GitHub Actions schedule

## Running manually
Go to the **Actions tab** → click **CS Bug Zendesk Weekly Report** → click **Run workflow**

## Secrets required
| Secret | Description |
|---|---|
| `JIRA_EMAIL` | Your Scribd email |
| `JIRA_API_TOKEN` | Jira API token from id.atlassian.com |
| `SMTP_USER` | Gmail address to send from |
| `SMTP_PASSWORD` | Gmail App Password |
| `REPORT_TO` | Recipient email(s), comma-separated |
