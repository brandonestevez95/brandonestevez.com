#!/usr/bin/env python3
"""Sync in_progress[] in data.json from the Google Sheet's `Cards` tab.

Reads four columns (Project, Year, Category, Links), parses each Links cell
into [{label, url}], and rewrites only the in_progress key of data.json.
Everything else in the file (projects, highlights, stats, about_items,
learning) is left exactly as it is.

Only the tab named `Cards` is ever opened — any other tab in the same
spreadsheet is never read.

Run by .github/workflows/sheet-sync.yml. Local use:

    export GCP_SA_KEY="$(cat service-account.json)"
    export SHEET_ID="<spreadsheet id from the sheet url>"
    python scripts/sync_sheet.py

Requires: pip install gspread google-auth
"""

import json
import os
import re
import sys
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = REPO_ROOT / "data.json"

TAB = "Cards"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

VALID_CATEGORIES = {
    "Science", "Education", "Politics", "History",
    "Agriculture", "Urban Planning", "Interdisciplinary", "Policy",
}

# "Label: https://..." — the label must be followed by something that still
# looks like a URL, so a bare "https://..." isn't split at its own scheme colon.
LABELLED = re.compile(r"^(?P<label>[^:]+):\s*(?P<url>\S.*)$")
SCHEMED = re.compile(r"^(https?://|www\.)", re.I)
BARE_DOMAIN = re.compile(r"^[\w-]+(\.[\w-]+)+(/|$)")


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def looks_like_url(s):
    return bool(SCHEMED.match(s) or BARE_DOMAIN.match(s))


def normalize_url(url):
    """A link pasted without a scheme would resolve relative to the site and
    404, so give it one."""
    return url if re.match(r"^https?://", url, re.I) else "https://" + url


def parse_links(cell, ctx):
    """One deliverable per line: 'Label: URL', or a bare URL labelled 'View'."""
    links = []
    for line in str(cell or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = LABELLED.match(line)
        if m and looks_like_url(m.group("url").strip()):
            label, url = m.group("label").strip(), m.group("url").strip()
        elif looks_like_url(line):
            label, url = "View", line
        else:
            warn(f"[{ctx}] no URL found in link line, skipping: {line!r}")
            continue
        links.append({"label": label, "url": normalize_url(url)})
    return links


def build_in_progress(worksheet):
    rows = worksheet.get_all_values()
    if not rows:
        sys.exit(f"The `{TAB}` tab is completely empty (not even headers)")

    header = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    idx = {
        "title": col("project", "title"),
        "year": col("year"),
        "category": col("category"),
        "links": col("links"),
    }
    missing = [k for k, v in idx.items() if v is None]
    if missing:
        sys.exit(
            f"The `{TAB}` tab is missing column(s) for: {', '.join(missing)}. "
            f"Expected headers: Project | Year | Category | Links. Found: {rows[0]}"
        )

    def cell(row, key):
        i = idx[key]
        return row[i].strip() if i < len(row) else ""

    items = []
    for row in rows[1:]:
        title = cell(row, "title")
        if not title:  # blank spacer row
            continue
        category = cell(row, "category")
        if category not in VALID_CATEGORIES:
            warn(f"[{title}] category {category!r} is not one of the site's 8 categories")
        items.append(
            {
                "title": title,
                "year": cell(row, "year"),
                "category": category,
                "links": parse_links(cell(row, "links"), title),
            }
        )
    return items


def main():
    sa_key = os.environ.get("GCP_SA_KEY")
    sheet_id = os.environ.get("SHEET_ID")
    if not sa_key or not sheet_id:
        sys.exit("GCP_SA_KEY and SHEET_ID environment variables are required")

    creds = Credentials.from_service_account_info(json.loads(sa_key), scopes=SCOPES)
    spreadsheet = gspread.authorize(creds).open_by_key(sheet_id)
    try:
        worksheet = spreadsheet.worksheet(TAB)
    except gspread.WorksheetNotFound:
        sys.exit(f"No tab named `{TAB}` in this spreadsheet — check the tab name")

    in_progress = build_in_progress(worksheet)
    if not in_progress:
        # An empty Cards tab is far more likely a broken read than a real intent
        # to clear the section. To genuinely empty it, edit data.json by hand.
        sys.exit(f"The `{TAB}` tab has no project rows; refusing to overwrite data.json")

    original = DATA_JSON.read_text(encoding="utf-8")
    data = json.loads(original)
    data["in_progress"] = in_progress
    updated = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    if updated == original:
        print("data.json already up to date — nothing to write")
        return

    DATA_JSON.write_text(updated, encoding="utf-8")
    print(f"data.json updated: {len(in_progress)} in_progress entries")


if __name__ == "__main__":
    main()
