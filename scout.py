#!/usr/bin/env python3
"""
SkyEye — finds edit-worthy moments in multi-track OBS stream recordings.

  The engine behind the SkyEye app (app.py). Run "SkyEye.bat" to use it.

Pipeline: split tracks -> Whisper transcription (mic / discord / donation)
-> audio signals (voice spikes, game spikes, donation events)
-> labelled timeline -> Claude picks highlight segments -> HTML / MD / CSV report.
Every step is cached in "<recording>_scout/", so re-runs only redo what's needed.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import unicodedata
import wave
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
VERSION = "2.0"
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}   # no console pop-ups from the GUI

ROLE_DEFAULTS = {
    "mic":      {"label": "호시아",      "transcribe": True,  "spikes": 9.0},
    "discord":  {"label": "디스코드",    "transcribe": True,  "spikes": 10.0},
    "donation": {"label": "후원",        "transcribe": True,  "spikes": None},
    "game":     {"label": "게임/콘텐츠", "transcribe": False, "spikes": 12.0},
    "bgm":      None,
    "ignore":   None,
}

HALLUCINATIONS = [
    "시청해주셔서 감사합니다", "시청해 주셔서 감사합니다", "구독과 좋아요", "좋아요와 구독",
    "구독 부탁", "다음 영상에서 만나요", "자막 제공", "한국어 자막", "MBC 뉴스", "KBS 뉴스",
    "SBS 뉴스", "이덕영입니다", "끝까지 시청", "감사합니다.​",
]


# ------------------------------------------------------------------ hooks
# The GUI (app.py) plugs into these to show logs / progress and to cancel jobs.
class Cancelled(Exception):
    pass


HOOKS = {"log": None, "progress": None, "cancel": None}


def set_hooks(log=None, progress=None, cancel=None):
    HOOKS.update(log=log, progress=progress, cancel=cancel)


def progress(stage: str, frac: float | None = None, detail: str = ""):
    """stage: download | extract | transcribe | signals | claude | report"""
    cb = HOOKS["progress"]
    if cb:
        cb(stage, frac, detail)


def check_cancel():
    cb = HOOKS["cancel"]
    if cb and cb():
        raise Cancelled()


# ------------------------------------------------------------------ utils
def log(msg=""):
    cb = HOOKS["log"]
    if cb:
        cb(str(msg))
    else:
        print(msg, flush=True)


def fmt(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def parse_ts(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    parts = str(s).strip().replace("[", "").replace("]", "").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec


def load_config() -> dict:
    p = HERE / "config.yaml"
    if not p.exists():
        sys.exit(f"config.yaml not found next to scout.py ({p})")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_ffmpeg_progress(args: list[str], duration: float, stage: str):
    """ffmpeg with live progress (reads -progress output)."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-nostats", "-progress", "pipe:1"] + args
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", **NOWIN)
    import threading
    err: list[str] = []
    th = threading.Thread(target=lambda: err.extend(p.stderr.readlines()), daemon=True)
    th.start()
    last = 0.0
    try:
        for line in p.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    sec = int(line.split("=")[1]) / 1e6
                except ValueError:
                    continue
                if duration and time.time() - last > 0.5:
                    last = time.time()
                    progress(stage, min(1.0, sec / duration), f"{fmt(sec)} / {fmt(duration)}")
                check_cancel()
    except Cancelled:
        p.kill()
        raise
    p.wait()
    th.join(timeout=2)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "".join(err)[-2000:])


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if not exe:
            sys.exit("ffmpeg not found. Run setup.bat again (it installs imageio-ffmpeg).")
        return exe


def run_ffmpeg(args: list[str]):
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **NOWIN)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-2000:])


def probe(path: Path) -> dict:
    """Parse `ffmpeg -i` output: duration + audio streams (with OBS track titles if any)."""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", **NOWIN)
    out = r.stderr
    dur = None
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    tracks, cur = [], None
    chapters, chap = [], None
    for line in out.splitlines():
        cm = re.match(r"\s*Chapter #\d+:\d+: start ([\d.]+), end ([\d.]+)", line)
        if cm:
            chap = {"t": float(cm.group(1)), "end": float(cm.group(2)), "label": ""}
            chapters.append(chap)
            cur = None
            continue
        if chap is not None:
            tm = re.match(r"\s+title\s*:\s*(.+)", line)
            if tm:
                chap["label"] = tm.group(1).strip()
                chap = None
                continue
        sm = re.match(r"\s*Stream #\d+:(\d+)\S*?: (\w+): (.*)", line)
        if sm:
            cur = None
            chap = None
            if sm.group(2) == "Audio":
                cur = {"number": len(tracks) + 1, "info": sm.group(3).strip(), "title": ""}
                tracks.append(cur)
            continue
        if cur is not None:
            tm = re.match(r"\s+(title|handler_name)\s*:\s*(.+)", line)
            if tm and (tm.group(1) == "title" or not cur["title"]):
                t = tm.group(2).strip()
                if t.lower() not in ("soundhandler", "sound handler"):
                    cur["title"] = t
    if not tracks and "No such file" in out:
        sys.exit(f"File not found: {path}")
    return {"duration": dur, "tracks": tracks, "chapters": chapters}


# ------------------------------------------------------------ track setup
def resolve_tracks(cfg: dict, info: dict) -> list[dict]:
    resolved = []
    n_audio = len(info["tracks"])
    for i, t in enumerate(cfg.get("tracks") or []):
        role = str(t.get("role", "")).lower()
        if role not in ROLE_DEFAULTS:
            sys.exit(f"config.yaml tracks[{i}]: unknown role '{role}'")
        base = ROLE_DEFAULTS[role]
        if base is None:
            continue
        num = t.get("number")
        if t.get("title_contains"):
            key = str(t["title_contains"]).lower()
            hit = [a for a in info["tracks"] if key in a["title"].lower()]
            if not hit:
                log(f"  ! no track title contains '{t['title_contains']}' — skipping {role}")
                continue
            num = hit[0]["number"]
        if not isinstance(num, int) or not (1 <= num <= n_audio):
            log(f"  ! {role}: track {num} doesn't exist (file has {n_audio} audio tracks) — skipping")
            continue
        resolved.append({
            "role": role,
            "number": num,
            "label": t.get("label") or base["label"],
            "transcribe": bool(t.get("transcribe", base["transcribe"])),
            "spikes": base["spikes"],
            "key": role if role not in [r["role"] for r in resolved] else f"{role}{num}",
            "file": f"t{num}",          # caches are per TRACK NUMBER, so changing roles never re-transcribes
        })
    if not any(r["role"] == "mic" for r in resolved):
        sys.exit("No usable 'mic' track. Go to Settings → Audio tracks and pick which track is your mic "
                 "(Recordings → Tracks lets you listen to each one).")
    return resolved


# ------------------------------------------------------------ extraction
def _track_needs_audio(work: Path, t: dict) -> bool:
    return (not (work / f"{t['file']}.db.npy").exists()) or (
        t["transcribe"] and not (work / f"{t['file']}.transcript.json").exists())


def extract_tracks(vod: Path, tracks: list[dict], work: Path, duration: float = 0):
    todo = [t for t in tracks if _track_needs_audio(work, t) and not (work / f"{t['file']}.wav").exists()]
    if not todo:
        return
    log(f"[1/5] Extracting {len(todo)} audio track(s)…")
    progress("extract", 0.0)
    args = ["-i", str(vod)]
    for t in todo:
        args += ["-map", f"0:a:{t['number'] - 1}", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(work / f"{t['file']}.part.wav")]
    t0 = time.time()
    run_ffmpeg_progress(args, duration, "extract")
    for t in todo:
        (work / f"{t['file']}.part.wav").replace(work / f"{t['file']}.wav")
    log(f"      done in {time.time() - t0:.0f}s")


# ---------------------------------------------------------- transcription
def _add_cuda_dll_paths():
    """pip-installed nvidia-cublas / nvidia-cudnn wheels keep DLLs in site-packages/nvidia/*/bin."""
    if os.name != "nt":
        return
    try:
        import importlib.util
        spec = importlib.util.find_spec("nvidia")
        roots = list(spec.submodule_search_locations) if spec else []
    except Exception:
        roots = []
    for root in roots:
        for sub in Path(root).glob("*/bin"):
            try:
                os.add_dll_directory(str(sub))
            except Exception:
                pass
            os.environ["PATH"] = str(sub) + os.pathsep + os.environ.get("PATH", "")


_MODEL = None
_BATCHED = None          # BatchedInferencePipeline, or False if batching failed on this PC


def unload_whisper() -> bool:
    """Free the GPU memory the model holds (~3–5 GB). It reloads in a few seconds when needed."""
    global _MODEL, _BATCHED
    if _MODEL is None:
        return False
    _MODEL = None
    _BATCHED = None
    import gc
    gc.collect()
    return True


def get_whisper(cfg):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    _add_cuda_dll_paths()
    from faster_whisper import WhisperModel
    name = cfg.get("whisper_model", "large-v3")
    dev = cfg.get("device", "cuda")
    ct = cfg.get("compute_type", "float16")
    if dev != "cpu" and cfg.get("low_vram"):
        ct = "int8_float16"                     # about half the GPU memory, nearly the same accuracy
    log(f"      loading Whisper '{name}' on {dev} (first run downloads the model, ~3 GB)…")
    try:
        _MODEL = WhisperModel(name, device=dev, compute_type=ct)
    except Exception as e:
        if dev == "cpu":
            raise
        log(f"      ! GPU load failed ({e}); falling back to CPU — this will be MUCH slower.")
        _MODEL = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODEL


def is_hallucination(text: str, seg) -> bool:
    s = text.strip()
    if not s:
        return True
    if any(h in s for h in HALLUCINATIONS) and len(s) < 40:
        return True
    if getattr(seg, "compression_ratio", 0) > 2.6:
        return True
    if getattr(seg, "no_speech_prob", 0) > 0.6 and getattr(seg, "avg_logprob", 0) < -0.8:
        return True
    return False


CHUNK_SEC = 20 * 60   # transcribe long streams in ~20-minute pieces (keeps memory low)


def _wav_info(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnframes()


def _read_wav(path: Path, start_s: float, end_s: float) -> np.ndarray:
    """16 kHz mono int16 WAV slice -> float32 array (what Whisper wants). Avoids PyAV entirely."""
    with wave.open(str(path), "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        a, b = max(0, int(start_s * sr)), min(n, int(end_s * sr))
        w.setpos(a)
        raw = w.readframes(max(0, b - a))
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _quiet_cut(path: Path, t: float, window: float = 20.0) -> float:
    """Quietest moment within ±window s of t, so chunk cuts don't split a sentence."""
    a = _read_wav(path, t - window, t + window)
    if a.size < 16000:
        return t
    hop = 4000                                   # 0.25 s
    k = a.size // hop
    rms = (a[: k * hop].reshape(k, hop) ** 2).mean(1)
    return max(0.0, t - window) + float(np.argmin(rms)) * hop / 16000


def _whisper_segments(cfg, model, audio, prompt, batched: bool):
    kw = dict(language="ko", beam_size=5, vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
              condition_on_previous_text=False, initial_prompt=prompt)
    if batched:
        global _BATCHED
        if not _BATCHED:
            from faster_whisper import BatchedInferencePipeline
            _BATCHED = BatchedInferencePipeline(model=model)
        bs = int(cfg.get("whisper_batch_size") or (4 if cfg.get("low_vram") else 8))
        segs, _ = _BATCHED.transcribe(audio, batch_size=bs, without_timestamps=False, **kw)
        return segs
    segs, _ = model.transcribe(audio, **kw)
    return segs


def transcribe_track(cfg, t: dict, work: Path, duration: float, idx: int = 0, n_tracks: int = 1) -> list[dict]:
    global _BATCHED
    out = work / f"{t['file']}.transcript.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    db = load_db(work, t)
    if db is not None and (db > -50).sum() * HOP < 3:          # (almost) silent track: nothing to transcribe
        log(f"      {t['label']} (track {t['number']}): silent — skipped")
        out.write_text("[]", encoding="utf-8")
        return []
    progress("transcribe", idx / max(1, n_tracks), f"{t['label']} — loading model…")
    model = get_whisper(cfg)
    vocab = cfg.get("vocabulary") or []
    prompt = ", ".join(vocab) if vocab else None
    wav = work / f"{t['file']}.wav"
    sr, nframes = _wav_info(wav)
    total = nframes / sr
    duration = duration or total
    cuts = [0.0]
    while cuts[-1] + CHUNK_SEC < total - 60:
        cuts.append(_quiet_cut(wav, cuts[-1] + CHUNK_SEC))
    cuts.append(total)

    t0 = time.time()
    res, last_text, repeat = [], None, 0
    last_print = 0.0
    for a, b in zip(cuts, cuts[1:]):
        check_cancel()
        audio = _read_wav(wav, a, b)
        if audio.size < sr // 2:
            continue
        for attempt in (0, 1):
            use_batched = attempt == 0 and _BATCHED is not False and int(cfg.get("whisper_batch_size") or 8) > 1
            chunk_res = []
            try:
                for seg in _whisper_segments(cfg, model, audio, prompt, use_batched):
                    text = seg.text.strip()
                    s_abs, e_abs = seg.start + a, seg.end + a
                    if text == last_text:
                        repeat += 1
                        if repeat >= 2:
                            continue
                    else:
                        repeat = 0
                    last_text = text
                    if not is_hallucination(text, seg):
                        chunk_res.append({"start": round(s_abs, 2), "end": round(e_abs, 2), "text": text})
                    check_cancel()
                    if time.time() - last_print > (1 if HOOKS["progress"] else 5):
                        last_print = time.time()
                        f = min(1.0, e_abs / duration) if duration else 0
                        progress("transcribe", (idx + f) / max(1, n_tracks),
                                 f"{t['label']} — {fmt(e_abs)} / {fmt(duration)}")
                        if not HOOKS["log"]:
                            print(f"\r      {t['label']}: {fmt(e_abs)} / {fmt(duration)}  ({f * 100:4.1f}%)   ",
                                  end="", flush=True)
                break
            except Cancelled:
                raise
            except Exception as e:
                if not use_batched:
                    raise
                log(f"      ! fast (batched) transcription failed ({type(e).__name__}: {str(e)[:120]}); "
                    "using the normal mode")
                _BATCHED = False
        res.extend(chunk_res)
        del audio
    if not HOOKS["log"]:
        print()
    res.sort(key=lambda r: r["start"])
    log(f"      {t['label']} (track {t['number']}): {len(res)} lines in {time.time() - t0:.0f}s")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=0), encoding="utf-8")
    return res


# ---------------------------------------------------------------- signals
HOP = 0.5  # seconds per loudness frame


def frame_db(wav_path: Path) -> np.ndarray:
    vals = []
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        n = int(sr * HOP)
        while True:
            b = w.readframes(n * 240)
            if not b:
                break
            a = np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0
            k = len(a) // n
            if k:
                blk = a[: k * n].reshape(k, n)
                vals.append(10 * np.log10((blk ** 2).mean(1) + 1e-10))
            if len(a) - k * n > n // 4:
                rest = a[k * n:]
                vals.append(np.array([10 * np.log10((rest ** 2).mean() + 1e-10)]))
    return np.concatenate(vals) if vals else np.zeros(0)


def _runs(mask: np.ndarray, merge_gap: int) -> list[tuple[int, int]]:
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    runs, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - p > merge_gap:
            runs.append((s, p))
            s = i
        p = i
    runs.append((s, p))
    return runs


def detect_spikes(db: np.ndarray, threshold: float, max_count: int) -> list[dict]:
    """Moments clearly louder than both the local context and this track's normal speaking level."""
    from numpy.lib.stride_tricks import sliding_window_view
    if db.size < 10:
        return []
    st = np.convolve(db, np.ones(3) / 3, mode="same")                     # 1.5 s short-term level
    floor = np.percentile(db, 10)
    active = db[db > floor + 10]
    if active.size < 20:
        active = db[db > -70]                                            # constant-level track (e.g. game)
    if active.size < 20:
        return []
    typical = np.median(active)                                          # normal speaking level
    w = 121                                                              # 60 s context
    padded = np.pad(db, (w // 2, w // 2), mode="edge")
    local = np.median(sliding_window_view(padded, w), axis=1)[: db.size]
    baseline = np.maximum(local, typical - 3)
    delta = st - baseline
    mask = (delta >= threshold) & (st > -45)
    mask[:6] = False                                                     # ignore file-start artefacts
    spikes = []
    for a, b in _runs(mask, merge_gap=10):
        seg = delta[a: b + 1]
        pk = a + int(np.argmax(seg))
        spikes.append({"t": pk * HOP, "start": a * HOP, "end": (b + 1) * HOP,
                       "delta": round(float(seg.max()), 1)})
    spikes.sort(key=lambda s: -s["delta"])
    spikes = spikes[:max_count]
    spikes.sort(key=lambda s: s["t"])
    return spikes


def detect_events(db: np.ndarray) -> list[dict]:
    """Any clear activity on a mostly-silent track (donation alerts / TTS)."""
    if db.size < 10:
        return []
    floor = np.percentile(db, 10)
    mask = (db > max(floor + 15, -55))
    ev = []
    for a, b in _runs(mask, merge_gap=6):
        if (b - a + 1) * HOP >= 1.0:
            ev.append({"start": a * HOP, "end": (b + 1) * HOP})
    return ev


def load_db(work: Path, t: dict) -> np.ndarray | None:
    """Loudness (dB per 0.5 s) for one track, measured once and cached as a tiny file."""
    f = work / f"{t['file']}.db.npy"
    if f.exists():
        return np.load(f).astype(np.float32)
    wav = work / f"{t['file']}.wav"
    if not wav.exists():
        return None
    db = frame_db(wav)
    np.save(f, db.astype(np.float16))
    return db


def compute_signals(tracks: list[dict], work: Path, hours: float) -> dict:
    """Recomputed every run from the cached loudness — takes well under a second."""
    out = work / "signals.json"
    sig = {}
    cap = max(20, int(25 * max(hours, 1)))
    for i, t in enumerate(tracks):
        progress("signals", i / len(tracks), t["label"])
        check_cancel()
        db = load_db(work, t)
        if db is None:
            continue
        entry = {}
        # 10-second loudness curve for the GUI timeline
        k = 20
        if db.size >= k:
            pw = 10 ** (db[: db.size // k * k].reshape(-1, k) / 10)
            entry["curve"] = [round(float(v), 1) for v in 10 * np.log10(pw.mean(1) + 1e-10)]
        if t["spikes"]:
            entry["spikes"] = detect_spikes(db, t["spikes"], cap)
        if t["role"] == "donation":
            entry["events"] = detect_events(db)
        sig[t["key"]] = entry
    out.write_text(json.dumps(sig, ensure_ascii=False), encoding="utf-8")
    return sig


# --------------------------------------------------------------- timeline
def build_timeline(tracks, transcripts, signals, markers=None, chat=None, chat_delay: float = 0.0) -> list[tuple[float, str]]:
    lines: list[tuple[float, str]] = []
    for m in markers or []:
        note = f" (메모: {m['label']})" if m.get("label") else ""
        lines.append((m["t"], f"[{fmt(m['t'])}] ⭐ 방송 중 표시 — 스트리머가 여기서 재밌다고 직접 표시함{note}"))
    label = {t["key"]: t["label"] for t in tracks}
    role = {t["key"]: t["role"] for t in tracks}

    for key, segs in transcripts.items():
        merged = []
        for s in segs:
            if merged and s["start"] - merged[-1]["end"] < 3.0 and len(merged[-1]["text"]) + len(s["text"]) < 220:
                merged[-1]["text"] += " " + s["text"]
                merged[-1]["end"] = s["end"]
            else:
                merged.append(dict(s))
        tag = "💰 후원 TTS" if role[key] == "donation" else label[key]
        for s in merged:
            lines.append((s["start"], f"[{fmt(s['start'])}] {tag}: {s['text']}"))

    for key, sig in signals.items():
        for sp in sig.get("spikes", []):
            if role[key] == "game":
                txt = f"💥 {label[key]} 소리 급상승 (+{sp['delta']:.0f}dB)"
            else:
                txt = f"⚡ {label[key]} 목소리 급상승 (+{sp['delta']:.0f}dB)"
            lines.append((sp["t"], f"[{fmt(sp['t'])}] {txt}"))
        if "events" in sig:
            tts = transcripts.get(key, [])
            for ev in sig["events"]:
                has_text = any(s["start"] < ev["end"] + 1 and s["end"] > ev["start"] - 1 for s in tts)
                if not has_text:
                    lines.append((ev["start"], f"[{fmt(ev['start'])}] 💰 후원 알림음 ({ev['end'] - ev['start']:.0f}초)"))

    mic = next((t["key"] for t in tracks if t["role"] == "mic"), None)
    if mic and transcripts.get(mic):
        segs = transcripts[mic]
        for a, b in zip(segs, segs[1:]):
            gap = b["start"] - a["end"]
            if gap >= 180:
                lines.append((a["end"] + 0.1,
                              f"[{fmt(a['end'])}] … {label[mic]} 발화 없음 (약 {gap / 60:.0f}분)"))
    vc = viewer_chat(chat or [], chat_delay)                # viewer chat from Arkbird (optional)
    if vc:
        lines += chat_lines(vc)
        for t, n in chat_bursts([c["te"] for c in vc]):
            lines.append((t, f"[{fmt(t)}] 📈 채팅 반응 급증 ({n}개/30초)"))
    lines.sort(key=lambda x: x[0])
    return lines


# ----------------------------------------------------------------- Claude
CATEGORIES = ["웃긴 장면", "리액션", "토크/썰", "후원 소통", "게임 플레이", "합방/디코 케미", "감동/진지", "기타"]

REPORT_TOOL = {
    "name": "submit_highlights",
    "description": "방송 분석 결과(편집 후보 구간 목록)를 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "stream_summary": {"type": "string",
                               "description": "방송 전체 흐름 요약. 3~8문장, 대략적인 시간대 포함."},
            "chapters": {
                "type": "array",
                "description": "방송 구성(시간순): 무엇을 했는지 큰 덩어리로.",
                "items": {"type": "object",
                          "properties": {"start": {"type": "string"}, "end": {"type": "string"},
                                         "what": {"type": "string"}},
                          "required": ["start", "end", "what"]},
            },
            "highlights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "H:MM:SS — 맥락(셋업)이 시작되는 지점"},
                        "end": {"type": "string", "description": "H:MM:SS — 반응/여운이 끝나는 지점"},
                        "title": {"type": "string", "description": "편집자가 한눈에 알아볼 짧은 제목 (영상 제목 아이디어로도 활용)"},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "format": {"type": "string", "enum": ["본편", "쇼츠", "둘다"]},
                        "score": {"type": "integer", "minimum": 1, "maximum": 10,
                                  "description": "편집 가치. 10=무조건 써야 함, 5=보통, 3 이하=여유 있으면"},
                        "summary": {"type": "string", "description": "무슨 일이 일어나는지 2~4문장"},
                        "why": {"type": "string", "description": "왜 편집 소재로 좋은지"},
                        "editor_note": {"type": "string",
                                        "description": "편집 팁: 앞부분 셋업 위치, 잘라도 되는 부분, 확인할 트랙, 불확실한 점 등"},
                        "key_quotes": {"type": "array", "items": {"type": "string"},
                                       "description": "핵심 대사 1~3개 (자막/썸네일용, 전사 그대로)"},
                    },
                    "required": ["start", "end", "title", "category", "format", "score", "summary", "why"],
                },
            },
            "compilation_ideas": {
                "type": "array",
                "description": "여러 장면을 묶은 영상 아이디어 (선택)",
                "items": {"type": "object",
                          "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                          "required": ["title", "description"]},
            },
        },
        "required": ["stream_summary", "highlights"],
    },
}


def channel_profile_section(cfg) -> str:
    """The learned style profile of the channel's existing edits (learn.py), if there is one."""
    p = HERE / "channel_profile.md"
    if not p.exists() or cfg.get("use_channel_profile") is False:
        return ""
    txt = re.sub(r"^<!--.*?-->\s*", "", p.read_text(encoding="utf-8"), flags=re.S).strip()
    if not txt:
        return ""
    return ("\n## 이 채널의 기존 편집 스타일 (지금까지 올라간 편집 영상을 분석한 결과)\n" + txt[:9000] +
            "\n\n위 스타일에 맞는 장면을 우선적으로 찾되, 새로운 유형이라도 편집 가치가 높은 장면은 놓치지 마세요. "
            "각 후보의 why에 이 채널의 어떤 기존 영상/패턴과 비슷한지 짧게 언급하면 좋습니다.\n")


def track_description(tracks) -> str:
    return "\n".join(
        f"- \"{t['label']}\": " + {
            "mic": "스트리머 본인 마이크",
            "discord": "디스코드(합방/통화 상대) 음성",
            "donation": "후원 알림·TTS",
            "game": "메인 PC 게임/콘텐츠 소리",
        }.get(t["role"], t["role"])
        for t in tracks)


# ------------------------------------------------- focus (per-stream instructions)
# Stan can tell Claude what to look for in one stream ("find every crash"). A "focus" is
# {"preset": name or "", "text": the full instruction, "extra": the free-text part}; empty = none.
DEFAULT_FOCUS_PRESETS = [
    {"name": "Ace Combat", "keywords": ["에컴", "에이스 컴뱃", "ace combat"],
     "text": "추락하는 장면(특히 HOTAS 조작 문제로 박는 장면)과 그 리액션을 전부 찾아줘."},
    {"name": "Trickal", "keywords": ["트릭컬", "trickal"],
     "text": "감정적인 반응(울컥, 감동, 과몰입)을 더 꼼꼼히 봐줘."},
]
AUTO_FOCUS = "__auto__"          # the "Auto (match by title)" choice


def focus_presets(cfg) -> list[dict]:
    """Presets from config.yaml (focus_presets). A config without that key gets the built-in examples."""
    raw = cfg.get("focus_presets")
    if raw is None:
        return [dict(p) for p in DEFAULT_FOCUS_PRESETS]
    out = []
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "").strip()
        if not name:
            continue
        kws = x.get("keywords") or []
        if isinstance(kws, str):
            kws = re.split(r"[,\n]", kws)
        out.append({"name": name, "keywords": [str(k).strip() for k in kws if str(k).strip()],
                    "text": str(x.get("text") or "").strip()})
    return out


def _focus_norm(s) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", str(s))).lower()


def match_focus_preset(presets: list[dict], filename: str) -> dict | None:
    """First preset with a keyword in the recording's file name (case-, space- and Unicode-form-insensitive)."""
    n = _focus_norm(filename)
    for p in presets:
        if any(k and k in n for k in (_focus_norm(k) for k in p["keywords"])):
            return p
    return None


def resolve_focus(cfg, req, filename: str) -> dict:
    """req = {"preset": AUTO_FOCUS | preset name | None, "text": extra instructions}. Returns the focus to use for
    this recording ({} = none). The preset's text comes first, the extra text is added below it."""
    if not isinstance(req, dict):
        return {}
    sel, extra = req.get("preset"), str(req.get("text") or "").strip()
    presets = focus_presets(cfg)
    chosen = None
    if sel == AUTO_FOCUS:
        chosen = match_focus_preset(presets, filename)
    elif sel:
        chosen = next((p for p in presets if p["name"] == sel), None)
    parts = [chosen["text"]] if chosen and chosen["text"] else []
    if extra:
        parts.append(extra)
    text = "\n".join(parts).strip()
    if not text:
        return {}
    return {"preset": chosen["name"] if chosen else "", "text": text, "extra": extra}


def focus_text(cfg) -> str:
    f = cfg.get("focus")
    return str(f.get("text") or "").strip() if isinstance(f, dict) else ""


def focus_block(cfg, judge: bool = False) -> str:
    """The 'special instruction' section of the prompts (pass 1 = scan, pass 2 = judge). '' without a focus,
    so the prompts are then exactly what they always were."""
    txt = focus_text(cfg)
    if not txt:
        return ""
    head = "\n## 이번 방송 특별 지시 (스트리머 요청 — 최우선)\n" + txt + "\n"
    if not judge:
        return head + ("- 위 지시에 해당하는 장면은 재미 정도와 상관없이 **하나도 빠짐없이** 후보로 올리고 focus=true로 표시하세요.\n"
                       "- 평소처럼 다른 재밌는 장면도 계속 찾으세요.\n")
    return head + ("- 위 지시에 해당하는 장면은 \"재미없다\"는 이유로 절대 빼지 말고 모두 남기세요. 점수는 솔직하게 매기되, "
                   "해당 항목에는 focus_match=true를 표시하세요. (1차 후보에 focus=true가 붙어 있으면 이 지시에 해당하는 후보입니다.)\n"
                   "- 이런 장면은 방송 1시간당 목표 개수에 포함하지 않습니다. 목표보다 많아져도 됩니다.\n"
                   "- 아주 가까이 붙은 해당 장면들(예: 1분 안에 3번)은 한 흐름으로 이어지는 하나의 장면일 때만 하나로 합치고, "
                   "그렇지 않으면 각각 따로 남기세요. 나중에 여러 방송의 같은 종류 장면을 모아 편집하는 데 쓰입니다.\n")


def system_prompt(cfg, track_desc: str) -> str:
    per_hour = cfg.get("highlights_per_hour", 5)
    chat_note = ("💬 줄은 시청자 채팅(방송 지연 보정됨), 💰 후원 채팅은 후원 메시지 원문, 📈은 채팅 반응이 몰린 순간입니다. "
                 "채팅은 스트리머가 지금 무엇에 답하는지, 시청자가 어디서 웃고 놀랐는지 이해하는 데 쓰세요 "
                 "(소규모 방송이라 채팅이 적어서, 채팅이 없다고 재미없는 것은 아닙니다)."
                 if cfg.get("_has_chat") else "소규모 방송이라 채팅 반응은 없습니다.")
    focus = focus_block(cfg, judge=True)
    return f"""당신은 버튜버·스트리머 방송을 유튜브 영상으로 만드는 경험 많은 편집 PD입니다.
스트리머의 방송 다시보기(자동 전사 + 오디오 신호)를 읽고, 편집자에게 넘길 "편집할 만한 구간" 목록을 만듭니다.
실제 편집은 편집자가 하므로, 당신의 역할은 좋은 구간을 정확한 시간과 함께 찾아서 이유를 설명하는 것입니다.

## 채널 정보
{cfg.get('channel_context', '').strip()}

## 편집 목표
{cfg.get('editing_goal', '').strip()}
{channel_profile_section(cfg)}
## 입력 형식
타임라인의 각 줄은 [시:분:초] 로 시작합니다. 녹화 파일의 오디오 트랙이 분리되어 있어서 누가 말했는지 구분됩니다.
{track_desc}
- "⚡ … 목소리 급상승": 평소 말하는 음량보다 확 커진 순간 (소리 지르기, 크게 웃기, 흥분).
- "💥 … 소리 급상승": 게임/콘텐츠 쪽 큰 소리 (점프스케어, 폭발, 보스전 등).
- "💰 후원 TTS / 후원 알림음": 후원이 들어온 순간과 TTS 내용.
- "… 발화 없음": 스트리머가 한동안 말하지 않은 구간 (영상 시청, 자리 비움, 집중 플레이 등).
- "⭐ 방송 중 표시": 스트리머가 방송 도중 직접 버튼을 눌러 "여기 재밌었다"고 표시한 순간. 보통 재밌는 일이 일어난
  직후(수 초~1분 뒤)에 누르므로, 그 직전 장면을 찾아야 합니다. 이 표시 근처의 장면은 반드시 후보로 다루세요.
{focus}
## 판단 기준
- {chat_note} 내용 자체로 판단하세요: 드립과 펀치라인, 큰 리액션, 기승전결이 있는 썰,
  후원 메시지에 대한 재밌는 반응, 합방 상대와의 티키타카, 게임 속 실수·명장면, 반복되는 개그(콜백), 캐릭터가 잘 드러나는 순간.
- 음량 급상승은 힌트일 뿐 증거가 아닙니다. 조용하지만 웃긴 장면도 있고, 시끄럽지만 재미없는 장면도 있습니다.
- 구간은 맥락이 이해되는 지점에서 시작해서 반응이 끝나는 지점에서 끝나야 합니다 (셋업 포함). 본편 소재는 보통 30초~10분,
  쇼츠는 15~60초로 혼자 봐도 이해되는 장면이어야 합니다.
- 전사는 자동 생성이라 오타·오인식이 있고, 마이크에 다른 소리가 섞일 수 있습니다. 전사에 근거 없는 내용을 지어내지 마세요.
  확실하지 않으면 editor_note에 "확인 필요"라고 적으세요.
- 방송 1시간당 대략 {per_hour}개 정도를 목표로 하되, 억지로 채우지 말고 좋은 게 많으면 더 넣어도 됩니다.
  점수(score)는 솔직하게 매기세요 — 모든 구간이 8점 이상이면 편집자에게 도움이 안 됩니다.
- 모든 출력은 한국어로 작성하세요.
- 결과는 반드시 submit_highlights 도구로 제출하세요."""


def claude_backend(cfg: dict) -> str:
    """'subscription' = run through Claude Code logged in with your Claude plan; 'api' = API key credits."""
    b = str(cfg.get("claude_backend") or "subscription").lower()
    return "api" if b == "api" else "subscription"


def claude_code_exe() -> str | None:
    cands = []
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    if os.name == "nt":
        cands.append(home / ".local" / "bin" / "claude.exe")
        if os.environ.get("APPDATA"):
            cands.append(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd")
        if os.environ.get("LOCALAPPDATA"):
            cands.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Links" / "claude.exe")
    else:
        cands.append(home / ".local" / "bin" / "claude")
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("claude")


def _claude_env() -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):   # make sure the subscription login is used
        env.pop(k, None)
    return env


def claude_code_status() -> dict:
    exe = claude_code_exe()
    if not exe:
        return {"installed": False, "logged_in": False, "detail": ""}
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, env=_claude_env(), stdin=subprocess.DEVNULL, **NOWIN)
    except Exception as e:
        return {"installed": True, "logged_in": False, "detail": f"{type(e).__name__}: {e}", "path": exe}
    detail = ""
    try:
        j = json.loads(r.stdout)
        bits = [str(j.get(k)) for k in ("email", "subscriptionType", "orgName") if j.get(k)]
        am = str(j.get("authMethod") or "").lower()
        if "api" in am or "console" in am:
            bits.append("⚠ logged in with a Console/API account — this uses API credits, not your subscription")
        detail = " · ".join(bits)
        logged = bool(j.get("loggedIn", r.returncode == 0))
    except Exception:
        logged = r.returncode == 0
    return {"installed": True, "logged_in": logged, "detail": detail, "path": exe}


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    cand = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""
    try:
        return json.loads(cand)
    except Exception:
        return None


def _run_claude_code(cfg, system: str, user: str, instruction: str, schema: dict | None = None,
                     stage: str = "claude", label: str = "", quiet: bool = False,
                     thinking: int | None = None) -> dict:
    """Run `claude -p` (Claude Code, logged in with the Claude subscription) and return its JSON result."""
    import tempfile
    import threading
    exe = claude_code_exe()
    if not exe:
        sys.exit("Claude Code isn't installed yet. Go to Settings → Claude and click “Install Claude Code”.")
    model = cfg.get("claude_model", "claude-sonnet-5-5")
    tmp = Path(tempfile.mkdtemp(prefix="skyeye_"))
    sysfile = tmp / "system.txt"
    sysfile.write_text(system, encoding="utf-8")
    cmd = [exe, "-p", "--output-format", "json", "--model", model, "--system-prompt-file", str(sysfile)]
    if schema:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    cmd += ["--tools", "", "--disallowedTools", "mcp__*", "--no-session-persistence", "--max-turns", "6",
            instruction]
    env = _claude_env()
    if thinking is not None:                     # hidden "thinking" costs time and plan usage
        env["MAX_THINKING_TOKENS"] = str(int(thinking))
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(tmp), env=env, text=True, encoding="utf-8", errors="replace", **NOWIN)
    out: dict = {}

    def _io():
        o, e = p.communicate(user)
        out["o"], out["e"] = o, e
    th = threading.Thread(target=_io, daemon=True)
    th.start()
    i, n = 1, 1
    t0 = time.time()
    try:
        while th.is_alive():
            th.join(timeout=1.0)
            el = time.time() - t0
            if not quiet:
                progress(stage, (i - 1 + min(0.9, el / 150)) / n,
                         (label or "Claude is reading the stream…") + f" ({int(el)}s)")
            check_cancel()
            if el > 1800:
                p.kill()
                raise RuntimeError("Claude Code took longer than 30 minutes and was stopped.")
    except Cancelled:
        p.kill()
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    stdout, stderr = out.get("o", ""), out.get("e", "")
    try:
        j = json.loads(stdout)
    except Exception:
        j = _extract_json(stdout) or {}
    text = str(j.get("result") or "")
    if p.returncode != 0 or j.get("is_error"):
        msg = (text or stderr or stdout or "").strip()[-800:]
        low = msg.lower()
        if "limit" in low and ("usage" in low or "rate" in low):
            raise RuntimeError("Your Claude plan's usage limit was reached. Try again after it resets "
                               "(or switch Settings → Claude to the API).\n" + msg)
        if "login" in low or "auth" in low or "credential" in low:
            raise RuntimeError("Claude Code needs you to log in again (Settings → Claude → Log in).\n" + msg)
        raise RuntimeError("Claude Code failed:\n" + msg)
    return j


def _cc_usage(j: dict) -> dict:
    u = j.get("usage") or {}
    return {"input_tokens": int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
            + int(u.get("cache_creation_input_tokens", 0)),
            "output_tokens": int(u.get("output_tokens", 0))}


def ask_claude_json(cfg, model: str, system: str, user: str, tool: dict, instruction: str,
                    thinking: int | None = None) -> tuple[dict, dict]:
    """Structured answer (matching tool['input_schema']) via the subscription or the API. Thread-safe, no progress."""
    if claude_backend(cfg) == "subscription":
        j = _run_claude_code(dict(cfg, claude_model=model), system, user, instruction,
                             schema=tool["input_schema"], quiet=True, thinking=thinking)
        res = j.get("structured_output")
        if not isinstance(res, dict):
            res = _extract_json(str(j.get("result") or ""))
        if not isinstance(res, dict):
            raise RuntimeError("Claude answered, but not in the expected format:\n" + str(j.get("result"))[:600])
        return res, _cc_usage(j)
    key = get_api_key()
    if not key:
        sys.exit("No Claude API key. Add it in Settings → Claude.")
    import anthropic
    client = anthropic.Anthropic(api_key=key, max_retries=5, timeout=900)
    with client.messages.stream(model=model, max_tokens=24000, system=system, tools=[tool],
                                tool_choice={"type": "tool", "name": tool["name"]},
                                messages=[{"role": "user", "content": user}]) as st:
        for _ in st:
            check_cancel()
        msg = st.get_final_message()
    for b in msg.content:
        if b.type == "tool_use":
            return b.input, {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    raise RuntimeError("Claude did not return a result.")


def ask_claude_text(cfg, system: str, user: str, stage: str = "claude", label: str = "") -> tuple[str, dict]:
    """Plain-text answer from Claude, via the subscription (Claude Code) or the API — whichever is set."""
    if claude_backend(cfg) == "subscription":
        j = _run_claude_code(cfg, system, user, "위 자료를 분석해서 요청한 결과만 출력하세요.",
                             stage=stage, label=label or "Claude is studying your channel…")
        return str(j.get("result") or ""), _cc_usage(j)
    key = get_api_key()
    if not key:
        sys.exit("No Claude API key. Add it in Settings → Claude.")
    import anthropic
    client = anthropic.Anthropic(api_key=key, max_retries=5, timeout=900)
    t0, last, text = time.time(), 0.0, ""
    with client.messages.stream(model=cfg.get("claude_model", "claude-sonnet-5-5"), max_tokens=16000,
                                system=system, messages=[{"role": "user", "content": user}]) as st:
        for ev in st:
            if getattr(ev, "type", "") == "text":
                text += getattr(ev, "text", "")
                if time.time() - last > 0.5:
                    last = time.time()
                    progress(stage, min(0.95, len(text) / 6000), f"{label or 'Claude is writing…'} ({len(text):,} chars)")
                    check_cancel()
        msg = st.get_final_message()
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return text, {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}


def get_api_key() -> str | None:
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k.strip()
    p = HERE / "api_key.txt"
    if p.exists():
        k = p.read_text(encoding="utf-8").strip()
        return k or None
    return None


def overlap_ratio(a, b) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    return inter / max(1.0, min(a[1] - a[0], b[1] - b[0]))


def analyze(cfg, tracks, timeline, duration, vod_name, work: Path, redo=False) -> dict:
    """Two passes: a quick wide scan, then a strict judge (twopass.py)."""
    out = work / "analysis.json"
    if out.exists() and not redo:
        log("[4/5] Using the saved Claude analysis.")
        return json.loads(out.read_text(encoding="utf-8"))
    if claude_backend(cfg) == "subscription":
        st = claude_code_status()
        if not st["installed"]:
            sys.exit("Claude Code isn't installed yet. Go to Settings → Claude and click “Install Claude Code”.")
        if not st["logged_in"]:
            sys.exit("Claude Code isn't logged in. Go to Settings → Claude and click “Log in”.")
    elif not get_api_key():
        sys.exit("No Claude API key. Add it in Settings → Claude, or switch to “My Claude subscription”.")
    import twopass
    res = twopass.analyze_two_pass(cfg, tracks, timeline, duration, vod_name, work)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# ---------------------------------------------------------------- reports
def yt_link(cfg, sec: float) -> str | None:
    url = (cfg.get("youtube_url") or "").strip()
    if not url:
        return None
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})", url)
    if not m:
        return None
    t = max(0, int(sec + float(cfg.get("youtube_offset_seconds") or 0)))
    return f"https://youtu.be/{m.group(1)}?t={t}"


def normalize(analysis: dict) -> list[dict]:
    hs = []
    for h in analysis.get("highlights", []):
        s, e = parse_ts(h.get("start")), parse_ts(h.get("end"))
        if s is None:
            continue
        if e is None or e <= s:
            e = s + 30
        hs.append({**h, "s": s, "e": e, "score": int(h.get("score") or 0)})
        if "focus_match" in h:
            hs[-1]["focus_match"] = bool(h.get("focus_match"))
    hs.sort(key=lambda h: h["s"])
    for i, h in enumerate(hs, 1):
        h["n"] = i
    return hs


def stars(score: int) -> str:
    full = max(0, min(5, int(score / 2 + 0.5)))
    return "★" * full + "☆" * (5 - full)


HTML_CSS = """
:root{--bg:#f7f6f3;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e4e2dc;--accent:#6a4cff;--accent-soft:#efeaff;--gold:#c98a00}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#17171a;--card:#212125;--ink:#ececef;--muted:#9a9aa3;--line:#33333a;--accent:#a592ff;--accent-soft:#2c2745;--gold:#f0b429}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:28px 16px 60px}h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 12px}
.meta{color:var(--muted);font-size:13px}.summary{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;white-space:pre-wrap}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 6px}.bar button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
.bar button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:12px 0}
.top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}.n{color:var(--muted);font-size:13px}.title{font-weight:650;font-size:16px}
.time{font-variant-numeric:tabular-nums;font-weight:600;color:var(--accent);cursor:pointer}.time:hover{text-decoration:underline}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 8px}.tag{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--accent-soft);color:var(--ink)}
.score{color:var(--gold);letter-spacing:1px}.row{margin:4px 0}.row b{color:var(--muted);font-weight:600;font-size:13px;margin-right:6px}
blockquote{margin:6px 0 0;padding:4px 10px;border-left:3px solid var(--accent);color:var(--muted)}
a{color:var(--accent)}table{border-collapse:collapse;width:100%;font-size:14px}td{padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td:first-child{white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums}.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:6px 14px;border-radius:8px;font-size:13px;opacity:0;transition:.2s}
.toast.show{opacity:1}
"""


def write_html(path: Path, cfg, meta, analysis, hs):
    e = html.escape
    cards = []
    for h in hs:
        link = yt_link(cfg, h["s"])
        quotes = "".join(f"<blockquote>{e(q)}</blockquote>" for q in (h.get("key_quotes") or []))
        note = f'<div class="row"><b>편집 메모</b>{e(h["editor_note"])}</div>' if h.get("editor_note") else ""
        yl = f' · <a href="{e(link)}" target="_blank">유튜브에서 보기 ↗</a>' if link else ""
        cards.append(f"""
<div class="card" data-score="{h['score']}" data-format="{e(h.get('format', ''))}" data-s="{h['s']}">
 <div class="top"><span class="n">#{h['n']}</span><span class="title">{e(h['title'])}</span></div>
 <div class="row"><span class="time" title="클릭해서 복사">{fmt(h['s'])}</span> ~ <span class="time" title="클릭해서 복사">{fmt(h['e'])}</span>
  <span class="meta"> ({(h['e'] - h['s']) / 60:.1f}분)</span>{yl}</div>
 <div class="tags">{'<span class="tag">🎯 Focus</span>' if h.get('focus_match') else ''}<span class="tag">{e(h.get('category', ''))}</span><span class="tag">{e(h.get('format', ''))}</span>
  <span class="score" title="{h['score']}/10">{stars(h['score'])}</span><span class="meta">{h['score']}/10</span></div>
 <div class="row"><b>내용</b>{e(h.get('summary', ''))}</div>
 <div class="row"><b>포인트</b>{e(h.get('why', ''))}</div>{note}{quotes}
</div>""")
    ideas = ""
    if analysis.get("compilation_ideas"):
        ideas = "<h2>영상 아이디어</h2>" + "".join(
            f'<div class="card"><div class="title">{e(c.get("title", ""))}</div><div>{e(c.get("description", ""))}</div></div>'
            for c in analysis["compilation_ideas"])
    chapters = ""
    if analysis.get("chapters"):
        chapters = "<h2>방송 구성</h2><table>" + "".join(
            f"<tr><td>{e(str(c.get('start', '')))} ~ {e(str(c.get('end', '')))}</td><td>{e(c.get('what', ''))}</td></tr>"
            for c in analysis["chapters"]) + "</table>"
    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>편집 후보 — {e(meta['vod'])}</title>
<style>{HTML_CSS}</style></head><body><div class="wrap">
<h1>편집 후보</h1><div class="meta">{e(meta['vod'])} · 방송 길이 {fmt(meta['duration'])} · 후보 {len(hs)}개 · {e(meta['generated'])}</div>
<h2>방송 요약</h2><div class="summary">{e(analysis.get('stream_summary', '').strip())}</div>
<h2>편집 후보</h2>
<div class="bar" id="sort"><button data-k="time" class="on">시간순</button><button data-k="score">점수순</button></div>
<div class="bar" id="filt"><button data-f="all" class="on">전체</button><button data-f="본편">본편</button><button data-f="쇼츠">쇼츠</button><button data-f="7">7점 이상</button></div>
<div id="list">{''.join(cards)}</div>{ideas}{chapters}
<div class="toast" id="toast">복사됨</div></div>
<script>
const list=document.getElementById('list');let sortK='time',filt='all';
function apply(){{const c=[...list.children];c.sort((a,b)=>sortK==='time'?a.dataset.s-b.dataset.s:b.dataset.score-a.dataset.score);
c.forEach(x=>{{list.appendChild(x);const f=x.dataset.format,s=+x.dataset.score;
x.style.display=(filt==='all'||(filt==='7'&&s>=7)||f===filt||(f==='둘다'&&(filt==='본편'||filt==='쇼츠')))?'':'none'}})}}
document.querySelectorAll('#sort button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('#sort button').forEach(x=>x.classList.remove('on'));b.classList.add('on');sortK=b.dataset.k;apply()}});
document.querySelectorAll('#filt button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('#filt button').forEach(x=>x.classList.remove('on'));b.classList.add('on');filt=b.dataset.f;apply()}});
document.querySelectorAll('.time').forEach(t=>t.onclick=()=>{{try{{navigator.clipboard.writeText(t.textContent);const o=document.getElementById('toast');o.classList.add('show');setTimeout(()=>o.classList.remove('show'),900)}}catch(e){{}}}});
</script></body></html>"""
    path.write_text(doc, encoding="utf-8")


def export_track_samples(vod: Path, out_dir: Path | None = None) -> dict:
    """30-second sample of every audio track + how active each one is."""
    info = probe(vod)
    work = out_dir or (vod.parent / f"{vod.stem}_scout" / "track_samples")
    work.mkdir(parents=True, exist_ok=True)
    start = min(600, (info["duration"] or 60) * 0.3)
    res = []
    for i, t in enumerate(info["tracks"]):
        progress("extract", i / max(1, len(info["tracks"])), f"track {t['number']}")
        check_cancel()
        safe = re.sub(r"[^\w-]+", "_", t["title"]) if t["title"] else ""
        name = f"track{t['number']}" + (f"_{safe}" if safe else "") + ".m4a"
        entry = {**t, "sample": None, "active": None, "level": None}
        try:
            run_ffmpeg(["-ss", str(start), "-t", "30", "-i", str(vod), "-map", f"0:a:{t['number'] - 1}",
                        "-c:a", "aac", "-b:a", "128k", str(work / name)])
            entry["sample"] = str(work / name)
            r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(work / name),
                                "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1"], capture_output=True, **NOWIN)
            a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
            if a.size >= 4000:
                fr = a[: a.size // 4000 * 4000].reshape(-1, 4000)     # 0.5 s frames
                db = 10 * np.log10((fr ** 2).mean(1) + 1e-10)
                entry["active"] = round(float((db > -45).mean()), 2)
                entry["level"] = round(float(np.percentile(db, 90)), 1)
        except Exception:
            pass
        res.append(entry)
    return {"file": str(vod), "duration": info["duration"], "start": start, "dir": str(work), "tracks": res}


def work_dir(cfg: dict, vod: Path) -> Path:
    return Path(cfg.get("output_dir") or vod.parent) / f"{vod.stem}_scout"


def needs_audio(work: Path, tracks: list[dict]) -> bool:
    """True if the recording itself is still needed (some track's audio hasn't been processed yet)."""
    return any(_track_needs_audio(work, t) for t in tracks)


def migrate_legacy(work: Path):
    """Older versions saved transcripts per role ("mic.transcript.json"); now they are per track number."""
    meta = read_meta(work)
    for t in meta.get("tracks") or []:
        n, key = t.get("number"), t.get("key")
        if not n or not key:
            continue
        old, new = work / f"{key}.transcript.json", work / f"t{n}.transcript.json"
        if old.exists() and not new.exists():
            old.replace(new)


_GENERIC_LABEL = re.compile(r"^(unnamed|chapter|챕터)\s*\d*$", re.I)


def read_marker_file(path: Path) -> list[dict]:
    """Stream Desk's sidecar file (see STREAM_DESK_MARKERS.md)."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return [{"t": float(m["t"]), "label": str(m.get("label") or ""), "source": "markers file"}
                for m in d.get("markers") or [] if "t" in m]
    except Exception:
        return []


CHAT_TYPES = ("chat", "donation")          # what SkyEye uses; "subscription"/"gap" items are kept in chat.json only


def read_chat_file(path: Path) -> list[dict]:
    """Chat saved by Arkbird in the same sidecar file as the marks (markers file version 2, see ARKBIRD_CHAT_LOG.md).
    Version 1 files have no "chat" and give []. Times are raw (when Arkbird got the message); sorted, bad items skipped."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        items = d.get("chat") if isinstance(d, dict) else None
    except Exception:
        return []
    out = []
    for c in items or []:
        try:
            t = float(c["t"])
            if not (t >= 0) or t == float("inf"):
                continue
            typ = str(c.get("type") or "chat")
            item = {"t": round(t, 2), "type": typ, "nick": str(c.get("nick") or "").strip()[:40],
                    "role": str(c.get("role") or "viewer"), "text": str(c.get("text") or "").strip()[:500]}
            if c.get("amount") is not None:
                item["amount"] = c["amount"]
            out.append(item)
        except Exception:
            continue
    out.sort(key=lambda c: c["t"])
    return out


def save_chat(work: Path, chat: list[dict]):
    """<work>/chat.json: [t, type, nick, role, text, amount?] per line, so re-runs don't need the markers file."""
    rows = [[c["t"], c["type"], c["nick"], c["role"], c["text"]] + ([c["amount"]] if "amount" in c else [])
            for c in chat]
    (work / "chat.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def read_saved_chat(work: Path) -> list[dict]:
    try:
        rows = json.loads((work / "chat.json").read_text(encoding="utf-8"))
        return [{"t": float(r[0]), "type": r[1], "nick": r[2], "role": r[3], "text": r[4],
                 **({"amount": r[5]} if len(r) > 5 else {})} for r in rows]
    except Exception:
        return []


def load_chat(work: Path, vod: Path) -> list[dict]:
    """Chat from the markers file (copied to <work>/markers.json from Drive, or next to a local recording). If there
    is none, the copy saved by an earlier run is used. A new read replaces chat.json."""
    best: list[dict] = []
    for f in (work / "markers.json", vod.parent / f"{vod.stem}.markers.json"):
        if f.exists():
            c = read_chat_file(f)
            if len(c) > len(best):
                best = c
    if best:
        save_chat(work, best)
        return best
    return read_saved_chat(work)


def viewer_chat(chat: list[dict], delay: float = 0.0) -> list[dict]:
    """Viewer (not the streamer's own) chat and donation messages, moved earlier by the chat delay: chat lags behind
    what happened on stream (stream latency + reading + typing). Adds "te" = the time on the video."""
    return [dict(c, te=max(0.0, c["t"] - delay)) for c in chat
            if c.get("type") in CHAT_TYPES and c.get("role") != "streamer" and c.get("text")]


def chat_bursts(times: list[float], win: float = 30.0) -> list[tuple[float, int]]:
    """Moments when chat suddenly gets busy: >= 3x the stream's usual (median of the 30 s windows that had chat)
    and at least 4 messages in 30 s. Returns (start of the busiest window, message count)."""
    times = sorted(times)
    n = len(times)
    if n < 4:
        return []
    cnt, j = [], 0
    for i in range(n):
        while j < n and times[j] < times[i] + win:
            j += 1
        cnt.append(j - i)
    per_bin: dict[int, int] = {}
    for t in times:
        per_bin[int(t // win)] = per_bin.get(int(t // win), 0) + 1
    thr = max(4, 3 * statistics.median(per_bin.values()))
    out, i = [], 0
    while i < n:
        if cnt[i] >= thr:
            k, best = i, i
            while k < n and times[k] < times[i] + win:
                if cnt[k] > cnt[best]:
                    best = k
                k += 1
            end = times[best] + win
            anchor, top = best, 0                       # the line goes where chat gets densest (10 s), not at the window edge
            for m in range(best, n):
                if times[m] >= end:
                    break
                c10 = sum(1 for q in range(m, n) if times[q] < times[m] + 10)
                if c10 > top:
                    anchor, top = m, c10
            out.append((times[anchor], cnt[best]))
            while i < n and times[i] < end:
                i += 1
        else:
            i += 1
    return out


def _chat_key(text: str) -> str:
    """Near-identical messages share a key: 'ㅋㅋㅋㅋ' and 'ㅋㅋ', 'gg!!' and 'GG'."""
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[\s.!?~,]+", "", text.lower()))


def chat_lines(vc: list[dict], bucket: float = 5.0) -> list[tuple[float, str]]:
    """Chat for Claude's timeline, kept short: donations always; ordinary chat at most about one line per 5 s, and a
    crowd saying the same thing (>= 3 near-identical messages in 5 s) becomes one 'reaction' line."""
    lines: list[tuple[float, str]] = []
    buckets: dict[int, list[dict]] = {}
    for c in vc:
        if c["type"] == "donation":
            amt = c.get("amount")
            try:
                amt = f" ({int(float(amt)):,})" if amt not in (None, "") else ""
            except Exception:
                amt = ""
            lines.append((c["te"], f"[{fmt(c['te'])}] 💰 후원 채팅 {c['nick']}{amt}: {c['text'][:200]}"))
        else:
            buckets.setdefault(int(c["te"] // bucket), []).append(c)
    for _, msgs in sorted(buckets.items()):
        groups: dict[str, list[dict]] = {}
        for c in msgs:
            groups.setdefault(_chat_key(c["text"]) or c["text"], []).append(c)
        crowd = max(groups.values(), key=len)
        if len(crowd) >= 3:
            lines.append((crowd[0]["te"], f"[{fmt(crowd[0]['te'])}] 💬 채팅 {crowd[0]['text'][:40]} 반응 {len(crowd)}개"))
        else:
            c = max(msgs, key=lambda m: len(m["text"]))
            lines.append((c["te"], f"[{fmt(c['te'])}] 💬 채팅 {c['nick']}: {c['text'][:120]}"))
    return lines


def chat_activity(vc: list[dict], duration: float, win: float = 30.0) -> dict:
    """For the report: messages per 30 s over the whole stream, plus the bursts."""
    n = int(duration // win) + 1
    bins = [0] * max(1, n)
    for c in vc:
        i = int(c["te"] // win)
        if 0 <= i < len(bins):
            bins[i] += 1
    return {"bin": win, "bins": bins, "bursts": [[round(t, 1), k] for t, k in chat_bursts([c["te"] for c in vc])]}


def load_markers(work: Path, vod: Path, info: dict) -> list[dict]:
    """'Mark moment' presses during the stream: OBS chapters inside the recording + a sidecar markers file."""
    ms = []
    for c in info.get("chapters") or []:
        if c["t"] < 1 and (not c.get("label") or _GENERIC_LABEL.match(c["label"])):
            continue                                       # some files have a dummy chapter at 0:00
        label = "" if _GENERIC_LABEL.match(c.get("label") or "") else c.get("label", "")
        ms.append({"t": c["t"], "label": label, "source": "OBS chapter"})
    for f in (work / "markers.json", vod.parent / f"{vod.stem}.markers.json"):
        if f.exists():
            ms += read_marker_file(f)
    ms.sort(key=lambda m: m["t"])
    out = []
    for m in ms:                                            # same press recorded twice (chapter + file)
        if out and abs(out[-1]["t"] - m["t"]) < 3:
            out[-1]["label"] = out[-1]["label"] or m["label"]
            continue
        out.append(m)
    return out


def match_marker_files(files: list[tuple[str, dict]], duration: float, vod_name: str) -> dict | None:
    """Pick the markers file that belongs to a recording even if the video was renamed:
    same file name, or the recording length matches (within a few seconds)."""
    stem = Path(vod_name).stem
    for name, d in files:
        if Path(name).name.removesuffix(".markers.json") == stem:
            return d
    best = None
    for name, d in files:
        rd = (d.get("recording") or {}).get("duration")
        if rd and abs(float(rd) - duration) < 5:
            if best is None or abs(float(rd) - duration) < abs(float((best.get("recording") or {})["duration"]) - duration):
                best = d
    return best


def mic_sanity(tracks: list[dict], transcripts: dict) -> str | None:
    """Warn when the 'mic' track has far less speech than another track — usually a wrong track setting."""
    mic = next((t for t in tracks if t["role"] == "mic"), None)
    if not mic:
        return None
    chars = {t["key"]: sum(len(s["text"]) for s in transcripts.get(t["key"], [])) for t in tracks if t["transcribe"]}
    best = max((t for t in tracks if t["transcribe"] and t is not mic), key=lambda t: chars.get(t["key"], 0), default=None)
    if best and chars.get(best["key"], 0) > 1500 and chars.get(mic["key"], 0) < 0.15 * chars[best["key"]]:
        return (f"Track {mic['number']} (set as your mic) has almost no speech, but track {best['number']} "
                f"(set as {best['label']}) has a lot. Your voice is probably on track {best['number']} — "
                "check Settings → Audio tracks.")
    return None


def load_probe(vod: Path, work: Path) -> dict:
    """File info is cached so later steps (e.g. --redo-ai) work even after the video is deleted."""
    pj = work / "probe.json"
    if pj.exists():
        return json.loads(pj.read_text(encoding="utf-8"))
    if not vod.exists():
        sys.exit(f"Recording not found: {vod}")
    info = probe(vod)
    if info["tracks"]:
        pj.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info


# ---------------------------------------------------------------- previews
PREVIEW_MAX = 600          # longest preview clip (s)
_ENCODER: list[str] | None = None


def preview_key(s: float, e: float) -> str:
    return f"{int(s)}-{int(e)}"


def preview_path(work: Path, s: float, e: float) -> Path:
    return work / "previews" / f"{preview_key(s, e)}.mp4"


def _video_encoder() -> list[str]:
    """NVIDIA hardware encoder if this ffmpeg/GPU has it (much faster), otherwise fast software x264."""
    global _ENCODER
    if _ENCODER is None:
        try:
            run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.2", "-c:v", "h264_nvenc",
                        "-f", "null", "-"])
            _ENCODER = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "30", "-b:v", "0"]
        except Exception:
            _ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27"]
    return _ENCODER


def make_previews(vod: Path, work: Path, cfg: dict, info: dict, hs: list[dict]) -> int:
    """Small 480p clips of every pick, with all stream audio mixed, so Stan can review in seconds."""
    if not vod.exists() or cfg.get("make_previews") is False:
        return 0
    todo = [h for h in hs if not preview_path(work, h["s"], h["e"]).exists()]
    if not todo:
        return 0
    (work / "previews").mkdir(exist_ok=True)
    roles = {int(t.get("number", 0)): str(t.get("role", "")).lower() for t in cfg.get("tracks") or []}
    nums = [t["number"] for t in info.get("tracks") or [] if roles.get(t["number"], "ignore") != "ignore"]
    if not nums:
        nums = [t["number"] for t in info.get("tracks") or []][:1]
    if len(nums) > 1:
        ins = "".join(f"[0:a:{n - 1}]" + ("volume=0.5," if roles.get(n) == "bgm" else "") + f"aformat=channel_layouts=stereo[a{i}];"
                      for i, n in enumerate(nums))
        amix = ins + "".join(f"[a{i}]" for i in range(len(nums))) + \
            f"amix=inputs={len(nums)}:normalize=0:dropout_transition=0,alimiter=limit=0.95[aout]"
        audio = ["-filter_complex", amix, "-map", "0:v:0?", "-map", "[aout]"]
    else:
        audio = ["-map", "0:v:0?", "-map", f"0:a:{nums[0] - 1}?"]
    enc = _video_encoder()
    log(f"      making {len(todo)} preview clip(s)…")
    for i, h in enumerate(todo):
        check_cancel()
        progress("previews", i / len(todo), f"{i + 1}/{len(todo)} — {h.get('title', '')[:40]}")
        a = max(0.0, h["s"] - 3)
        dur = min(h["e"] + 3, h["s"] + PREVIEW_MAX) - a
        out = preview_path(work, h["s"], h["e"])
        part = out.with_suffix(".part.mp4")
        args = ["-ss", f"{a:.2f}", "-t", f"{dur:.2f}", "-i", str(vod)] + audio + \
            ["-vf", "scale=-2:480,fps=30"] + enc + ["-c:a", "aac", "-b:a", "96k", "-ac", "2",
                                                   "-movflags", "+faststart", str(part)]
        try:
            run_ffmpeg(args)
        except RuntimeError:
            if enc[1] == "h264_nvenc":                       # hardware encoder refused: retry in software
                globals()["_ENCODER"] = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27"]
                enc = _ENCODER
                args = ["-ss", f"{a:.2f}", "-t", f"{dur:.2f}", "-i", str(vod)] + audio + \
                    ["-vf", "scale=-2:480,fps=30"] + enc + ["-c:a", "aac", "-b:a", "96k", "-ac", "2",
                                                           "-movflags", "+faststart", str(part)]
                run_ffmpeg(args)
            else:
                raise
        part.replace(out)
    progress("previews", 1.0, "done")
    return len(todo)


def read_meta(work: Path) -> dict:
    p = work / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def write_meta(work: Path, meta: dict):
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def report_base(work: Path, meta: dict | None = None) -> str:
    meta = meta if meta is not None else read_meta(work)
    stem = Path(meta["vod"]).stem if meta.get("vod") else work.name.removesuffix("_scout")
    return f"{stem}_편집후보"


def write_reports(work: Path, cfg: dict) -> list[dict]:
    """(Re)build the HTML report from analysis.json + meta.json — no Claude call."""
    meta = read_meta(work)
    analysis = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
    rcfg = dict(cfg, youtube_url=meta.get("youtube_url") or "",          # the stream's own archive link
                youtube_offset_seconds=meta.get("youtube_offset_seconds") or 0)
    hs = normalize(analysis)
    m = {"vod": meta.get("vod", work.name), "duration": meta.get("duration", 0),
         "generated": meta.get("generated") or time.strftime("%Y-%m-%d %H:%M")}
    base = report_base(work, meta)
    write_html(work / f"{base}.html", rcfg, m, analysis, hs)
    return hs


def cmd_run(vod: Path, cfg: dict, redo_ai=False, fresh=False):
    t_start = time.time()
    log(f"\n=== SkyEye {VERSION} — {vod.name} ===")
    work = work_dir(cfg, vod)
    if fresh and work.exists():
        for p in work.glob("*"):
            if p.is_file():
                p.unlink()
    work.mkdir(parents=True, exist_ok=True)

    info = load_probe(vod, work)
    migrate_legacy(work)
    timings: dict = {}
    duration = info["duration"] or 0
    if not info["tracks"]:
        sys.exit("No audio tracks in this file.")
    log(f"Length {fmt(duration)}, {len(info['tracks'])} audio tracks")
    tracks = resolve_tracks(cfg, info)
    log("Using: " + ", ".join(f"#{t['number']}={t['label']}({t['role']})" for t in tracks))

    if needs_audio(work, tracks):
        if not vod.exists():
            sys.exit(f"Recording not found (needed for transcription): {vod}")
        t0 = time.time()
        extract_tracks(vod, tracks, work, duration)
        for t in tracks:                                       # loudness first: it's cheap, and lets us
            load_db(work, t)                                   # skip silent tracks before Whisper
        timings["extract"] = time.time() - t0

    transcripts = {}
    to_tx = [t for t in tracks if t["transcribe"]]
    if any(not (work / f"{t['file']}.transcript.json").exists() for t in to_tx):
        log("[2/5] Transcribing (Whisper)…")
    t0 = time.time()
    for i, t in enumerate(to_tx):
        transcripts[t["key"]] = transcribe_track(cfg, t, work, duration, i, len(to_tx))
    if time.time() - t0 > 1:
        timings["transcribe"] = time.time() - t0

    t0 = time.time()
    signals = compute_signals(tracks, work, duration / 3600)
    timings["signals"] = time.time() - t0

    if not cfg.get("keep_extracted_audio"):
        for t in tracks:
            p = work / f"{t['file']}.wav"
            if p.exists():
                p.unlink()

    warn = mic_sanity(tracks, transcripts)
    if warn:
        log("      ⚠ " + warn)

    markers = load_markers(work, vod, info)
    if markers:
        log(f"      ⭐ {len(markers)} moment(s) marked during the stream")
    chat = load_chat(work, vod)
    delay = float(cfg.get("chat_delay_seconds") if cfg.get("chat_delay_seconds") is not None else 6)
    vc = viewer_chat(chat, delay)
    if vc:
        log(f"      💬 {len(vc)} chat message(s) from Arkbird (chat delay {delay:g}s)")
    timeline = build_timeline(tracks, transcripts, signals, markers, chat, delay)
    (work / "transcript.txt").write_text(
        f"{vod.name} — 전체 전사 + 신호 타임라인\n\n" + "\n".join(l for _, l in timeline), encoding="utf-8")

    meta = read_meta(work)
    meta.update({"vod": vod.name, "duration": duration, "warning": warn or "", "markers": markers,
                 "tracks": [{k: t[k] for k in ("key", "label", "role", "number")} for t in tracks]})
    meta.setdefault("youtube_url", "")
    write_meta(work, meta)

    t0 = time.time()
    focus = cfg.get("focus") if isinstance(cfg.get("focus"), dict) and focus_text(cfg) else {}
    if focus:
        log(f"      🎯 Focus{' (' + focus['preset'] + ')' if focus.get('preset') else ''}: {focus_text(cfg)[:120]}")
    fresh = redo_ai or not (work / "analysis.json").exists()
    analysis = analyze(dict(cfg, _has_chat=bool(vc)), tracks, timeline, duration, vod.name, work, redo=redo_ai)
    if fresh:                                                  # a saved analysis keeps the focus it was made with
        if focus:
            meta["focus"] = {k: focus.get(k, "") for k in ("preset", "text", "extra")}
        else:
            meta.pop("focus", None)
    meta["chat_count"] = len(vc)
    meta["chat_delay"] = delay
    if time.time() - t0 > 1:
        timings["claude"] = time.time() - t0
    log("[5/5] Writing report…")
    progress("report", 0.5)
    meta["generated"] = time.strftime("%Y-%m-%d %H:%M")
    if cfg.get("_download_secs"):
        timings = {"download": cfg["_download_secs"], **timings}
    meta["timings"] = {k: round(v, 1) for k, v in timings.items()}
    write_meta(work, meta)
    hs = write_reports(work, cfg)
    if vod.exists():
        t0 = time.time()
        try:
            if make_previews(vod, work, cfg, info, hs):
                timings["previews"] = time.time() - t0
        except Cancelled:
            raise
        except Exception as e:                       # previews are a convenience; never fail the job for them
            log(f"      ! preview clips failed: {str(e)[:300]}")
        meta["timings"] = {k: round(v, 1) for k, v in timings.items()}
        write_meta(work, meta)
    names = {"download": "download", "extract": "extract audio", "transcribe": "transcribe",
             "signals": "signals", "claude": "Claude", "previews": "previews"}
    log("Time per step: " + " · ".join(f"{names.get(k, k)} {v / 60:.1f} min" if v >= 60 else f"{names.get(k, k)} {v:.0f}s"
                                       for k, v in timings.items()))
    base = f"{vod.stem}_편집후보"
    log(f"\nDone in {(time.time() - t_start) / 60:.1f} min — {len(hs)} candidates.\n"
        f"  Report : {work / (base + '.html')}\n"
        f"  Full transcript: transcript.txt")
    if os.name == "nt" and cfg.get("_open_report", True):
        try:
            os.startfile(work / f"{base}.html")  # type: ignore[attr-defined]
        except Exception:
            pass
    return work


# ----------------------------------------------------------- Google Drive
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".m4v"}


def rclone_cmd() -> list[str]:
    exe = HERE / "bin" / ("rclone.exe" if os.name == "nt" else "rclone")
    if not exe.exists():
        found = shutil.which("rclone")
        if not found:
            sys.exit("rclone not found. Go to Settings → Google Drive and click “Connect Google Drive”.")
        exe = Path(found)
    return [str(exe), "--config", str(HERE / "rclone.conf")]


def drive_settings(cfg: dict) -> dict:
    d = dict(cfg.get("google_drive") or {})
    fid = str(d.get("folder_id") or "").strip()
    m = re.search(r"folders/([\w-]+)", fid)
    if m:
        fid = m.group(1)
    if not fid:
        sys.exit("Set google_drive → folder_id in config.yaml (the Drive folder link works too).")
    d["folder_id"] = fid
    d["remote"] = d.get("remote") or "gdrive"
    d["download_dir"] = Path(d.get("download_dir") or HERE / "downloads")
    d["results_dir"] = Path(d.get("results_dir") or HERE / "results")
    return d


# Google Drive is READ-ONLY for SkyEye: it can list the folder and download copies, nothing else.
_RCLONE_ALLOWED = {"lsjson", "lsd", "copyto"}
_RCLONE_FORBIDDEN_FLAGS = ("--delete", "--backup-dir", "--suffix", "--remove", "--drive-trashed", "--max-delete")


def _guard_rclone(d: dict, args: list[str]):
    cmd = args[0] if args else ""
    if cmd not in _RCLONE_ALLOWED or any(a.startswith(_RCLONE_FORBIDDEN_FLAGS) for a in args):
        raise RuntimeError(f"Blocked Drive command '{cmd}' — SkyEye never changes your Drive.")
    if cmd == "copyto" and (len(args) < 3 or args[2].startswith(f"{d['remote']}:")):
        raise RuntimeError("Blocked a write to Google Drive — SkyEye only downloads from Drive.")


def rclone_run(d: dict, args: list[str], capture=True) -> str:
    _guard_rclone(d, args)
    cmd = rclone_cmd() + args + ["--drive-root-folder-id", d["folder_id"]]
    r = subprocess.run(cmd, capture_output=capture, text=True, encoding="utf-8", errors="replace", **NOWIN)
    if r.returncode != 0:
        err = (r.stderr or "")[-1500:] if capture else ""
        if "token" in err.lower() or "oauth" in err.lower() or "401" in err:
            err += "\n→ Google login expired or missing. Go to Settings → Google Drive → “Log in again”."
        raise RuntimeError(f"rclone {args[0]} failed.\n{err}")
    return r.stdout if capture else ""


def drive_list(d: dict) -> list[dict]:
    raw = rclone_run(d, ["lsjson", f"{d['remote']}:", "--files-only"])
    items = [x for x in json.loads(raw or "[]") if Path(x["Name"]).suffix.lower() in VIDEO_EXT]
    items.sort(key=lambda x: x.get("ModTime", ""), reverse=True)
    return items


def drive_status(cfg: dict, d: dict, item: dict) -> str:
    stem = Path(item["Name"]).stem
    return work_status(cfg, d["results_dir"] / f"{stem}_scout", stem)


def work_status(cfg: dict, work: Path, stem: str) -> str:
    if (work / f"{stem}_편집후보.html").exists():
        return "done"
    if (work / "probe.json").exists():
        info = json.loads((work / "probe.json").read_text(encoding="utf-8"))
        try:
            tracks = resolve_tracks(cfg, info)
        except SystemExit:
            return "new"
        if not needs_audio(work, tracks):
            return "transcribed"      # only the Claude step is left — no download needed
    return "new"


def _iso_secs(v) -> float:
    """rclone's ModTime ('2026-10-19T21:00:03.123456789Z') as seconds; 0 if unknown."""
    try:
        from datetime import datetime
        t = re.sub(r"(\.\d{6})\d+", r"\1", str(v)).replace("Z", "+00:00")
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0.0


def drive_fetch_markers(d: dict, item: dict, work: Path, duration: float | None = None):
    """Copy the Stream Desk markers file for this recording from the Drive folder (if any) into the results."""
    try:
        raw = rclone_run(d, ["lsjson", f"{d['remote']}:", "--files-only", "--include", "*.markers.json"])
        entries = json.loads(raw or "[]")
    except Exception:
        return
    names = [x["Name"] for x in entries]
    if not names:
        return
    stem = Path(item["Name"]).stem
    cand = [n for n in names if n.removesuffix(".markers.json") == stem]
    if not cand:
        # Arkbird now writes a file for every stream, so there can be many: if the recording was renamed, only look at
        # the ones uploaded closest to it in time (match_marker_files then checks the length, within 5 s).
        mod = {x["Name"]: _iso_secs(x.get("ModTime")) for x in entries}
        ref = _iso_secs(item.get("ModTime"))
        cand = sorted(names, key=lambda n: abs(mod[n] - ref) if ref else -mod[n])
    tmp = work / "_markers_dl"
    files = []
    for n in cand[:40]:
        try:
            rclone_run(d, ["copyto", f"{d['remote']}:{n}", str(tmp / n)])
            files.append((n, json.loads((tmp / n).read_text(encoding="utf-8"))))
        except Exception:
            continue
    if duration is None and (work / "probe.json").exists():
        duration = json.loads((work / "probe.json").read_text(encoding="utf-8")).get("duration")
    m = match_marker_files(files, float(duration or 0), item["Name"])
    if m:
        (work / "markers.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)


def drive_needs_download(cfg: dict, d: dict, item: dict) -> bool:
    """Only download when some track's audio still has to be processed (transcripts/loudness are cached)."""
    stem = Path(item["Name"]).stem
    work = d["results_dir"] / f"{stem}_scout"
    migrate_legacy(work) if work.exists() else None
    if not (work / "probe.json").exists():
        return True
    try:
        tracks = resolve_tracks(cfg, json.loads((work / "probe.json").read_text(encoding="utf-8")))
    except SystemExit:
        return True
    return needs_audio(work, tracks)


def drive_download(d: dict, item: dict, quiet: bool = False, on_progress=None, cancel=None) -> Path:
    """quiet=True is used for downloading the NEXT recording in the background while another one is processed."""
    say = (lambda m: None) if quiet else log
    report = on_progress or ((lambda f, t: None) if quiet else (lambda f, t: progress("download", f, t)))
    stop = cancel or check_cancel
    dest_dir = d["download_dir"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / item["Name"]
    size = int(item.get("Size") or 0)
    if dest.exists() and dest.stat().st_size == size:
        say("      already downloaded")
        return dest
    free = shutil.disk_usage(dest_dir).free
    if free < size * 1.05 + 3e9:
        sys.exit(f"Not enough disk space in {dest_dir}: need ~{(size + 3e9) / 1e9:.0f} GB, "
                 f"free {free / 1e9:.0f} GB. Change the download folder in Settings → Google Drive.")
    say(f"[0/5] Downloading from Google Drive ({size / 1e9:.1f} GB)…")
    report(0.0, f"0 / {size / 1e9:.1f} GB")
    t0 = time.time()
    _guard_rclone(d, ["copyto", f"{d['remote']}:{item['Name']}", str(dest)])
    cmd = rclone_cmd() + ["copyto", f"{d['remote']}:{item['Name']}", str(dest),
                          "--drive-root-folder-id", d["folder_id"], "--multi-thread-streams", "16",
                          "--multi-thread-cutoff", "64M", "--buffer-size", "32M",
                          "--stats", "1s", "--stats-one-line", "--stats-log-level", "NOTICE"]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", **NOWIN)
    tail: list[str] = []
    try:
        for line in p.stderr:
            tail = (tail + [line])[-20:]
            m = re.search(r"([\d.]+\s*\w?i?B)\s*/\s*([\d.]+\s*\w?i?B),\s*(\d+)%,\s*([\d.]+\s*\w?i?B/s)(?:,\s*ETA\s*(\S+))?", line)
            if m:
                pct = int(m.group(3))
                detail = f"{m.group(1)} / {m.group(2)} · {m.group(4)}" + (f" · ETA {m.group(5)}" if m.group(5) else "")
                report(pct / 100, detail)
                if not HOOKS["log"] and not quiet:
                    print(f"\r      {pct:3d}%  {detail}      ", end="", flush=True)
            stop()
    except Cancelled:
        p.kill()
        raise
    p.wait()
    if not HOOKS["log"] and not quiet:
        print()
    if p.returncode != 0:
        err = "".join(tail)[-1500:]
        if "token" in err.lower() or "oauth" in err.lower():
            err += "\n→ Google login expired. Log in to Google Drive again."
        raise RuntimeError("Download failed.\n" + err)
    say(f"      downloaded in {(time.time() - t0) / 60:.1f} min")
    return dest


def remove_local_copy(d: dict, local: Path) -> bool:
    """Delete ONLY SkyEye's own temporary download on this PC — never anything on Google Drive."""
    try:
        dl = d["download_dir"].resolve()
        p = local.resolve()
    except Exception:
        return False
    if p.parent != dl or not p.is_file():
        return False
    synced = re.search(r"(google ?drive|my drive|내 드라이브|onedrive|dropbox)", str(dl), re.I)
    if synced:
        log("      (kept the downloaded copy: the download folder looks like a synced cloud folder, "
            "so deleting there could delete the cloud file. Choose a normal local folder in Settings.)")
        return False
    p.unlink()
    log("      removed the temporary copy from this PC — the original on Google Drive is untouched")
    return True
