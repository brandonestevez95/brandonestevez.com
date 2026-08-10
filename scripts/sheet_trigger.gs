/**
 * Sheet → GitHub sync trigger for brandonestevez.com.
 *
 * This file is the canonical copy; it runs in Google Apps Script, not in the
 * repo. Install (one time, ~2 minutes):
 *
 *   1. Open the Sheet → Extensions → Apps Script, paste this file in.
 *   2. Project Settings (gear icon) → Script Properties → add:
 *        GITHUB_TOKEN = a fine-grained GitHub PAT scoped to ONLY this repo
 *                       with "Contents: Read and write" permission
 *                       (that's what the repository_dispatch API requires).
 *   3. Triggers (clock icon) → Add Trigger:
 *        function: onSheetEdit · source: From spreadsheet · type: On edit.
 *      This must be an INSTALLABLE trigger added here — a plain onEdit(e)
 *      simple trigger is not allowed to call external APIs like GitHub's.
 *   4. Authorize when prompted. Done — every edit now lands in the repo
 *      within about a minute.
 *
 * How it works: each edit resets a one-shot timer DEBOUNCE_SECONDS out, so a
 * burst of edits (typing a whole row, ticking several checkboxes) coalesces
 * into a single repository_dispatch after the last edit — one workflow run,
 * one commit, instead of one per keystroke.
 */

const OWNER = 'brandonestevez95';
const REPO = 'brandonestevez.com';
const EVENT_TYPE = 'sheet-edit'; // must match .github/workflows/sheet-sync.yml
const DEBOUNCE_SECONDS = 60;

function onSheetEdit(e) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return; // another edit is already rescheduling
  try {
    deleteDispatchTriggers_();
    ScriptApp.newTrigger('sendDispatch')
      .timeBased()
      .after(DEBOUNCE_SECONDS * 1000)
      .create();
  } finally {
    lock.releaseLock();
  }
}

function sendDispatch() {
  deleteDispatchTriggers_(); // clean up the one-shot timer that fired us

  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add GITHUB_TOKEN in Project Settings → Script Properties');

  const res = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + OWNER + '/' + REPO + '/dispatches',
    {
      method: 'post',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
      },
      contentType: 'application/json',
      payload: JSON.stringify({ event_type: EVENT_TYPE }),
      muteHttpExceptions: true,
    }
  );

  // Success is 204 No Content. Anything else: log it, and the workflow's
  // 30-minute cron picks up the change anyway.
  if (res.getResponseCode() !== 204) {
    console.error('Dispatch failed: ' + res.getResponseCode() + ' ' + res.getContentText());
  }
}

function deleteDispatchTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'sendDispatch') ScriptApp.deleteTrigger(t);
  });
}
