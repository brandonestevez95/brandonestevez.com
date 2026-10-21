#!/usr/bin/env python3
"""Pull the site's photos into assets/: the bio polaroids and the project cards.

Two independent sections, each skipped when its inputs aren't configured:

  Bio       Drive folder BIO_PHOTOS_FOLDER_ID -> assets/bio/
            Twelve fixed names that index.html already references.

  Projects  -> assets/projects/ + assets/projects/photos.json
            Each card in data.json gets, in order of preference:
              1. a photo from Drive folder PROJECT_PHOTOS_FOLDER_ID whose
                 filename is a run of whole words from the project title
                 (`ants.jpg`, `freedmen-villages.png`, the full title, ...)
              2. the link preview of its first deliverable link — the og:image
                 a chat app shows when the link is pasted
              3. nothing, and the card keeps its "Photo needed" gradient
            The link-preview path needs no Google credentials at all.

The card photos are recorded in photos.json, not data.json: the sheet sync
rewrites projects[] every 15 minutes and would drop any field it didn't write.
The page maps project title -> photo through that file instead.

Not part of the 15-minute sheet-sync cron: these are binary assets that change
rarely, so .github/workflows/fetch-drive-photos.yml runs this hourly and from
its manual "Run workflow" button, and the lock files below make a run where
nothing changed download nothing.

Uses the same service account as scripts/sync_sheet.py via GCP_SA_KEY, but
asks for `drive.readonly` only — the sheet sync keeps its own narrower
spreadsheets.readonly scope, so neither script holds access it doesn't use.

Drive files are matched by stem, ignoring case/extension, so
`Kid-Worldmap.JPG` in Drive still lands as `kid-worldmap.jpg`. The extension
comes from Drive's own metadata rather than being assumed — and if it turns out
a bio photo isn't the .jpg index.html currently expects, the <img src> for that
one photo is rewritten to match.

Photos are downscaled to MAX_EDGE on the long side and re-encoded on the way
in, so phone originals don't ship at 4.8MB. Format is preserved — a line
drawing saved as a PNG stays a PNG rather than picking up JPEG artefacts.

Local use:

    export GCP_SA_KEY="$(cat service-account.json)"
    export BIO_PHOTOS_FOLDER_ID="<folder id from the drive url>"
    export PROJECT_PHOTOS_FOLDER_ID="<folder id from the drive url>"
    python scripts/fetch_drive_photos.py

Requires: pip install google-api-python-client google-auth pillow
"""

import io
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from PIL import Image, ImageOps

REPO_ROOT = Path(__file__).resolve().parent.parent
BIO_DIR = REPO_ROOT / "assets" / "bio"
PROJECT_DIR = REPO_ROOT / "assets" / "projects"
# Served with the site: the page reads it to find each card's photo.
PROJECT_INDEX = PROJECT_DIR / "photos.json"
INDEX_HTML = REPO_ROOT / "index.html"
DATA_JSON = REPO_ROOT / "data.json"
# Build metadata, not assets — they record where each file came from. They live
# beside the script so they aren't served with the site.
BIO_LOCK = Path(__file__).resolve().parent / "bio_photos.lock.json"
PROJECT_LOCK = Path(__file__).resolve().parent / "project_photos.lock.json"

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

# Link previews come from arbitrary pages, so cap what one is allowed to cost.
HTTP_TIMEOUT = 20
MAX_PAGE_BYTES = 2_000_000
MAX_PREVIEW_BYTES = 15_000_000
USER_AGENT = "Mozilla/5.0 (compatible; brandonestevez.com link-preview fetcher)"
# Long titles make long filenames; cut at a word boundary past this.
SLUG_MAX = 60


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def normalize_ext(ext):
    ext = ext.lower()
    return {".jpeg": ".jpg", ".tiff": ".tif"}.get(ext, ext)


def pick_ext(name, mime):
    """The filename's extension wins when it's a real image extension;
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


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ───────────────────────────── Drive ─────────────────────────────


def drive_service(sa_key):
    # Imported here so the link-preview path runs without the Google libraries.
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(json.loads(sa_key), scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folder(service, folder_id, env_name):
    """Every non-trashed file directly in the folder, following pagination."""
    from googleapiclient.errors import HttpError

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
                    f"{env_name}, and that the folder is shared with "
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


def image_files(files):
    """Drop subfolders and Google-native docs, which can't be photos."""
    out = []
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            continue
        if f["mimeType"].startswith("application/vnd.google-apps."):
            warn(f"{f['name']!r} is a Google-native file, not a photo — skipping")
            continue
        out.append(f)
    return out


def download(service, file_id):
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf, service.files().get_media(fileId=file_id, supportsAllDrives=True)
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ─────────────────────────── processing ──────────────────────────


def proc_key(stem=None):
    """Cache key for "already processed with the current settings".

    The source checksum alone isn't enough: changing the cap or the quality
    changes the output bytes while the source stays identical, and the run
    would skip every photo and keep the old encoding.
    """
    return f"v{PROCESS_VERSION}:{MAX_EDGE}:{JPEG_QUALITY}:{GRAPHIC_STEMS.get(stem, '-')}"


def process_image(raw, src_ext, label, max_edge=MAX_EDGE, graphic=False):
    """Downscale and re-encode. Returns (bytes, out_ext, note).

    The written format isn't always the source format: an opaque PNG is
    usually a screenshot of a photograph, and PNG is a poor container for
    that. Drawings (graphic=True) stay PNG, since JPEG rings all over linework.
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


def load_lock(path):
    """Where each local file came from.

    Needed because processing means the stored bytes no longer match the
    source, so the file itself can't answer "is this still current?".
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        warn(f"{path.name} is unreadable — refetching everything")
        return {}


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


# ────────────────────────────── bio ──────────────────────────────


def match_to_stems(files):
    """Map each expected stem to its Drive file, ignoring case and extension."""
    wanted = {s.lower(): s for s in EXPECTED}
    matched, unmatched = {}, []
    for f in image_files(files):
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


def sync_bio(service, folder_id):
    files = list_folder(service, folder_id, "BIO_PHOTOS_FOLDER_ID")
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

    manifest = load_lock(BIO_LOCK)
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
        data, out_ext, note = process_image(
            raw, ext, f["name"],
            max_edge=GRAPHIC_STEMS.get(stem, MAX_EDGE),
            graphic=stem in GRAPHIC_STEMS,
        )
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
    write_json(BIO_LOCK, manifest)

    retargeted = retarget_html(final_ext)
    total = sum((BIO_DIR / f"{s}{e}").stat().st_size for s, e in final_ext.items())

    print(f"bio: downloaded {len(written)}, unchanged {len(skipped)}, of {len(EXPECTED)} expected")
    for line in sorted(written):
        print(f"  {line}")
    if saved_bytes:
        print(f"  saved {saved_bytes/1_000_000:.1f}MB by resizing")
    print(f"  assets/bio total: {total/1_000_000:.1f}MB")
    if retargeted:
        print("  index.html retargeted: " + "; ".join(retargeted))


# ──────────────────────── project cards ──────────────────────────


class _MetaImage(HTMLParser):
    """Collects og:image / twitter:image from a page's <meta> tags."""

    KEYS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")

    def __init__(self):
        super().__init__()
        self.found = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = dict(attrs)
        key = (a.get("property") or a.get("name") or "").strip().lower()
        if key in self.KEYS and a.get("content") and key not in self.found:
            self.found[key] = a["content"].strip()


def http_get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"response over {limit // 1_000_000}MB")
        return body, resp.headers.get_content_type(), resp.geturl()


def full_size(url):
    """ArcGIS (every StoryMap) serves the preview at ?w=400. Asking for
    MAX_EDGE gets the largest it has without upscaling. Dropping the parameter
    isn't the same thing: a collection thumbnail then falls back to 200px."""
    parts = urlsplit(url)
    if not parts.netloc.endswith("arcgis.com"):
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "w"]
    q.append(("w", str(MAX_EDGE)))
    return urlunsplit(parts._replace(query=urlencode(q)))


def preview_url(page_url):
    """The image a chat app would show for this link, or None."""
    body, ctype, final = http_get(page_url, MAX_PAGE_BYTES)
    if ctype.startswith("image/"):
        return final  # the deliverable is itself an image
    if "html" not in ctype:
        return None
    parser = _MetaImage()
    parser.feed(body.decode("utf-8", "replace"))
    for key in _MetaImage.KEYS:
        if key in parser.found:
            return full_size(urljoin(final, parser.found[key]))
    return None


def title_slug(title):
    s = slugify(title)
    if len(s) > SLUG_MAX:
        s = s[:SLUG_MAX].rsplit("-", 1)[0]
    return s or "project"


def match_drive_to_projects(files, titles):
    """{title: Drive file}. A file belongs to a project when its stem, as
    words, appears as a whole-word run inside the title — so `ants.jpg` finds
    the ants project and `freedmen-villages.jpg` finds Raleigh's freedmen one.
    A stem that fits two titles is ambiguous and is skipped with a warning;
    when two files fit one title, the more specific (longer) name wins."""
    padded = {t: f"-{slugify(t)}-" for t in titles}
    best = {}
    for f in image_files(files):
        stem = slugify(Path(f["name"]).stem)
        if not stem:
            continue
        hits = [t for t, p in padded.items() if f"-{stem}-" in p]
        if not hits:
            warn(f"Drive file {f['name']!r} matches no project title — ignored")
            continue
        if len(hits) > 1:
            warn(
                f"Drive file {f['name']!r} matches {len(hits)} projects "
                f"({'; '.join(h[:40] for h in hits)}) — rename it with more of the title"
            )
            continue
        t = hits[0]
        prev = best.get(t)
        if prev is None or len(stem) > len(slugify(Path(prev["name"]).stem)):
            if prev is not None:
                warn(f"{t[:40]!r}: using {f['name']!r} over {prev['name']!r}")
            best[t] = f
        else:
            warn(f"{t[:40]!r}: using {prev['name']!r} over {f['name']!r}")
    return best


def sync_projects(service, folder_id):
    projects = json.loads(DATA_JSON.read_text(encoding="utf-8")).get("projects") or []
    titles = [p["title"] for p in projects if p.get("title")]
    if not titles:
        sys.exit("data.json has no projects; leaving assets/projects untouched")

    drive = {}
    if service and folder_id:
        # An error here exits before anything is written, so a sharing mistake
        # can't quietly swap every Drive photo for its link preview. An empty
        # folder is fine: it's the starting state.
        drive = match_drive_to_projects(
            list_folder(service, folder_id, "PROJECT_PHOTOS_FOLDER_ID"), titles
        )

    lock = load_lock(PROJECT_LOCK)
    index, new_lock, report = {}, {}, []
    used_slugs = set()

    for p in projects:
        title = p.get("title")
        if not title:
            continue
        slug = base = title_slug(title)
        n = 2
        while slug in used_slugs:
            slug, n = f"{base}-{n}", n + 1
        used_slugs.add(slug)
        prev = lock.get(title) or {}

        def keep_previous(why):
            """A failed fetch keeps last run's photo rather than dropping it."""
            existing = PROJECT_DIR / prev.get("out", "")
            if prev.get("out") and existing.exists():
                index[title] = f"assets/projects/{existing.name}"
                new_lock[title] = prev
                report.append(f"kept   {existing.name} ({why})")
            else:
                report.append(f"none   {title[:50]} ({why})")

        # Work out where the photo should come from.
        f = drive.get(title)
        if f:
            ext = pick_ext(f["name"], f["mimeType"])
            if ext is None:
                warn(f"can't tell what kind of file {f['name']!r} is ({f['mimeType']}) — skipping")
                keep_previous("unreadable Drive file")
                continue
            source = f"drive:{f['id']}:{f.get('md5Checksum')}"
            label = f"Drive {f['name']}"
            fetch = lambda f=f: (download(service, f["id"]), ext)
        else:
            img_url = None
            for link in p.get("links") or []:
                try:
                    img_url = preview_url(link["url"])
                except (urllib.error.URLError, OSError, ValueError) as e:
                    warn(f"[{title[:40]}] couldn't read {link['url']}: {e}")
                    continue
                if img_url:
                    break
            if not img_url:
                if p.get("links"):
                    keep_previous("no link preview")
                else:
                    report.append(f"none   {title[:50]} (no links, no Drive photo)")
                continue
            source = f"link:{img_url}"
            label = img_url

            def fetch(img_url=img_url, title=title):
                raw, ctype, final = http_get(img_url, MAX_PREVIEW_BYTES)
                ext = pick_ext(urlsplit(final).path, ctype)
                if ext is None:
                    raise ValueError(f"not an image ({ctype})")
                return raw, ext

        # Same source and settings as last run, and the file's still there.
        existing = PROJECT_DIR / prev.get("out", "")
        if (
            prev.get("source") == source
            and prev.get("proc") == proc_key()
            and prev.get("out", "").startswith(slug + ".")
            and existing.exists()
        ):
            index[title] = f"assets/projects/{existing.name}"
            new_lock[title] = prev
            report.append(f"same   {existing.name}")
            continue

        try:
            raw, ext = fetch()
        except (urllib.error.URLError, OSError, ValueError) as e:
            warn(f"[{title[:40]}] couldn't download {label}: {e}")
            keep_previous("download failed")
            continue
        data, out_ext, note = process_image(raw, ext, label)
        dest = PROJECT_DIR / f"{slug}{out_ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if len(data) > SIZE_WARN_BYTES:
            warn(f"{dest.name} is still {len(data)/1_000_000:.1f}MB after processing")
        index[title] = f"assets/projects/{dest.name}"
        new_lock[title] = {"source": source, "out": dest.name, "proc": proc_key()}
        kind = "drive " if source.startswith("drive:") else "link  "
        report.append(f"{kind}{dest.name} {len(data)/1000:.0f}KB" + (f" ({note})" if note else ""))

    # Anything on disk that no card points at any more — a renamed or deleted
    # project, or a photo that changed extension.
    keep = {Path(v).name for v in index.values()} | {PROJECT_INDEX.name}
    for old in PROJECT_DIR.glob("*"):
        if old.is_file() and old.name not in keep:
            old.unlink()
            report.append(f"removed {old.name}")

    write_json(PROJECT_INDEX, index)
    write_json(PROJECT_LOCK, new_lock)

    print(f"projects: {len(index)} of {len(titles)} cards have a photo")
    for line in report:
        print(f"  {line}")


def main():
    sa_key = os.environ.get("GCP_SA_KEY", "").strip()
    bio_folder = os.environ.get("BIO_PHOTOS_FOLDER_ID", "").strip()
    project_folder = os.environ.get("PROJECT_PHOTOS_FOLDER_ID", "").strip()
    service = drive_service(sa_key) if sa_key else None

    # Each section runs even if the other fails, so a broken bio folder can't
    # hold back the project photos. Failures still fail the run, afterwards.
    failures = []
    sections = [("projects", lambda: sync_projects(service, project_folder))]
    if service and bio_folder:
        sections.insert(0, ("bio", lambda: sync_bio(service, bio_folder)))
    else:
        print("bio: GCP_SA_KEY / BIO_PHOTOS_FOLDER_ID not set — skipped")
    if not (service and project_folder):
        print("projects: no Drive folder configured — link previews only")

    for name, run in sections:
        try:
            run()
        except SystemExit as e:
            if e.code not in (None, 0):
                print(f"{name}: {e.code}", file=sys.stderr)
                failures.append(name)
    if failures:
        sys.exit(f"failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
