# Allmind task: save the stream's chat log for SkyEye

**For the Allmind Claude session** (Allmind was formerly Stream Desk; repo `Hoshia05/CentralStreamingHQ`). Talk to Stan in English, in plain words. Allmind's UI text and release notes stay Korean.

## Context
- Stan (VTuber 호시아) streams on Chzzk with two PCs.
  - The **stream PC** runs OBS and Allmind, and nothing heavy.
  - OBS records every stream locally as a multi-track MP4. Stan uploads the recordings to a Google Drive folder.
- **SkyEye**, a separate program on the main PC, downloads each recording, transcribes it, and has Claude suggest highlight moments for his YouTube editors.
- SkyEye already reads Allmind's ⭐ "Mark moment" file, `<recording>.markers.json`. The original spec is `STREAM_DESK_MARKERS.md` in the SkyEye folder: `C:\Users\arkht\Downloads\vod-scout_1\vod-scout\`.

**New:** Allmind already receives Chzzk chat for its chat panel. Save that chat, with timestamps relative to the recording, **into the same markers file**. SkyEye will use it as context, for example "what was he answering?", chat bursts, and exact donation messages. The SkyEye side is being built separately against the format below, so follow it exactly.

> If the ⭐ Mark feature from `STREAM_DESK_MARKERS.md` isn't built yet in Allmind, build the recording-tracking and file parts of it together with this. Both share the same file and the same OBS events.

---

## 1. File format: markers file version 2

Same file, same name rules as now. Keep `"format": "vodscout-markers"` exactly as it is (SkyEye looks for that string). The only changes:
- `"version": 2`
- a new `"chat"` array
- the file is written for **every recording that has at least one chat message or mark**. Before, it was written only when there were marks.

```json
{
  "format": "vodscout-markers",
  "version": 2,
  "recording": {
    "file": "2026-10-20 20-00-03.mp4",
    "started_at": "2026-10-20T20:00:03.120+09:00",
    "duration": 14520.4,
    "obs_chapters": true
  },
  "markers": [
    { "t": 1234.5, "label": "", "created_at": "2026-10-20T20:20:37+09:00" }
  ],
  "chat": [
    { "t": 1230.6, "type": "chat",     "nick": "시청자1", "role": "viewer",   "text": "ㅋㅋㅋㅋㅋ" },
    { "t": 1290.2, "type": "donation", "nick": "시청자2", "role": "viewer",   "text": "추락 장인", "amount": 1000 },
    { "t": 1302.0, "type": "chat",     "nick": "호시아",  "role": "streamer", "text": "아니 이거 HOTAS 문제라니까" }
  ]
}
```

**Fields of each chat item:**
- **`t`** (required): seconds from the start of the recording **file**, float, at the moment Allmind **received** the message. Don't add or subtract any delay; SkyEye corrects for stream delay itself.
- **`type`**: `chat` or `donation`. Optionally `subscription` if Allmind already sees those. Skip system or notice messages.
- **`nick`**: the display nickname. Don't store user IDs or channel IDs; they aren't needed.
- **`role`**: `streamer` | `manager` | `viewer`, mapped from Chzzk's user role (streamer, channel manager, everyone else).
- **`text`**: the message text. Keep emoji codes or tags as Chzzk sends them, or strip them to plain text; either is fine. Max 500 characters.
- **`amount`**: donations only, if known.

`markers` may be an empty array. `chat` may be empty if there were marks but no chat.

## 2. Timing (the important part)

`t` must line up with the video file, so measure it the same way the marks do.
- **Start:** when obs-websocket sends `RecordStateChanged` with `OBS_WEBSOCKET_OUTPUT_STARTED`, store a monotonic clock value (not wall-clock, so clock adjustments don't matter).
- **For each message:** `t = (now_monotonic - start) - total_paused_time`.
- **Pauses:** on `…_PAUSED` / `…_RESUMED`, accumulate the paused time. Messages that arrive **while paused** get the pause-start `t` (they belong to that cut point).
- **Drift check:** every ~60 s while recording, call `GetRecordStatus` and compare `outputDuration / 1000` with your computed elapsed time. If they differ by more than 1 s, adjust an offset so later messages use OBS's clock. This is cheap: one request per minute.
- **Messages received when OBS isn't recording** are not written. The chat panel still shows them as usual.

## 3. Writing the file
- While recording, keep the working file `vodscout-markers-<started_at yyyyMMdd-HHmmss>.json` in the OBS recording folder, as the marks spec already says.
- **Save it every ~15 s** if anything new arrived (and immediately on a ⭐ press). Write a temp file, then rename it over the old one, so a crash never leaves a broken file.
- Keep chat in memory as a list. A 4-hour stream with small chat is a few thousand lines (well under 1 MB), so there's no need for a database.
- **On `…_STOPPED`:** fill in `recording.file` (from `outputPath`) and `recording.duration` (as in the marks spec: active recording time, minus pauses). Do a final save, then rename the file to `<recording base name>.markers.json` next to the video.
- Toast (Korean UI), e.g. `💬 채팅 1,234개 · ⭐ 3개 저장됨 — 영상과 함께 ….markers.json 도 업로드하세요`.
- **No chat and no marks** → no file, as before.

## 4. Edge cases
- **Chat connection drops mid-stream:** reconnect as the panel already does. Keep what you have. Optionally add one `{"t": …, "type": "gap", "text": "chat disconnected 20:31–20:33"}` item so SkyEye knows chat was missing. SkyEye will ignore unknown types safely.
- **OBS disconnects mid-stream:** keep saving chat using the monotonic clock. When OBS reconnects, re-sync with `GetRecordStatus`. If recording stopped while disconnected, finalize using the last known duration.
- **Several recordings in one session:** one file per recording. Chat between recordings is not saved.
- **Allmind restarted mid-recording:** on startup, if `GetRecordStatus` says recording is active, start a new working file with the start time from `now - outputDuration`. Merge into the existing working file for that recording if it exists.
- **Performance:** the stream PC must stay light. Do no extra polling beyond the 60 s drift check, and no per-message disk writes.

## 5. Settings
Add one toggle in Allmind's settings: **채팅 로그 저장 (SkyEye용)**, on by default. When it's off, behavior is exactly as before (marks only).

## 6. How to test
1. Start an OBS recording.
2. Send a few chat messages from another account or a test chat, at about 0:10, 0:30 and 0:50. Pause the recording for about 10 s at around 0:40, if pause is available.
3. Stop the recording after about 1:10.
4. Check `<name>.markers.json` next to the video:
   - `version: 2`
   - the chat items have `t` close to the positions in the **video** (the pause is excluded)
   - `recording.duration` is about 60–70
5. Check that a recording with no chat and no marks writes no file.
6. Check that a file with marks but no chat still has `"chat": []` and version 2.
7. Tell Stan in plain words: upload the `….markers.json` with the video, as before. It now appears after every stream, not just when he pressed ⭐.
