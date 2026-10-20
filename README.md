# SkyEye

SkyEye finds the parts of your stream recordings that are worth editing and makes a report you can hand to your editors.

It pulls recordings from your Google Drive folder (or this PC) and transcribes your mic, Discord and donation tracks separately on your GPU. It marks loud reactions and donations, then has Claude pick highlight segments with times, scores, reasons and notes for the editor.

## Start

**Double-click `SkyEye.bat`.** The first time, it installs everything (a few minutes) and puts a **SkyEye** shortcut on your desktop. After that, just use the shortcut.

If Python isn't installed, it tells you. Install Python with `winget install Python.Python.3.12` and start SkyEye again.

## First-time setup (inside the app → Settings)

1. **Claude**: choose how Claude runs.
   - **My Claude subscription** (the default): click *Install Claude Code*. A window opens that installs Claude Code (Anthropic's official tool) and asks you to log in with your Claude account. Choose your subscription, not Console. There's no extra cost, but the analysis counts toward your plan's usage limits, the same ones your chats use.
   - **API key**: pay per use from prepaid credits at platform.claude.com. It doesn't touch your plan's limits.
2. **Google Drive**: click *Connect Google Drive* if it isn't connected already.
3. **Audio tracks**: go to Recordings and click **Tracks** on any recording. Once it's ready, click *Set up tracks*. You'll get a player and a "how much sound" bar for each track, and you pick what each one is. Then save.
4. **About your channel**: add characters, running jokes, nicknames and what your editors like. This makes the biggest difference to the picks.
5. **Learn from my YouTube channel**: paste your channel link and click *Learn from my channel*. SkyEye reads your uploaded edits and Shorts (titles, view counts, chapters and auto-subtitles) and has Claude write a **style profile**: which kinds of moments your channel turns into videos, what performs best, your title patterns and running jokes. Every stream analysis then looks for moments that fit that style. You can edit the profile yourself, and click *Learn again* after you've uploaded more videos.

## Everyday use

- **Recordings**: your Drive folder (or *This PC*), with each recording marked *New*, *Transcribed* or *Analyzed*. Tick a few and click **Analyze selected**, or use *Select new*.
- **Jobs**: live progress: download → extract audio → transcribe → audio signals → Claude → report. You can cancel, and you can queue as many as you like.
- **Reports**: every analyzed stream. Open one to see:
  - a **timeline** of the stream showing your voice level, game level, loud reactions, donations, chapters, and every pick as a colored block you can click
  - the picks, which you can filter by main edit / Shorts, category and score
  - a transcript for each pick, plus a searchable full transcript
  - **YouTube archive** link: paste the stream's archive link and every timestamp becomes a clickable YouTube link, in the app, the report and Discord
  - **Re-run Claude**, which re-analyzes without re-transcribing (for example after you improve the channel description)
- **Review mode** (on the Picks tab of a report): every pick has a short **preview clip**, so you can judge it in seconds without opening the stream.
  - Press **K** to keep, **S** to skip, **Space** to play, **↑/↓** to move, **U** to undo. After each decision it jumps to the next pick and starts its preview.
  - **Send to editors** builds one web page with only the moments you kept (summary, timeline, every moment with its clip playing inside), puts it online (see *Editor page* below) and posts a single Discord card with the link. If you haven't set up the Editor page, it posts the card with a clip per moment instead.
  - Your Keep/Skip decisions are remembered, and future analyses use them as examples of your taste.
  - Preview clips are kept for 3 months, then removed automatically to save space (reports and transcripts stay).
- **Focus: tell Claude what to look for** (Settings → Focus presets). When you click **Analyze selected** (or **Re-run Claude** on a report), a small window asks what to focus on. Claude then finds **every** matching moment, even ones that aren't funny, marks them 🎯 in the report (and on the editors' page), and they don't count toward the "picks per hour" target.
  - **Auto (match by title)**, the default, picks the first preset whose keywords appear in the recording's file name (for example `에컴` → *Ace Combat*), and the window shows what it picked for each file. Choose **None**, or a specific preset, to override it. The *Extra instructions* box adds a one-off instruction on top (or works alone).
  - Presets ship with two examples (*Ace Combat*: every crash and the reaction; *Trickal*: emotional reactions). Edit or add your own in Settings; each has a name, file-name keywords (comma-separated) and the instruction.
  - **Re-run Claude** is how you redo an old stream with a focus: the transcript is reused, so it's quick. The picks are rebuilt, so earlier Keep/Skip choices only stay on moments whose times didn't change. The report header shows `🎯 Focus: …`, and the Picks tab has a **🎯 Focus** filter.
- **Chat from Arkbird** (optional): if Arkbird saves the stream's chat into the `….markers.json` file you already upload (version 2, see `STREAM_DESK_MARKERS.md`), SkyEye reads it as extra context: what you were answering, exact donation messages, and 📈 chat bursts (a sudden pile of ㅋㅋㅋ). Chat appears in the report (a Chat row on the timeline, and chat lines in the transcripts, also on the editors' page). Your own chat lines are skipped. Viewers react a few seconds after what happens, so SkyEye moves chat earlier by **Chat delay** (Settings → Chat from Arkbird, default 6 s). Without a chat file, nothing changes.
- **How the picks are found**: in **two passes**. A fast model scans the stream in 15-minute pieces and lists many possible moments, then the main model reads each candidate with its context and keeps the real ones with tight start/end times.
- **Discord** (Settings → Discord): paste a webhook link for your crew's channel. In Discord: right-click the channel → *Edit Channel* → *Integrations* → *Webhooks* → *New Webhook* → *Copy Webhook URL*.
  - **Nothing is posted automatically.** After an analysis, go through the picks in review mode and press **Send to editors**. Only then does SkyEye post a card with the stream summary, the moments you kept (times link to YouTube if you set the archive link) and the report file.
  - **Video clips (only without the Editor page):** under the card, each pick gets its own message with a **playable clip** of that moment (2 s before to 2 s after, up to 60 s), so editors can watch it right in Discord. The clips are cut from the review previews and fit the server's upload limit (10 MB normally; pick 50 or 100 MB in Settings if the server is boosted). They're deleted from this PC once posted.
  - You can choose who gets pinged (nobody, @here or a role).
- **⭐ Marks from Stream Desk**: moments you mark during the stream (saved as chapters inside the recording, or in a `….markers.json` file uploaded to the same Drive folder) are always included as candidates and shown as ★ on the timeline. See `STREAM_DESK_MARKERS.md`.

- **Editor page** (Settings → Editor page): SkyEye uploads the editors' page and the clips of the moments you kept to a free **Cloudflare R2** bucket and posts only the link to Discord. Each stream gets its own long, hidden folder, so only people with the link can open it. One-time setup in Cloudflare: turn on R2, create a bucket, allow public access (r2.dev), add a lifecycle rule that deletes objects after a while (90 or 180 days), and create an API token with *Object Read & Write* for that bucket. Then paste the Endpoint URL, bucket name, Public URL, Access Key ID and Secret into Settings and click *Test connection*. The uploaded clips are small 360p copies made once when you send (pick *Original* under *Clip quality* for the 480p previews); a 4-hour stream with about 20 kept moments is roughly 70-100 MB instead of ~215 MB. Sending again later updates the same link, and moments you un-kept are removed from the bucket. A copy of the page is also saved in the report's folder (`editor_page`).

**Your recordings on Google Drive are never deleted, moved or changed.** SkyEye only reads them. To process a recording it downloads a temporary copy to this PC, and by default removes that copy afterwards to save space (Settings → *Clean up this PC*). It never writes anything to your Drive; reports reach your editors through Discord.

SkyEye keeps running in the background until you click **Quit SkyEye** (Settings → App), so it no longer goes to sleep on its own. Reopening the shortcut brings its window back. While a job is running, your PC won't go to sleep (the screen can still turn off).

## Files you should keep private

`api_key.txt` (Claude key, if you use one), `rclone.conf` (Drive login), `discord_webhook.txt` (anyone with it can post in your channel) and `r2_keys.json` (can upload to your Cloudflare bucket).

## Troubleshooting

- **The app doesn't open**: look at `app.log` in this folder.
- **"GPU load failed … falling back to CPU"**: update your NVIDIA driver, then run `setup.bat` again.
- **Drive errors about login or token**: go to Settings → Google Drive → *Log in again*.
