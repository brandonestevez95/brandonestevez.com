# Sheet Sync — setup guide

Edit the site's **In the Pipeline** section from a Google Sheet on your phone.
Every edit lands in this repo as a normal commit within ~1 minute, Cloudflare
redeploys like it does for any push, and nothing new exists on the live domain.

```
Google Sheet ──(Apps Script onEdit)──▶ repository_dispatch ──▶ GitHub Action
                                                                   │
                                             scripts/sync_sheet.py rebuilds
                                             in_progress[] in data.json,
                                             pulls Drive images → assets/log/,
                                             commits, pushes ──▶ Cloudflare deploys
```

Only `in_progress[]` is sheet-managed. `projects`, `highlights`, `stats`,
`about_items`, and `learning` stay hand-edited in `data.json`. `log.py` (the
desktop CLI) keeps working too — it edits the same file. **Heads up:** the
Sheet is the source of truth for `in_progress[]`; anything the CLI or a hand
edit writes into that one key gets replaced on the next sync, so make
in-progress edits in the Sheet.

## 1. Create the Sheet

One spreadsheet, three tabs, headers in row 1 exactly as below (case doesn't
matter; the sync lowercases headers).

**Tab `Projects`** — one row per project:

| id | title | category | year | status | percent | desc | tools |
|----|-------|----------|------|--------|---------|------|-------|
| supply-chain | Supply Chain Analysis | Agriculture | 2026 | In progress | | Food supply chain mapping… | ArcGIS Pro, SAS Viya |

- `id`: short slug, stable forever — it links the three tabs and names the
  image folder. Current ids: `supply-chain`, `medicare-fraud`,
  `sea-level-rise`, `ant-war`.
- `category`: one of `Science, Education, Politics, History, Agriculture,
  Urban Planning, Interdisciplinary, Policy` (tip: Data → Data validation →
  dropdown from that list).
- `percent`: leave blank once a project has `Steps` rows — a checklist
  computes its own percent and wins. Only fill it for a project with no
  checklist yet.
- `tools`: comma-separated.

**Tab `Steps`** — many rows per id, in display order:

| id | text | done |
|----|------|------|

Make `done` a real checkbox column: select the column → Insert → Checkbox.
Tappable on mobile, no typing TRUE/FALSE.

**Tab `Log`** — many rows per id (newest at the bottom is fine; the site
shows the latest entries first):

| id | date | text | image |
|----|------|------|-------|

- `date`: type `2026-08-10` or any common format — the sync normalizes it.
- `image`: optional. Upload a screenshot via the Drive app, copy its share
  link, paste it here. The sync downloads the bytes into
  `assets/log/<id>/<drive-file-id>.<ext>` and rewrites the field to that repo
  path, so the live site never depends on the Drive link. Already-synced
  images are skipped on re-runs.

## 2. Google Cloud service account

1. [console.cloud.google.com](https://console.cloud.google.com) → create a
   project (e.g. `brandonestevez-site-sync`).
2. APIs & Services → Library → enable **Google Sheets API** and
   **Google Drive API**.
3. IAM & Admin → Service Accounts → Create (no roles needed). Open it →
   Keys → Add key → JSON. Keep the downloaded file private — it never goes
   in this repo.
4. Share the **Sheet** with the service account's email (`…@….iam.gserviceaccount.com`),
   Viewer is enough.
5. For Log images: keep screenshots in one Drive folder and share that folder
   with the same service-account email (Viewer). That beats per-file "anyone
   with the link" — no sharing setting to accidentally flip off later.

## 3. GitHub repo secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

- **`GCP_SA_KEY`** — paste the *entire contents* of the JSON key file.
- **`SHEET_ID`** — the long id from the sheet URL:
  `docs.google.com/spreadsheets/d/`**`<this part>`**`/edit`.

No Cloudflare secrets, and no PAT here — the workflow pushes with its own
`GITHUB_TOKEN` (`permissions: contents: write`).

## 4. Apps Script trigger

Follow the install comment at the top of
[`scripts/sheet_trigger.gs`](../scripts/sheet_trigger.gs): paste it into the
Sheet's Apps Script editor, add a `GITHUB_TOKEN` script property (fine-grained
PAT, **this repo only**, Contents: Read and write — that's the one PAT in the
whole system, and it lives in Google's script properties, not the repo), and
add an **installable** On-edit trigger for `onSheetEdit`.

Even if the trigger ever breaks, the workflow's 30-minute cron still syncs,
and the Actions tab has a manual **Run workflow** button (works from the
GitHub mobile app too).

## 5. Test it

1. **Local dry run** (against a copy of the Sheet first if you like):
   `export GCP_SA_KEY="$(cat key.json)" SHEET_ID=…` then
   `python scripts/sync_sheet.py` — diff `data.json` and check the schema.
2. **Trigger**: edit a cell; a `repository_dispatch` run should appear in the
   Actions tab within ~1 minute (the trigger debounces 60s of edits into one).
3. **Images**: add a Drive link to a `Log` row → confirm the file lands in
   `assets/log/<id>/` and renders on the site.
4. **Idempotency**: run the workflow twice with no Sheet changes → the second
   run must produce no commit.
5. **`log.py`** still works for everything outside `in_progress[]`.

## Notes

- Commit messages deliberately have **no `[skip ci]`**: Cloudflare honors it
  by skipping the deploy (which would break the whole point), and pushes made
  with `GITHUB_TOKEN` don't retrigger Actions anyway.
- The sync refuses to write if the Sheet yields zero projects — an empty or
  unreadable Sheet can't blank the section.
- Deleting a `Log` row removes the entry from the site but leaves the
  already-committed image file in `assets/log/`; delete it manually if you
  care.
