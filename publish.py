"""
SkyEye — publish the editors' page (and its preview clips) to Cloudflare R2.

R2 is Cloudflare's file storage (S3-compatible). SkyEye uploads a small web page plus the preview clips of
the moments Stan kept into a folder with a long random name, and posts the link to Discord. Anyone with the
link can watch; nobody can find it without the link. Reports are removed after 90 days by a lifecycle rule
that Stan sets once on the bucket in Cloudflare.

Nothing here touches Google Drive. The access keys live in r2_keys.json next to the app (private, like
discord_webhook.txt); the endpoint, bucket and public URL are not secret and live in config.yaml.

The upload speaks plain S3 (AWS Signature V4) through httpx, so there is nothing extra to install.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import mimetypes
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import scout
from scout import HERE

KEYS_FILE = HERE / "r2_keys.json"
REGION = "auto"                     # R2 ignores the region but it is part of the signature
SERVICE = "s3"


# ------------------------------------------------------------------ settings
def get_keys() -> dict:
    try:
        d = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        return {"access_key_id": str(d.get("access_key_id", "")).strip(),
                "secret_access_key": str(d.get("secret_access_key", "")).strip()}
    except Exception:
        return {"access_key_id": "", "secret_access_key": ""}


def set_keys(access_key_id: str | None = None, secret_access_key: str | None = None, clear: bool = False):
    """Save what was given and keep the rest ('' / None = leave as is). clear=True forgets both."""
    if clear:
        KEYS_FILE.unlink(missing_ok=True)
        return
    cur = get_keys()
    if access_key_id and access_key_id.strip():
        cur["access_key_id"] = access_key_id.strip()
    if secret_access_key and secret_access_key.strip():
        cur["secret_access_key"] = secret_access_key.strip()
    KEYS_FILE.write_text(json.dumps(cur, indent=1), encoding="utf-8")


def _tidy_url(u: str, keep_path: bool) -> str:
    """Forgive what people paste: spaces, a missing https://, a trailing slash — and for the endpoint, a
    bucket name stuck on the end (https://xxxx.r2.cloudflarestorage.com/my-bucket)."""
    u = (u or "").strip().rstrip("/")
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    if not keep_path:
        p = urlparse(u)
        u = f"{p.scheme}://{p.netloc}"
    return u


def settings(cfg: dict) -> dict:
    h = dict(cfg.get("hosting") or {})
    return {"endpoint": _tidy_url(h.get("endpoint"), keep_path=False),
            "bucket": str(h.get("bucket") or "").strip().strip("/"),
            "public_url": _tidy_url(h.get("public_url"), keep_path=True),
            "clip_quality": "original" if str(h.get("clip_quality") or "").lower() == "original" else "small"}


def configured(cfg: dict) -> bool:
    s, k = settings(cfg), get_keys()
    return bool(s["endpoint"] and s["bucket"] and s["public_url"] and k["access_key_id"] and k["secret_access_key"])


def missing(cfg: dict) -> list[str]:
    s, k = settings(cfg), get_keys()
    out = []
    for label, val in (("Endpoint URL", s["endpoint"]), ("Bucket name", s["bucket"]),
                       ("Public URL", s["public_url"]), ("Access Key ID", k["access_key_id"]),
                       ("Secret Access Key", k["secret_access_key"])):
        if not val:
            out.append(label)
    return out


def _check_urls(s: dict):
    for name, u in (("Endpoint URL", s["endpoint"]), ("Public URL", s["public_url"])):
        p = urlparse(u)
        local = p.hostname in ("localhost", "127.0.0.1")
        if p.scheme != "https" and not (p.scheme == "http" and local):
            raise RuntimeError(f"{name} must start with https:// (got “{u[:40]}”).")
        if not p.hostname:
            raise RuntimeError(f"{name} doesn't look like a web address.")


# ------------------------------------------------------------------ AWS Signature V4
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, payload_hash: str, access_key: str, secret_key: str,
         now: _dt.datetime | None = None, extra_headers: dict | None = None) -> dict:
    """Headers (incl. Authorization) for one S3 request, path-style, no query string."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = v
    names = sorted(headers)
    canon_headers = "".join(f"{n}:{str(headers[n]).strip()}\n" for n in names)
    signed = ";".join(names)
    canonical = "\n".join([method, quote(path, safe="/-_.~"), "", canon_headers, signed, payload_hash])
    scope = f"{day}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode(), day)
    for part in (REGION, SERVICE, "aws4_request"):
        k = _hmac(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {n: str(v) for n, v in headers.items() if n != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


class R2:
    def __init__(self, cfg: dict):
        self.s = settings(cfg)
        self.k = get_keys()
        _check_urls(self.s)
        self.host = urlparse(self.s["endpoint"]).netloc

    def _url_path(self, key: str) -> str:
        return f"/{self.s['bucket']}/{key.lstrip('/')}"

    def request(self, method: str, key: str, body: bytes = b"", content_type: str | None = None,
                cache: str | None = None):
        import httpx
        path = self._url_path(key)
        extra = {}
        if content_type:
            extra["content-type"] = content_type
        if cache:
            extra["cache-control"] = cache
        h = sign(method, self.host, path, hashlib.sha256(body).hexdigest(),
                 self.k["access_key_id"], self.k["secret_access_key"], extra_headers=extra)
        url = self.s["endpoint"] + quote(path, safe="/-_.~")
        last = None
        for attempt in range(3):
            try:
                r = httpx.request(method, url, content=body if method == "PUT" else None, headers=h, timeout=300)
            except httpx.HTTPError as e:            # network hiccup: try again
                last = e
                time.sleep(1.5 * (attempt + 1))
                h = sign(method, self.host, path, hashlib.sha256(body).hexdigest(),
                         self.k["access_key_id"], self.k["secret_access_key"], extra_headers=extra)
                continue
            if r.status_code >= 500:
                last = RuntimeError(f"R2 said {r.status_code}")
                time.sleep(1.5 * (attempt + 1))
                h = sign(method, self.host, path, hashlib.sha256(body).hexdigest(),
                         self.k["access_key_id"], self.k["secret_access_key"], extra_headers=extra)
                continue
            return r
        raise RuntimeError(f"Couldn't reach Cloudflare R2: {last}")

    def put_file(self, key: str, path: Path, content_type: str | None = None, cache: str | None = None):
        ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.put_bytes(key, path.read_bytes(), ctype, cache)

    def put_bytes(self, key: str, data: bytes, content_type: str, cache: str | None = None):
        r = self.request("PUT", key, data, content_type, cache)
        if r.status_code >= 300:
            raise RuntimeError(explain(r))

    def delete(self, key: str):
        self.request("DELETE", key)

    def public(self, key: str) -> str:
        return f"{self.s['public_url']}/{quote(key.lstrip('/'), safe='/-_.~')}"


def explain(r) -> str:
    """A plain-words reason for a failed R2 request."""
    code = r.status_code
    body = (r.text or "")[:300]
    if code in (401, 403):
        return ("Cloudflare refused the keys (403). Check the Access Key ID and Secret, and that the API token "
                "has “Object Read & Write” permission for this bucket.")
    if code == 404 or "NoSuchBucket" in body:
        return "Cloudflare says that bucket doesn't exist. Check the bucket name and the Endpoint URL."
    return f"Cloudflare R2 said {code}: {body}"


# ------------------------------------------------------------------ connection test
def test_connection(cfg: dict) -> str:
    """Upload a tiny file, read it back through the public URL, delete it. Returns a short success message."""
    miss = missing(cfg)
    if miss:
        raise RuntimeError("Fill in: " + ", ".join(miss) + ".")
    import httpx
    r2 = R2(cfg)
    key = f"_skyeye_test_{secrets.token_hex(4)}.txt"
    stamp = f"SkyEye {time.strftime('%Y-%m-%d %H:%M:%S')}"
    r2.put_bytes(key, stamp.encode(), "text/plain; charset=utf-8")
    try:
        try:
            g = httpx.get(r2.public(key), timeout=30, follow_redirects=True)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Upload worked, but the Public URL can't be reached: {e}")
        if g.status_code != 200 or g.text.strip() != stamp:
            raise RuntimeError(f"Upload worked, but the Public URL didn't serve the file (got {g.status_code}). "
                               "In the bucket's Settings, turn on public access (the r2.dev subdomain) and copy "
                               "that URL here.")
    finally:
        try:
            r2.delete(key)
        except Exception:
            pass
    return "Connected. Uploading and reading back both work."


# ------------------------------------------------------------------ publish the editors' page
PUBLISHED = "published.json"


def read_published(work: Path) -> dict:
    try:
        return json.loads((work / PUBLISHED).read_text(encoding="utf-8"))
    except Exception:
        return {}


def publish_page(work: Path, cfg: dict, page_html: str, clips: list[tuple[str, Path]],
                 on_progress=None, log=None) -> str:
    """Upload clips (name, local file) then index.html into this stream's folder. Returns the page link.
    The folder (random token) is created once per stream and reused, so the link stays the same when Stan
    sends an updated list."""
    miss = missing(cfg)
    if miss:
        raise RuntimeError("Editor page hosting isn't set up: " + ", ".join(miss) + " (Settings → Editor page).")
    say = log or (lambda m: None)
    rep = on_progress or (lambda f, t: None)
    r2 = R2(cfg)
    pub = read_published(work)
    token = pub.get("token") or secrets.token_urlsafe(16).replace("_", "x").replace("-", "y")
    prefix = f"r/{token}"
    old = set(pub.get("clips") or []) if pub.get("bucket") == r2.s["bucket"] else set()
    wanted = {n for n, _ in clips}
    done = old & wanted
    todo = [(n, p) for n, p in clips if n not in done]
    for i, (name, path) in enumerate(todo):
        scout.check_cancel()
        rep(i / max(1, len(todo) + 1), f"uploading clip {i + 1}/{len(todo)}")
        r2.put_file(f"{prefix}/clips/{name}", path, "video/mp4", "public, max-age=86400")
        done.add(name)
    scout.check_cancel()
    rep(len(todo) / max(1, len(todo) + 1), "uploading the page")
    r2.put_bytes(f"{prefix}/index.html", page_html.encode("utf-8"), "text/html; charset=utf-8", "no-cache")
    url = r2.public(f"{prefix}/index.html")
    for name in sorted(old - wanted):                          # clips no longer on the page (un-kept, or the
        try:                                                    # old bigger copies): free the space
            r2.delete(f"{prefix}/clips/{name}")
        except Exception:
            done.add(name)                                      # couldn't delete: remember it, try next time
    (work / PUBLISHED).write_text(json.dumps(
        {"token": token, "url": url, "clips": sorted(done), "bucket": r2.s["bucket"],
         "published": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False, indent=1), encoding="utf-8")
    say(f"  editor page: {url}")
    rep(1.0, "page is online")
    return url
