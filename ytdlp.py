"""
ytdlp.py
--------
Async-ready yt-dlp wrapper with:
  - In-memory TTL cache (8 min) — same URL won't hit YouTube twice
  - Async via ThreadPoolExecutor (non-blocking FastAPI)
  - Multi-client YouTube fallback (android → ios → web → tv_embedded → mweb)
  - Direct YouTube requests with proxy-free network fallback
  - PO Token + cookie file support
  - Exponential backoff retry on IP blocks
"""

import os
import random
import time
import logging
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import yt_dlp
from cachetools import TTLCache
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

# ── Logger ──────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Thread pool for blocking yt-dlp calls ───────────────────────────────────
_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.getenv("YTDLP_WORKERS", "32")),
    thread_name_prefix="ytdlp",
)

# ── In-memory cache (url+format → result, TTL = 8 min) ──────────────────────
_CACHE_TTL  = int(os.getenv("YTDLP_CACHE_TTL", "10800"))   # seconds
_CACHE_SIZE = int(os.getenv("YTDLP_CACHE_SIZE", "5000"))   # max entries
_cache      = TTLCache(maxsize=_CACHE_SIZE, ttl=_CACHE_TTL)
_cache_lock = threading.Lock()


# ── Optional shared Redis cache (REDIS_URL) ──────────────────────────────────
# With Redis, every worker / every server shares the same resolved links:
# a song fetched once by ANY server plays instantly on ALL servers.
_redis = None
_REDIS_URL = os.getenv("REDIS_URL", "").strip()
if _REDIS_URL:
    try:
        import json as _json
        import redis as _redis_mod
        _redis = _redis_mod.Redis.from_url(_REDIS_URL, socket_timeout=0.3,
                                           socket_connect_timeout=0.5, decode_responses=True)
        _redis.ping()
        logger.info("Shared Redis cache enabled")
    except Exception as _e:
        logger.warning(f"Redis unavailable, using RAM cache only: {_e}")
        _redis = None


def _link_expired(value) -> bool:
    # googlevideo links die at their 'expire' time -> refresh 5 min before.
    return isinstance(value, dict) and value.get("_exp", 1e18) < time.time() + 300


def _cache_get(key: str):
    with _cache_lock:
        v = _cache.get(key)
    if v is not None and not _link_expired(v):
        return v
    if _redis is not None:
        try:
            raw = _redis.get("ba:" + key)
            if raw:
                v = _json.loads(raw)
                if not _link_expired(v):
                    with _cache_lock:
                        _cache[key] = v
                    return v
        except Exception:
            pass
    return None


def _cache_set(key: str, value):
    with _cache_lock:
        _cache[key] = value
    if _redis is not None:
        try:
            ttl = _CACHE_TTL
            if isinstance(value, dict) and value.get("_exp"):
                ttl = max(60, int(value["_exp"] - time.time() - 300))
            _redis.setex("ba:" + key, ttl, _json.dumps(value, default=str))
        except Exception:
            pass


def cache_stats() -> dict:
    with _cache_lock:
        return {"size": len(_cache), "maxsize": _CACHE_SIZE, "ttl": _CACHE_TTL}


def cache_clear():
    with _cache_lock:
        _cache.clear()


# ── User-agent pool ──────────────────────────────────────────────────────────
try:
    from fake_useragent import UserAgent as _FUA
    _fua = _FUA()
    def _random_ua() -> str:
        try:
            return _fua.random
        except Exception:
            return _random_ua_static()
except Exception:
    _fua = None
    def _random_ua() -> str:
        return _random_ua_static()

_STATIC_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36",
]

def _random_ua_static() -> str:
    return random.choice(_STATIC_UAS)


# ── Proxy pool (fast, thread-safe round-robin) ─────────────────────────────
_proxy_lock  = threading.Lock()
_proxy_index = 0


def _normalise_proxy(raw: str) -> Optional[str]:
    """Validate a proxy URL without exposing credentials in logs."""
    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https", "socks4", "socks5") or not parsed.hostname:
        return None
    return value


def _load_proxies() -> List[str]:
    """Optional proxies from PROXY_LIST (comma separated). Empty = direct.

    A dead proxy never blocks extraction: every attempt falls back to a
    direct connection (see _try_extract).
    """
    raw = os.getenv("PROXY_LIST", "") or os.getenv("PROXY_URL", "")
    proxies = [p for p in (_normalise_proxy(x) for x in raw.split(",")) if p]
    if proxies:
        logger.info("Loaded %d proxies.", len(proxies))
    else:
        logger.info("No proxy configured; using direct YouTube connections.")
    return proxies


_PROXY_POOL: List[str] = _load_proxies()


def reload_proxies() -> int:
    global _PROXY_POOL, _proxy_index
    with _proxy_lock:
        _PROXY_POOL = _load_proxies()
        _proxy_index = 0
    return len(_PROXY_POOL)


def get_proxy_pool() -> List[str]:
    with _proxy_lock:
        return list(_PROXY_POOL)


def _get_proxy() -> Optional[str]:
    global _proxy_index
    with _proxy_lock:
        if not _PROXY_POOL:
            return None
        proxy = _PROXY_POOL[_proxy_index % len(_PROXY_POOL)]
        # Every extraction/client attempt gets the next proxy.
        _proxy_index += 1
    return proxy


# ── Cookie POOL (multiple accounts, auto-rotate, auto-skip bad ones) ─────────
# Put any number of Netscape cookie files in ./cookies/*.txt (one per account).
# Also used: YTDLP_COOKIE_FILE and ./cookies.txt. A cookie that triggers
# "Sign in to confirm you're not a bot" is benched for COOKIE_COOLDOWN seconds
# and the next one is used automatically -> playback never stops.
import glob as _glob
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_COOKIE_COOLDOWN = int(os.getenv("COOKIE_COOLDOWN", "1800"))
_cookie_lock = threading.Lock()
_cookie_bad: Dict[str, float] = {}
_cookie_idx = 0
_cookie_tls = threading.local()


def _cookie_files() -> List[str]:
    files = []
    env = os.getenv("YTDLP_COOKIE_FILE", "").strip()
    if env and os.path.isfile(env):
        files.append(env)
    files += sorted(_glob.glob(os.path.join(_BASE_DIR, "cookies", "*.txt")))
    single = os.path.join(_BASE_DIR, "cookies.txt")
    if os.path.isfile(single):
        files.append(single)
    return list(dict.fromkeys(files))


def _cookie_path():
    """Return next healthy cookie file (round-robin) or None."""
    global _cookie_idx
    files = _cookie_files()
    if not files:
        return None
    now = time.time()
    with _cookie_lock:
        healthy = [f for f in files if _cookie_bad.get(f, 0) < now] or files
        _cookie_idx = (_cookie_idx + 1) % len(healthy)
        chosen = healthy[_cookie_idx]
    _cookie_tls.current = chosen
    return chosen


def mark_cookie_bad(path: Optional[str] = None):
    path = path or getattr(_cookie_tls, "current", None)
    if path:
        with _cookie_lock:
            _cookie_bad[path] = time.time() + _COOKIE_COOLDOWN
        logger.warning(f"Cookie benched for {_COOKIE_COOLDOWN}s: {os.path.basename(path)}")


def cookie_pool_status() -> dict:
    now = time.time()
    files = _cookie_files()
    return {
        "total": len(files),
        "healthy": sum(1 for f in files if _cookie_bad.get(f, 0) < now),
        "files": [{"name": os.path.basename(f),
                   "benched_for": max(0, int(_cookie_bad.get(f, 0) - now))} for f in files],
    }


# ── Sleep between calls ──────────────────────────────────────────────────────
_MIN_SLEEP = float(os.getenv("YTDLP_MIN_SLEEP", "0"))
_GEO_COUNTRY = os.getenv("YTDLP_GEO_COUNTRY", "US").strip().upper() or "US"
_MAX_SLEEP = float(os.getenv("YTDLP_MAX_SLEEP", "0"))


def _sleep():
    if _MAX_SLEEP <= 0:
        return
    time.sleep(random.uniform(_MIN_SLEEP, _MAX_SLEEP))


# ── Custom exceptions ────────────────────────────────────────────────────────
class IPBlockedError(Exception):
    """YouTube IP block / 429 / bot-check."""

class ExtractionError(Exception):
    """Genuine yt-dlp extraction failure."""


# ── Error classifiers ────────────────────────────────────────────────────────
def _is_ip_block(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in [
        "429", "too many requests", "ip block", "blocked",
        "bot", "captcha", "nsig extraction failed",
        "sign in to confirm", "confirm you're not a bot",
        "http error 403", "forbidden",
    ])


def _is_unavailable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in [
        "video unavailable", "private video",
        "has been removed", "no longer available",
        "does not exist", "account has been terminated",
        "copyright",
    ])


def _is_network_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in [
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname",
        "getaddrinfo failed",
        "connection refused",
        "network is unreachable",
        "connection reset",
        "timed out",
        "proxy error",
    ])


# ── yt-dlp options builder ───────────────────────────────────────────────────
def _build_opts(
    client: Optional[list],
    extra: Optional[Dict] = None,
    proxy_override: Optional[str] = None,
    disable_proxy: bool = False,
    use_cookies: bool = True,
) -> Dict[str, Any]:
    proxy = proxy_override if proxy_override is not None else (None if disable_proxy else _get_proxy())
    ua    = _random_ua()

    opts: Dict[str, Any] = {
        "quiet":          True,
        "no_warnings":    True,
        "skip_download":  True,
        "noplaylist":     True,
        "socket_timeout": 20,
        "retries":        3,
        "extractor_retries": 2,
        "nocheckcertificate": True,
        "geo_bypass":     True,
        "geo_bypass_country": _GEO_COUNTRY,
        "http_headers": {
            "User-Agent":                ua,
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding":           "gzip, deflate, br",
            "Referer":                   "https://www.google.com/",
            "DNT":                       "1",
            "Cache-Control":             "max-age=0",
        },
        "extractor_args": {"youtube": {}},
    }
    if client:
        opts["extractor_args"]["youtube"]["player_client"] = client

    if proxy:
        opts["proxy"] = proxy

    cookie_file = _cookie_path() if use_cookies else None
    if cookie_file:
        opts["cookiefile"] = cookie_file

    po_token = os.getenv("YTDLP_PO_TOKEN", "").strip()
    if po_token:
        opts["extractor_args"]["youtube"]["po_token"] = [
            t if "+" in t else f"web+{t}" for t in po_token.split(",") if t.strip()
        ]

    if extra:
        opts.update(extra)

    return opts


# ── Multi-client fallback ────────────────────────────────────────────────────
# None = yt-dlp's own default client set (best maintained, tried first).
_YT_CLIENTS = [
    None,
    ["tv"],
    ["web_safari"],
    ["mweb"],
    ["android_vr"],
    ["ios"],
]


_LAST_GOOD = None


def _route_plan():
    """Route order (ROUTE_MODE=auto):
    - sirf cookies      -> direct+cookies, phir direct
    - sirf proxy        -> proxy, doosri proxy, phir direct
    - proxy + cookies   -> proxy (main), backup: direct+cookies, phir direct
    ROUTE_MODE=cookies / proxy force karne ke liye."""
    mode = os.getenv("ROUTE_MODE", "auto").lower()
    has_p = bool(get_proxy_pool()) and mode != "cookies"
    has_c = bool(_cookie_files()) and mode != "proxy"
    plan = []
    if has_p:
        plan.append((_get_proxy(), False, False))
        if len(get_proxy_pool()) > 1:
            plan.append((_get_proxy(), False, False))
    if has_c:
        plan.append((None, True, True))
    plan.append((None, True, False))
    return plan


def route_mode() -> str:
    p, c = bool(get_proxy_pool()), bool(_cookie_files())
    return "proxy+cookies(backup)" if p and c else "proxy" if p else "cookies" if c else "direct"


def _try_extract(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None

    global _LAST_GOOD
    extra_opts = dict(extra_opts or {})
    skip = extra_opts.pop("_skip_clients", [])
    order = _YT_CLIENTS
    if _LAST_GOOD in _YT_CLIENTS:   # try the client that worked last time first
        order = [_LAST_GOOD] + [c for c in _YT_CLIENTS if c != _LAST_GOOD]
    if skip:
        order = [c for c in order if c not in skip] + [c for c in order if c in skip]
    for client in order:
        attempts = _route_plan()

        for proxy_override, disable_proxy, use_cookies in attempts:
            try:
                _sleep()
                opts = _build_opts(
                    client,
                    extra_opts,
                    proxy_override=proxy_override,
                    disable_proxy=disable_proxy,
                    use_cookies=use_cookies,
                )
                route = "direct" if disable_proxy else "proxy"
                logger.debug(f"Client {client[0] if client else 'default'} via {route} → {url[:60]}")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info is None:
                    raise ExtractionError("yt-dlp returned no data.")
                logger.debug(f"OK with client {client[0] if client else 'default'} via {route}")
                _LAST_GOOD = client
                info["_client"] = client
                return info
            except yt_dlp.utils.DownloadError as e:
                if use_cookies and _is_ip_block(e):
                    mark_cookie_bad()
                if _is_unavailable(e):
                    raise ExtractionError(str(e)) from e
                if proxy_override and _is_network_error(e):
                    logger.warning("Proxy request failed; retrying directly.")
                last_exc = e
            except Exception as e:
                if _is_unavailable(e):
                    raise ExtractionError(str(e)) from e
                if proxy_override and _is_network_error(e):
                    logger.warning("Proxy request failed; retrying directly.")
                last_exc = e

    if last_exc and _is_ip_block(last_exc):
        raise IPBlockedError(str(last_exc)) from last_exc
    raise ExtractionError(str(last_exc or "All clients failed."))


@retry(
    reraise=True,
    stop=stop_after_attempt(int(os.getenv("YTDLP_MAX_RETRIES", "2"))),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(IPBlockedError),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _extract_sync(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    return _try_extract(url, extra_opts)


# ── Async wrappers ───────────────────────────────────────────────────────────
_INFLIGHT: Dict[str, "asyncio.Future"] = {}


async def _extract_async(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    """Run sync extraction in threadpool. Identical concurrent requests share
    ONE yt-dlp call (single-flight) so 1000 users asking the same song = 1 hit."""
    key = f"{url}::{(extra_opts or {}).get('format', '')}"
    fut = _INFLIGHT.get(key)
    if fut is not None:
        return await asyncio.shield(fut)
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_EXECUTOR, lambda: _extract_sync(url, extra_opts))
    _INFLIGHT[key] = fut
    try:
        return await asyncio.shield(fut)
    finally:
        _INFLIGHT.pop(key, None)


def _info_summary(info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id":               info.get("id"),
        "title":            info.get("title"),
        "uploader":         info.get("uploader"),
        "channel_url":      info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "view_count":       info.get("view_count"),
        "like_count":       info.get("like_count"),
        "upload_date":      info.get("upload_date"),
        "thumbnail":        info.get("thumbnail"),
        "description":      (info.get("description") or "")[:500],
        "webpage_url":      info.get("webpage_url"),
        "is_live":          info.get("is_live", False),
        "tags":             (info.get("tags") or [])[:10],
    }


async def get_info_and_download(url: str, format_id: Optional[str] = None):
    """ONE yt-dlp call returns both metadata and direct URL (was 2 calls)."""
    fmt = format_id or "best"
    info_c = _cache_get(f"info::{url}")
    dl_c = _cache_get(f"dl::{url}::{fmt}")
    if info_c and dl_c:
        return info_c, dl_c
    dl = await get_direct_download_url(url, format_id=fmt)
    info_c = _cache_get(f"info::{url}")
    if not info_c:
        info_c = await get_video_info(url)
    return info_c, dl


# ── Public API ───────────────────────────────────────────────────────────────

async def get_video_info(url: str) -> Dict[str, Any]:
    # 1. RAM cache check
    cache_key = f"info::{url}"
    cached = _cache_get(cache_key)
    if cached:
        logger.debug(f"RAM cache HIT info: {url[:50]}")
        return cached

    # 2. DB cache check (survives restarts)
    try:
        from database import get_cached_video_metadata, cache_video_metadata
        db_cached = get_cached_video_metadata(url)
        if db_cached:
            logger.debug(f"DB cache HIT info: {url[:50]}")
            _cache_set(cache_key, db_cached)   # warm RAM cache too
            return db_cached
    except Exception:
        pass

    # 3. Fetch from YouTube
    info = await _extract_async(url)
    result = {
        "id":               info.get("id"),
        "title":            info.get("title"),
        "uploader":         info.get("uploader"),
        "channel_url":      info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "view_count":       info.get("view_count"),
        "like_count":       info.get("like_count"),
        "upload_date":      info.get("upload_date"),
        "thumbnail":        info.get("thumbnail"),
        "description":      (info.get("description") or "")[:500],
        "webpage_url":      info.get("webpage_url"),
        "is_live":          info.get("is_live", False),
        "tags":             (info.get("tags") or [])[:10],
    }

    # 4. Save to both caches
    _cache_set(cache_key, result)
    try:
        cache_video_metadata(url, result)
    except Exception:
        pass

    return result


async def get_available_formats(url: str) -> List[Dict[str, Any]]:
    cache_key = f"formats::{url}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    info    = await _extract_async(url)
    formats = info.get("formats", [])
    result  = []
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        if (f.get("format_id") or "").startswith("sb"):
            continue
        result.append({
            "format_id":       f.get("format_id"),
            "ext":             f.get("ext"),
            "resolution":      f.get("resolution") or f.get("format_note", ""),
            "fps":             f.get("fps"),
            "vcodec":          f.get("vcodec"),
            "acodec":          f.get("acodec"),
            "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
            "tbr":             f.get("tbr"),
        })
    _cache_set(cache_key, result)
    return result


def _probe_ok(u: str, headers: dict) -> bool:
    """Tiny 2-byte request: makes sure the link really plays before we hand it out."""
    import urllib.request
    try:
        h = {k: v for k, v in headers.items() if k.lower() in ("user-agent", "referer", "origin")}
        h["Range"] = "bytes=0-1"
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=6) as r:
            return r.status in (200, 206)
    except Exception as e:
        return "403" not in str(e) and "410" not in str(e)


async def get_direct_download_url(url: str, format_id: Optional[str] = None) -> Dict[str, Any]:
    fmt       = format_id or "best"
    cache_key = f"dl::{url}::{fmt}"
    cached    = _cache_get(cache_key)
    if cached:
        logger.debug(f"Cache HIT dl: {url[:50]} fmt={fmt}")
        return cached

    info = None
    bad_clients = []
    for _try in range(len(_YT_CLIENTS)):
        info = await _extract_async(url, {"format": fmt, "_skip_clients": list(bad_clients)})
        _u = info.get("url") or next((rf.get("url") for rf in (info.get("requested_formats") or []) if rf.get("url")), None)
        _h = dict(info.get("http_headers") or {})
        if not _u or info.get("is_live") or ".m3u8" in _u:
            break
        if await asyncio.to_thread(_probe_ok, _u, _h):
            break
        logger.warning(f"Stream link 403 on probe (try {_try + 1}) — re-resolving {url[:50]}")
        if info.get("_client") is not None:
            bad_clients.append(info["_client"])
    if not _cache_get(f"info::{url}"):
        _cache_set(f"info::{url}", _info_summary(info))
    direct_url = info.get("url")

    if not direct_url:
        for rf in (info.get("requested_formats") or []):
            if rf.get("url"):
                direct_url = rf["url"]
                break

    if not direct_url:
        for f in info.get("formats", []):
            if f.get("url"):
                direct_url = f["url"]
                break

    if not direct_url:
        raise ExtractionError("Could not resolve a direct stream URL for this format.")

    result = {
        "title":      info.get("title"),
        "format_id":  info.get("format_id"),
        "ext":        info.get("ext"),
        "resolution": info.get("resolution"),
        "direct_url": direct_url,
        "is_live":    info.get("is_live", False),
        "http_headers": {k: v for k, v in (info.get("http_headers") or {}).items()
                         if k.lower() in ("user-agent", "referer", "origin")},
    }
    try:
        from urllib.parse import parse_qs
        _e = parse_qs(urlparse(direct_url).query).get("expire")
        if _e:
            result["_exp"] = int(_e[0])
    except Exception:
        pass
    _cache_set(cache_key, result)
    return result


async def search_youtube(query: str, max_results: int = 5, country: Optional[str] = None) -> List[Dict[str, Any]]:
    """Search YouTube and return simplified result list. Optional country (e.g. IN, US, GB) sets the search region."""
    region = (country or "").strip().upper() or _GEO_COUNTRY
    cache_key = f"search::{query}::{max_results}::{region}"
    cached    = _cache_get(cache_key)
    if cached:
        return cached

    def _do_search(use_proxy: bool):
        opts = _build_opts(
            None,
            {"extract_flat": True, "noplaylist": True},
            disable_proxy=not use_proxy,
        )
        opts["geo_bypass_country"] = region
        opts["http_headers"]["Accept-Language"] = f"{region.lower()},{region.lower()}-{region};q=0.9,en;q=0.8"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            return info.get("entries", []) if info else []

    # Fast path: direct YouTube InnerTube search with the requested region (gl).
    try:
        results = await _innertube_search(query, max_results, region)
        if results:
            _cache_set(cache_key, results)
            return results
    except Exception as e:
        logger.warning(f"InnerTube search failed ({e}); falling back to yt-dlp.")

    loop = asyncio.get_event_loop()
    # Try the configured route first, then direct DNS/network access.
    routes = [True, False] if get_proxy_pool() else [False, False]
    last_exc = None
    entries = []
    for index, use_proxy in enumerate(routes):
        try:
            entries = await loop.run_in_executor(
                _EXECUTOR, lambda route=use_proxy: _do_search(route)
            )
            break
        except Exception as e:
            last_exc = e
            if index < len(routes) - 1:
                logger.warning("YouTube search route failed; retrying with fallback.")
                await asyncio.sleep(1.5 * (index + 1))
    if last_exc is not None and not entries:
        raise ExtractionError(f"YouTube search network error: {last_exc}") from last_exc

    results = []
    for e in entries:
        vid_id = e.get("id") or ""
        if not vid_id:
            continue
        results.append({
            "id":       vid_id,
            "title":    e.get("title", ""),
            "uploader": e.get("uploader") or e.get("channel", ""),
            "duration": e.get("duration"),
            "url":      f"https://www.youtube.com/watch?v={vid_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
        })

    _cache_set(cache_key, results)
    return results


# ── Fast InnerTube search (country-aware, ~10x lighter than yt-dlp) ───────────
_HTTP: Optional["httpx.AsyncClient"] = None

def _http():
    global _HTTP
    import httpx
    if _HTTP is None:
        _HTTP = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=4.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            http2=False,
        )
    return _HTTP


def _walk_videos(o):
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            vr = cur.get("videoRenderer")
            if vr:
                yield vr
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _dur_to_sec(txt):
    if not txt:
        return None
    try:
        sec = 0
        for part in txt.split(":"):
            sec = sec * 60 + int(part)
        return sec
    except ValueError:
        return None


async def _innertube_search(query: str, limit: int, region: str) -> List[Dict[str, Any]]:
    body = {
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00",
                               "hl": "en", "gl": region}},
        "query": query,
        "params": "EgIQAQ%3D%3D",   # videos only
    }
    proxy_note = None  # direct route; proxies are only used by yt-dlp
    r = await _http().post(
        "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
        json=body,
        headers={"User-Agent": _random_ua(), "Accept-Language": f"en-{region},en;q=0.9"},
    )
    r.raise_for_status()
    out = []
    for v in _walk_videos(r.json()):
        vid = v.get("videoId")
        if not vid:
            continue
        title = "".join(x.get("text", "") for x in (v.get("title", {}).get("runs") or []))
        owner = (v.get("ownerText", {}).get("runs") or [{}])[0].get("text", "")
        length = (v.get("lengthText") or {}).get("simpleText")
        out.append({
            "id": vid,
            "title": title,
            "uploader": owner,
            "duration": _dur_to_sec(length),
            "duration_text": length or "",
            "is_live": length is None,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "country": region,
        })
        if len(out) >= limit:
            break
    return out


def prewarm(url: str, fmt: str):
    """Fire-and-forget: resolve stream URL into cache so the next play is instant."""
    async def _go():
        try:
            await get_info_and_download(url, fmt)
        except Exception:
            pass
    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        pass
