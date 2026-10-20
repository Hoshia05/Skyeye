"""
SkyEye — post reports to a Discord channel through a webhook.

A webhook is a posting address for ONE channel (Discord: channel settings → Integrations →
Webhooks → New Webhook → Copy Webhook URL). No bot needed. The URL is stored in
discord_webhook.txt next to the app (keep it private: anyone with it can post there).
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import editor_page
import publish
import scout
from scout import HERE, fmt

WEBHOOK_FILE = HERE / "discord_webhook.txt"
_VALID = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
_TEST_OVERRIDE: dict = {}          # tests only: lets a local fake webhook receive posts

COLOR_REPORT = 0x8B7BFF            # purple: new report (all picks)
COLOR_KEPT = 0x4ADE80              # green: Stan's final list


def get_webhook() -> str:
    try:
        return WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def set_webhook(url: str):
    url = (url or "").strip()
    if not url:
        if WEBHOOK_FILE.exists():
            WEBHOOK_FILE.unlink()
        return
    if not _VALID.match(url) and not _TEST_OVERRIDE.get("allow"):
        raise ValueError("That doesn't look like a Discord webhook link "
                         "(it starts with https://discord.com/api/webhooks/…).")
    WEBHOOK_FILE.write_text(url, encoding="utf-8")


def settings(cfg: dict) -> dict:
    d = dict(cfg.get("discord") or {})
    d.setdefault("mention", "")
    d.setdefault("top_n", 12)
    d.setdefault("attach_files", True)
    d.setdefault("clips", True)              # one message per pick with a playable video clip
    d.setdefault("clip_max", 60)             # seconds
    d.setdefault("upload_mb", 10)            # the server's upload limit: 10 (normal), 50 or 100 (boosted)
    return d


# ------------------------------------------------------------------ clips
def clip_for(work: Path, h: dict, cfg: dict) -> Path | None:
    """The moment itself (±2 s, capped) cut from the review preview, compressed to fit Discord's upload limit."""
    pv = scout.preview_path(work, h["s"], h["e"])
    if not pv.exists():
        return None
    ds = settings(cfg)
    maxlen = max(5.0, float(ds["clip_max"]))
    limit = float(ds["upload_mb"]) * 1024 * 1024 * 0.92
    pv_start = max(0.0, h["s"] - 3)                      # previews start 3 s before the pick
    a = max(0.0, (h["s"] - 2) - pv_start)
    dur = max(3.0, min(h["e"] + 2, h["s"] - 2 + maxlen) - max(0.0, h["s"] - 2))
    out_dir = work / "discord_clips"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{scout.preview_key(h['s'], h['e'])}_{int(maxlen)}s_{int(ds['upload_mb'])}mb.mp4"
    if out.exists() and out.stat().st_size <= limit:
        return out
    for factor in (0.95, 0.7, 0.5):
        total_kbps = limit * 8 / 1000 / dur * factor
        v = int(max(120, min(2500, total_kbps - 96 - 30)))
        scout.run_ffmpeg(["-ss", f"{a:.2f}", "-t", f"{dur:.2f}", "-i", str(pv),
                          "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{v}k", "-maxrate", f"{int(v * 1.3)}k",
                          "-bufsize", f"{v * 2}k", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                          "-movflags", "+faststart", str(out)])
        if out.stat().st_size <= limit:
            return out
    out.unlink(missing_ok=True)
    return None


def _yt(meta: dict, cfg: dict, sec: float) -> str | None:
    c = dict(cfg)
    if meta.get("youtube_url"):
        c["youtube_url"] = meta["youtube_url"]
        c["youtube_offset_seconds"] = meta.get("youtube_offset_seconds", 0)
    return scout.yt_link(c, sec)


def _fmt_label(f: str) -> str:
    return {"쇼츠": "쇼츠", "둘다": "본편+쇼츠"}.get(f or "", "본편")


def _pick_line(h: dict, meta: dict, cfg: dict) -> str:
    link = _yt(meta, cfg, h["s"])
    t = f"`{fmt(h['s'])}~{fmt(h['e'])}`"
    t = f"[{t}]({link})" if link else t
    return f"{t} **{h.get('title', '')}** · {_fmt_label(h.get('format'))} · {h.get('score', '')}/10"


def _mention_payload(mention: str) -> tuple[str, dict]:
    m = (mention or "").strip()
    if not m:
        return "", {"parse": []}
    if m in ("@here", "@everyone"):
        return m, {"parse": ["everyone"]}
    if re.fullmatch(r"\d{5,25}", m):                         # a role ID
        return f"<@&{m}>", {"roles": [m]}
    if re.fullmatch(r"<@&\d+>", m):
        return m, {"roles": [m[3:-1]]}
    return m, {"parse": []}


def build_message(work: Path, cfg: dict, kind: str = "report", page_url: str | None = None,
                  page_note: str = "") -> tuple[dict, list[Path]]:
    """kind = 'report' (new analysis, top picks) or 'kept' (Stan's final list for the editors).
    page_url = the editors' web page (clips play inside it); page_note = a warning line (e.g. upload failed)."""
    meta = scout.read_meta(work)
    analysis = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
    hs = scout.normalize(analysis)
    ds = settings(cfg)
    name = Path(meta.get("vod") or work.name.removesuffix("_scout")).stem
    files: list[Path] = []

    if kind == "kept":
        rv = {}
        try:
            rv = json.loads((work / "review.json").read_text(encoding="utf-8")).get("decisions") or {}
        except Exception:
            pass
        picks = [h for h in hs if (rv.get(scout.preview_key(h["s"], h["e"])) or {}).get("d") == "keep"]
        picks.sort(key=lambda h: h["s"])
        title = f"✅ {name} — 편집할 장면 {len(picks)}개"
        intro = "호시아가 직접 고른 장면입니다. 시간순 정리."
        color = COLOR_KEPT
        base = f"{name}_편집목록"
    else:
        picks = sorted(hs, key=lambda h: -int(h.get("score") or 0))[: int(ds["top_n"])]
        picks.sort(key=lambda h: h["s"])
        title = f"📺 {name} — 편집 후보 {len(hs)}개"
        intro = (analysis.get("stream_summary") or "").strip()
        if len(intro) > 700:
            intro = intro[:690].rsplit(" ", 1)[0] + " …"
        color = COLOR_REPORT
        base = scout.report_base(work, meta)

    lines = [_pick_line(h, meta, cfg) for h in picks]
    desc = ""
    if page_url:
        desc += f"▶ **[영상으로 보기 — 편집 페이지 열기]({page_url})**\n미리보기 영상이 페이지 안에서 바로 재생돼요.\n\n"
    if page_note:
        desc += page_note + "\n\n"
    desc += intro + ("\n\n" if intro else "")
    if kind == "report" and len(hs) > len(picks):
        desc += f"**점수 높은 {len(picks)}개** (전체는 첨부 파일)\n"
    shown = 0
    for ln in lines:
        if len(desc) + len(ln) + 1 > 3900:
            break
        desc += ln + "\n"
        shown += 1
    if shown < len(lines):
        desc += f"… 외 {len(lines) - shown}개 (첨부 파일 참고)"

    fields = [{"name": "방송 길이", "value": fmt(meta.get("duration") or 0), "inline": True}]
    if kind == "report":
        shorts = sum(1 for h in hs if h.get("format") in ("쇼츠", "둘다"))
        fields.append({"name": "쇼츠 후보", "value": str(shorts), "inline": True})
        if meta.get("markers"):
            fields.append({"name": "⭐ 방송 중 표시", "value": str(len(meta["markers"])), "inline": True})
    if meta.get("youtube_url"):
        fields.append({"name": "다시보기", "value": f"[YouTube]({meta['youtube_url']})", "inline": True})

    embed = {"title": title[:256], "description": desc.strip()[:4096], "color": color, "fields": fields,
             "footer": {"text": "SkyEye"}, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if page_url:
        embed["url"] = page_url                                  # the title becomes a link too
    if embed["timestamp"]:
        ts = embed["timestamp"]
        embed["timestamp"] = ts[:-2] + ":" + ts[-2:] if re.search(r"[+-]\d{4}$", ts) else ts
    content, allowed = _mention_payload(ds.get("mention", ""))
    payload = {"username": "SkyEye", "content": content, "embeds": [embed], "allowed_mentions": allowed}

    if ds.get("attach_files", True) and not page_url:           # the page replaces the attached file
        f = work / f"{base}.html"
        if f.exists() and f.stat().st_size < 8 * 1024 * 1024:
            files.append(f)
    return payload, files


def _send(url: str, payload: dict, files: list[Path]) -> None:
    import httpx
    url = url + ("&" if "?" in url else "?") + "wait=true"
    for attempt in range(3):
        if files:
            multipart = [("payload_json", (None, json.dumps(payload, ensure_ascii=False), "application/json"))]
            for i, f in enumerate(files):
                ctype = {".html": "text/html", ".md": "text/markdown", ".mp4": "video/mp4"}.get(f.suffix, "application/octet-stream")
                multipart.append((f"files[{i}]", (f.name, f.read_bytes(), ctype)))
            r = httpx.post(url, files=multipart, timeout=60)
        else:
            r = httpx.post(url, json=payload, timeout=60)
        if r.status_code == 429:                             # rate limited: wait as told, then retry
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            time.sleep(min(wait, 30))
            continue
        if r.status_code >= 400:
            msg = r.text[:300]
            if r.status_code in (401, 404):
                msg = "The webhook doesn't exist anymore (deleted in Discord?). Paste a new one in Settings → Discord."
            raise RuntimeError(f"Discord said {r.status_code}: {msg}")
        return
    raise RuntimeError("Discord kept rate-limiting the post; try again in a minute.")


def post(work: Path, cfg: dict, kind: str = "report", on_progress=None, log=None) -> str:
    """The report card, then (if enabled) one message per pick with a playable clip of that moment."""
    url = _TEST_OVERRIDE.get("url") or get_webhook()
    if not url:
        raise RuntimeError("No Discord webhook set (Settings → Discord).")
    say = log or (lambda m: None)
    report = on_progress or (lambda f, t: None)
    if kind == "kept" and publish.configured(cfg):
        return _post_kept_page(url, work, cfg, report, say)
    payload, files = build_message(work, cfg, kind)
    report(0.02, "posting the report card…")
    _send(url, payload, files)
    ds = settings(cfg)
    if not ds.get("clips"):
        return payload["embeds"][0]["title"]
    meta = scout.read_meta(work)
    picks = _picks(work, cfg, kind)
    sent = missing = 0
    for i, h in enumerate(picks):
        scout.check_cancel()
        report((i + 0.3) / max(1, len(picks)), f"clip {i + 1}/{len(picks)} — {h.get('title', '')[:40]}")
        try:
            clip = clip_for(work, h, cfg)
        except Exception as e:
            say(f"  ! couldn't make the clip for '{h.get('title', '')}': {str(e)[:150]}")
            clip = None
        line = f"**#{i + 1} {h.get('title', '')}**\n" + _pick_line(h, meta, cfg).split("**", 2)[0] + \
            f"· {_fmt_label(h.get('format'))} · {h.get('score', '')}/10"
        if h.get("summary"):
            line += f"\n> {h['summary'][:300]}"
        if clip is None:
            missing += 1
            line += "\n*(미리보기 영상 없음)*"
        elif (h["e"] - h["s"]) + 4 > float(ds["clip_max"]):
            line += f"\n*(영상은 앞부분 {int(float(ds['clip_max']))}초만 — 전체 구간은 위 시간 참고)*"
        _send(url, {"username": "SkyEye", "content": line[:1990], "allowed_mentions": {"parse": []}},
              [clip] if clip else [])
        sent += 1
        time.sleep(0.6)                                      # stay well under the webhook rate limit
    report(1.0, "posted")
    shutil.rmtree(work / "discord_clips", ignore_errors=True)     # they live on in Discord; free the disk space
    if missing:
        say(f"  {missing} pick(s) had no preview clip (use “Make previews” on the report, then post again).")
    return payload["embeds"][0]["title"] + f" (+{sent} clips)"


def _post_kept_page(url: str, work: Path, cfg: dict, report, say) -> str:
    """Editors' list with hosting set up: build the page, upload it with the kept clips, then post ONE card
    with the link. No per-pick clip messages. If the upload fails the card still goes out, with a warning."""
    picks = _picks(work, cfg, "kept")
    for i, h in enumerate(picks, 1):
        h["n"] = i
    report(0.02, "preparing the editors' page…")
    page_html, clips = editor_page.build(work, cfg, picks, publish.settings(cfg)["clip_quality"],
                                         on_progress=lambda f, t: report(0.02 + f * 0.43, t))
    try:
        editor_page.write_local(work, page_html, clips)
    except Exception as e:
        say(f"  ! couldn't save a local copy of the page: {str(e)[:150]}")
    no_clip = len(picks) - len(clips)
    page_url, note = None, ""
    try:
        page_url = publish.publish_page(work, cfg, page_html, clips,
                                        on_progress=lambda f, t: report(0.45 + f * 0.5, t), log=say)
    except scout.Cancelled:
        raise
    except Exception as e:
        say(f"! couldn't upload the editors' page: {e}")
        note = f"⚠️ 영상 페이지를 올리지 못했어요 ({str(e)[:160]}). 아래는 목록만 있는 버전입니다."
    if page_url and no_clip:
        note = f"ℹ️ 미리보기 영상이 없는 장면 {no_clip}개는 페이지에 영상 없이 표시돼요."
    payload, files = build_message(work, cfg, "kept", page_url=page_url, page_note=note)
    report(0.97, "posting the card…")
    _send(url, payload, files)
    report(1.0, "posted")
    return payload["embeds"][0]["title"] + (" (+page)" if page_url else " (page upload failed)")


def _picks(work: Path, cfg: dict, kind: str) -> list[dict]:
    """The same picks the card lists: top N for a report, everything kept for the editors' list."""
    analysis = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
    hs = scout.normalize(analysis)
    if kind == "kept":
        try:
            rv = json.loads((work / "review.json").read_text(encoding="utf-8")).get("decisions") or {}
        except Exception:
            rv = {}
        out = [h for h in hs if (rv.get(scout.preview_key(h["s"], h["e"])) or {}).get("d") == "keep"]
    else:
        out = sorted(hs, key=lambda h: -int(h.get("score") or 0))[: int(settings(cfg)["top_n"])]
    return sorted(out, key=lambda h: h["s"])


def post_test(cfg: dict) -> None:
    url = _TEST_OVERRIDE.get("url") or get_webhook()
    if not url:
        raise RuntimeError("Paste the webhook link first.")
    content, allowed = _mention_payload(settings(cfg).get("mention", ""))
    _send(url, {"username": "SkyEye", "content": (content + " " if content else "") +
                "👋 SkyEye가 이 채널에 연결됐어요. 새 편집 후보 리포트가 여기에 올라옵니다.",
                "allowed_mentions": allowed}, [])
