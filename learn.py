"""
SkyEye — learn the channel's editing style from its existing YouTube videos.

Reads the channel's uploaded edits and Shorts (titles, descriptions, view counts,
chapters and Korean subtitles, via yt-dlp), then asks Claude to write a
"style profile": what kinds of stream moments this channel turns into videos.
The profile (channel_profile.md) is added to every highlight analysis.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import scout
from scout import HERE, check_cancel, log, progress

PROFILE = HERE / "channel_profile.md"
DATA_DIR = HERE / "channel_data"
MAX_PROFILE_CHARS = 9000


# ------------------------------------------------------------- yt-dlp setup
def _pip(*args: str) -> bool:
    exe = Path(sys.executable)
    py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    r = subprocess.run([str(py), "-m", "pip", "install", "-q", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **scout.NOWIN)
    if r.returncode != 0:
        log("  ! pip: " + (r.stderr or r.stdout)[-400:])
    return r.returncode == 0


def ensure_ytdlp():
    """yt-dlp must be fresh (YouTube changes often); deno lets it solve YouTube's JS checks."""
    stamp = DATA_DIR / ".ytdlp_updated"
    DATA_DIR.mkdir(exist_ok=True)
    fresh = stamp.exists() and time.time() - stamp.stat().st_mtime < 3 * 86400
    try:
        import yt_dlp  # noqa: F401
        have = True
    except ImportError:
        have = False
    if not have or not fresh:
        progress("fetch", 0.02, "updating the YouTube reader (yt-dlp)…")
        log("Updating yt-dlp…")
        if _pip("-U", "yt-dlp[default]"):
            stamp.write_text(time.strftime("%Y-%m-%d"), encoding="utf-8")
        _pip("deno")                                           # optional helper; ignore failure
    scripts = Path(sys.executable).parent
    os.environ["PATH"] = str(scripts) + os.pathsep + os.environ.get("PATH", "")
    import importlib
    import yt_dlp
    importlib.reload(yt_dlp)
    return yt_dlp


class _Logger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if "JavaScript runtime" in msg or "js runtime" in msg.lower():
            return
        log("  ! " + msg[:300])

    def error(self, msg):
        log("  ! " + msg[:300])


# --------------------------------------------------------------- channel
def normalize_channel(url: str) -> str:
    u = url.strip().rstrip("/")
    if not u:
        return ""
    if u.startswith("@"):
        u = "https://www.youtube.com/" + u
    elif not u.startswith("http"):
        u = "https://www.youtube.com/@" + u
    u = re.sub(r"/(videos|shorts|streams|featured|playlists|community|about)$", "", u)
    return u


def list_tab(yt_dlp, channel: str, tab: str, limit: int) -> list[dict]:
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True, "logger": _Logger(),
            "playlistend": max(limit * 3, limit), "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{channel}/{tab}", download=False) or {}
    out = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        out.append({"id": e["id"], "title": e.get("title") or "", "views": e.get("view_count"),
                    "duration": e.get("duration"), "kind": "shorts" if tab == "shorts" else "video",
                    "channel": info.get("channel") or info.get("uploader") or ""})
    return out


def pick_videos(entries: list[dict], limit: int) -> list[dict]:
    """Newest ones plus the most-viewed ones, so the profile sees both 'current style' and 'what works'."""
    if len(entries) <= limit:
        return entries
    recent = entries[: max(1, int(limit * 0.6))]
    rest = [e for e in entries if e not in recent]
    top = sorted(rest, key=lambda e: -(e.get("views") or 0))[: limit - len(recent)]
    return recent + top


def json3_text(path: Path) -> str:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    parts = []
    for ev in d.get("events") or []:
        segs = ev.get("segs")
        if not segs:
            continue
        t = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if t and (not parts or parts[-1] != t):
            parts.append(t)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def sample_text(text: str, budget: int) -> str:
    """Beginning, middle and end of a long subtitle text."""
    if len(text) <= budget:
        return text
    third = budget // 3
    mid = len(text) // 2
    return text[:third] + " … " + text[mid - third // 2: mid + third // 2] + " … " + text[-third:]


def fetch_details(yt_dlp, videos: list[dict]) -> list[dict]:
    subdir = DATA_DIR / "subs"
    subdir.mkdir(parents=True, exist_ok=True)
    opts = {"skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": ["ko", "ko-orig", "ko-KR"], "subtitlesformat": "json3",
            "outtmpl": str(subdir / "%(id)s.%(ext)s"), "quiet": True, "no_warnings": True,
            "logger": _Logger(), "ignoreerrors": True, "sleep_interval_subtitles": 1,
            "sleep_interval_requests": 0.5}
    out = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for i, v in enumerate(videos):
            check_cancel()
            progress("subtitles", i / max(1, len(videos)), f"{i + 1}/{len(videos)} — {v['title'][:50]}")
            cached = DATA_DIR / f"{v['id']}.json"
            if cached.exists():
                out.append(json.loads(cached.read_text(encoding="utf-8")))
                continue
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={v['id']}", download=True)
            except Exception as e:
                log(f"  ! {v['title'][:40]}: {e}")
                info = None
            if not info:
                continue
            subs = ""
            for f in sorted(subdir.glob(f"{v['id']}.*.json3")):
                subs = json3_text(f)
                if subs:
                    break
            rec = {"id": v["id"], "kind": v["kind"], "title": info.get("title") or v["title"],
                   "upload_date": info.get("upload_date"), "duration": info.get("duration"),
                   "views": info.get("view_count"), "likes": info.get("like_count"),
                   "comments": info.get("comment_count"),
                   "description": (info.get("description") or "")[:600],
                   "tags": (info.get("tags") or [])[:15],
                   "chapters": [{"t": int(c.get("start_time") or 0), "title": c.get("title")}
                                for c in (info.get("chapters") or [])][:30],
                   "subtitles": subs}
            cached.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            out.append(rec)
    return out


# ---------------------------------------------------------------- profile
PROFILE_SYSTEM = """당신은 버튜버/스트리머 편집 채널을 분석하는 유튜브 콘텐츠 전략가입니다.
아래는 한 채널에 올라간 편집 영상(본편·쇼츠)의 제목, 조회수, 길이, 설명, 챕터, 자동 자막입니다.
이 자료를 바탕으로, 앞으로 **생방송 다시보기에서 어떤 장면을 골라야 이 채널다운 편집 영상이 되는지** 알려주는
'채널 편집 스타일 프로필'을 한국어 마크다운으로 작성하세요. 이 프로필은 다른 AI가 방송 전사를 읽고
편집 후보를 고를 때 참고 자료로 그대로 쓰입니다.

포함할 내용 (각 항목은 구체적인 영상 제목/예시를 근거로):
1. 채널 요약 — 어떤 채널인지, 주 콘텐츠(게임/잡담/합방 등), 스트리머 캐릭터와 말투
2. 자주 편집되는 장면 유형 — 유형별로 무엇이 편집 포인트였는지 (예: 공포게임 비명, 후원 드립, 억울함 연기 등)
3. 잘 된 영상의 공통점 — 조회수가 채널 평균보다 높은 영상들의 소재·구성·제목 특징
4. 본편 vs 쇼츠 — 각각 어떤 장면을 쓰는지, 보통 길이, 구성 방식
5. 제목 패턴 — 자주 쓰는 표현, 말투, 이모지/특수문자, 따옴표 인용 방식
6. 반복되는 밈·별명·유행어·등장인물 (합방 상대, 시청자 애칭 등)
7. 방송에서 찾아야 할 것 체크리스트 — 편집 후보를 고를 때 우선순위 순으로 10개 내외
8. 피해야 할 것 — 이 채널에서 잘 안 쓰이거나 반응이 약했던 유형 (근거가 있을 때만)

규칙: 자료에 근거가 없는 내용은 쓰지 마세요. 자막은 자동 생성이라 오류가 있을 수 있습니다.
전체 분량은 한국어 기준 4000자 이내로, 바로 참고할 수 있게 간결하게 쓰세요. 마크다운만 출력하세요."""


def build_prompt(recs: list[dict], budget_chars: int = 90000) -> str:
    views = sorted([r["views"] for r in recs if r.get("views")])
    median = views[len(views) // 2] if views else 0
    n_long = sum(1 for r in recs if r["kind"] == "video")
    per_long = max(800, int(budget_chars * 0.85 / max(1, n_long)))
    lines = [f"영상 {len(recs)}개 · 조회수 중앙값 {median:,}", ""]
    for r in sorted(recs, key=lambda r: r.get("upload_date") or "", reverse=True):
        rel = f" (중앙값 대비 {r['views'] / median:.1f}배)" if median and r.get("views") else ""
        dur = r.get("duration") or 0
        lines.append(f"### [{'쇼츠' if r['kind'] == 'shorts' else '본편'}] {r['title']}")
        lines.append(f"업로드 {r.get('upload_date') or '?'} · 길이 {dur // 60}분 {dur % 60}초 · "
                     f"조회수 {r.get('views') or 0:,}{rel} · 좋아요 {r.get('likes') or 0:,}")
        if r.get("description"):
            lines.append("설명: " + r["description"].replace("\n", " ")[:300])
        if r.get("chapters"):
            lines.append("챕터: " + " / ".join(f"{scout.fmt(c['t'])} {c['title']}" for c in r["chapters"]))
        if r.get("subtitles"):
            lines.append("자막: " + sample_text(r["subtitles"], 1200 if r["kind"] == "shorts" else per_long))
        lines.append("")
    return "\n".join(lines)


def learn_channel(cfg: dict) -> dict:
    urls = [normalize_channel(u) for u in (cfg.get("youtube_channels") or []) if str(u).strip()]
    urls = [u for u in urls if u]
    if not urls:
        sys.exit("Add your YouTube channel link first (Settings → About your channel).")
    n_videos = int(cfg.get("learn_videos") or 30)
    n_shorts = int(cfg.get("learn_shorts") or 20)

    yt_dlp = ensure_ytdlp()
    progress("fetch", 0.1, "reading the channel's video list…")
    picked: list[dict] = []
    for u in urls:
        check_cancel()
        vids = list_tab(yt_dlp, u, "videos", n_videos) if n_videos else []
        shorts = list_tab(yt_dlp, u, "shorts", n_shorts) if n_shorts else []
        log(f"{u}: {len(vids)} videos, {len(shorts)} shorts found")
        picked += pick_videos(vids, n_videos) + pick_videos(shorts, n_shorts)
    if not picked:
        sys.exit("Couldn't find any videos on that channel. Check the link (e.g. https://www.youtube.com/@yourhandle).")
    log(f"Studying {len(picked)} videos…")

    recs = fetch_details(yt_dlp, picked)
    with_subs = sum(1 for r in recs if r.get("subtitles"))
    log(f"Got details for {len(recs)} videos ({with_subs} with subtitles).")
    if not recs:
        sys.exit("YouTube didn't return any video details. Try again in a few minutes.")
    (DATA_DIR / "videos.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")

    check_cancel()
    progress("claude", 0.05, "Claude is studying your channel…")
    user = build_prompt(recs)
    text, usage = scout.ask_claude_text(cfg, PROFILE_SYSTEM, user, stage="claude")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n|\n```$", "", text).strip()
    header = (f"<!-- learned {time.strftime('%Y-%m-%d')} from {len(recs)} videos "
              f"({', '.join(urls)}) -->\n")
    PROFILE.write_text(header + text[:MAX_PROFILE_CHARS], encoding="utf-8")
    log(f"Style profile saved ({len(text):,} characters). Tokens: in {usage['input_tokens']:,} / out {usage['output_tokens']:,}")
    progress("claude", 1.0, "done")
    return {"videos": len(recs), "with_subs": with_subs}


def profile_info() -> dict:
    if not PROFILE.exists():
        return {"text": "", "learned": "", "videos": 0}
    raw = PROFILE.read_text(encoding="utf-8")
    m = re.match(r"<!-- learned (\S+) from (\d+) videos.*?-->\n?", raw)
    return {"text": raw[m.end():] if m else raw, "learned": m.group(1) if m else "",
            "videos": int(m.group(2)) if m else 0}


def save_profile(text: str):
    info = profile_info()
    header = f"<!-- learned {info['learned'] or time.strftime('%Y-%m-%d')} from {info['videos']} videos (edited) -->\n"
    if text.strip():
        PROFILE.write_text(header + text.strip(), encoding="utf-8")
    elif PROFILE.exists():
        PROFILE.unlink()
