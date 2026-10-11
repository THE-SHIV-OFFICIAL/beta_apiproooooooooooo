"""
youtube.py
----------
BetaAPI — Drop-in replacement for any music bot's youtube.py

Setup:
  1. Copy this file to your music bot's platforms folder
     (rename to Youtube.py if needed)
  2. Add to .env:
       BETAAPI_URL=http://localhost:8000
       BETAAPI_KEY=SHIV255445425731BETA
  3. Done — no other changes needed

Compatible with: AnonXMusic, YukkiMusic, SankiMusic, EnafulMusic,
                 any bot using YouTubeAPI class
"""

import asyncio
import os
import re
from typing import Union
from urllib.parse import urlparse

import aiohttp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message

# ── Config ───────────────────────────────────────────────────────────────────
def _normalise_base_url(raw: str) -> str:
    value = (raw or "").strip().rstrip("/")
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        scheme = "http" if value.startswith(("localhost", "127.", "0.0.0.0")) else "https"
        value = scheme + "://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("BETAAPI_URL must include a valid HTTP(S) host.")
    return value.rstrip("/")

def _ensure_http_url(raw: str, label: str = "URL") -> str:
    """Return a valid absolute HTTP(S) URL for outbound requests."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError(f"{label} is empty.")
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{label} must include a valid HTTP(S) URL.")
    return value

API_URL = _normalise_base_url(os.environ.get("BETAAPI_URL", ""))
API_KEY = os.environ.get("BETAAPI_KEY", "")

if not API_URL:
    raise EnvironmentError(
        "[BetaAPI] BETAAPI_URL not set.\n"
        "Add to .env:  BETAAPI_URL=http://your-server:8000"
    )
if not API_KEY:
    raise EnvironmentError(
        "[BetaAPI] BETAAPI_KEY not set.\n"
        "Add to .env:  BETAAPI_KEY=SHIV255445425731BETA\n"
        "Get key: BetaAPI Telegram Bot → /start"
    )

_HEADERS  = {"x-api-key": API_KEY, "Connection": "keep-alive"}
_TIMEOUT  = aiohttp.ClientTimeout(total=60, connect=10)
_VTIMEOUT = aiohttp.ClientTimeout(total=120, connect=10)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")

# Connection pool — reuse connections for speed
_connector = None

def _get_connector():
    global _connector
    if _connector is None or _connector.closed:
        _connector = aiohttp.TCPConnector(
            limit=20,
            ttl_dns_cache=300,
            use_dns_cache=True,
        )
    return _connector


# ── Standalone functions (direct imports) ────────────────────────────────────

async def download_song(link: str) -> str:
    """Download audio → file path. Used by bots importing directly."""
    video_id = link.split("v=")[-1].split("&")[0] if "v=" in link else link.strip()
    if not video_id or len(video_id) < 3:
        return None
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        return file_path
    try:
        data = await _api_get("/api/stream", {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "type": "audio"
        })
        stream_url = data.get("stream_url") or data.get("direct_url")
        if not stream_url:
            return None
        ok = await _fetch_media(stream_url, f"https://www.youtube.com/watch?v={video_id}",
                                "audio", file_path, headers=data.get("http_headers"))
        return file_path if ok else None
    except Exception:
        _cleanup(file_path)
        return None


async def download_video(link: str) -> str:
    """Download video → file path. Used by bots importing directly."""
    video_id = link.split("v=")[-1].split("&")[0] if "v=" in link else link.strip()
    if not video_id or len(video_id) < 3:
        return None
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        return file_path
    try:
        data = await _api_get("/api/stream", {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "type": "video"
        }, timeout=_VTIMEOUT)
        stream_url = data.get("stream_url") or data.get("direct_url")
        if not stream_url:
            return None
        ok = await _fetch_media(stream_url, f"https://www.youtube.com/watch?v={video_id}",
                                "video", file_path, timeout=_VTIMEOUT,
                                headers=data.get("http_headers"))
        return file_path if ok else None
    except Exception:
        _cleanup(file_path)
        return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _time_to_seconds(time_str: str) -> int:
    try:
        parts = [int(x) for x in str(time_str).split(":")]
        return sum(x * (60 ** i) for i, x in enumerate(reversed(parts)))
    except Exception:
        return 0


def _seconds_to_min(seconds) -> str:
    try:
        m, s = divmod(int(seconds or 0), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return "0:00"


def _cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def _api_get(path: str, params: dict, timeout=None, retries: int = 2) -> dict:
    """GET request to BetaAPI with auto-retry on failure."""
    url = _ensure_http_url(f"{API_URL}{path}", "BetaAPI request URL")
    last_exc = None
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(
                connector=_get_connector(),
                connector_owner=False,
            ) as session:
                async with session.get(
                    url,
                    params=params,
                    headers=_HEADERS,
                    timeout=timeout or _TIMEOUT,
                ) as resp:
                    if resp.status == 401:
                        raise PermissionError(
                            "BetaAPI key invalid/expired. "
                            "Get new key from BetaAPI bot → /start"
                        )
                    if resp.status == 429:
                        data = await resp.json()
                        msg  = (data.get("detail") or {}).get("message", "Rate limit exceeded.")
                        raise ConnectionAbortedError(f"Rate limit: {msg}")
                    if resp.status == 503:
                        raise ConnectionError("YouTube IP block. Retry in a few minutes.")
                    if resp.status != 200:
                        text = await resp.text()
                        raise ConnectionError(f"BetaAPI {resp.status}: {text[:100]}")
                    return await resp.json()
        except (PermissionError, ConnectionAbortedError):
            raise   # Don't retry auth/rate limit errors
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last_exc or ConnectionError("BetaAPI request failed")


_CHUNK = 4 * 1024 * 1024
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


async def _download_file(url: str, path: str, timeout=None, headers=None):
    """Fast download: parallel 4 MB range chunks (YouTube 403s/throttles one big GET)."""
    url = _ensure_http_url(url, "Stream URL")
    async with aiohttp.ClientSession(headers={**_UA, **(headers or {})}, timeout=timeout or _VTIMEOUT) as session:
        async def part(start, end):
            for _ in range(3):
                async with session.get(url, headers={"Range": f"bytes={start}-{end}"}) as r:
                    if r.status in (200, 206):
                        return r.status, r.headers.get("Content-Range", ""), await r.read()
                    last = r.status
            raise ConnectionError(f"Download failed: HTTP {last}")

        status, cr, first = await part(0, _CHUNK - 1)
        total = int(cr.rsplit("/", 1)[1]) if status == 206 and "/" in cr and cr.rsplit("/", 1)[1].isdigit() else len(first)
        sem = asyncio.Semaphore(6)

        async def lim(s, e):
            async with sem:
                return (await part(s, e))[2]

        rest = [lim(s, min(s + _CHUNK, total) - 1) for s in range(len(first), total, _CHUNK)]
        chunks = await asyncio.gather(*rest)
        with open(path, "wb") as f:
            f.write(first)
            for c in chunks:
                f.write(c)
        if os.path.getsize(path) < total:
            raise ConnectionError("Incomplete download")


async def _download_via_api(link: str, media_type: str, path: str, timeout=None):
    """Fallback: let the BetaAPI server fetch the media and stream it to us.

    YouTube stream URLs are bound to the IP that resolved them, so a bot on a
    different server often gets 403/empty files when it downloads them itself.
    """
    url = _ensure_http_url(f"{API_URL}/download", "BetaAPI download URL")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            params={"url": link, "type": media_type},
            headers=_HEADERS,
            timeout=timeout or _VTIMEOUT,
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise ConnectionError(f"BetaAPI /download {resp.status}: {text[:100]}")
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(131072):
                    f.write(chunk)


async def _fetch_media(stream_url: str, link: str, media_type: str,
                       path: str, timeout=None, headers=None) -> bool:
    """Direct download first, then server-side proxy. True if file is non-empty."""
    try:
        await _download_file(stream_url, path, timeout=timeout, headers=headers)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
    except Exception:
        pass
    _cleanup(path)
    try:
        await _download_via_api(link, media_type, path, timeout=timeout)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
    except Exception:
        pass
    _cleanup(path)
    return False


# ── YouTubeAPI class ──────────────────────────────────────────────────────────

class YouTubeAPI:

    def __init__(self):
        self.base     = "https://www.youtube.com/watch?v="
        self.regex    = r"(?:youtube\.com|youtu\.be)"
        self.listbase = "https://youtube.com/playlist?list="
        self.reg      = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    async def exists(self, link: str, videoid: Union[bool, str] = None) -> bool:
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    async def url(self, message_1: Message) -> Union[str, None]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        for message in messages:
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption
                        return text[entity.offset: entity.offset + entity.length]
            elif message.caption_entities:
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
        return None

    def _clean(self, link: str) -> str:
        value = (link or "").strip()
        if not re.match(r"^https?://", value, re.IGNORECASE) and re.match(r"^(?:(?:www\.)?youtube\.com|youtu\.be)(?:/|$)", value, re.IGNORECASE):
            value = _ensure_http_url(value, "YouTube URL")
        return value.split("&")[0] if "&" in value else value

    async def details(self, link: str, videoid: Union[bool, str] = None):
        """Returns: title, duration_min, duration_sec, thumbnail, vidid"""
        if videoid:
            link = self.base + link
        link = self._clean(link)
        is_url = link.startswith("http")
        if is_url:
            data         = await _api_get("/info", {"url": link})
            d            = data.get("data", {})
            title        = d.get("title") or "Unknown"
            duration_sec = int(d.get("duration_seconds") or 0)
            duration_min = _seconds_to_min(duration_sec)
            thumbnail    = d.get("thumbnail") or ""
            vidid        = d.get("id") or ""
        else:
            data    = await _api_get("/search", {"q": link, "limit": 1})
            results = data.get("results", [])
            if not results:
                raise ValueError(f"No results: {link}")
            r            = results[0]
            title        = r.get("title") or "Unknown"
            vidid        = r.get("id") or ""
            duration_sec = int(r.get("duration") or 0)
            duration_min = _seconds_to_min(duration_sec)
            thumbnail    = r.get("thumbnail") or ""
        return title, duration_min, duration_sec, thumbnail, vidid

    async def title(self, link: str, videoid: Union[bool, str] = None) -> str:
        t, *_ = await self.details(link, videoid)
        return t

    async def duration(self, link: str, videoid: Union[bool, str] = None) -> str:
        _, d, *_ = await self.details(link, videoid)
        return d

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None) -> str:
        _, _, _, t, _ = await self.details(link, videoid)
        return t

    async def track(self, link: str, videoid: Union[bool, str] = None):
        """Returns: track_details dict, vidid"""
        if videoid:
            link = self.base + link
        link    = self._clean(link)
        is_url  = link.startswith("http")
        if is_url:
            data         = await _api_get("/info", {"url": link})
            d            = data.get("data", {})
            title        = d.get("title") or "Unknown"
            vidid        = d.get("id") or ""
            duration_min = _seconds_to_min(d.get("duration_seconds"))
            thumbnail    = d.get("thumbnail") or ""
            yturl        = link
        else:
            data    = await _api_get("/search", {"q": link, "limit": 1})
            results = data.get("results", [])
            if not results:
                raise ValueError(f"No results: {link}")
            r            = results[0]
            title        = r.get("title") or "Unknown"
            vidid        = r.get("id") or ""
            duration_min = _seconds_to_min(r.get("duration"))
            thumbnail    = r.get("thumbnail") or ""
            yturl        = r.get("url") or self.base + vidid
        return {
            "title":        title,
            "link":         yturl,
            "vidid":        vidid,
            "duration_min": duration_min,
            "thumb":        thumbnail,
        }, vidid

    async def formats(self, link: str, videoid: Union[bool, str] = None):
        """Returns: formats_list, link"""
        if videoid:
            link = self.base + link
        link = self._clean(link)
        data = await _api_get("/formats", {"url": link})
        result = []
        for f in data.get("formats", []):
            fmt_str = f.get("resolution") or f.get("ext") or ""
            if "dash" in fmt_str.lower():
                continue
            result.append({
                "format":      f"{f.get('format_id')} - {fmt_str}",
                "filesize":    f.get("filesize_approx"),
                "format_id":   f.get("format_id"),
                "ext":         f.get("ext"),
                "format_note": f.get("resolution") or "",
                "yturl":       link,
            })
        return result, link

    async def slider(self, link: str, query_type: int,
                     videoid: Union[bool, str] = None):
        """Returns: title, duration_min, thumbnail, vidid"""
        if videoid:
            link = self.base + link
        link    = self._clean(link)
        data    = await _api_get("/search", {"q": link, "limit": 10})
        results = data.get("results", [])
        if not results:
            return "Unknown", "0:00", "", ""
        idx = min(query_type, len(results) - 1)
        r   = results[idx]
        return (
            r.get("title") or "Unknown",
            _seconds_to_min(r.get("duration")),
            r.get("thumbnail") or "",
            r.get("id") or "",
        )

    async def playlist(self, link, limit, user_id,
                       videoid: Union[bool, str] = None):
        """Returns list of video IDs from playlist."""
        if videoid:
            link = self.listbase + link
        link = self._clean(link)
        try:
            data    = await _api_get("/info", {"url": link})
            entries = data.get("data", {}).get("entries", [])
            return [
                e["id"] for e in entries[:limit]
                if isinstance(e, dict) and e.get("id")
            ]
        except Exception:
            return []

    async def related(self, videoid: str,
                      exclude_ids: Union[list, set, None] = None):
        """Returns next recommended track dict or None."""
        exclude = set(exclude_ids or [])
        exclude.add(videoid)
        try:
            info      = await _api_get("/info", {"url": self.base + videoid})
            seed_title = info.get("data", {}).get("title", "")
            if not seed_title:
                return None
            search = await _api_get("/search", {"q": seed_title, "limit": 10})
            for r in search.get("results", []):
                vid = r.get("id")
                dur = r.get("duration")
                if vid and dur and vid not in exclude:
                    return {
                        "title":        r.get("title", "Unknown"),
                        "vidid":        vid,
                        "duration_min": _seconds_to_min(dur),
                        "thumb":        r.get("thumbnail", ""),
                        "link":         r.get("url", self.base + vid),
                    }
        except Exception:
            pass
        return None

    async def video(self, link: str, videoid: Union[bool, str] = None):
        """Returns: (1, file_path) or (0, error_msg)"""
        if videoid:
            link = self.base + link
        link = self._clean(link)
        try:
            data       = await _api_get("/api/stream",
                                        {"url": link, "type": "video"},
                                        timeout=_VTIMEOUT)
            stream_url = data.get("stream_url") or data.get("direct_url")
            if not stream_url:
                return 0, "No stream URL"
            vid_id    = link.split("v=")[-1].split("&")[0] if "v=" in link else link[-11:]
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            file_path = os.path.join(DOWNLOAD_DIR, f"{vid_id}.mp4")
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                return 1, file_path
            if await _fetch_media(stream_url, link, "video", file_path, timeout=_VTIMEOUT,
                                headers=data.get("http_headers")):
                return 1, file_path
            return 0, "Empty file"
        except Exception as e:
            return 0, str(e)

    async def download(
        self,
        link: str,
        mystic,
        video:     Union[bool, str] = None,
        videoid:   Union[bool, str] = None,
        songaudio: Union[bool, str] = None,
        songvideo: Union[bool, str] = None,
        format_id: Union[bool, str] = None,
        title:     Union[bool, str] = None,
    ) -> tuple:
        """Main download — used by /play, /vplay. Returns (path, True) or (None, False)."""
        if videoid:
            link = self.base + link
        link = self._clean(link)
        try:
            media_type = "video" if (video or songvideo) else "audio"
            if format_id:
                data = await _api_get("/ytdl",
                                      {"url": link, "format": str(format_id)},
                                      timeout=_VTIMEOUT)
            else:
                data = await _api_get("/api/stream",
                                      {"url": link, "type": media_type},
                                      timeout=_VTIMEOUT)

            stream_url = (data.get("stream_url")
                          or data.get("direct_url")
                          or data.get("url"))
            if not stream_url:
                return None, False

            vid_id    = link.split("v=")[-1].split("&")[0] if "v=" in link else link[-11:]
            ext       = data.get("ext") or ("mp4" if media_type == "video" else "mp3")
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            file_path = os.path.join(DOWNLOAD_DIR, f"{vid_id}.{ext}")

            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                return file_path, True

            if await _fetch_media(stream_url, link, media_type, file_path, timeout=_VTIMEOUT,
                                  headers=data.get("http_headers")):
                return file_path, True
            return None, False

        except Exception:
            try:
                if 'file_path' in locals():
                    _cleanup(file_path)
            except Exception:
                pass
            return None, False


# ── Singleton ─────────────────────────────────────────────────────────────────
YouTube = YouTubeAPI()
