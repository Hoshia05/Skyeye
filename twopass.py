"""
SkyEye — two-pass highlight analysis.

Pass 1 (scan):  a fast, cheap model reads the stream in ~15-minute windows and lists MANY possible
                moments (high recall). Windows run in parallel.
Pass 2 (judge): the main model looks at each candidate together with the transcript around it,
                plus examples of what Stan kept/skipped in review mode, and keeps only the real
                "fun points" with tight start/end times and honest scores (precision).
Overview:       one small call writes the stream summary, chapters and video ideas.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import scout
from scout import HERE, check_cancel, fmt, log, parse_ts, progress

WINDOW = 15 * 60
OVERLAP = 90

SCAN_TOOL = {
    "name": "submit_candidates",
    "description": "이 구간에서 찾은 재미 포인트 후보를 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "window_summary": {"type": "string", "description": "이 구간에서 무엇을 했는지 1~2문장"},
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "H:MM:SS (대략이어도 됨, 셋업 시작 쪽으로)"},
                        "end": {"type": "string", "description": "H:MM:SS"},
                        "what": {"type": "string", "description": "무슨 일인지 한 문장"},
                        "why": {"type": "string", "description": "왜 재밌을 수 있는지 짧게"},
                        "strength": {"type": "integer", "minimum": 1, "maximum": 5,
                                     "description": "재미 가능성 1(애매)~5(확실)"},
                        "quote": {"type": "string", "description": "핵심 대사 (전사 그대로)"},
                    },
                    "required": ["start", "end", "what", "strength"],
                },
            },
        },
        "required": ["window_summary", "candidates"],
    },
}

_item = json.loads(json.dumps(scout.REPORT_TOOL["input_schema"]["properties"]["highlights"]["items"]))
_item["properties"]["candidate"] = {"type": "integer", "description": "근거가 된 후보 번호 (합쳤으면 대표 번호)"}
JUDGE_TOOL = {
    "name": "submit_judgement",
    "description": "후보를 평가해서 실제 편집할 가치가 있는 장면만 제출합니다.",
    "input_schema": {"type": "object",
                     "properties": {"highlights": {"type": "array", "items": _item}},
                     "required": ["highlights"]},
}

def scan_tool(focus: bool) -> dict:
    """SCAN_TOOL; with a focus, candidates may carry focus=true."""
    if not focus:
        return SCAN_TOOL
    t = json.loads(json.dumps(SCAN_TOOL))
    t["input_schema"]["properties"]["candidates"]["items"]["properties"]["focus"] = {
        "type": "boolean", "description": "스트리머의 이번 방송 특별 지시에 해당하는 장면이면 true"}
    return t


def judge_tool(focus: bool) -> dict:
    """JUDGE_TOOL; with a focus, picks may carry focus_match=true."""
    if not focus:
        return JUDGE_TOOL
    t = json.loads(json.dumps(JUDGE_TOOL))
    t["input_schema"]["properties"]["highlights"]["items"]["properties"]["focus_match"] = {
        "type": "boolean", "description": "스트리머의 이번 방송 특별 지시에 해당하는 장면이면 true"}
    return t


_props = scout.REPORT_TOOL["input_schema"]["properties"]
OVERVIEW_TOOL = {
    "name": "submit_overview",
    "description": "방송 전체 요약, 구성, 영상 아이디어를 제출합니다.",
    "input_schema": {"type": "object",
                     "properties": {"stream_summary": _props["stream_summary"], "chapters": _props["chapters"],
                                    "compilation_ideas": _props["compilation_ideas"]},
                     "required": ["stream_summary", "chapters"]},
}


# ------------------------------------------------------------------ prompts
def scan_system(cfg, track_desc: str) -> str:
    chat_in = ("\n- 💬 = 시청자 채팅(방송 지연 보정됨), 💰 후원 채팅 = 후원 메시지 원문, 📈 = 채팅 반응 급증. "
               "스트리머가 무엇에 답하는지 이해하고, 시청자가 웃거나 놀란 곳을 찾는 단서로 쓰세요." if cfg.get("_has_chat") else "")
    return f"""당신은 스트리머 방송 다시보기를 빠르게 훑어보는 '1차 스캐너'입니다.
지금 보는 것은 긴 방송의 일부 구간(약 15분)입니다. 목표는 **재밌을 가능성이 있는 순간을 하나도 놓치지 않는 것**입니다.
최종 선별은 다음 단계에서 하므로, 애매해도 일단 후보로 올리고 strength로 확신 정도를 표시하세요.

## 채널 정보
{cfg.get('channel_context', '').strip()}
{scout.channel_profile_section(cfg)}
## 입력
각 줄은 [시:분:초]로 시작하는 자동 전사(트랙별로 화자 구분)와 오디오 신호입니다.
{track_desc}
- ⚡/💥 = 음량 급상승, 💰 = 후원, ⭐ = 스트리머가 방송 중 직접 표시한 순간(반드시 후보로, 보통 표시 직전 장면).{chat_in}

## 찾을 것
드립·펀치라인, 큰 리액션(비명, 폭소, 당황, 분노), 기승전결 있는 썰, 후원/디코 상대와의 티키타카,
게임 속 실수·명장면, 반복 개그(콜백), 캐릭터가 드러나는 말버릇·억지·허세, 예상 밖의 반전, 진지하거나 감동적인 순간.
조용하지만 웃긴 말장난도 놓치지 마세요. 음량은 힌트일 뿐입니다.
{scout.focus_block(cfg)}
## 규칙
- 이 구간 안에서 시작하는 장면만 제출하세요 (앞뒤 겹치는 부분은 맥락 참고용).
- 15분에 보통 5~12개. 확신이 없어도 조금이라도 웃기거나 특이하면 strength 1~2로 꼭 올리세요
  (놓치는 것이 잘못 올리는 것보다 훨씬 나쁩니다). 정말 아무 일도 없는 구간만 0개로 두세요.
- 전사에 근거 없는 내용은 지어내지 마세요. 한국어로 쓰세요."""


def judge_system(cfg, track_desc: str) -> str:
    return scout.system_prompt(cfg, track_desc).replace(
        "- 결과는 반드시 submit_highlights 도구로 제출하세요.", "") + f"""

## 이번 작업: 2차 심사
1차 스캐너가 넓게 모아 온 후보 목록과, 각 후보 주변의 전사 원문을 받습니다. 원문을 직접 읽고 판단하세요.
- 실제로 재밌는지 엄격하게 확인하고, 재미없거나 맥락 없이는 이해 안 되는 후보는 빼세요.
  단, 애매하지만 가능성이 있는 것은 빼지 말고 낮은 점수(3~4)로 남기세요 — 스트리머가 직접 리뷰해서 고릅니다.
  평범한 잡담·설명·근황 이야기처럼 그 자체로는 재미 포인트가 없는 것(2점 이하)은 빼세요.
- 시작/끝을 정확히 다듬으세요: 셋업이 시작되는 지점부터, 반응과 여운이 끝나는 지점까지.
- 같은 장면을 가리키는 후보는 하나로 합치세요. 이어지는 흐름이면 본편용 긴 구간으로 묶어도 됩니다.
- ⭐(방송 중 표시) 후보는 스트리머가 직접 고른 것이므로 빼지 말고, 정확한 구간만 찾아 주세요.
- 점수는 솔직하게: 10 = 무조건 써야 함, 7~8 = 좋음, 5~6 = 괜찮음, 3~4 = 여유 있으면.
{taste_section()}
- 결과는 submit_judgement 형식으로 제출하세요. 각 항목의 candidate에 근거 후보 번호를 적으세요."""


def taste_section() -> str:
    """Examples of what Stan kept / skipped in review mode (taste.json, written by the app)."""
    p = HERE / "taste.json"
    if not p.exists():
        return ""
    try:
        t = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    keep, skip = (t.get("keep") or [])[-15:], (t.get("skip") or [])[-15:]
    if not keep and not skip:
        return ""

    def row(x):
        q = f' — "{x["quote"]}"' if x.get("quote") else ""
        return f"  - [{x.get('category', '')}/{x.get('format', '')}] {x.get('title', '')}: {x.get('summary', '')[:120]}{q}"
    out = "\n## 스트리머(호시아)가 직접 리뷰한 결과 — 이 취향에 맞추세요\n"
    if keep:
        out += "실제로 편집하기로 고른 장면:\n" + "\n".join(row(x) for x in keep) + "\n"
    if skip:
        out += "후보였지만 건너뛴 장면 (이런 건 점수를 낮추거나 빼세요):\n" + "\n".join(row(x) for x in skip) + "\n"
    return out


# ------------------------------------------------------------------ helpers
def _lines_between(timeline, a: float, b: float) -> list[str]:
    return [l for t, l in timeline if a <= t < b]


def _merge_candidates(cands: list[dict]) -> list[dict]:
    cands = sorted(cands, key=lambda c: c["s"])
    out: list[dict] = []
    for c in cands:
        if out:
            p = out[-1]
            same = scout.overlap_ratio((p["s"], p["e"]), (c["s"], c["e"])) > 0.5 or abs(p["s"] - c["s"]) < 20
            if same:
                if c["strength"] > p["strength"]:
                    c["s"], c["e"] = min(p["s"], c["s"]), max(p["e"], c["e"])
                    c["marker"] = c.get("marker") or p.get("marker")
                    c["focus"] = bool(c.get("focus") or p.get("focus"))
                    out[-1] = c
                else:
                    p["e"] = max(p["e"], c["e"])
                    p["marker"] = p.get("marker") or c.get("marker")
                    p["focus"] = bool(p.get("focus") or c.get("focus"))
                continue
        out.append(c)
    for i, c in enumerate(out, 1):
        c["id"] = i
    return out


def _excerpt(timeline, group: list[dict]) -> str:
    """Transcript around each candidate (setup before, reaction after); overlapping ranges are joined."""
    ranges = sorted((max(0, c["s"] - 120), c["e"] + 90) for c in group)
    merged = []
    for a, b in ranges:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    parts = []
    for a, b in merged:
        parts.append(f"--- {fmt(a)} ~ {fmt(b)} ---\n" + "\n".join(_lines_between(timeline, a, b)))
    return "\n\n".join(parts)


def _run_parallel(jobs: list, fn, workers: int, stage: str, label: str, base: float, span: float):
    """Run fn(job) for each job on a few threads; report progress from this (the job's) thread."""
    results: list = [None] * len(jobs)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(fn, j): i for i, j in enumerate(jobs)}
        pending = set(futs)
        t0 = time.time()
        while pending:
            check_cancel()
            finished = [f for f in pending if f.done()]
            for f in finished:
                pending.discard(f)
                results[futs[f]] = f.result()          # re-raises errors (incl. usage limit)
                done += 1
            progress(stage, base + span * (done / len(jobs)),
                     f"{label} {done}/{len(jobs)} ({int(time.time() - t0)}s)")
            if pending:
                time.sleep(0.5)
    return results


# --------------------------------------------------------------------- main
def analyze_two_pass(cfg, tracks, timeline, duration, vod_name, work: Path) -> dict:
    track_desc = scout.track_description(tracks)
    main_model = cfg.get("claude_model", "claude-sonnet-5-5")
    scan_model = cfg.get("scan_model") or "claude-haiku-4-5-20251001"
    workers = int(cfg.get("claude_parallel") or 3)
    usage = {"input_tokens": 0, "output_tokens": 0}

    def add(u):
        for k in usage:
            usage[k] += int(u.get(k, 0))

    # ---------------- pass 1: scan windows
    windows = []
    s = 0.0
    while s < max(duration, 1):
        e = min(duration, s + WINDOW)
        if duration - e < 240:
            e = duration
        windows.append((s, e))
        s = e
    log(f"[4/5] Pass 1 — scanning {len(windows)} pieces with {scan_model}…")
    sys_scan = scan_system(cfg, track_desc)
    has_focus = bool(scout.focus_text(cfg))
    stool, jtool = scan_tool(has_focus), judge_tool(has_focus)

    def scan(win):
        a, b = win
        lines = _lines_between(timeline, a - OVERLAP, b + OVERLAP)
        if not lines:
            return {"window_summary": "", "candidates": []}, {}
        user = (f"방송 파일: {vod_name} (전체 {fmt(duration)})\n이번 구간: {fmt(a)} ~ {fmt(b)}\n\n<timeline>\n"
                + "\n".join(lines) + "\n</timeline>")
        return scout.ask_claude_json(cfg, scan_model, sys_scan, user, stool,
                                     "이 구간에서 재밌을 가능성이 있는 순간을 모두 후보로 제출하세요.",
                                     thinking=int(cfg.get("scan_thinking", 0)))

    t0 = time.time()
    scans = _run_parallel(windows, scan, workers, "claude", "pass 1 · scanning", 0.0, 0.45)
    cands, summaries = [], []
    for (a, b), (res, u) in zip(windows, scans):
        add(u)
        summaries.append((a, b, res.get("window_summary", "")))
        for c in res.get("candidates") or []:
            cs, ce = parse_ts(c.get("start")), parse_ts(c.get("end"))
            if cs is None or not (a - 30 <= cs < b + 30):
                continue
            ce = ce if ce and ce > cs else cs + 45
            cands.append({"s": cs, "e": min(ce, cs + 900), "what": c.get("what", ""), "why": c.get("why", ""),
                          "strength": int(c.get("strength") or 2), "quote": c.get("quote", ""),
                          "focus": bool(has_focus and c.get("focus"))})
    for t, l in timeline:                              # markers pressed during the stream are always candidates
        if "⭐" in l:
            cands.append({"s": max(0, t - 60), "e": t + 15, "what": "방송 중 직접 표시한 순간",
                          "why": "스트리머가 직접 표시", "strength": 5, "quote": "", "marker": True})
    cands = _merge_candidates(cands)
    cap = max(25, int(duration / 3600 * 18))
    if len(cands) > cap:
        ranked = sorted(cands, key=lambda c: (not c.get("marker"), not c.get("focus"), -c["strength"]))
        keep = ranked[:cap] + [c for c in ranked[cap:] if c.get("focus")]       # focus matches are never cut
        cands = sorted(keep, key=lambda c: c["s"])
    log(f"      {len(cands)} candidates after pass 1 ({time.time() - t0:.0f}s)")
    if not cands:
        return {"stream_summary": "\n".join(x[2] for x in summaries if x[2]), "chapters": [], "highlights": [],
                "compilation_ideas": [], "candidates": [], "usage": usage, "model": main_model, "mode": "two_pass"}

    # ---------------- pass 2: judge in groups
    groups, cur = [], []
    for c in cands:
        if cur and (len(cur) >= 7 or c["s"] - cur[0]["s"] > 1800):
            groups.append(cur)
            cur = []
        cur.append(c)
    if cur:
        groups.append(cur)
    log(f"      Pass 2 — {main_model} is judging the candidates in {len(groups)} group(s)…")
    sys_judge = judge_system(cfg, track_desc)

    def judge(group):
        listing = "\n".join(
            f"#{c['id']} [{fmt(c['s'])}~{fmt(c['e'])}] {'⭐ ' if c.get('marker') else ''}{'🎯 ' if c.get('focus') else ''}(가능성 {c['strength']}/5) "
            f"{c['what']}" + (f" — {c['why']}" if c.get("why") else "") + (f' / "{c["quote"]}"' if c.get("quote") else "")
            for c in group)
        user = (f"방송 파일: {vod_name} (전체 {fmt(duration)})\n\n## 1차 후보\n{listing}\n\n"
                f"## 후보 주변 전사 원문\n<timeline>\n{_excerpt(timeline, group)}\n</timeline>")
        return scout.ask_claude_json(cfg, main_model, sys_judge, user, jtool,
                                     "후보를 심사해서 편집 가치가 있는 장면만 제출하세요.",
                                     thinking=int(cfg.get("judge_thinking", 3000)))

    t1 = time.time()
    judged = _run_parallel(groups, judge, max(1, min(workers, 2)), "claude", "pass 2 · judging", 0.45, 0.45)
    highlights = []
    focus_ids = {c["id"] for c in cands if c.get("focus")}
    for res, u in judged:
        add(u)
        for h in res.get("highlights") or []:
            hs, he = parse_ts(h.get("start")), parse_ts(h.get("end"))
            if hs is None:
                continue
            if has_focus:                       # the judge forgot to say? then the candidate's own flag counts
                h["focus_match"] = bool(h["focus_match"]) if h.get("focus_match") is not None \
                    else (h.get("candidate") in focus_ids)
            dup = next((x for x in highlights
                        if scout.overlap_ratio((hs, he or hs + 30), (parse_ts(x["start"]) or 0, parse_ts(x["end"]) or 0)) > 0.5),
                       None)
            if dup is None:
                highlights.append(h)
            elif int(h.get("score") or 0) > int(dup.get("score") or 0):
                if has_focus:
                    h["focus_match"] = bool(h.get("focus_match") or dup.get("focus_match"))
                highlights[highlights.index(dup)] = h
            elif has_focus and h.get("focus_match"):
                dup["focus_match"] = True
    log(f"      {len(highlights)} picks kept after pass 2 ({time.time() - t1:.0f}s)")

    # ---------------- overview (summary, chapters, ideas)
    progress("claude", 0.92, "writing the stream summary…")
    picks = "\n".join(f"- {h.get('start')}~{h.get('end')} {h.get('title')} ({h.get('score')}/10)"
                      for h in sorted(highlights, key=lambda h: parse_ts(h.get("start")) or 0))
    user = (f"방송 파일: {vod_name} (전체 {fmt(duration)})\n\n## 구간별 요약\n"
            + "\n".join(f"- {fmt(a)}~{fmt(b)}: {t}" for a, b, t in summaries if t)
            + f"\n\n## 최종 선정된 장면\n{picks}")
    try:
        ov, u = scout.ask_claude_json(
            cfg, main_model,
            "당신은 방송 편집 PD입니다. 구간별 요약과 선정된 장면 목록을 보고 방송 전체 요약(3~8문장, 시간대 포함), "
            "방송 구성(챕터), 여러 장면을 묶은 영상 아이디어를 한국어로 작성하세요.",
            user, OVERVIEW_TOOL, "방송 개요를 제출하세요.", thinking=0)
        add(u)
    except scout.Cancelled:
        raise
    except Exception as e:                              # the picks matter more than the summary
        log(f"      ! summary step failed ({e}); using the per-piece summaries")
        ov = {"stream_summary": "\n".join(f"({fmt(a)}~{fmt(b)}) {t}" for a, b, t in summaries if t), "chapters": []}

    log(f"      total tokens: in {usage['input_tokens']:,} / out {usage['output_tokens']:,}")
    return {"stream_summary": ov.get("stream_summary", ""), "chapters": ov.get("chapters") or [],
            "compilation_ideas": ov.get("compilation_ideas") or [], "highlights": highlights,
            "candidates": [{k: c.get(k) for k in ("id", "s", "e", "what", "why", "strength", "quote", "marker", "focus")}
                           for c in cands],
            "usage": usage, "model": main_model, "scan_model": scan_model, "mode": "two_pass"}
