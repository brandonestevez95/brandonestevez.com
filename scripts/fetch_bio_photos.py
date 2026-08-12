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

Photos are downscaled to MAX_EDGE on the long side and re-encoded on the way
in, so phone originals don't ship at 4.8MB. Format is preserved — a line
drawing saved as a PNG stays a PNG rather than picking up JPEG artefacts.

Run by .github/workflows/fetch-bio-photos.yml. Local use:

    export GCP_SA_KEY="$(cat service-account.json)"
    export BIO_PHOTOS_FOLDER_ID="<folder id from the drive url>"
    python scripts/fetch_bio_photos.py

Requires: pip install google-api-python-client google-auth pillow
"""

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
from PIL import Image, ImageOps

REPO_ROOT = Path(__file__).resolve().parent.parent
BIO_DIR = REPO_ROOT / "assets" / "bio"
INDEX_HTML = REPO_ROOT / "index.html"
# Build metadata, not an asset — it records which Drive revision each file came
# from. Lives beside the script so it isn't served with the site.
MANIFEST = Path(__file__).resolve().parent / "bio_photos.lock.json"

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

# The frames render at ~400px at most, so 1600 leaves plenty of headroom for
# retina without shipping 6000px phone originals.
MAX_EDGE = 1600
JPEG_QUALITY = 82

# Stems whose source is a drawing rather than a photograph. These keep PNG —
# JPEG would ring all over the linework — and take a tighter cap, because line
# art doesn't need photo resolution to stay crisp at 400px.
GRAPHIC_STEMS = {"village-plan": 900}

# Part of the cache key below. Bump when a change to process_image would
# produce different bytes from the same source, or already-fetched photos will
# be skipped and keep their old encoding forever.
PROCESS_VERSION = 2
# Anything still over this after processing is worth a look by hand.
SIZE_WARN_BYTES = 1_000_000


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


def download(service, file_id):
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf, service.files().get_media(fileId=file_id, supportsAllDrives=True)
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def proc_key(stem):
    """Cache key for "already processed with the current settings".

    The Drive md5 alone isn't enough: changing the cap or the quality changes
    the output bytes while the source stays identical, and the run would skip
    every photo and keep the old encoding.
    """
    return f"v{PROCESS_VERSION}:{MAX_EDGE}:{JPEG_QUALITY}:{GRAPHIC_STEMS.get(stem, '-')}"


def process_image(raw, src_ext, stem, label):
    """Downscale and re-encode. Returns (bytes, out_ext, note).

    The written format isn't always the source format: these PNGs are
    screenshots of photographs, and PNG is a poor container for that. Drawings
    listed in GRAPHIC_STEMS stay PNG, since JPEG rings all over linework.
    Metadata is dropped as a side effect, which also takes GPS coordinates out
    of the shipped files.
    """
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # HEIC without pillow-heif, or anything exotic
        warn(f"{label}: Pillow couldn't read this ({e}) — storing the original as-is")
        return raw, src_ext, None

    # Phone cameras record orientation in EXIF rather than rotating the pixels.
    # Bake it in before re-encoding drops the tag, or the photo ships sideways.
    img = ImageOps.exif_transpose(img)

    graphic = stem in GRAPHIC_STEMS
    max_edge = GRAPHIC_STEMS[stem] if graphic else MAX_EDGE
    # Real transparency has to keep PNG; flattening it onto JPEG would paint
    # whatever showed through solid black.
    opaque = img.mode != "RGBA" or img.getchannel("A").getextrema() == (255, 255)

    if not graphic and src_ext == ".png" and opaque:
        out_ext = ".jpg"
    else:
        out_ext = src_ext

    fmt = {".jpg": "JPEG", ".png": "PNG"}.get(out_ext)
    if fmt is None:
        return raw, src_ext, None  # no encoder settings for this one

    w, h = img.size
    scale = max_edge / max(w, h)
    resized = scale < 1  # only ever downscale — first-presentation is 309x246
    if resized:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    else:
        # A fully opaque alpha channel is a quarter of the pixel data carrying
        # no information. Only dropped when it really is opaque everywhere.
        if img.mode == "RGBA" and opaque:
            img = img.convert("RGB")
        img.save(buf, "PNG", optimize=True)
    out = buf.getvalue()

    # Re-encoding an already-tight file can make it bigger; don't take a loss
    # for nothing when nothing else about the file changed.
    if out_ext == src_ext and not resized and len(out) >= len(raw):
        return raw, src_ext, None

    bits = []
    if out_ext != src_ext:
        bits.append(f"{src_ext.lstrip('.')}->{out_ext.lstrip('.')}")
    bits.append(f"{w}x{h} -> {img.size[0]}x{img.size[1]}" if resized else "re-encoded")
    return out, out_ext, ", ".join(bits)


def load_manifest():
    """Which Drive revision each local file came from.

    Needed because processing means the stored bytes no longer match Drive's
    md5, so the file itself can't answer "is this still current?".
    """
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        warn(f"{MANIFEST.name} is unreadable — refetching everything")
        return {}


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

    manifest = load_manifest()
    written, skipped, final_ext, saved_bytes = [], [], {}, 0
    for stem, f in matched.items():
        ext = pick_ext(f["name"], f["mimeType"])
        if ext is None:
            warn(f"can't tell what kind of file {f['name']!r} is ({f['mimeType']}) — skipping")
            continue

        # Compare against the Drive md5 recorded last run, not the local bytes:
        # the local file has been resized, so it can never match Drive again.
        # The processing key goes in too, so changing the cap or the quality
        # refetches instead of silently keeping the old encoding.
        remote_md5 = f.get("md5Checksum")
        prev = manifest.get(stem)
        if (
            prev
            and remote_md5
            and prev.get("md5") == remote_md5
            and prev.get("proc") == proc_key(stem)
        ):
            existing = BIO_DIR / prev.get("out", "")
            if existing.exists():
                final_ext[stem] = existing.suffix
                skipped.append(existing.name)
                continue

        raw = download(service, f["id"])
        data, out_ext, note = process_image(raw, ext, stem, f["name"])
        saved_bytes += len(raw) - len(data)

        dest = BIO_DIR / f"{stem}{out_ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        final_ext[stem] = out_ext
        manifest[stem] = {
            "md5": remote_md5,
            "out": dest.name,
            "drive_name": f["name"],
            "proc": proc_key(stem),
        }
        written.append(
            f"{dest.name} {len(raw)/1_000_000:.1f}->{len(data)/1_000_000:.1f}MB"
            + (f" ({note})" if note else " (unchanged)")
        )

        if len(data) > SIZE_WARN_BYTES:
            warn(f"{dest.name} is still {len(data)/1_000_000:.1f}MB after processing")

        # An earlier run may have saved this photo under a different extension.
        for old in BIO_DIR.glob(f"{stem}.*"):
            if old != dest:
                old.unlink()
                print(f"removed stale {old.name}")

    # Drop entries for photos no longer in Drive so the manifest can't go stale.
    for stem in [s for s in manifest if s not in matched]:
        del manifest[stem]
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    retargeted = retarget_html(final_ext)
    total = sum((BIO_DIR / f"{s}{e}").stat().st_size for s, e in final_ext.items())

    print(f"downloaded {len(written)}, unchanged {len(skipped)}, of {len(EXPECTED)} expected")
    for line in sorted(written):
        print(f"  {line}")
    if saved_bytes:
        print(f"  saved {saved_bytes/1_000_000:.1f}MB by resizing")
    print(f"  assets/bio total: {total/1_000_000:.1f}MB")
    if retargeted:
        print("  index.html retargeted: " + "; ".join(retargeted))


if __name__ == "__main__":
    main()
