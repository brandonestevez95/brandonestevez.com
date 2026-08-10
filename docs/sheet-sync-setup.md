# Sheet Sync — setup guide

Edit the site's **In the Pipeline** cards from one Google Sheet tab. A GitHub
Action reads it every 15 minutes, writes `in_progress[]` into `data.json`, and
commits — Cloudflare redeploys from that push like it does for any other.
Nothing new exists on the live domain.

```
Google Sheet (`Cards` tab)
        │  every 15 min, or the manual "Run workflow" button
        ▼
GitHub Action → scripts/sync_sheet.py rewrites in_progress[] in data.json
        │
        └─ commits + pushes ──▶ Cloudflare deploys
```

Only `in_progress[]` comes from the Sheet. `projects`, `highlights`, `stats`,
`about_items`, and `learning` stay hand-edited in `data.json`, and `log.py`
still works for those. **The Sheet is the source of truth for `in_progress[]`** —
anything hand-edited into that one key gets replaced on the next sync.

## 1. Create the Sheet

One tab named exactly **`Cards`**, headers in row 1:

| Project | Year | Category | Links |
|---------|------|----------|-------|
| Comparative Supply Chain Fragility Study | 2026 | Agriculture | Draft StoryMap: https://… |
| Medicare Fraud Mapped | 2026 | Policy | Preliminary Map: https://…<br>Methodology Doc: https://… |

- **Category** must be one of `Science, Education, Politics, History,
  Agriculture, Urban Planning, Interdisciplinary, Policy` — these map to the
  site's category colors. Worth adding as Data → Data validation → Dropdown so
  it can't drift.
- **Links** — one deliverable per line inside the cell (**Alt+Enter** for a line
  break, **Option+Enter** on a Mac). Each line is either `Label: https://…` or a
  bare URL, which gets the label "View". Leave it empty and the card shows
  "no deliverables linked yet".
- A row with an empty **Project** cell is skipped, so blank spacer rows are fine.

**Any other tab in the same spreadsheet is never read.** The script opens
`Cards` by name and nothing else, so a scratch or notes tab stays private by
simply not being touched.

## 2. Google Cloud service account

1. [console.cloud.google.com](https://console.cloud.google.com) → create a
   project (e.g. `brandonestevez-site-sync`).
2. APIs & Services → Library → enable the **Google Sheets API**. That's the only
   one needed — no Drive scope, since there are no images to fetch.
3. IAM & Admin → Service Accounts → Create (no roles needed). Open it → Keys →
   Add key → JSON. Keep that file private; it never goes in this repo.
4. Share the Sheet with the service account's email
   (`…@….iam.gserviceaccount.com`) — **Viewer** is enough.

## 3. GitHub repo secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

- **`GCP_SA_KEY`** — the *entire contents* of the JSON key file.
- **`SHEET_ID`** — the long id from the sheet URL:
  `docs.google.com/spreadsheets/d/`**`<this part>`**`/edit`.

No PAT, no Cloudflare secrets. The workflow pushes with its own `GITHUB_TOKEN`
via `permissions: contents: write`. Until both secrets exist the workflow exits
cleanly with a "not set yet" message rather than failing every 15 minutes.

## 4. Test it

1. **Local dry run** against a copy of the Sheet, on a test branch:
   ```bash
   export GCP_SA_KEY="$(cat service-account.json)" SHEET_ID="…"
   python scripts/sync_sheet.py && git diff data.json
   ```
2. Edit a row, then Actions → this workflow → **Run workflow** — confirm the
   commit lands and the live card updates.
3. Confirm a project with an empty **Links** cell renders
   "no deliverables linked yet" instead of breaking.
4. Confirm nothing outside `in_progress` changed: `git diff data.json` should
   only ever touch that one key.

## Notes

- **Cadence** is every 15 minutes (plus the manual button). GitHub's scheduler
  runs on a best-effort queue, so a tick can land a few minutes late. To change
  it, edit the `cron` line in the workflow.
- **If 15 minutes ever feels slow**, the upgrade path is a small Apps Script
  `onEdit` trigger firing a `repository_dispatch` at the repo, which makes edits
  land in seconds. Not built — worth knowing it exists.
- **No `[skip ci]`** in the sync commit message: Cloudflare honors it by skipping
  the deploy, which would keep synced changes off the live site. Pushes made
  with `GITHUB_TOKEN` don't retrigger Actions anyway.
- The sync **refuses to write** if the `Cards` tab has no project rows, so a
  broken read or a renamed tab can't blank the section. To genuinely empty it,
  edit `data.json` by hand.
