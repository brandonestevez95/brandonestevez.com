#!/usr/bin/env python3
"""Pull the bio section's photos from Google Drive into assets/bio/.

Deliberately NOT part of the 15-minute sheet-sync cron: these are binary
assets that change rarely, so this runs from the manual "Run workflow" button
(.github/workflows/fetch-bio-photos.yml) instead.

Uses the same service account as scripts/sync_sheet.py via GCP_SA_KEY, but
asks for `drive.readonly` only — the sheet sync keeps its own narrower
spreadsheets.readonly scope, so neither script holds access it doesn't use.

Files are matched to the twelve names the bio markup already references, by
stem and ignoring case/extension, so `Kid-Worldmap.JPG` in Drive still lands
as `kid-worldmap.jpg`. The extension comes from Drive's own metadata rather
than being assumed — and if it turns out a photo isn't the .jpg index.html
currently expects, the <img src> for that one photo is rewritten to match.

Run by .github/workflows/fetch-bio-photos.yml. Local use:

    export GCP_SA_KEY="$(cat service-account.json)"
    export BIO_PHOTOS_FOLDER_ID="<folder id from the drive url>"
    python scripts/fetch_bio_photos.py

Requires: pip install google-api-python-client google-auth
"""

import hashlib
import io
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

REPO_ROOT = Path(__file__).resolve().parent.parent
BIO_DIR = REPO_ROOT / "assets" / "bio"
INDEX_HTML = REPO_ROOT / "index.html"

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# The twelve photos the bio markup references, in reading order. A Drive file
# whose stem isn't in here is left alone; a stem in here with no Drive file is
# warned about but isn't fatal, since the polaroid renders a "photo pending"
# frame until the file shows up.
EXPECTED = [
    "kid-worldmap", "kid-nature", "village-plan", "esri-2024",
    "first-presentation", "tedx-photo", "esri25-group-dangermund",
    "esri25-meposters", "capsmun-1", "juntos-1", "juntos-2", "juntos-3",
]

# Trusted before mimetypes.guess_extension, which is platform-dependent and
# has been known to hand back ".jpe" for image/jpeg.
MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/tiff": ".tif",
    "image/avif": ".avif",
}
KNOWN_EXTS = set(MIME_EXT.values()) | {".jpeg", ".tiff"}

# Phone originals run 3-5MB each; twelve of those is a slow bio section and a
# heavy repo. Not fatal, and not something to silently re-encode either.
SIZE_WARN_BYTES = 1_500_000


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def normalize_ext(ext):
    ext = ext.lower()
    return {".jpeg": ".jpg", ".tiff": ".tif"}.get(ext, ext)


def pick_ext(name, mime):
    """Drive's filename extension wins when it's a real image extension;
    otherwise fall back to the declared mimeType."""
    ext = normalize_ext(Path(name).suffix)
    if ext in KNOWN_EXTS:
        return normalize_ext(ext)
    if mime in MIME_EXT:
        return MIME_EXT[mime]
    # Only guess for image/*: guess_extension happily turns
    # application/octet-stream into ".bin", which would save junk and then
    # point an <img> at it.
    if (mime or "").startswith("image/"):
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return normalize_ext(guessed)
    return None


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_folder(service, folder_id):
    """Every non-trashed file directly in the folder, following pagination."""
    files, token = [], None
    while True:
        try:
            resp = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, size)",
                pageSize=200,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                sys.exit(
                    f"Drive folder {folder_id!r} not found. Check "
                    f"BIO_PHOTOS_FOLDER_ID, and that the folder is shared with "
                    f"the service account's email."
                )
            if e.resp.status == 403:
                sys.exit(
                    f"Drive denied access to folder {folder_id!r}. Confirm the "
                    f"Drive API is enabled on the project and the folder is "
                    f"shared with the service account (Viewer is enough)."
                )
            raise
        files.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return files


def match_to_stems(files):
    """Map each expected stem to its Drive file, ignoring case and extension."""
    wanted = {s.lower(): s for s in EXPECTED}
    matched, unmatched = {}, []
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            continue
        if f["mimeType"].startswith("application/vnd.google-apps."):
            warn(f"{f['name']!r} is a Google-native file, not a photo — skipping")
            continue
        stem = wanted.get(Path(f["name"]).stem.strip().lower())
        if stem is None:
            unmatched.append(f["name"])
            continue
        if stem in matched:
            warn(
                f"two Drive files both match {stem!r} "
                f"({matched[stem]['name']!r} and {f['name']!r}) — keeping the first"
            )
            continue
        matched[stem] = f
    return matched, unmatched


def download(service, file_id, dest):
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf, service.files().get_media(fileId=file_id, supportsAllDrives=True)
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(buf.getvalue())


def retarget_html(stem_to_ext):
    """Point each <img> at the extension the photo actually turned out to be.

    Only the twelve known bio paths are touched, and only when the extension
    genuinely differs, so this is a no-op on the common all-.jpg case.
    """
    if not INDEX_HTML.exists():
        return []
    html = original = INDEX_HTML.read_text(encoding="utf-8")
    changed = []
    for stem, ext in stem_to_ext.items():
        pattern = re.compile(rf"(assets/bio/{re.escape(stem)})\.([A-Za-z0-9]+)")

        def swap(m):
            if m.group(2).lower() == ext.lstrip(".").lower():
                return m.group(0)
            changed.append(f"{stem}: .{m.group(2)} -> {ext}")
            return f"{m.group(1)}{ext}"

        html = pattern.sub(swap, html)
    if html != original:
        INDEX_HTML.write_text(html, encoding="utf-8")
    return changed


def main():
    sa_key = os.environ.get("GCP_SA_KEY")
    folder_id = os.environ.get("BIO_PHOTOS_FOLDER_ID")
    if not sa_key or not folder_id:
        sys.exit("GCP_SA_KEY and BIO_PHOTOS_FOLDER_ID environment variables are required")

    creds = Credentials.from_service_account_info(json.loads(sa_key), scopes=SCOPES)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    files = list_folder(service, folder_id.strip())
    if not files:
        # Same reasoning as the sheet sync refusing to write an empty Cards tab:
        # an empty read is far more likely a broken setup than a real intent.
        sys.exit(
            f"Drive folder {folder_id!r} came back empty; refusing to treat that "
            f"as 'delete every bio photo'. Check the folder id and sharing."
        )

    matched, unmatched = match_to_stems(files)
    for name in unmatched:
        warn(f"Drive file {name!r} matches none of the bio's twelve names — ignored")
    for stem in EXPECTED:
        if stem not in matched:
            warn(f"no Drive file found for {stem!r} — its frame stays 'photo pending'")

    written, skipped, final_ext = [], [], {}
    for stem, f in matched.items():
        ext = pick_ext(f["name"], f["mimeType"])
        if ext is None:
            warn(f"can't tell what kind of file {f['name']!r} is ({f['mimeType']}) — skipping")
            continue
        final_ext[stem] = ext
        dest = BIO_DIR / f"{stem}{ext}"

        # Drive hands us an md5 for binary files; use it to leave untouched
        # photos alone rather than rewriting twelve files on every run.
        remote_md5 = f.get("md5Checksum")
        if dest.exists() and remote_md5 and md5_of(dest) == remote_md5:
            skipped.append(dest.name)
            continue

        download(service, f["id"], dest)
        written.append(dest.name)

        size = dest.stat().st_size
        if size > SIZE_WARN_BYTES:
            warn(
                f"{dest.name} is {size/1_000_000:.1f}MB — consider resizing before "
                f"this ships; the bio loads all twelve"
            )

        # An earlier run may have saved this photo under a different extension.
        for old in BIO_DIR.glob(f"{stem}.*"):
            if old != dest:
                old.unlink()
                print(f"removed stale {old.name}")

    retargeted = retarget_html(final_ext)

    print(f"downloaded {len(written)}, unchanged {len(skipped)}, of {len(EXPECTED)} expected")
    if written:
        print("  written: " + ", ".join(sorted(written)))
    if retargeted:
        print("  index.html retargeted: " + "; ".join(retargeted))


if __name__ == "__main__":
    main()
