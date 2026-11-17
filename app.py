#!/usr/bin/env python3
"""
SkyEye — desktop GUI.

Runs a small local web server (127.0.0.1 only) and opens it in an app-style
Edge/Chrome window. All the real work is done by scout.py.
Start it with "SkyEye.bat".
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("SKYEYE_PORT", "8765"))

# pythonw has no console: send output to a log file instead
if sys.stdout is None or sys.stderr is None or "pythonw" in Path(sys.executable).name.lower():
    _lf = open(HERE / "app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _lf

import yaml  # noqa: E402

import scout  # noqa: E402
import learn  # noqa: E402
import discord_post  # noqa: E402
import publish  # noqa: E402

NOWIN = scout.NOWIN
LAST_PING = time.time()
STATE_FILE = HERE / "app_state.json"          # history of analyzed streams, added local files

# ------------------------------------------------------------------ state
_state_lock = threading.Lock()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"history": [], "local_files": []}


def save_state(st: dict):
    with _state_lock:
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def remember_work(work: Path):
    st = load_state()
    p = str(work)
    if p not in st["history"]:
        st["history"].append(p)
        save_state(st)


# ----------------------------------------------------------------- config
def read_cfg() -> dict:
    return scout.load_config()


def _q(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _num(v, default: float) -> float:
    try:
        x = float(v)
        return int(x) if x == int(x) else x
    except (TypeError, ValueError):
        return default


def _block(text: str) -> str:
    text = (text or "").rstrip()
    if not text:
        return '""'
    return "|\n" + "\n".join("  " + ln if ln else "" for ln in text.splitlines())


def _focus_yaml(presets: list[dict]) -> str:
    """focus_presets as readable YAML (instruction text as a block, so it can be edited by hand)."""
    if not presets:
        return "focus_presets: []"
    out = ["focus_presets:"]
    for p in presets:
        out.append(f"  - name: {_q(p['name'])}")
        out.append(f"    keywords: {_q(p['keywords'])}")
        txt = (p.get("text") or "").strip("\n").rstrip()
        if txt and not txt.splitlines()[0][:1].isspace():
            out.append("    text: |")
            out += ["      " + ln if ln.strip() else "" for ln in txt.splitlines()]
        else:
            out.append(f"    text: {_q(txt)}")
    return "\n".join(out)


def render_config(c: dict) -> str:
    g = c.get("google_drive") or {}
    tracks = "\n".join(
        f"  - number: {int(t.get('number', 1))}\n    role: {t.get('role', 'ignore')}"
        + (f"\n    label: {_q(t['label'])}" if t.get("label") else "")
        + (f"\n    transcribe: {'true' if t['transcribe'] else 'false'}" if "transcribe" in t else "")
        for t in (c.get("tracks") or []))
    return f"""# ============================================================
#  SkyEye settings — edited by the app (Settings page).
#  You can still edit this by hand; keep it UTF-8.
# ============================================================

# --- Audio tracks --------------------------------------------
# number = order of the audio track in the recording (1 = first).
# role: mic | discord | donation | game | bgm | ignore
tracks:
{tracks}

# --- About the channel / what editors want --------------------
channel_context: {_block(c.get('channel_context', ''))}

editing_goal: {_block(c.get('editing_goal', ''))}

highlights_per_hour: {_q(c.get('highlights_per_hour', 5))}

# --- Focus presets: what to look for in a stream, picked by words in the recording's file name ---
# Settings → Focus presets. "Auto" uses the first preset with a keyword in the file name.
{_focus_yaml(scout.focus_presets(c))}

# Chat from Allmind (its markers file): viewers react a few seconds after what happens on stream
chat_delay_seconds: {_q(_num(c.get('chat_delay_seconds'), 6))}

# Learn the editing style from your existing YouTube videos (Settings → Learn from my channel)
youtube_channels: {_q(c.get('youtube_channels') or [])}
learn_videos: {_q(c.get('learn_videos', 30))}
learn_shorts: {_q(c.get('learn_shorts', 20))}
use_channel_profile: {_q(c.get('use_channel_profile', True) is not False)}
vocabulary: {_q(c.get('vocabulary') or [])}

# --- Claude ---------------------------------------------------
# claude_backend: subscription = use your Claude plan through Claude Code
#                 api          = use an API key (api_key.txt) with prepaid credits
claude_backend: {_q(c.get('claude_backend', 'subscription'))}
claude_model: {_q(c.get('claude_model', 'claude-sonnet-5-5'))}
scan_model: {_q(c.get('scan_model') or 'claude-haiku-4-5-20251001')}  # model for the quick first pass
claude_parallel: {_q(int(c.get('claude_parallel') or 3))}          # Claude calls running at the same time
make_previews: {_q(c.get('make_previews', True) is not False)}     # short preview clips for review mode
preview_keep_days: {_q(int(c.get('preview_keep_days') or 90))}     # preview clips are removed after this many days

# --- Transcription (Whisper) ----------------------------------
whisper_model: {_q(c.get('whisper_model', 'large-v3'))}
device: {_q(c.get('device', 'cuda'))}
compute_type: {_q(c.get('compute_type', 'float16'))}
whisper_batch_size: {_q(int(c.get('whisper_batch_size') or 8))}   # pieces of audio the GPU handles at once (1 = off)
low_vram: {_q(bool(c.get('low_vram', False)))}              # true = about half the GPU memory, slightly slower

# --- Performance ----------------------------------------------
background_priority: {_q(c.get('background_priority', True) is not False)}   # keep games/OBS smooth while SkyEye works
prefetch: {_q(c.get('prefetch', True) is not False)}                         # download the next queued recording early

# --- Google Drive ---------------------------------------------
google_drive:
  folder_id: {_q(g.get('folder_id', ''))}
  download_dir: {_q(str(g.get('download_dir') or ''))}
  results_dir: {_q(str(g.get('results_dir') or ''))}
  delete_after_analysis: {_q(bool(g.get('delete_after_analysis', True)))}   # only the temporary copy on THIS PC; Drive is never touched
  max_age_days: {_q(g.get('max_age_days', 14))}

# --- Discord (webhook link itself is in discord_webhook.txt) ----
discord:
  mention: {_q((c.get('discord') or {}).get('mention', ''))}                                # "", "@here", or a role ID
  top_n: {_q(int((c.get('discord') or {}).get('top_n', 12)))}
  clips: {_q((c.get('discord') or {}).get('clips', True) is not False)}           # a playable clip under each pick
  clip_max: {_q(int((c.get('discord') or {}).get('clip_max', 60)))}               # seconds per clip
  upload_mb: {_q(int((c.get('discord') or {}).get('upload_mb', 10)))}            # server upload limit: 10, 50 or 100 (boosted)
  attach_files: {_q((c.get('discord') or {}).get('attach_files', True) is not False)}

# --- Editor page hosting (Cloudflare R2; the access keys are in r2_keys.json) ---
hosting:
  endpoint: {_q((c.get('hosting') or {}).get('endpoint', ''))}
  bucket: {_q((c.get('hosting') or {}).get('bucket', ''))}
  public_url: {_q((c.get('hosting') or {}).get('public_url', ''))}
  clip_quality: {_q('original' if (c.get('hosting') or {}).get('clip_quality') == 'original' else 'small')}   # small = 360p uploads (about a third of the size), original = the 480p previews

# --- App ------------------------------------------------------
keep_running: {_q(c.get('keep_running', True) is not False)}   # stay running until you click Quit (no auto-sleep)

# --- This PC --------------------------------------------------
local_folder: {_q(c.get('local_folder', ''))}

# --- Misc -----------------------------------------------------
keep_extracted_audio: {_q(bool(c.get('keep_extracted_audio', False)))}
"""


def write_cfg(new: dict):
    text = render_config(new)
    yaml.safe_load(text)                          # validate before overwriting
    p = HERE / "config.yaml"
    if p.exists():
        shutil.copy2(p, HERE / "config.backup.yaml")
    p.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------- jobs
JOBS: list[dict] = []
JLOCK = threading.Condition()


def new_job(kind: str, source: str, name: str, mode: str = "full", **extra) -> dict:
    stages = []
    if kind == "analyze":
        if source == "drive" and mode == "full":
            stages.append("download")
        stages += ["extract", "transcribe", "signals", "claude", "report", "previews"]
    elif kind == "learn":
        stages = ["fetch", "subtitles", "claude"]
    elif kind == "discord":
        stages = ["discord"]
    elif kind == "previews":
        stages = (["download"] if source == "drive" else []) + ["previews"]
    else:
        stages = (["download"] if source == "drive" else []) + ["extract"]
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "source": source, "name": name, "mode": mode,
           "status": "queued", "stages": stages, "stage": None, "frac": 0.0, "detail": "",
           "log": [], "created": time.time(), "started": None, "finished": None, "error": None,
           "work": None, "result": None, "cancel": False, **extra}
    with JLOCK:
        JOBS.append(job)
        JLOCK.notify_all()
    return job


def public_job(j: dict, full_log=False) -> dict:
    out = {k: v for k, v in j.items() if k not in ("log", "cancel", "item")}
    out["log"] = j["log"] if full_log else j["log"][-60:]
    out["cancelling"] = j["cancel"]
    pf = PREFETCH.get(j.get("name") or "")
    if pf and j["status"] == "queued":
        out["prefetch"] = {k: pf.get(k) for k in ("pct", "detail", "done", "error", "skipped")}
    if j.get("work"):
        out["result_id"] = enc_id(j["work"])
    return out


LAST_JOB_END = [time.time()]
LAST_PRUNE = [0.0]                       # 0 = clean up old previews soon after start
PREFETCH: dict[str, dict] = {}     # recording name -> background download state


def _stay_awake(on: bool):
    """Stop Windows from going to sleep while a job runs (the screen may still turn off)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


def worker():
    while True:
        with JLOCK:
            job = next((j for j in JOBS if j["status"] == "queued"), None)
            if job is None:
                JLOCK.wait(timeout=2)
                # nothing to do for 2 minutes: give the GPU memory back (games, OBS, etc.)
                if scout._MODEL is not None and time.time() - LAST_JOB_END[0] > 120:
                    if scout.unload_whisper():
                        print("Whisper unloaded (idle) — GPU memory freed", flush=True)
                if time.time() - LAST_PRUNE[0] > 86400:     # once a day: old preview clips
                    LAST_PRUNE[0] = time.time()
                    try:
                        prune_previews(read_cfg(), _log=lambda m: print(m, flush=True))
                    except Exception as e:
                        print(f"preview cleanup failed: {e}", flush=True)
                continue
            job["status"] = "running"
            job["started"] = time.time()
        _stay_awake(True)
        try:
            run_job(job)
            job["status"] = "done"
        except scout.Cancelled:
            job["status"] = "cancelled"
            job["log"].append("Cancelled.")
        except SystemExit as e:
            job["status"] = "failed"
            job["error"] = str(e)
            job["log"].append(f"!! {e}")
        except Exception as e:
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            job["log"].append(f"!! {job['error']}")
            print(traceback.format_exc(), flush=True)
        finally:
            if not any(j["status"] == "queued" for j in JOBS):
                _stay_awake(False)
            job["finished"] = time.time()
            LAST_JOB_END[0] = time.time()
            scout.set_hooks()


def _prefetch_cancel(job):
    if job["cancel"]:
        raise scout.Cancelled()


def _prefetch_thread(job: dict, d: dict):
    st = PREFETCH[job["name"]]
    try:
        scout.drive_download(d, job["item"], quiet=True,
                             on_progress=lambda f, t: st.update(pct=f, detail=t),
                             cancel=lambda: _prefetch_cancel(job))
        st.update(done=True, pct=1.0)
    except scout.Cancelled:
        st["error"] = "cancelled"
    except SystemExit as e:                    # e.g. not enough disk space yet — it will download normally later
        st["error"] = str(e)
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"
    finally:
        st["running"] = False


def prefetcher():
    """While one recording is being transcribed (GPU), download the next one (network) at the same time."""
    while True:
        time.sleep(5)
        try:
            cur = next((j for j in JOBS if j["status"] == "running"), None)
            if not cur or cur.get("stage") in (None, "download"):
                continue
            if any(p.get("running") for p in PREFETCH.values()):
                continue
            nxt = next((j for j in JOBS if j["status"] == "queued" and j["source"] == "drive"
                        and j["name"] not in PREFETCH), None)
            if not nxt:
                continue
            cfg = read_cfg()
            if cfg.get("prefetch") is False:
                continue
            d = scout.drive_settings(cfg)
            if nxt["kind"] == "analyze" and not scout.drive_needs_download(cfg, d, nxt["item"]):
                PREFETCH[nxt["name"]] = {"done": True, "skipped": True}
                continue
            if not nxt["item"].get("Size"):
                continue
            PREFETCH[nxt["name"]] = {"running": True, "pct": 0.0, "detail": "", "done": False}
            threading.Thread(target=_prefetch_thread, args=(nxt, d), daemon=True).start()
        except Exception:
            print(traceback.format_exc(), flush=True)


def run_job(job: dict):
    def _log(msg):
        for ln in str(msg).splitlines() or [""]:
            job["log"].append(ln)
        if len(job["log"]) > 3000:
            del job["log"][:1000]

    def _prog(stage, frac, detail):
        if stage != job["stage"]:
            job["frac"] = 0.0
        job["stage"], job["detail"] = stage, detail
        if frac is not None:
            job["frac"] = frac

    scout.set_hooks(log=_log, progress=_prog, cancel=lambda: job["cancel"])
    cfg = read_cfg()

    if job["kind"] == "learn":
        job["result"] = learn.learn_channel(cfg)
        return

    if job["kind"] == "tracks":
        if job["source"] == "drive":
            d = scout.drive_settings(cfg)
            local = scout.drive_download(d, job["item"])
            job["downloaded"] = str(local)
        else:
            local = Path(job["path"])
        out_dir = HERE / "track_samples" / re.sub(r"[^\w-]+", "_", local.stem)[:60]
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        data = scout.export_track_samples(local, out_dir)
        for t in data["tracks"]:
            if t["sample"]:
                t["url"] = "/media?f=" + enc_id(t["sample"])
        job["result"] = data
        _log(f"Found {len(data['tracks'])} audio tracks.")
        return

    if job["kind"] == "discord":
        title = discord_post.post(Path(job["work"]), cfg, job.get("post_kind", "kept"),
                                  on_progress=lambda f, t: _prog("discord", f, t), log=_log)
        _log(f"Posted to Discord: {title}")
        return

    if job["kind"] == "previews":
        work = Path(job["work"])
        meta = scout.read_meta(work)
        a = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
        hs = scout.normalize(a)
        info = json.loads((work / "probe.json").read_text(encoding="utf-8"))
        local, d = None, None
        if job["source"] == "drive":
            d = scout.drive_settings(cfg)
            it = next((x for x in scout.drive_list(d) if x["Name"] == meta.get("vod")), None)
            if not it:
                raise RuntimeError(f"Couldn't find {meta.get('vod')} in the Drive folder.")
            local = scout.drive_download(d, it)
        else:
            local = work.parent / meta.get("vod", "")
        n = scout.make_previews(local, work, cfg, info, hs)
        _log(f"Made {n} preview clip(s).")
        if d and d.get("delete_after_analysis", True) and local.exists():
            scout.remove_local_copy(d, local)
        return

    redo_ai = job["mode"] == "redo_ai"
    if job["source"] == "drive":
        d = scout.drive_settings(cfg)
        it = job["item"]
        stem = Path(it["Name"]).stem
        work = d["results_dir"] / f"{stem}_scout"
        job["work"] = str(work)
        run_cfg = dict(cfg, output_dir=str(d["results_dir"]), _open_report=False, focus=job.get("focus") or {})
        status = scout.drive_status(cfg, d, it)
        local = d["download_dir"] / it["Name"]
        if scout.drive_needs_download(cfg, d, it):
            if "download" not in job["stages"]:
                job["stages"].insert(0, "download")
            if not it.get("Size"):                       # e.g. "Re-run Claude" on an old result: look it up
                it = next((x for x in scout.drive_list(d) if x["Name"] == it["Name"]), it)
                job["item"] = it
            t0 = time.time()
            pf = PREFETCH.get(it["Name"])
            while pf and pf.get("running"):              # it is already being downloaded in the background
                _prog("download", pf.get("pct", 0), (pf.get("detail") or "") + " (started early)")
                scout.check_cancel()
                time.sleep(0.5)
            local = scout.drive_download(d, it)
            run_cfg["_download_secs"] = time.time() - t0
        PREFETCH.pop(it["Name"], None)
        work.mkdir(parents=True, exist_ok=True)
        try:                                              # ⭐ marks from Stream Desk (markers file in the Drive folder)
            dur = scout.load_probe(local, work).get("duration") if (local.exists() or (work / "probe.json").exists()) else None
            scout.drive_fetch_markers(d, it, work, dur)
        except SystemExit:
            pass
        except Exception as e:
            _log(f"! couldn't check for a markers file: {e}")
        scout.cmd_run(local, run_cfg, redo_ai=redo_ai or status == "done")
        remember_work(work)
        if d.get("delete_after_analysis", True) and local.exists():
            scout.remove_local_copy(d, local)
    else:
        vod = Path(job["path"])
        work = scout.work_dir(cfg, vod)
        job["work"] = str(work)
        scout.cmd_run(vod, dict(cfg, _open_report=False, focus=job.get("focus") or {}), redo_ai=redo_ai)
        remember_work(work)
    _prog("report", 1.0, "done")
    # Nothing is posted here on purpose: Stan reviews the picks first and posts with "Send to editors".


def queue_kept_post(work: Path) -> bool:
    """'Send to editors' → queue a background Discord job for the kept list. The only way anything gets posted."""
    if not discord_post.get_webhook() or not (work / "analysis.json").exists():
        return False
    new_job("discord", "discord", f"Discord · {scout.read_meta(work).get('vod', work.name)}",
            work=str(work), post_kind="kept")
    return True


# ---------------------------------------------------------------- helpers
def enc_id(path: str) -> str:
    return base64.urlsafe_b64encode(str(path).encode("utf-8")).decode().rstrip("=")


def dec_id(s: str) -> str:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode()).decode("utf-8")


def known_works(cfg: dict) -> list[Path]:
    seen, out = set(), []
    cands = [Path(p) for p in load_state().get("history", [])]
    try:
        d = scout.drive_settings(cfg)
        if d["results_dir"].exists():
            cands += sorted(d["results_dir"].glob("*_scout"))
    except SystemExit:
        pass
    lf = cfg.get("local_folder")
    if lf and Path(lf).exists():
        cands += sorted(Path(lf).glob("*_scout"))
    for p in cands:
        k = str(p.resolve()) if p.exists() else None
        if k and k not in seen and ((p / "meta.json").exists() or (p / "analysis.json").exists()):
            seen.add(k)
            out.append(p)
    return out


def safe_work(rid: str, cfg: dict) -> Path:
    p = Path(dec_id(rid))
    if any(p.resolve() == w.resolve() for w in known_works(cfg)):
        return p
    raise PermissionError("unknown result")


def result_summary(work: Path) -> dict:
    meta = scout.read_meta(work)
    a = {}
    if (work / "analysis.json").exists():
        try:
            a = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
        except Exception:
            a = {}
    hs = scout.normalize(a) if a else []
    top = sorted(hs, key=lambda h: -h["score"])[:3]
    vod = meta.get("vod") or work.name.removesuffix("_scout")
    return {"id": enc_id(str(work)), "name": Path(vod).stem, "vod": vod, "duration": meta.get("duration", 0),
            "generated": meta.get("generated", ""), "count": len(hs),
            "shorts": sum(1 for h in hs if h.get("format") in ("쇼츠", "둘다")),
            "top": [{"title": h["title"], "score": h["score"], "s": h["s"]} for h in top],
            "summary": (a.get("stream_summary") or "")[:220], "analyzed": bool(a),
            "focus": (meta.get("focus") or {}).get("preset") or ("Custom" if (meta.get("focus") or {}).get("text") else ""),
            "focus_count": sum(1 for h in hs if h.get("focus_match")), "chat_count": meta.get("chat_count") or 0,
            "mtime": (work / "analysis.json").stat().st_mtime if a else (work / "meta.json").stat().st_mtime
            if (work / "meta.json").exists() else 0}


def chat_for_report(work: Path, meta: dict) -> dict | None:
    """Viewer chat from Allmind for the report: lines [time on video, nick, text, is_donation] + activity per 30 s."""
    chat = scout.read_saved_chat(work)
    if not chat:
        return None
    delay = meta.get("chat_delay")
    if delay is None:
        delay = _num(read_cfg().get("chat_delay_seconds"), 6)
    vc = scout.viewer_chat(chat, float(delay))
    if not vc:
        return None
    dur = float(meta.get("duration") or 0) or max(c["te"] for c in vc)
    return {"count": len(vc), "delay": delay, **scout.chat_activity(vc, dur),
            "lines": [[round(c["te"], 1), c["nick"], c["text"], 1 if c["type"] == "donation" else 0] for c in vc]}


def result_detail(work: Path) -> dict:
    meta = scout.read_meta(work)
    a = json.loads((work / "analysis.json").read_text(encoding="utf-8")) if (work / "analysis.json").exists() else {}
    sig = json.loads((work / "signals.json").read_text(encoding="utf-8")) if (work / "signals.json").exists() else {}
    tracks = meta.get("tracks") or []
    labels = {t["key"]: t for t in tracks}
    transcript = []
    by_file = {f"t{t['number']}": t["key"] for t in tracks if t.get("number")}
    for f in sorted(work.glob("*.transcript.json")):
        key = f.name.split(".")[0]
        key = by_file.get(key, key)
        try:
            for s in json.loads(f.read_text(encoding="utf-8")):
                transcript.append([s["start"], s["end"], key, s["text"]])
        except Exception:
            pass
    transcript.sort(key=lambda x: x[0])
    base = scout.report_base(work, meta)
    hs = scout.normalize(a) if a else []
    previews = {}
    for h in hs:
        k = scout.preview_key(h["s"], h["e"])
        p = scout.preview_path(work, h["s"], h["e"])
        if p.exists():
            previews[k] = "/media?f=" + enc_id(str(p))
    a.pop("candidates", None)
    return {"id": enc_id(str(work)), "meta": meta, "analysis": a, "highlights": hs,
            "signals": sig, "tracks": labels, "transcript": transcript, "dir": str(work), "chat": chat_for_report(work, meta),
            "review": read_review(work).get("decisions") or {}, "previews": previews,
            "editor_page": publish.read_published(work).get("url") or "",
            "files": {ext: (work / f"{base}.{ext}").exists() for ext in ("html",)}}


def read_review(work: Path) -> dict:
    p = work / "review.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"decisions": {}}
    except Exception:
        return {"decisions": {}}


def rebuild_taste(cfg: dict):
    """Collect every Keep/Skip Stan made into taste.json — the two-pass judge learns from it."""
    keep, skip = [], []
    for w in known_works(cfg):
        rv = read_review(w).get("decisions") or {}
        if not rv or not (w / "analysis.json").exists():
            continue
        try:
            hs = scout.normalize(json.loads((w / "analysis.json").read_text(encoding="utf-8")))
        except Exception:
            continue
        for h in hs:
            dct = rv.get(scout.preview_key(h["s"], h["e"]))
            if not dct:
                continue
            row = {"title": h.get("title", ""), "summary": h.get("summary", ""), "category": h.get("category", ""),
                   "format": h.get("format", ""), "quote": (h.get("key_quotes") or [""])[0], "t": dct.get("t", 0)}
            (keep if dct.get("d") == "keep" else skip).append(row)
    keep.sort(key=lambda r: r["t"])
    skip.sort(key=lambda r: r["t"])
    (HERE / "taste.json").write_text(json.dumps({"keep": keep[-40:], "skip": skip[-40:]}, ensure_ascii=False, indent=1),
                                     encoding="utf-8")


def export_kept(work: Path, cfg: dict) -> dict:
    """'Send to editors': only the moments Stan kept, as …_편집목록.html, then posted to Discord."""
    meta = scout.read_meta(work)
    analysis = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
    rv = read_review(work).get("decisions") or {}
    hs = [h for h in scout.normalize(analysis) if (rv.get(scout.preview_key(h["s"], h["e"])) or {}).get("d") == "keep"]
    if not hs:
        raise RuntimeError("Nothing kept yet — press Keep on the moments you want edited.")
    for i, h in enumerate(hs, 1):
        h["n"] = i
    rcfg = dict(cfg, youtube_url=meta.get("youtube_url") or "",
                youtube_offset_seconds=meta.get("youtube_offset_seconds") or 0)
    stem = Path(meta.get("vod") or work.name.removesuffix("_scout")).stem
    base = f"{stem}_편집목록"
    m = {"vod": meta.get("vod", ""), "duration": meta.get("duration", 0), "generated": time.strftime("%Y-%m-%d %H:%M")}
    a2 = dict(analysis, highlights=[], compilation_ideas=[])
    scout.write_html(work / f"{base}.html", rcfg, m, a2, hs)
    posted = queue_kept_post(work)
    return {"count": len(hs), "posted": posted, "file": str(work / f"{base}.html")}


def prune_previews(cfg: dict, _log=print) -> int:
    """Preview clips are only for reviewing: remove the ones older than preview_keep_days (default 90)."""
    days = int(cfg.get("preview_keep_days") or 90)
    cutoff = time.time() - days * 86400
    removed = 0
    for w in known_works(cfg):
        pv = w / "previews"
        if not w.name.endswith("_scout") or not pv.is_dir():
            continue
        files = [f for f in pv.iterdir() if f.is_file()]
        if files and max(f.stat().st_mtime for f in files) < cutoff:
            shutil.rmtree(pv, ignore_errors=True)
            shutil.rmtree(w / "editor_page", ignore_errors=True)      # its clips are links to the previews
            shutil.rmtree(w / "web_clips", ignore_errors=True)
            removed += 1
    if removed:
        _log(f"Removed preview clips of {removed} report(s) older than {days} days.")
    return removed


def open_path(p: Path):
    if os.name == "nt":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


PICK_SCRIPT = r"""
import sys, json
import tkinter as tk
from tkinter import filedialog
r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)
if sys.argv[1] == 'folder':
    p = filedialog.askdirectory(title='Choose a folder'); out = [p] if p else []
else:
    out = list(filedialog.askopenfilenames(title='Choose recordings',
        filetypes=[('Video', '*.mp4 *.mkv *.mov *.flv *.ts *.m4v'), ('All files', '*.*')]))
sys.stdout.write(json.dumps(out)); sys.stdout.flush()
"""


def native_pick(kind: str) -> list[str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-c", PICK_SCRIPT, kind], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, **NOWIN)
    try:
        return [str(Path(p)) for p in json.loads(r.stdout or "[]")]
    except Exception:
        return []


# ------------------------------------------------------------ Claude Code
CLAUDE = {"status": None, "t": 0.0, "busy": False}


def claude_status(refresh=False) -> dict:
    if refresh or CLAUDE["status"] is None or time.time() - CLAUDE["t"] > 60:
        if not CLAUDE["busy"]:
            CLAUDE["busy"] = True
            try:
                CLAUDE["status"] = scout.claude_code_status()
                CLAUDE["t"] = time.time()
            finally:
                CLAUDE["busy"] = False
    return CLAUDE["status"] or {"installed": False, "logged_in": False, "detail": ""}


def claude_status_bg():
    threading.Thread(target=lambda: claude_status(refresh=True), daemon=True).start()


def open_console(args: list[str]):
    """Open a visible PowerShell window (for install / login, which need the user)."""
    if os.name != "nt":
        raise RuntimeError("Only supported on Windows.")
    subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", *args],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


# ------------------------------------------------------------ Drive login
DRIVE = {"login": "idle", "message": "", "cache": None, "cache_t": 0.0}


def ensure_rclone():
    exe = HERE / "bin" / ("rclone.exe" if os.name == "nt" else "rclone")
    if exe.exists() or shutil.which("rclone"):
        return
    if os.name != "nt":
        raise RuntimeError("Install rclone first.")
    DRIVE["message"] = "Downloading rclone…"
    data = urllib.request.urlopen("https://downloads.rclone.org/rclone-current-windows-amd64.zip", timeout=120).read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.endswith("rclone.exe"))
        exe.parent.mkdir(exist_ok=True)
        exe.write_bytes(z.read(name))


def drive_connected_quick() -> bool:
    p = HERE / "rclone.conf"
    return p.exists() and "[gdrive]" in p.read_text(encoding="utf-8", errors="ignore")


def drive_login_thread():
    DRIVE["login"], DRIVE["message"] = "running", "Starting…"
    try:
        ensure_rclone()
        rc = scout.rclone_cmd()
        DRIVE["message"] = "A browser tab opened — log in to Google and click Allow."
        if drive_connected_quick():
            cmd = rc + ["config", "reconnect", "gdrive:", "--auto-confirm"]
        else:
            cmd = rc + ["config", "create", "gdrive", "drive", "scope=drive"]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=600, **NOWIN)
        test = subprocess.run(rc + ["lsd", "gdrive:", "--max-depth", "1"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, **NOWIN)
        if test.returncode == 0:
            DRIVE["login"], DRIVE["message"] = "ok", "Google Drive connected."
            DRIVE["cache"] = None
        else:
            DRIVE["login"] = "failed"
            DRIVE["message"] = "Login didn't complete. " + (r.stderr or test.stderr or "")[-300:]
    except Exception as e:
        DRIVE["login"], DRIVE["message"] = "failed", f"{type(e).__name__}: {e}"


def drive_items(cfg: dict, refresh=False) -> dict:
    if not drive_connected_quick():
        return {"connected": False, "items": []}
    d = scout.drive_settings(cfg)
    if refresh or DRIVE["cache"] is None or time.time() - DRIVE["cache_t"] > 60:
        DRIVE["cache"] = scout.drive_list(d)
        DRIVE["cache_t"] = time.time()
    items = []
    for it in DRIVE["cache"]:
        stem = Path(it["Name"]).stem
        work = d["results_dir"] / f"{stem}_scout"
        st = scout.drive_status(cfg, d, it)
        items.append({"name": it["Name"], "size": int(it.get("Size") or 0), "date": it.get("ModTime", "")[:16],
                      "status": st, "result_id": enc_id(str(work)) if st != "new" else None,
                      "downloaded": (d["download_dir"] / it["Name"]).exists()})
    return {"connected": True, "items": items}


def local_items(cfg: dict) -> dict:
    files: list[Path] = []
    lf = cfg.get("local_folder")
    if lf and Path(lf).is_dir():
        files += [p for p in Path(lf).iterdir() if p.suffix.lower() in scout.VIDEO_EXT]
    files += [Path(p) for p in load_state().get("local_files", [])]
    seen, items = set(), []
    for p in files:
        if str(p) in seen or not p.exists():
            continue
        seen.add(str(p))
        work = scout.work_dir(cfg, p)
        st = scout.work_status(cfg, work, p.stem)
        stt = p.stat()
        items.append({"name": p.name, "path": str(p), "size": stt.st_size,
                      "date": time.strftime("%Y-%m-%dT%H:%M", time.localtime(stt.st_mtime)),
                      "status": st, "result_id": enc_id(str(work)) if st != "new" else None})
    items.sort(key=lambda x: x["date"], reverse=True)
    return {"folder": lf or "", "items": items}


# ----------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "SkyEye"

    def log_message(self, fmt, *args):
        pass

    # --- plumbing
    def _ok_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, p: Path, ctype: str):
        """Serve a file with HTTP Range support, so videos can be scrubbed."""
        size = p.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if m and (m.group(1) or m.group(2)) else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if m and (m.group(1) or m.group(2)):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # --- GET
    def do_GET(self):
        global LAST_PING
        if not self._ok_host():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "ui" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                LAST_PING = time.time()
                cfg = read_cfg()
                with JLOCK:
                    jobs = [public_job(j) for j in JOBS]
                return self._json({
                    "version": scout.VERSION, "jobs": jobs,
                    "setup": {"api_key": bool(scout.get_api_key()), "drive": drive_connected_quick(),
                              "backend": scout.claude_backend(cfg),
                              "claude": CLAUDE["status"] or {"installed": None, "logged_in": None, "detail": ""},
                              "drive_login": DRIVE["login"], "drive_message": DRIVE["message"],
                              "folder": bool((cfg.get("google_drive") or {}).get("folder_id"))},
                })
            if u.path == "/api/config":
                cfg = read_cfg()
                cfg["_has_key"] = bool(scout.get_api_key())
                cfg["claude_backend"] = scout.claude_backend(cfg)
                cfg["_profile"] = learn.profile_info()
                cfg["_has_webhook"] = bool(discord_post.get_webhook())
                k = publish.get_keys()
                cfg["_has_r2_id"] = bool(k["access_key_id"])
                cfg["_has_r2_secret"] = bool(k["secret_access_key"])
                cfg["focus_presets"] = scout.focus_presets(cfg)
                cfg["chat_delay_seconds"] = _num(cfg.get("chat_delay_seconds"), 6)
                return self._json(cfg)
            if u.path == "/api/claude":
                return self._json(claude_status(refresh=q.get("refresh") == "1"))
            if u.path == "/api/drive":
                try:
                    return self._json(drive_items(read_cfg(), refresh=q.get("refresh") == "1"))
                except (RuntimeError, SystemExit) as e:
                    return self._json({"connected": drive_connected_quick(), "items": [], "error": str(e)})
            if u.path == "/api/local":
                return self._json(local_items(read_cfg()))
            if u.path == "/api/results":
                cfg = read_cfg()
                rs = [result_summary(w) for w in known_works(cfg)]
                rs.sort(key=lambda r: r["mtime"], reverse=True)
                return self._json(rs)
            if u.path == "/api/result":
                return self._json(result_detail(safe_work(q["id"], read_cfg())))
            if u.path == "/api/job":
                j = next((j for j in JOBS if j["id"] == q.get("id")), None)
                return self._json(public_job(j, full_log=True) if j else {"error": "not found"})
            if u.path == "/media":
                p = Path(dec_id(q["f"])).resolve()
                ok = (HERE / "track_samples").resolve() in p.parents or (
                    p.parent.name == "previews" and any(p.parent.parent == w.resolve() for w in known_works(read_cfg())))
                if not ok or not p.is_file():
                    return self._send(404, b"not found", "text/plain")
                return self._send_file(p, "video/mp4" if p.parent.name == "previews" else "audio/mp4")
            return self._send(404, b"not found", "text/plain")
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except Exception as e:
            print(traceback.format_exc(), flush=True)
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # --- POST
    def do_POST(self):
        if not self._ok_host() or self.headers.get("X-VS") != "1":
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        try:
            b = self._body()
            cfg = read_cfg()
            if u.path == "/api/config":
                key = (b.pop("api_key", None) or "").strip()
                if key:
                    (HERE / "api_key.txt").write_text(key, encoding="utf-8")
                b.pop("_has_key", None)
                b.pop("_profile", None)
                b.pop("_has_webhook", None)
                b.pop("_has_r2_id", None)
                b.pop("_has_r2_secret", None)
                rid, rsec = (b.pop("r2_access_key", None) or "").strip(), (b.pop("r2_secret", None) or "").strip()
                if rid == "-" or rsec == "-":
                    publish.set_keys(clear=True)
                elif rid or rsec:
                    publish.set_keys(rid, rsec)
                if "discord_webhook" in b:
                    wh = (b.pop("discord_webhook") or "").strip()
                    if wh == "-":
                        discord_post.set_webhook("")
                    elif wh:
                        discord_post.set_webhook(wh)
                if "channel_profile" in b:
                    learn.save_profile(b.pop("channel_profile") or "")
                if "focus_presets" in b:
                    b["focus_presets"] = scout.focus_presets({"focus_presets": b["focus_presets"]})
                if "chat_delay_seconds" in b:
                    b["chat_delay_seconds"] = max(0, min(60, _num(b["chat_delay_seconds"], 6)))
                merged = {**cfg, **b}
                if "google_drive" in b:
                    merged["google_drive"] = {**(cfg.get("google_drive") or {}), **b["google_drive"]}
                write_cfg(merged)
                DRIVE["cache"] = None
                return self._json({"ok": True})
            if u.path == "/api/jobs":
                made = []
                mode = b.get("mode", "full")
                kind = b.get("kind", "analyze")
                freq = b.get("focus")                         # {"preset": "__auto__" | name | null, "text": extra}
                if b.get("source") == "drive":
                    d = scout.drive_settings(cfg)
                    listing = {x["Name"]: x for x in (DRIVE["cache"] or scout.drive_list(d))}
                    for name in b.get("names", []):
                        if name in listing:
                            st = scout.drive_status(cfg, d, listing[name])
                            m = mode if not (mode == "full" and st != "new") else ("redo_ai" if st == "done" else "full")
                            j = new_job(kind, "drive", name, m if kind == "analyze" else "full", item=listing[name],
                                        focus=scout.resolve_focus(cfg, freq, name) if kind == "analyze" else {})
                            if kind == "analyze" and st != "new" and "download" in j["stages"]:
                                j["stages"].remove("download")
                            made.append(j["id"])
                else:
                    for p in b.get("paths", []):
                        j = new_job(kind, "local", Path(p).name, mode, path=p,
                                    focus=scout.resolve_focus(cfg, freq, Path(p).name) if kind == "analyze" else {})
                        made.append(j["id"])
                return self._json({"ok": True, "jobs": made})
            if u.path == "/api/learn":
                if any(j["kind"] == "learn" and j["status"] in ("queued", "running") for j in JOBS):
                    return self._json({"error": "Already learning — see Jobs."}, 400)
                j = new_job("learn", "youtube", "Learn from my YouTube channel")
                return self._json({"ok": True, "job": j["id"]})
            if u.path == "/api/result/redo":
                w = safe_work(b["id"], cfg)
                meta = scout.read_meta(w)
                vod = meta.get("vod") or w.name.removesuffix("_scout")
                d = None
                try:
                    d = scout.drive_settings(cfg)
                except SystemExit:
                    pass
                fo = scout.resolve_focus(cfg, b.get("focus"), vod)
                if d and w.parent.resolve() == d["results_dir"].resolve():
                    item = {"Name": vod, "Size": 0}
                    j = new_job("analyze", "drive", vod, "redo_ai", item=item, focus=fo)
                else:
                    j = new_job("analyze", "local", vod, "redo_ai", path=str(w.parent / vod), focus=fo)
                return self._json({"ok": True, "job": j["id"]})
            if u.path == "/api/focus/preview":               # what "Auto" would pick for each recording
                names = [str(n) for n in b.get("names", [])]
                return self._json({"items": [
                    {"name": n, **{k: v for k, v in scout.resolve_focus(cfg, b.get("focus"), n).items() if k in ("preset", "text")}}
                    for n in names]})
            if u.path == "/api/jobs/cancel":
                for j in JOBS:
                    if j["id"] == b.get("id"):
                        if j["status"] == "queued":
                            j["status"] = "cancelled"
                        j["cancel"] = True
                return self._json({"ok": True})
            if u.path == "/api/jobs/clear":
                with JLOCK:
                    JOBS[:] = [j for j in JOBS if j["status"] in ("queued", "running")]
                return self._json({"ok": True})
            if u.path == "/api/review":
                w = safe_work(b["id"], cfg)
                rv = read_review(w)
                if b.get("decision") in ("keep", "skip"):
                    rv.setdefault("decisions", {})[b["key"]] = {"d": b["decision"], "t": time.time()}
                else:
                    rv.get("decisions", {}).pop(b["key"], None)
                (w / "review.json").write_text(json.dumps(rv, ensure_ascii=False, indent=1), encoding="utf-8")
                threading.Thread(target=rebuild_taste, args=(cfg,), daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/hosting/test":
                return self._json({"ok": True, "message": publish.test_connection(cfg)})
            if u.path == "/api/discord/test":
                discord_post.post_test(cfg)
                return self._json({"ok": True})
            if u.path == "/api/review/export":
                return self._json({"ok": True, **export_kept(safe_work(b["id"], cfg), cfg)})
            if u.path == "/api/previews":
                w = safe_work(b["id"], cfg)
                meta = scout.read_meta(w)
                src = "drive"
                try:
                    if w.parent.resolve() != scout.drive_settings(cfg)["results_dir"].resolve():
                        src = "local"
                except SystemExit:
                    src = "local"
                j = new_job("previews", src, meta.get("vod", w.name), work=str(w))
                return self._json({"ok": True, "job": j["id"]})
            if u.path == "/api/result/youtube":
                w = safe_work(b["id"], cfg)
                meta = scout.read_meta(w)
                meta["youtube_url"] = (b.get("url") or "").strip()
                meta["youtube_offset_seconds"] = float(b.get("offset") or 0)
                scout.write_meta(w, meta)
                if (w / "analysis.json").exists():
                    scout.write_reports(w, cfg)
                return self._json({"ok": True})
            if u.path == "/api/open":
                w = safe_work(b["id"], cfg)
                what = b.get("what", "folder")
                target = w if what == "folder" else w / f"{scout.report_base(w)}.{what}"
                open_path(target)
                return self._json({"ok": True})
            if u.path == "/api/open_samples":
                p = Path(b["dir"]).resolve()
                if (HERE / "track_samples").resolve() in p.parents:
                    open_path(p)
                return self._json({"ok": True})
            if u.path == "/api/pick":
                paths = native_pick(b.get("kind", "files"))
                if b.get("kind") == "files" and paths:
                    st = load_state()
                    for p in paths:
                        if p not in st["local_files"]:
                            st["local_files"].append(p)
                    save_state(st)
                return self._json({"paths": paths})
            if u.path == "/api/local/remove":
                st = load_state()
                st["local_files"] = [p for p in st["local_files"] if p != b.get("path")]
                save_state(st)
                return self._json({"ok": True})
            if u.path == "/api/claude/install":
                open_console(["Write-Host 'Installing Claude Code...' -ForegroundColor Cyan; "
                              "irm https://claude.ai/install.ps1 | iex; "
                              "Write-Host ''; Write-Host 'Now log in with your Claude account (a browser window opens).' -ForegroundColor Cyan; "
                              "& (Join-Path $env:USERPROFILE '.local\\bin\\claude.exe') auth login; "
                              "Write-Host ''; Write-Host 'Done - you can close this window and go back to SkyEye.' -ForegroundColor Green"])
                CLAUDE["status"] = None
                return self._json({"ok": True})
            if u.path == "/api/claude/login":
                exe = scout.claude_code_exe()
                if not exe:
                    return self._json({"error": "Claude Code isn't installed yet."}, 400)
                open_console([f"Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue; & '{exe}' auth login; "
                              "Write-Host ''; Write-Host 'Done - you can close this window and go back to SkyEye.' -ForegroundColor Green"])
                CLAUDE["status"] = None
                return self._json({"ok": True})
            if u.path == "/api/drive/login":
                if DRIVE["login"] != "running":
                    threading.Thread(target=drive_login_thread, daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/quit":
                self._json({"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            return self._send(404, b"not found", "text/plain")
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except SystemExit as e:
            return self._json({"error": str(e)}, 400)
        except (ValueError, RuntimeError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            print(traceback.format_exc(), flush=True)
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


# ----------------------------------------------------------------- window
def open_window(url: str):
    if os.environ.get("SKYEYE_NO_WINDOW"):
        return
    if os.name == "nt":
        cands = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        prof = HERE / ".app-window"
        flags = [f"--app={url}", "--window-size=1440,920", f"--user-data-dir={prof}",
                 "--no-first-run", "--no-default-browser-check",
                 # keep the window awake when minimized / covered (Edge "sleeping tabs" & timer throttling)
                 "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                 "--disable-backgrounding-occluded-windows",
                 "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion,"
                 "msEdgeSleepingTabs,TabFreeze,BackForwardCache"]
        for exe in cands:
            if exe.exists():
                subprocess.Popen([str(exe), *flags], **NOWIN)
                return
    webbrowser.open(url)


def already_running() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=1.5).read()
        return True
    except Exception:
        return False


IDLE_QUIT_SEC = 20 * 60   # minimized/background windows only check in about once a minute, so be patient


def watchdog():
    """Quit when the window has been gone for a long while and nothing is running."""
    while True:
        time.sleep(30)
        if read_cfg().get("keep_running", True) is not False:
            continue                       # stays running until "Quit SkyEye" (no auto-sleep)
        idle = time.time() - LAST_PING > IDLE_QUIT_SEC
        busy = any(j["status"] in ("queued", "running") for j in JOBS)
        if idle and not busy:
            os._exit(0)


def main():
    url = f"http://127.0.0.1:{PORT}/"
    if already_running():
        open_window(url)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        if os.name == "nt" and read_cfg().get("background_priority", True) is not False:
            import ctypes          # child processes (ffmpeg, rclone) inherit this
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
    except Exception:
        pass
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=prefetcher, daemon=True).start()
    claude_status_bg()
    if not os.environ.get("SKYEYE_NO_WINDOW"):
        threading.Thread(target=watchdog, daemon=True).start()
    print(f"SkyEye GUI on {url}", flush=True)
    threading.Timer(0.5, lambda: open_window(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
