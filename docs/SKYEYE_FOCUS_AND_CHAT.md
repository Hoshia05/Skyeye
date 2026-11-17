# SkyEye task: Focus instructions + reading Allmind's chat log

**For the SkyEye Claude session.** Read `CLAUDE.md` first: hard rules, deploy steps and conventions all apply. Talk to Stan in English, in plain words. Program UI text stays English as it is now; Claude prompts stay Korean.

There are two parts. Part A is the main job. Part B reads a new chat log that Allmind (formerly Stream Desk, Stan's stream-PC dashboard) will write into the existing markers file. The Allmind side is specified separately in `ALLMIND_CHAT_LOG.md`, and section B1 below repeats the format.

---

## Part A: Focus instructions

### Goal
Today Claude decides alone what is "good". Stan wants to steer an analysis per stream, for example:
- Ace Combat: "추락하는 장면(특히 HOTAS 조작 문제로 박는 장면)과 그 리액션을 전부 찾아줘"
- Trickal: "감정적인 반응(울컥, 감동, 과몰입)을 더 꼼꼼히 봐줘"

This is also step 1 toward a later feature: one compilation across several streams, e.g. all crashes from 5 Ace Combat streams. So focus matches must be **found completely**, not just ranked higher.

### A1. Focus presets (Settings)
- New config key, written by `render_config` in `app.py`. Add it to the UI save body in the same change, as the conventions say.
  ```yaml
  focus_presets:
    - name: "Ace Combat"
      keywords: ["에컴", "에이스 컴뱃", "ace combat"]   # matched against the recording name, case-insensitive
      text: |
        추락하는 장면(특히 HOTAS 조작 문제로 박는 장면)과 그 리액션을 전부 찾아줘.
  ```
- In the Settings page, add a "Focus presets" card: a list of presets (name, keywords as comma-separated, multi-line instruction), with Add and Remove. Same style as the existing cards.

### A2. Choosing a focus when analyzing
- **Analyze selected** (Recordings page, `queue()` in `ui/index.html`) opens a small dialog before queueing. Use a modal, not `window.confirm`. It contains:
  - **Focus** dropdown: `Auto (match by title)` (default), `None`, then each preset by name.
  - **Extra instructions**: an optional free-text box. It's added on top of the preset, or used alone.
  - **Auto** resolves per recording: the first preset whose keyword appears in the file name, otherwise none. Show which preset Auto picked for each selected file before Stan confirms.
- `/api/jobs` accepts `focus: {"preset": str|null, "text": str}` per job, resolved per recording on the server side so that Auto works per file. Store it on the job.
- The worker puts it into the run config: `run_cfg["focus"] = {...}` in both the Drive branch and the local branch, around `app.py` lines 411 and 445, where `run_cfg` / `dict(cfg, _open_report=False)` are built.
- **Re-run Claude** (`redo()` and `/api/result/redo`) opens the same dialog, pre-filled with the report's current focus, so Stan can re-run a stream with a different focus. Transcripts are reused as now. This is how he'll redo his existing Ace Combat reports for crashes.

### A3. Prompts
Inject the focus into **both** passes. If the focus is empty, the prompts must stay exactly as they are now.
- **Pass 1** (`twopass.scan_system`): add a section after `## 찾을 것`:
  ```
  ## 이번 방송 특별 지시 (스트리머 요청 — 최우선)
  <preset text + extra text>
  - 위 지시에 해당하는 장면은 재미 정도와 상관없이 **하나도 빠짐없이** 후보로 올리고 focus=true로 표시하세요.
  - 평소처럼 다른 재밌는 장면도 계속 찾으세요.
  ```
  Add an optional boolean `focus` to the candidate item schema in `SCAN_TOOL`.
- **Pass 2** (`scout.system_prompt`, used by `judge_system`): add the same section, with these rules:
  - Focus matches are never dropped for being "not funny enough". Keep them, give an honest score, and set `focus_match: true`.
  - They **don't count** toward the `highlights_per_hour` target.
  - Several focus matches very close together (e.g. 3 crashes within 1 minute) may be one pick only if it's one continuous scene. Otherwise keep them separate, because compilations need each instance.
- Add an optional boolean `focus_match` to the judge item schema (`_item` / `JUDGE_TOOL`), and keep it through `scout.normalize`.
- `_merge_candidates`: when merging, a merged candidate is `focus=true` if any part was.

### A4. Saving and showing it
- `meta.json` gets `"focus": {"preset": "Ace Combat", "text": "<full resolved text>"}`. Empty or missing means no focus.
- **Report header:** a chip such as `🎯 Focus: Ace Combat`, with the full text on hover.
- **Picks tab:** a `🎯 Focus` filter chip next to the category chips, showing only `focus_match` picks. Matching cards get a small 🎯 badge.
- **Editor page** (`editor_page.py`): carry `focus_match` into the data and show the same badge and filter.
- **Report list** (`result_summary`): include the focus preset name so the list can show it.

### A5. Testing
- Use the stand-in fixture approach from `CLAUDE.md`, not Stan's real files or webhook.
- Unit-check the prompt builders: with no focus, the output is byte-identical to the current prompts. With a focus, the section appears in both passes.
- Check that the Auto preset matching works on Korean file names such as `2026-10-18 에컴 pt.6 (END).mp4`.
- Run `py_compile` and the `node --check` step, then deploy and verify file sizes. Stan has to **fully quit and restart SkyEye** after the deploy: Settings → Quit, then reopen. It keeps running in the background, and a stale process caused a 404 once already.

---

## Part B: Read Allmind's chat log

### B1. The format (written by Allmind; see `ALLMIND_CHAT_LOG.md`)
Allmind extends the existing `<recording>.markers.json`. The `format` string stays `"vodscout-markers"` on purpose. It changes only:
- `"version": 2`
- the file is now written for **every** recording that has chat or marks, not only when there are marks
- an optional `"chat"` array:

```json
"chat": [
  {"t": 1234.6, "type": "chat",     "nick": "시청자1", "role": "viewer",  "text": "ㅋㅋㅋㅋㅋ"},
  {"t": 1300.2, "type": "donation", "nick": "시청자2", "role": "viewer",  "text": "추락 장인", "amount": 1000},
  {"t": 1302.0, "type": "chat",     "nick": "호시아",  "role": "streamer", "text": "아니 이거 HOTAS 문제라니까"}
]
```

- `t` = seconds from the start of the recording file, in the same clock as markers. It's **when Allmind received the message**: no delay correction is applied by Allmind.
- `role` is one of `streamer | manager | viewer`. `amount` is only on donations and is optional.
- Version 1 files (no `chat`) must keep working exactly as now.

### B2. Reading it
- `scout.read_marker_file` currently returns only markers. Add a sibling `read_chat_file(path) -> list[dict]`.
  - `drive_fetch_markers` already copies the matched file to `<work>/markers.json`, and `load_markers` already looks at `<work>/markers.json` and `<vod>.markers.json`. Read the chat from the same files.
  - Matching by name or duration (`match_marker_files`) is unchanged.
  - Note: `drive_fetch_markers` + `match_marker_files` currently pick files listed by `*.markers.json`. Files will now exist for every stream, so make sure the duration match stays strict (±5 s) and picks the closest.
- Save it to `<work>/chat.json`, a list of `[t, type, nick, role, text, amount?]`, so re-runs don't need the original file.

### B3. Chat delay
Viewers react a few seconds **after** the moment, because of stream latency plus reading and typing time.
- New config key `chat_delay_seconds` (default `6`), with a Settings field: "Chat delay — how many seconds chat lags behind you".
- Subtract it when placing chat on the timeline (`t_effective = max(0, t - delay)`). Keep the raw `t` in `chat.json`.

### B4. Using it
- **Timeline for Claude** (`scout.build_timeline`): add lines `[H:MM:SS] 💬 채팅 <nick>: <text>`, and `💰 후원 채팅 <nick>: <text>` for donations.
  - Skip `role=streamer` lines; his own chat is rarely useful and adds tokens.
  - For long streams, cap the chat lines (e.g. at most about 1 line per 5 s; keep donations always), and collapse runs of near-identical messages (`ㅋㅋㅋ` ×7 → `💬 채팅 ㅋㅋ 반응 7개`).
- **Chat bursts as signals:** compute messages per 30 s. A burst (≥ 3× the stream's median and ≥ 4 messages) becomes a line `[H:MM:SS] 📈 채팅 반응 급증 (N개/30초)`, similar to the ⚡ spikes.
- **Prompts:** `scout.system_prompt` currently says `소규모 방송이라 채팅 반응은 없습니다.` When chat exists, replace that sentence with an explanation: 💬 lines are viewer chat (delay already corrected), 💰 후원 채팅 is the donation message text, 📈 is a chat burst, and chat explains what the streamer is answering. Mention 💬/📈 in `scan_system`'s `## 입력` too. When there's no chat, leave the prompts unchanged.
- **Donation TTS:** when a donation chat exists within ±15 s of a 💰 TTS transcript line, Claude now has the exact text. Just keep both lines; no merging logic is needed.
- **Report UI:**
  - Add a thin chat-activity row to the timeline SVG (`timelineSVG` in `ui/index.html`), with "Chat" in the legend.
  - Interleave chat lines (in a different color) in the **Transcript** tab and the per-pick transcript.
  - On the **editor page**, interleave chat in each pick's "대사 보기" transcript.
- `meta.json`: add `"chat_count": N` so the report header can show `💬 N chat`.

### B5. Testing
- Make a fake `…markers.json` v2 with about 200 chat lines, a few donations and a burst. Check matching by duration, the delay shift, burst detection, the timeline lines and the prompt switch.
- Check that a v1 file and a missing file both behave exactly as before.

---

## Deliver
- Update `README.md` with a Focus section and a "Chat from Allmind" note, and `CLAUDE.md` with the pipeline line and conventions.
- Update `STREAM_DESK_MARKERS.md` with a short pointer that v2 adds `chat`, written by Allmind.
- Deploy through the device bridge and verify file sizes by listing the folder. If a file is stale, re-commit it from a new staged path.
- Tell Stan in plain words:
  - what to click
  - that he must fully quit and restart SkyEye
  - that he can now re-run his Ace Combat reports with the "Ace Combat" focus
