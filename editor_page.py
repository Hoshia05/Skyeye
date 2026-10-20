"""
SkyEye — the editors' page.

One self-contained web page (index.html) with the stream summary, the timeline, and every moment Stan kept,
each with its preview clip playing right in the page. publish.py uploads it (plus clips/…mp4) to Cloudflare R2
and discord_post.py posts the link. The same files are also written to <result folder>/editor_page/ so the page
can be opened straight from disk (it uses relative clip links).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import scout

CATHEX = {"웃긴 장면": "#fbbf24", "리액션": "#f87171", "토크/썰": "#60a5fa", "후원 소통": "#f472b6",
          "게임 플레이": "#34d399", "합방/디코 케미": "#a78bfa", "감동/진지": "#22d3ee", "기타": "#94a3b8"}


def _signals(work: Path, meta: dict) -> dict:
    """Voice/game level curves, loud spikes, donations and Stream Desk marks for the timeline."""
    try:
        sig = json.loads((work / "signals.json").read_text(encoding="utf-8"))
    except Exception:
        sig = {}
    roles = {}
    for t in meta.get("tracks") or []:
        roles.setdefault(t.get("role"), t.get("key"))
    out: dict = {"mic": None, "game": None, "dono": [], "markers": []}
    for role in ("mic", "game"):
        k = roles.get(role)
        s = sig.get(k) if k else None
        if s and s.get("curve"):
            out[role] = {"curve": [round(float(v), 1) for v in s["curve"]],
                         "spikes": [round(float(p["t"]), 1) for p in (s.get("spikes") or []) if "t" in p]}
    dk = roles.get("donation")
    if dk and sig.get(dk):
        out["dono"] = [round(float(e["start"]), 1) for e in (sig[dk].get("events") or []) if "start" in e]
    out["markers"] = [round(float(m["t"]), 1) for m in (meta.get("markers") or []) if "t" in m]
    return out


def _transcript(work: Path, meta: dict) -> list[list]:
    labels = {}
    for t in meta.get("tracks") or []:
        if t.get("number"):
            labels[f"t{t['number']}"] = t.get("role") or ""
    lines: list[list] = []
    for f in sorted(work.glob("*.transcript.json")):
        role = labels.get(f.name.split(".")[0], "")
        if role in ("bgm", "ignore"):
            continue
        try:
            for s in json.loads(f.read_text(encoding="utf-8")):
                txt = (s.get("text") or "").strip()
                if txt:
                    lines.append([float(s["start"]), role, txt])
        except Exception:
            pass
    lines.sort(key=lambda x: x[0])
    return lines


ROLE_KO = {"mic": "나", "discord": "디코", "donation": "후원", "game": "게임"}


WEB_HEIGHT = 360                  # the uploaded copy is 360p: enough to judge a moment, about a third of the size


def web_clip(work: Path, h: dict, quality: str = "small") -> tuple[str, Path] | None:
    """(name on the page, local file) for one kept moment. quality 'original' = the 480p review preview as it is;
    'small' (default) = a 360p copy made once and kept in <result>/web_clips (names end in .s.mp4, so the two
    never mix up in the bucket)."""
    pv = scout.preview_path(work, h["s"], h["e"])
    if not pv.exists():
        return None
    key = scout.preview_key(h["s"], h["e"])
    if quality == "original":
        return f"{key}.mp4", pv
    out = work / "web_clips" / f"{key}.s.mp4"
    if out.exists() and out.stat().st_mtime >= pv.stat().st_mtime and out.stat().st_size > 0:
        return out.name, out
    out.parent.mkdir(exist_ok=True)
    part = out.with_suffix(".part.mp4")
    vf = f"scale=-2:min({WEB_HEIGHT}\\,ih)"
    audio = ["-c:a", "aac", "-b:a", "64k", "-ac", "2", "-movflags", "+faststart", str(part)]
    enc = scout._video_encoder()
    if enc[1] == "h264_nvenc":
        enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "35", "-b:v", "0"]
    else:
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "31"]
    try:
        scout.run_ffmpeg(["-i", str(pv), "-vf", vf] + enc + audio)
    except RuntimeError:
        if enc[1] != "h264_nvenc":
            raise
        scout.run_ffmpeg(["-i", str(pv), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "31"] + audio)
    part.replace(out)
    return out.name, out


def build(work: Path, cfg: dict, picks: list[dict], quality: str = "small",
          on_progress=None) -> tuple[str, list[tuple[str, Path]]]:
    """Returns (index.html text, [(clip file name, local file), …]). `picks` = the kept moments
    (scout.normalize() dicts), already in time order and numbered 1..N. Makes the small web copies first
    (on_progress(fraction, text))."""
    meta = scout.read_meta(work)
    analysis = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
    tx = _transcript(work, meta)
    delay = meta.get("chat_delay")
    chat = scout.viewer_chat(scout.read_saved_chat(work), float(delay if delay is not None else 6))   # Arkbird chat, if any
    clips: list[tuple[str, Path]] = []
    items = []
    for i, h in enumerate(picks):
        scout.check_cancel()
        if on_progress:
            on_progress(i / max(1, len(picks)), f"preparing clip {i + 1}/{len(picks)}")
        wc = web_clip(work, h, quality)
        name = None
        if wc:
            name = wc[0]
            clips.append(wc)
        lines = [[round(t, 1), ROLE_KO.get(r, ""), x[:300]] for t, r, x in tx if h["s"] - 3 <= t <= h["e"] + 3]
        for c in chat:                                               # viewer chat, marked with a 4th value so it gets its colour
            if h["s"] - 3 <= c["te"] <= h["e"] + 3:
                lines.append([round(c["te"], 1), "채팅", ("💰 " if c["type"] == "donation" else "") + f"{c['nick']}: {c['text']}"[:300], 1])
        lines = sorted(lines, key=lambda l: l[0])[:250]
        items.append({"n": h["n"], "s": h["s"], "e": h["e"], "title": h.get("title", ""),
                      "category": h.get("category", ""), "format": h.get("format", ""), "score": h.get("score", 0),
                      "summary": h.get("summary", ""), "why": h.get("why", ""), "note": h.get("editor_note", ""),
                      "quotes": h.get("key_quotes") or [], "clip": name, "focus": bool(h.get("focus_match")),
                      "len": min(h["e"] + 3, h["s"] + scout.PREVIEW_MAX) - max(0.0, h["s"] - 3), "tx": lines})
    chapters = []
    for c in analysis.get("chapters") or []:
        a, b = scout.parse_ts(c.get("start")), scout.parse_ts(c.get("end"))
        if a is not None and b is not None and b > a:
            chapters.append({"s": a, "e": b, "what": c.get("what", "")})
    data = {"name": Path(meta.get("vod") or work.name.removesuffix("_scout")).stem,
            "duration": float(meta.get("duration") or 0), "generated": meta.get("generated") or "",
            "model": analysis.get("model", ""), "summary": (analysis.get("stream_summary") or "").strip(),
            "chapters": chapters, "picks": items, "tl": _signals(work, meta), "cats": CATHEX,
            "focus": {"preset": (meta.get("focus") or {}).get("preset") or "", "text": (meta.get("focus") or {}).get("text") or ""}}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028")
    return PAGE.replace("__TITLE__", _esc(data["name"])).replace("__DATA__", blob), clips


def write_local(work: Path, page_html: str, clips: list[tuple[str, Path]]) -> Path:
    """Same page on disk (clips copied next to it) so it opens by double-click. Rebuilt each time."""
    out = work / "editor_page"
    shutil.rmtree(out, ignore_errors=True)
    (out / "clips").mkdir(parents=True)
    for name, p in clips:
        try:
            os.link(p, out / "clips" / name)                  # same file, no extra disk space
        except OSError:
            shutil.copy2(p, out / "clips" / name)
    (out / "index.html").write_text(page_html, encoding="utf-8")
    return out / "index.html"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>편집할 장면 — __TITLE__</title>
<meta property="og:title" content="편집할 장면 — __TITLE__"><meta property="og:description" content="호시아가 직접 고른 장면과 미리보기 영상">
<style>
:root{--bg:#0e0e13;--s1:#17171f;--s2:#1e1e29;--s3:#262634;--line:#2a2a3a;--line2:#353547;--ink:#ececf3;--ink2:#c3c3d2;--muted:#8a8aa0;--faint:#5f5f75;--acc:#8b7bff;--acc2:#b3a8ff;--accbg:rgba(139,123,255,.14);--warn:#fbbf24;--r:12px}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.6 "Pretendard Variable",Pretendard,-apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:26px 18px 90px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.4px}
.meta{color:var(--muted);font-size:13px;display:flex;gap:16px;flex-wrap:wrap}
.card{background:var(--s1);border:1px solid var(--line);border-radius:var(--r);padding:16px 20px;margin-top:16px}
.lab{font-size:11.5px;font-weight:700;color:var(--muted);letter-spacing:.4px;margin-bottom:8px}
.summary{white-space:pre-wrap;color:var(--ink2);font-size:14px;line-height:1.75}
#tlWrap{position:relative}#tlWrap svg{width:100%;height:auto;display:block}
.tip{position:absolute;pointer-events:none;background:#000d;border:1px solid var(--line2);border-radius:8px;padding:5px 9px;font-size:12px;transform:translate(10px,-120%);white-space:nowrap;display:none;z-index:3}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:10px;font-size:12px;color:var(--muted)}.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:22px 0 4px}
.seg{display:inline-flex;background:var(--s1);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.seg button{border:0;background:none;color:var(--muted);padding:5px 14px;border-radius:7px;font:inherit;font-weight:600;cursor:pointer}
.seg button.on{background:var(--s3);color:#fff}
.fchip{display:inline-flex;gap:7px;align-items:center;border:1px solid var(--line);background:var(--s1);border-radius:999px;padding:4px 13px;font:inherit;font-size:13px;color:var(--ink2);cursor:pointer}
.fchip.on{background:var(--s3);border-color:#4a4a62;color:#fff}.fchip i{width:8px;height:8px;border-radius:50%}
.hl{display:grid;grid-template-columns:5px 1fr;background:var(--s1);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;margin-top:14px;transition:box-shadow .2s}
.hl.flash{box-shadow:0 0 0 2px var(--acc)}.hl .in{padding:16px 18px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:20px;align-items:start}
@media(max-width:900px){.grid{grid-template-columns:1fr}.grid .pv{order:-1}}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.n{color:var(--faint);font-weight:700;font-size:12.5px}h4{margin:0;font-size:16px;letter-spacing:-.2px}
.tm{font-variant-numeric:tabular-nums;font-weight:700;color:var(--acc2);cursor:pointer;border-radius:5px;padding:0 3px}.tm:hover{background:var(--accbg)}
.chip{font-size:12px;font-weight:600;padding:2px 10px;border-radius:999px;background:var(--s3);color:var(--ink2)}
.row{margin-top:8px;color:var(--ink2);font-size:14px}.row b{color:var(--muted);font-weight:650;font-size:12px;margin-right:8px;display:inline-block;min-width:52px}
blockquote{margin:9px 0 0;padding:3px 12px;border-left:3px solid var(--line2);color:var(--muted);font-size:13px}
.scorebar{display:inline-flex;gap:2px;align-items:center}.scorebar i{width:6px;height:12px;border-radius:2px;background:var(--s3)}.scorebar i.f{background:var(--warn)}
.scorebar span{margin-left:6px;font-weight:700;font-size:12.5px;color:var(--warn)}
.pv{aspect-ratio:16/9;background:#000;border-radius:10px;overflow:hidden;border:1px solid var(--line);display:grid;place-items:center;color:var(--faint);font-size:13px;text-align:center}
.pv video{width:100%;height:100%;display:block;background:#000}
.btn{border:1px solid var(--line2);background:var(--s2);border-radius:8px;padding:4px 11px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer;color:var(--ink2)}.btn:hover{background:var(--s3)}
.tx{margin-top:12px;border-top:1px solid var(--line);padding-top:8px;max-height:300px;overflow:auto;font-size:13px}
.tl{display:grid;grid-template-columns:56px 40px 1fr;gap:8px;padding:2px 0;color:var(--ink2)}.tl .t{color:var(--faint);font-variant-numeric:tabular-nums}.tl .r{color:var(--muted)}
.tl.chat .r{color:#38bdf8}.tl.chat span:last-child{opacity:.85}
.fchip.focus.on{background:rgba(251,191,36,.18);border-color:#fbbf24;color:#fbbf24}.chip.focus{background:rgba(251,191,36,.16);color:#fbbf24}
.empty{color:var(--muted);text-align:center;padding:30px}
table{width:100%;border-collapse:collapse}td{padding:6px 4px;border-bottom:1px solid var(--line);vertical-align:top;color:var(--ink2)}td:first-child{white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums;padding-right:14px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#fff;color:#111;padding:7px 15px;border-radius:9px;font-size:13px;font-weight:600;opacity:0;transition:.2s;pointer-events:none}.toast.show{opacity:1}
</style></head><body><div class="wrap" id="app"></div><div class="toast" id="toast"></div>
<script>
const D=__DATA__;
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>{t=Math.max(0,Math.round(t));const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`};
const fmtMin=t=>{const m=Math.round(t/60);return m>=60?`${Math.floor(m/60)}시간 ${m%60}분`:`${m}분`};
const CAT=D.cats, F={fmt:'all',cats:new Set(),min:1,sort:'time',focus:false};
function toast(m){const o=$('#toast');o.textContent=m;o.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>o.classList.remove('show'),1100)}
function copy(t){try{navigator.clipboard.writeText(t);toast('복사됨 '+t)}catch(e){}}
const dur=D.duration||Math.max(1,...D.picks.map(p=>p.e));
function timeline(){
  const W=1000,H=150,x=t=>t/dur*W,T=D.tl||{};
  const norm=db=>Math.max(0,Math.min(1,(db+60)/48));
  const curve=(c,top,h)=>{if(!c||!c.curve||!c.curve.length)return'';const step=W/Math.max(1,dur/10);let d=`M0 ${top+h}`;c.curve.forEach((v,i)=>{d+=` L${(i*step).toFixed(1)} ${(top+h-norm(v)*h).toFixed(1)}`});return d+` L${(c.curve.length*step).toFixed(1)} ${top+h} Z`};
  let s=`<svg viewBox="0 0 ${W} ${H}" id="tlSvg"><rect width="${W}" height="${H}" fill="transparent"/>`;
  const st=dur>4*3600?3600:dur>3600?1800:dur>1200?600:300;
  for(let t=0;t<=dur;t+=st)s+=`<line x1="${x(t)}" x2="${x(t)}" y1="0" y2="128" stroke="#2a2a3a"/><text x="${x(t)+3}" y="146" fill="#5f5f75" font-size="10">${fmt(t).replace(/:00$/,'')}</text>`;
  if(T.game)s+=`<path d="${curve(T.game,6,48)}" fill="#34d399" opacity=".18"/>`;
  if(T.mic)s+=`<path d="${curve(T.mic,6,48)}" fill="#b3a8ff" opacity=".45"/>`;
  ((T.mic&&T.mic.spikes)||[]).forEach(t=>s+=`<path d="M${x(t)} 2 l3 6 h-6z" fill="#fbbf24" opacity=".9"/>`);
  (T.dono||[]).forEach(t=>s+=`<circle cx="${x(t)}" cy="60" r="2.6" fill="#f472b6"/>`);
  (T.markers||[]).forEach(t=>s+=`<text x="${x(t)}" y="68" font-size="13" text-anchor="middle" fill="#fbbf24">★</text><line x1="${x(t)}" x2="${x(t)}" y1="0" y2="128" stroke="#fbbf24" stroke-dasharray="2 3" opacity=".6"/>`);
  D.picks.forEach(h=>{const sh=h.format==='쇼츠';s+=`<rect class="hlr" data-n="${h.n}" x="${x(h.s)}" y="${sh?84:70}" width="${Math.max(3,x(h.e)-x(h.s))}" height="${sh?12:26}" rx="2.5" fill="${CAT[h.category]||'#94a3b8'}" opacity="${(.45+h.score/20).toFixed(2)}" style="cursor:pointer"/>`});
  D.chapters.forEach((c,i)=>{const w=x(c.e)-x(c.s);s+=`<rect x="${x(c.s)+.5}" y="104" width="${Math.max(1,w-1)}" height="22" rx="4" fill="${i%2?'#1e1e29':'#262634'}"/>`;const mc=Math.floor((w-8)/10.5);if(mc>=2)s+=`<text x="${x(c.s)+5}" y="119" fill="#8a8aa0" font-size="10.5">${esc(c.what.length>mc?c.what.slice(0,mc-1)+'…':c.what)}</text>`});
  return s+`<line id="cur" x1="0" x2="0" y1="0" y2="128" stroke="#fff" opacity="0" pointer-events="none"/></svg>`;
}
function card(h){
  const col=CAT[h.category]||'#94a3b8',bars=Array.from({length:10},(_,i)=>`<i class="${i<h.score?'f':''}"></i>`).join('');
  const pv=h.clip?`<div class="pv" id="pv${h.n}"><video src="clips/${esc(h.clip)}#t=0.1" controls preload="metadata" playsinline></video></div>`:`<div class="pv">미리보기 영상 없음</div>`;
  return `<div class="hl" id="hl${h.n}"><div style="background:${col}"></div><div class="in"><div class="grid"><div>
   <div class="top"><span class="n">#${h.n}</span><h4>${esc(h.title)}</h4><span style="flex:1"></span><span class="scorebar">${bars}<span>${h.score}</span></span></div>
   <div class="top" style="margin-top:6px;gap:8px"><span class="tm" data-c="${fmt(h.s)}">${fmt(h.s)}</span><span style="color:var(--muted)">→</span><span class="tm" data-c="${fmt(h.e)}">${fmt(h.e)}</span>
    <span style="color:var(--muted);font-size:12px">(${((h.e-h.s)/60).toFixed(1)}분)</span><span class="chip" style="background:${col}22;color:${col}">${esc(h.category)}</span><span class="chip">${h.format==='쇼츠'?'쇼츠':h.format==='둘다'?'본편 + 쇼츠':'본편'}</span>${h.focus?'<span class="chip focus" title="호시아가 이번 방송에서 특별히 찾아달라고 한 장면">🎯 Focus</span>':''}</div>
   <div class="row"><b>내용</b>${esc(h.summary)}</div><div class="row"><b>포인트</b>${esc(h.why)}</div>${h.note?`<div class="row"><b>편집 메모</b>${esc(h.note)}</div>`:''}
   ${(h.quotes||[]).map(q=>`<blockquote>“${esc(q)}”</blockquote>`).join('')}
   <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn" data-line="${esc(fmt(h.s)+' ~ '+fmt(h.e)+' '+h.title)}">한 줄 복사</button>${h.tx.length?`<button class="btn" data-tx="${h.n}">대사 보기</button>`:''}</div>
   </div>${pv}</div><div id="tx${h.n}"></div></div></div>`;
}
function list(){
  let hs=D.picks.filter(h=>(F.fmt==='all'||h.format===F.fmt||h.format==='둘다')&&(!F.cats.size||F.cats.has(h.category))&&h.score>=F.min&&(!F.focus||h.focus));
  hs=hs.slice().sort((a,b)=>F.sort==='time'?a.s-b.s:b.score-a.score);
  $('#list').innerHTML=hs.length?hs.map(card).join(''):'<div class="card empty">조건에 맞는 장면이 없어요.</div>';
  $('#count').textContent=`${hs.length} / ${D.picks.length}`;
  bindCards();
}
function bindCards(){
  $$('.tm').forEach(e=>e.onclick=()=>copy(e.dataset.c));
  $$('[data-line]').forEach(e=>e.onclick=()=>copy(e.dataset.line));
  $$('[data-tx]').forEach(b=>b.onclick=()=>{const n=+b.dataset.tx,box=$('#tx'+n);if(box.innerHTML){box.innerHTML='';b.textContent='대사 보기';return}
    const h=D.picks.find(p=>p.n===n);box.innerHTML='<div class="tx">'+h.tx.map(l=>`<div class="tl${l[3]?' chat':''}"><span class="t">${fmt(l[0])}</span><span class="r">${esc(l[1])}</span><span>${esc(l[2])}</span></div>`).join('')+'</div>';b.textContent='대사 숨기기'});
  $$('video').forEach(v=>v.addEventListener('play',()=>$$('video').forEach(o=>{if(o!==v)o.pause()})));
}
function jump(n){F.fmt='all';F.cats.clear();F.min=1;F.focus=false;draw(true);const c=$('#hl'+n);if(!c)return;c.scrollIntoView({behavior:'smooth',block:'center'});c.classList.add('flash');setTimeout(()=>c.classList.remove('flash'),1500);const v=c.querySelector('video');if(v)setTimeout(()=>v.play().catch(()=>{}),450)}
function draw(keepTl){
  const cats=[...new Set(D.picks.map(p=>p.category))];
  const f=$('#filters');f.innerHTML=`<div class="seg"><button data-f="all" class="${F.fmt==='all'?'on':''}">전체</button><button data-f="본편" class="${F.fmt==='본편'?'on':''}">본편</button><button data-f="쇼츠" class="${F.fmt==='쇼츠'?'on':''}">쇼츠</button></div>`
   +(D.picks.some(p=>p.focus)?`<button class="fchip focus ${F.focus?'on':''}" id="ffocus" title="${esc((D.focus||{}).text||'')}">🎯 Focus (${D.picks.filter(p=>p.focus).length})</button>`:'')
   +cats.map(c=>`<button class="fchip ${F.cats.has(c)?'on':''}" data-c="${esc(c)}"><i style="background:${CAT[c]||'#94a3b8'}"></i>${esc(c)}</button>`).join('')
   +`<span style="flex:1"></span><span style="color:var(--muted);font-size:12.5px">최소 점수</span><input type="range" min="1" max="10" value="${F.min}" id="min" style="accent-color:var(--acc);width:100px"><b id="minv" style="width:16px">${F.min}</b>
   <div class="seg"><button data-s="time" class="${F.sort==='time'?'on':''}">시간순</button><button data-s="score" class="${F.sort==='score'?'on':''}">점수순</button></div><span id="count" style="color:var(--muted);font-size:12.5px"></span>`;
  $$('[data-f]',f).forEach(b=>b.onclick=()=>{F.fmt=b.dataset.f;draw()});
  const ff=$('#ffocus');if(ff)ff.onclick=()=>{F.focus=!F.focus;draw()};
  $$('[data-c]',f).forEach(b=>b.onclick=()=>{const c=b.dataset.c;F.cats.has(c)?F.cats.delete(c):F.cats.add(c);draw()});
  $$('[data-s]',f).forEach(b=>b.onclick=()=>{F.sort=b.dataset.s;draw()});
  const r=$('#min');r.oninput=()=>$('#minv').textContent=r.value;r.onchange=()=>{F.min=+r.value;draw()};
  list();
}
function init(){
  const shorts=D.picks.filter(p=>p.format!=='본편').length;
  $('#app').innerHTML=`<h1>편집할 장면</h1><div class="meta"><span>${esc(D.name)}</span><span>방송 ${fmtMin(dur)}</span><span>${D.picks.length}개 장면</span>${shorts?`<span>쇼츠 후보 ${shorts}개</span>`:''}${D.focus&&D.focus.text?`<span class="chip focus" title="${esc(D.focus.text)}">🎯 ${esc(D.focus.preset||'Focus')}</span>`:''}${D.generated?`<span>분석 ${esc(D.generated)}</span>`:''}</div>
  ${D.summary?`<div class="card"><div class="lab">방송 요약</div><div class="summary">${esc(D.summary)}</div></div>`:''}
  <div class="card"><div style="display:flex;align-items:center;gap:10px"><div class="lab" style="margin:0">타임라인</div><span style="flex:1"></span><span style="color:var(--muted);font-size:12px">장면 블록을 누르면 해당 영상으로 이동해요</span></div>
   <div id="tlWrap">${timeline()}<div class="tip" id="tip"></div></div>
   <div class="legend">${Object.entries(CAT).filter(([c])=>D.picks.some(p=>p.category===c)).map(([c,h])=>`<span><i style="background:${h}"></i>${esc(c)}</span>`).join('')}<span><i style="background:#b3a8ff;opacity:.5"></i>내 목소리</span><span><i style="background:#34d399;opacity:.6"></i>게임 소리</span><span style="color:#fbbf24">▲ 큰 리액션</span><span style="color:#f472b6">● 후원</span>${(D.tl.markers||[]).length?'<span style="color:#fbbf24">★ 방송 중 표시</span>':''}</div></div>
  <div class="filters" id="filters"></div><div id="list"></div>
  ${D.chapters.length?`<div class="card" style="margin-top:26px"><div class="lab">방송 구성</div><table>${D.chapters.map(c=>`<tr><td>${fmt(c.s)} ~ ${fmt(c.e)}</td><td>${esc(c.what)}</td></tr>`).join('')}</table></div>`:''}`;
  const svg=$('#tlSvg'),tip=$('#tip'),wrap=$('#tlWrap'),cur=$('#cur');
  svg.onmousemove=ev=>{const r=svg.getBoundingClientRect(),wr=wrap.getBoundingClientRect(),t=Math.max(0,Math.min(dur,(ev.clientX-r.left)/r.width*dur)),n=ev.target.dataset&&ev.target.dataset.n,h=n&&D.picks.find(z=>z.n==n);
    tip.style.display='block';tip.style.left=(ev.clientX-wr.left)+'px';tip.style.top=(ev.clientY-wr.top)+'px';tip.innerHTML=h?`<b>#${h.n} ${esc(h.title)}</b> · ${fmt(h.s)}–${fmt(h.e)} · ★${h.score}`:fmt(t);
    cur.setAttribute('x1',t/dur*1000);cur.setAttribute('x2',t/dur*1000);cur.setAttribute('opacity','.5')};
  svg.onmouseleave=()=>{tip.style.display='none';cur.setAttribute('opacity','0')};
  svg.onclick=ev=>{const n=ev.target.dataset&&ev.target.dataset.n;if(n)jump(+n)};
  draw();
}
init();
</script></body></html>
"""
