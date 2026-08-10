#!/usr/bin/env python3
"""Sync in_progress[] in data.json from the Google Sheet.

Reads three tabs (Projects, Steps, Log), assembles the in_progress[] array,
downloads any Google Drive images referenced in Log rows into assets/log/,
and rewrites only the in_progress key of data.json. Everything else in the
file (projects, highlights, stats, about_items, learning) is untouched.

Run by .github/workflows/sheet-sync.yml. Local use:

    export GCP_SA_KEY="$(cat service-account.json)"
    export SHEET_ID="<spreadsheet id from the sheet url>"
    python scripts/sync_sheet.py

Requires: pip install gspread google-auth google-api-python-client
"""

import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = REPO_ROOT / "data.json"
LOG_ASSETS = REPO_ROOT / "assets" / "log"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

VALID_CATEGORIES = {
    "Science", "Education", "Politics", "History",
    "Agriculture", "Urban Planning", "Interdisciplinary", "Policy",
}

DRIVE_ID_PATTERNS = [
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),       # .../file/d/<id>/view
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),   # ...open?id=<id>
]

MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/heic": "heic",
}

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y"]


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def norm_row(row):
    """Lowercase/strip header keys so column naming in the Sheet is forgiving."""
    return {str(k).strip().lower(): v for k, v in row.items()}


def as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1", "x", "✓")


def as_str(v):
    return str(v).strip() if v is not None else ""


def parse_percent(v):
    s = as_str(v)
    if not s:
        return None
    try:
        return int(float(s.rstrip("%")))
    except ValueError:
        warn(f"unparseable percent {v!r} — treating as blank")
        return None


def parse_date(v):
    s = as_str(v)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # keep whatever was typed


def drive_file_id(url):
    for pat in DRIVE_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def resolve_image(drive, proj_id, raw):
    """Turn a Log image cell into a repo-relative asset path (downloading from
    Drive on first sight), or None. The live site must never depend on a Drive
    link staying valid, so anything we can't localize becomes None."""
    val = as_str(raw)
    if not val:
        return None
    if val.startswith("assets/"):  # already a local path
        return val

    file_id = drive_file_id(val)
    if not file_id:
        warn(f"[{proj_id}] image is not a recognizable Drive link, skipping: {val!r}")
        return None

    dest_dir = LOG_ASSETS / proj_id
    existing = list(dest_dir.glob(f"{file_id}.*"))
    if existing:  # already downloaded on a previous run
        return existing[0].relative_to(REPO_ROOT).as_posix()

    try:
        meta = drive.files().get(
            fileId=file_id, fields="mimeType,name", supportsAllDrives=True
        ).execute()
        ext = MIME_EXT.get(meta.get("mimeType", ""))
        if not ext:
            name_ext = Path(meta.get("name", "")).suffix.lstrip(".").lower()
            ext = name_ext or "bin"
        dest = dest_dir / f"{file_id}.{ext}"

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buf, drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        )
        done = False
        while not done:
            _, done = downloader.next_chunk()

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf.getvalue())
        print(f"downloaded {dest.relative_to(REPO_ROOT)} ({len(buf.getvalue())} bytes)")
        return dest.relative_to(REPO_ROOT).as_posix()
    except Exception as e:
        warn(f"[{proj_id}] could not fetch Drive image {file_id}: {e}")
        return None


def build_in_progress(sheet, drive):
    projects = [norm_row(r) for r in sheet.worksheet("Projects").get_all_records()]
    steps = [norm_row(r) for r in sheet.worksheet("Steps").get_all_records()]
    logs = [norm_row(r) for r in sheet.worksheet("Log").get_all_records()]

    steps_by_id, logs_by_id = {}, {}
    for row in steps:
        pid = as_str(row.get("id"))
        if not pid or not as_str(row.get("text")):
            continue
        steps_by_id.setdefault(pid, []).append(
            {"text": as_str(row["text"]), "done": as_bool(row.get("done"))}
        )
    for row in logs:
        pid = as_str(row.get("id"))
        if not pid or not as_str(row.get("text")):
            continue
        logs_by_id.setdefault(pid, []).append(
            {
                "date": parse_date(row.get("date")),
                "text": as_str(row["text"]),
                "image": resolve_image(drive, pid, row.get("image")),
            }
        )

    result = []
    for row in projects:
        pid, title = as_str(row.get("id")), as_str(row.get("title"))
        if not pid or not title:
            continue
        category = as_str(row.get("category"))
        if category not in VALID_CATEGORIES:
            warn(f"[{pid}] category {category!r} is not one of the site's 8 categories")
        result.append(
            {
                "id": pid,
                "title": title,
                "category": category,
                "year": as_str(row.get("year")),
                "status": as_str(row.get("status")) or "In progress",
                "percent": parse_percent(row.get("percent")),
                "desc": as_str(row.get("desc")),
                "tools": [t.strip() for t in as_str(row.get("tools")).split(",") if t.strip()],
                "next_steps": steps_by_id.get(pid, []),
                "log": logs_by_id.get(pid, []),
            }
        )

    orphans = (set(steps_by_id) | set(logs_by_id)) - {p["id"] for p in result}
    if orphans:
        warn(f"Steps/Log rows reference ids with no Projects row: {sorted(orphans)}")
    return result


def main():
    sa_key = os.environ.get("GCP_SA_KEY")
    sheet_id = os.environ.get("SHEET_ID")
    if not sa_key or not sheet_id:
        sys.exit("GCP_SA_KEY and SHEET_ID environment variables are required")

    creds = Credentials.from_service_account_info(json.loads(sa_key), scopes=SCOPES)
    sheet = gspread.authorize(creds).open_by_key(sheet_id)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    in_progress = build_in_progress(sheet, drive)
    if not in_progress:
        # An empty Projects tab is far more likely a broken read than a real
        # intent to clear the section — refuse rather than blank the site.
        sys.exit("Sheet produced zero in_progress entries; refusing to overwrite data.json")

    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    data["in_progress"] = in_progress
    new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    if new_text == DATA_JSON.read_text(encoding="utf-8"):
        print("data.json already up to date — nothing to write")
        return

    DATA_JSON.write_text(new_text, encoding="utf-8")
    print(f"data.json updated: {len(in_progress)} in_progress entries")


if __name__ == "__main__":
    main()
