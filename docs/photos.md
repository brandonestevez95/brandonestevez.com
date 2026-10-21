# Photos — bio and project cards

`scripts/fetch_drive_photos.py`, run by `.github/workflows/fetch-drive-photos.yml`
every hour and from the **Run workflow** button, pulls every photo on the site,
resizes it, commits it, and Cloudflare deploys it.

## Project cards

Each card picks its photo in this order:

1. **A photo in the project-photos Drive folder** whose filename is a run of
   whole words from the project's title. Case, punctuation and extension don't
   matter:

   | Project title | Any of these filenames work |
   |---|---|
   | Mapping out the biggest war on the planet: between ants | `ants.jpg`, `biggest war.png`, `Mapping out the biggest war.jpeg` |
   | Rediscovering Raleigh's Lost Freedmen Villages. | `freedmen villages.jpg`, `lost-freedmen.jpg` |

   A name that fits two titles (`raleigh.jpg`) is skipped with a warning in the
   Action log, so add more words. If two files fit one project, the longer name
   wins.
2. **The link preview of its first deliverable link** — the same image a text
   or Slack message shows when you paste the link. Every StoryMap has one, so
   this needs no work: add a link in the Sheet and the photo follows within the
   hour.
3. **Nothing** — the card keeps its "Photo needed" gradient.

So to **override** a StoryMap's preview, or give a link-less card a photo, drop
an image into the Drive folder. To go back to the preview, delete it.

Photos are resized to 1600px on the long side and saved as JPEG. Results go to
`assets/projects/`, and `assets/projects/photos.json` maps each title to its
photo. That map is separate from `data.json` because the sheet sync rewrites
`projects[]` every 15 minutes and would drop any field it didn't write.

**Renaming a project in the Sheet** detaches its photo until the next hourly
run re-matches it. A Drive filename that's no longer part of the new title
stops matching.

### One-time setup for the Drive folder

Link previews work with no setup. Drive overrides need:

1. A Drive folder for project photos, shared with the service account's email
   (the same one the Sheet and bio photos use) as **Viewer**.
2. Repo → Settings → Secrets and variables → Actions → New secret
   **`PROJECT_PHOTOS_FOLDER_ID`** — the id from the folder URL:
   `drive.google.com/drive/folders/`**`<this part>`**.

## Bio polaroids

Drive folder `BIO_PHOTOS_FOLDER_ID` → `assets/bio/`. Files must be named one of
the twelve stems in `EXPECTED` in the script (`kid-worldmap`, `tedx-photo`, …),
which match the `<img>` tags in `index.html`. Replacing a photo means uploading
one with the same name. Adding a new one means adding its name to `EXPECTED`
and a `.polaroid` to the markup.

## Running it locally

```bash
python3 scripts/fetch_drive_photos.py
```

With no environment variables it does link previews only. Export `GCP_SA_KEY`
plus the folder ids to include Drive.
