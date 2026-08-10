#!/usr/bin/env python3
"""Sync projects[] in data.json from the Google Sheet's `Cards` tab.

Reads the public columns (Project Name, Project Description, Category, Year,
Deliverable Links, Completion x/100) and rewrites only the projects key of
data.json. Everything else in the file (highlights, stats, about_items,
learning) is left exactly as it is.

`Project Work Doc` is never read, and only the tab named `Cards` is ever
opened — any other tab in the same spreadsheet stays private by not being
touched.

Completion is the whole published/in-progress distinction: 100 (or blank)
means finished and renders no indicator, anything less renders one.

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


def parse_completion(cell, ctx):
    """0-100, or None when blank/unparseable. None renders the same as 100 —
    missing data shouldn't read as unfinished work."""
    s = str(cell or "").strip().rstrip("%").strip()
    if not s:
        return None
    try:
        value = int(round(float(s)))
    except ValueError:
        warn(f"[{ctx}] completion {cell!r} is not a number — treating as blank")
        return None
    if not 0 <= value <= 100:
        warn(f"[{ctx}] completion {value} out of range — clamping to 0-100")
        value = max(0, min(100, value))
    return value


def build_projects(worksheet):
    rows = worksheet.get_all_values()
    if not rows:
        sys.exit(f"The `{TAB}` tab is completely empty (not even headers)")

    header = [h.strip().lower() for h in rows[0]]

    def col(*names, contains=None):
        for n in names:
            if n in header:
                return header.index(n)
        if contains:
            for i, h in enumerate(header):
                if contains in h:
                    return i
        return None

    idx = {
        "title": col("project name", "project", "title"),
        "desc": col("project description", "description", "desc", contains="description"),
        "category": col("category"),
        "year": col("year"),
        "links": col("deliverable links", "links", contains="link"),
        "completion": col("completion x/100", "completion", contains="completion"),
    }
    # Only the title is structurally required — a row needs something to show.
    # Any other missing column just means that field is blank everywhere.
    if idx["title"] is None:
        sys.exit(
            f"The `{TAB}` tab has no Project Name column. Expected headers: "
            f"Project Name | Project Description | Category | Year | "
            f"Deliverable Links | Project Work Doc | Completion x/100. Found: {rows[0]}"
        )
    for key in ("desc", "category", "year", "links", "completion"):
        if idx[key] is None:
            warn(f"no column found for {key} — treating it as blank for every row")

    def cell(row, key):
        i = idx[key]
        if i is None or i >= len(row):
            return ""
        return row[i].strip()

    projects = []
    for row in rows[1:]:
        title = cell(row, "title")
        if not title:  # blank spacer row — nothing to show
            continue
        # Blank category/year are expected input, not an error; only a
        # non-blank value that isn't one of the site's 8 is worth flagging.
        category = cell(row, "category")
        if category and category not in VALID_CATEGORIES:
            warn(f"[{title}] category {category!r} is not one of the site's 8 categories")
        projects.append(
            {
                "title": title,
                "desc": cell(row, "desc"),
                "category": category,
                "year": cell(row, "year"),
                "completion": parse_completion(cell(row, "completion"), title),
                "links": parse_links(cell(row, "links"), title),
            }
        )
    return projects


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

    projects = build_projects(worksheet)
    if not projects:
        # An empty Cards tab is far more likely a broken read than a real intent
        # to clear the site. To genuinely empty it, edit data.json by hand.
        sys.exit(f"The `{TAB}` tab has no project rows; refusing to overwrite data.json")

    original = DATA_JSON.read_text(encoding="utf-8")
    data = json.loads(original)
    data.pop("in_progress", None)  # superseded by the single projects list
    data["projects"] = projects
    updated = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    if updated == original:
        print("data.json already up to date — nothing to write")
        return

    DATA_JSON.write_text(updated, encoding="utf-8")
    print(f"data.json updated: {len(projects)} projects")


if __name__ == "__main__":
    main()
