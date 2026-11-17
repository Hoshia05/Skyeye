# Stream Desk feature request: "Mark moment" for SkyEye

> **Update (2026-10-20):** Stream Desk is now called **Allmind** (briefly Arkbird). The same file gets a **version 2** that also holds the stream's chat: an optional `"chat"` array, and the file is written for every recording that has chat or marks (the `"format": "vodscout-markers"` string stays the same). Allmind writes it (spec: `ALLMIND_CHAT_LOG.md`) and SkyEye reads it (`scout.read_chat_file`). Version 1 files without chat keep working exactly as described below.

**Context for the Stream Desk developer (Claude session):** Stan (VTuber 호시아) streams on Chzzk with a two-PC setup. He games and talks on the **main PC**. The **stream PC** runs OBS and Stream Desk. OBS records every stream locally (multi-track MP4), and Stan uploads the recordings to a Google Drive folder. A separate program on the main PC, **SkyEye**, later downloads each recording, transcribes it and suggests "fun points" for YouTube edits.

This feature lets Stan press a button **during the stream** when something funny happens. SkyEye then treats those moments as guaranteed highlight candidates. SkyEye already supports everything described below; only the Stream Desk side needs building.

---

## 1. What the user sees

- **A big "⭐ Mark moment" button** in Stream Desk, visible while OBS is recording.
  - Pressing it gives brief feedback (flash, toast or counter "⭐ 3"). No dialogs: Stan is live and must not be interrupted.
- **A small list of this recording's marks** (time + optional note). Stan can type a short note later (for example "디코 친구 개웃김"), never required at press time.
- **It must be pressable from where Stan sits, which is the main PC, not the stream PC.**
  - Serve a tiny page on the LAN (for example `http://<stream-pc>:<port>/mark`) with one huge button. He can then mark from his phone or iPad.
  - Expose `POST /api/mark` (optional JSON body `{"label": "..."}`). A hotkey on the main PC (AutoHotkey, a Stream Deck key, etc.) can then call it with one HTTP request.
  - Respond quickly: `200 {"ok": true, "t": <seconds>, "count": <n>}`, or `409 {"ok": false, "reason": "not recording"}`.
- If OBS isn't recording, show "Not recording", and don't create a mark.

## 2. On every press

Use **obs-websocket v5** (OBS 28+ ships it; Stream Desk may already be connected).

1. Send `GetRecordStatus`. If `outputActive` is false, stop and show "not recording".
2. Compute the **mark time**: `t = outputDuration / 1000`, in seconds from the start of the recording file. Use OBS's value, not wall-clock time, because it is correct even if the recording was paused.
3. Send `CreateRecordChapter`:
   - Include `chapterName` only if Stan typed a note at press time; otherwise omit it, and OBS names it "Unnamed N".
   - This writes the mark **inside the recording file**, so it survives renaming and needs no extra upload.
   - It needs **OBS 30.2+ with Recording Format = "Hybrid MP4"**. If the request fails (older OBS, or a different format), ignore the error silently: the markers file below still works.
4. Append the mark to the **markers file** (section 3) and save it immediately, so a crash doesn't lose marks.

## 3. The markers file (backup, and holds notes)

SkyEye reads this exact format:

```json
{
  "format": "vodscout-markers",
  "version": 1,
  "recording": {
    "file": "2026-10-16 20-00-03.mp4",
    "started_at": "2026-10-16T20:00:03.120+09:00",
    "duration": 7265.4,
    "obs_chapters": true
  },
  "markers": [
    { "t": 1234.5, "label": "", "created_at": "2026-10-16T20:20:37+09:00" },
    { "t": 2950.0, "label": "디코 친구 개웃김", "created_at": "2026-10-16T20:49:13+09:00" }
  ]
}
```

- **`t`** (required): seconds from the start of the recording (from step 2 above). A float is fine.
- **`label`**: an optional note, editable later in the list. SkyEye passes it to Claude as a hint.
- **`recording.duration`** (important): the **final length of the recording in seconds**. Stan renames videos before uploading (for example to "2026-10-16 저챗.mp4"), and SkyEye matches a markers file to its video by this length, within ±5 s.
  - Track it as wall-clock time from `RecordStateChanged: OBS_WEBSOCKET_OUTPUT_STARTED` to `…_STOPPED`, **minus any paused time** (`…_PAUSED` / `…_RESUMED`). Alternatively, keep the last `outputDuration` from `GetRecordStatus` and poll it at least every second near the end.
- **`recording.obs_chapters`**: whether `CreateRecordChapter` succeeded (informational).

**Where it goes and what it's called:**
- **While recording:** keep it in the OBS recording folder as `vodscout-markers-<started_at as yyyyMMdd-HHmmss>.json`, rewritten after every press. If a recording has **no** marks, don't create a file.
- **When recording stops:** the `…_STOPPED` event includes `outputPath`. Fill in `recording.file` and `recording.duration`, then rename the file to **`<recording base name>.markers.json`** next to the video. For example `2026-10-16 20-00-03.mp4` → `2026-10-16 20-00-03.markers.json`.
- Show a toast like "⭐ 5 marks saved — upload `….markers.json` with the video".

**Uploading:** Stan puts the markers file in the **same Google Drive folder** as the recording. It can keep its original name even if he renames the video. SkyEye looks at every `*.markers.json` in that folder and finds the right one by name or by length.

## 4. Edge cases

- **OBS disconnects mid-stream:** keep the marks you have; reconnect and continue. Never lose the file.
- **Several recordings in one session** (stop and start again): one markers file per recording.
- **Double presses within 3 s:** keep both. SkyEye merges marks that are close together.
- **Recording paused:** `outputDuration` already excludes paused time, so nothing special is needed for `t`.
- **Running on the stream PC:** keep it light. Stan wants that PC to run only OBS and Stream Desk, so there should be no polling heavier than about 1/s while recording.

## 5. Recommended OBS setting (tell Stan)

Settings → Output → Recording → **Recording Format: Hybrid MP4** (OBS 30.2 or newer).
- Marks then live inside the video file, so the markers file is only a backup.
- After switching, **check that all 5 audio tracks still appear** in a test recording. SkyEye needs them separate.

## 6. How to test

1. Start recording.
2. Press Mark at about 0:10, from the LAN page on a phone at about 0:30, and send `POST /api/mark` with `{"label":"test"}` at about 0:50.
3. Stop after about 1:10.
4. Check that `<name>.markers.json` exists next to the video, with 3 marks at about 10/30/50 s and `duration` of about 70.
5. If using Hybrid MP4, check the chapters are inside the file: `ffprobe -show_chapters <file>.mp4` should list the 3 chapters.
6. Rename the video, upload both files to the Drive folder, and analyze it in SkyEye. The report should mention "⭐ 3 moment(s) marked during the stream", and picks should appear near those times.
