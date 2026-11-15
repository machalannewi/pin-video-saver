"""Download engine for Stash: saves pictures and videos from Pinterest, Instagram and X/Twitter.

Every download returns a list of saved files, because one post can hold several
pictures or videos. Nothing here logs in: only public posts work.
"""
import glob
import json
import math
import mimetypes
import os
import re
import urllib.error
import urllib.request

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError, sanitize_filename

# Prefer a direct MP4 that has sound (Pinterest's doesn't say, hence "!=?").
# Otherwise FFmpeg joins the best picture and sound tracks into a proper MP4.
BEST_FORMAT = "best[protocol^=http][ext=mp4][acodec!=?none]/bv*+ba/b"
# Instagram's sound labels are unreliable: depending on the request it may leave
# out the separate sound track and mark its plain MP4s as silent even though they
# contain sound. So join picture and sound tracks when both exist, and otherwise
# take a plain MP4 (format ids without "dash"), which keeps any sound it has.
INSTAGRAM_FORMAT = "bv*+ba/b*[format_id!*=dash]/b*"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"

_LINK = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)*(?:pinterest\.[a-z.]{2,8}|pin\.it|instagram\.com|instagr\.am|x\.com|twitter\.com)"
    r"/[^\s\"'<>]+",
    re.IGNORECASE,
)
_SHORT_PIN = re.compile(r"^(?:https?://)?(?:[\w-]+\.)*pin\.it/", re.IGNORECASE)
_PIN_ID = re.compile(r"pinterest\.[a-z.]{2,8}/pin/(?:[^/?#]*--)?(\d+)", re.IGNORECASE)
_INSTAGRAM = re.compile(r"(?:instagram\.com|instagr\.am)/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)", re.IGNORECASE)
_X_POST = re.compile(r"(?:x\.com|twitter\.com)/(?:[\w]+|i(?:/web)?)/status(?:es)?/(\d+)", re.IGNORECASE)
_UNFINISHED = (".part", ".ytdl", ".temp")
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
                "image/heic": ".heic"}


class PinError(Exception):
    """A failure with a message that is safe to show the user as-is."""


class Stopped(Exception):
    """The user pressed Stop."""


class _SilentLogger:
    """yt-dlp must not print: a windowed app has no console to print to."""

    def debug(self, msg):
        pass

    info = warning = error = debug


def find_links(text):
    """Return each distinct supported link in the text, in the order pasted."""
    return list(dict.fromkeys(m.group(0).rstrip(".,;)") for m in _LINK.finditer(text)))


def _ffmpeg_path():
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _friendly(error):
    text = str(error)
    lowered = text.lower()
    if "rate-limit" in lowered or "rate limit" in lowered:
        return "The site is limiting requests: wait a few minutes and try again"
    if "empty media response" in lowered or "login" in lowered or "registered users" in lowered:
        # Instagram answers this way for private accounts and for accounts with an age limit.
        return ("Only visible to logged-in users: the account is private or has an age limit, "
                "so this post can't be saved")
    if "http error 404" in lowered or "unable to extract" in lowered or "not found" in lowered:
        return "Post not found: it may be deleted or private"
    if "http error 429" in lowered:
        return "The site is limiting requests: wait a few minutes and try again"
    if any(s in lowered for s in ("getaddrinfo", "timed out", "connection", "network")):
        return "Couldn't reach the site: check your internet connection"
    if "no space left" in lowered or "permission denied" in lowered:
        return "Couldn't save the file: check the download folder"
    return "Download failed: " + re.sub(r"^ERROR:\s*", "", text)[:160]


def identify(link):
    """Work out which site a link belongs to. Returns (site, post id, page url)."""
    if not re.match(r"https?://", link, re.IGNORECASE):
        link = "https://" + link
    if _SHORT_PIN.match(link):
        request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                link = response.geturl()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise PinError("This short link doesn't lead to a pin") from error
            raise PinError(_friendly(error)) from error
        except OSError as error:
            raise PinError(_friendly(error)) from error

    if match := _PIN_ID.search(link):
        return "pinterest", match.group(1), f"https://www.pinterest.com/pin/{match.group(1)}/"
    if match := _INSTAGRAM.search(link):
        return "instagram", match.group(1), f"https://www.instagram.com/p/{match.group(1)}/"
    if match := _X_POST.search(link):
        return "x", match.group(1), f"https://x.com/i/status/{match.group(1)}"
    if re.search(r"pinterest\.|pin\.it", link, re.IGNORECASE):
        raise PinError("Not a single pin: open the pin and copy its link")
    if re.search(r"instagram\.com|instagr\.am", link, re.IGNORECASE):
        raise PinError("Not a single post: open the post or reel and copy its link")
    raise PinError("Not a single post: open the post on X and copy its link")


def _tag(site, post_id):
    return {"pinterest": "pin", "instagram": "ig", "x": "x"}[site] + "-" + post_id


def _existing_files(tag, folder):
    found = []
    for pattern in (f"*[[]{tag}[]].*", f"*[[]{tag}-[0-9]*[]].*"):
        found += glob.glob(os.path.join(glob.escape(folder), pattern))
    return sorted(p for p in set(found) if not p.endswith(_UNFINISHED))


def _file_title(text, fallback):
    text = re.sub(r"https?://\S+", "", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    title = sanitize_filename(text[:80].strip() or fallback)
    return title.strip(" .") or "Stash"


# ---- plain file downloads (pictures, and X videos) ---------------------------

def _fetch(url, path_without_ext, on_progress, should_stop, referer=None):
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = _IMAGE_TYPES.get(kind) or mimetypes.guess_extension(kind) or os.path.splitext(url.split("?")[0])[1] or ".jpg"
            ext = ".jpg" if ext == ".jpe" else ext
            total = int(response.headers.get("Content-Length") or 0)
            final = path_without_ext + ext
            partial = final + ".part"
            done = 0
            with open(partial, "wb") as out:
                while chunk := response.read(256 * 1024):
                    if should_stop and should_stop():
                        raise Stopped()
                    out.write(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress(min(done / total, 1.0))
            os.replace(partial, final)
            return final
    except (urllib.error.URLError, OSError) as error:
        raise PinError(_friendly(error)) from error


# ---- X / Twitter -------------------------------------------------------------

def _x_token(post_id):
    """The token the public embed widget sends along with a post id."""
    n = (int(post_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole, frac, out = int(n), n - int(n), ""
    while whole:
        out = digits[whole % 36] + out
        whole //= 36
    for _ in range(8):
        frac *= 36
        out += digits[int(frac)]
        frac -= int(frac)
    return re.sub(r"(0+|\.)", "", out or "0")


def _x_items(post_id):
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={post_id}&token={_x_token(post_id)}&lang=en"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as r:
            data = json.loads(r.read().decode("utf8") or "{}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise PinError("Post not found: it may be deleted or private") from error
        raise PinError(_friendly(error)) from error
    except (OSError, ValueError) as error:
        raise PinError(_friendly(error)) from error
    if data.get("__typename") == "TweetTombstone" or not data.get("id_str"):
        raise PinError("Post not found: it may be deleted, private or age-restricted")

    title = _file_title(data.get("text"), "Post by @" + (data.get("user") or {}).get("screen_name", "unknown"))
    items = []
    for media in data.get("mediaDetails") or []:
        if media.get("type") == "photo" and media.get("media_url_https"):
            base = media["media_url_https"]
            stem, ext = os.path.splitext(base)
            items.append(("picture", f"{stem}?format={ext.lstrip('.') or 'jpg'}&name=orig"))
        elif media.get("type") in ("video", "animated_gif"):
            variants = [v for v in (media.get("video_info") or {}).get("variants", [])
                        if v.get("content_type") == "video/mp4" and v.get("url")]
            if variants:
                items.append(("video", max(variants, key=lambda v: v.get("bitrate") or 0)["url"]))
    return title, items


# ---- Pinterest and Instagram, through yt-dlp ----------------------------------

def _ytdlp_options(**extra):
    options = {
        "format": BEST_FORMAT,
        "merge_output_format": "mp4",
        "windowsfilenames": True,
        "noplaylist": False,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "logger": _SilentLogger(),
        "ignore_no_formats_error": True,
    }
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg
    options.update(extra)
    return options


def _ytdlp_post(page_url):
    try:
        with yt_dlp.YoutubeDL(_ytdlp_options()) as ydl:
            info = ydl.extract_info(page_url, download=False, process=False)
    except DownloadError as error:
        raise PinError(_friendly(error)) from error
    entries = [e for e in (info.get("entries") or [info]) if e]
    for entry in entries:  # Entries need these to be processed on their own later.
        for key in ("extractor", "extractor_key", "webpage_url", "original_url"):
            entry.setdefault(key, info.get(key) or page_url)
    if info.get("extractor_key") == "Instagram":
        title = info.get("description") or ""
        fallback = f"Instagram post by {info['channel']}" if info.get("channel") else "Instagram post"
    else:  # Pinterest's description is often its generic page text, so use the pin title.
        title = info.get("title") or ""
        fallback = "Pinterest pin"
    items = []
    for entry in entries:
        if entry.get("formats"):
            items.append(("video", entry))
        else:
            pictures = [t for t in entry.get("thumbnails") or [] if t.get("url")]
            if pictures:
                best = max(enumerate(pictures), key=lambda p: ((p[1].get("width") or 0), p[0]))[1]
                items.append(("picture", best["url"]))
    return _file_title(title if len(title) <= 120 else title[:120], fallback), items


def _ytdlp_video(entry, path_without_ext, on_progress, should_stop):
    def hook(status):
        if should_stop and should_stop():
            raise DownloadCancelled()
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if on_progress and total and status["status"] == "downloading":
            on_progress(min(status.get("downloaded_bytes", 0) / total, 1.0))

    template = path_without_ext.replace("%", "%%") + ".%(ext)s"
    chosen = INSTAGRAM_FORMAT if entry.get("extractor_key") == "Instagram" else BEST_FORMAT
    try:
        with yt_dlp.YoutubeDL(_ytdlp_options(outtmpl=template, progress_hooks=[hook], noplaylist=True,
                                             format=chosen)) as ydl:
            done = ydl.process_ie_result(dict(entry), download=True)
            return done["requested_downloads"][0]["filepath"]
    except DownloadCancelled as error:
        raise Stopped() from error
    except (DownloadError, OSError, KeyError) as error:
        raise PinError(_friendly(error)) from error


# ---- the one entry point -------------------------------------------------------

def download_post(link, folder, on_progress=None, should_stop=None):
    """Save every picture and video in a post into folder.

    Returns (list of saved paths, already_had_it). on_progress receives a fraction
    from 0 to 1 for the whole post. Raises PinError for failures to show the user,
    and Stopped if should_stop() turns true.
    """
    site, post_id, page_url = identify(link)
    tag = _tag(site, post_id)
    existing = _existing_files(tag, folder)
    if existing:
        return existing, True

    title, items = _x_items(post_id) if site == "x" else _ytdlp_post(page_url)
    if not items:
        raise PinError("This post has no pictures or videos to save")

    os.makedirs(folder, exist_ok=True)
    saved = []
    for index, (kind, source) in enumerate(items):
        if should_stop and should_stop():
            raise Stopped()
        suffix = f"-{index + 1}" if len(items) > 1 else ""
        target = os.path.join(folder, f"{title} [{tag}{suffix}]")

        def progress(fraction, index=index):
            if on_progress:
                on_progress((index + fraction) / len(items))

        if kind == "video" and isinstance(source, dict):
            saved.append(_ytdlp_video(source, target, progress, should_stop))
        else:
            referer = "https://www.instagram.com/" if site == "instagram" else None
            saved.append(_fetch(source, target, progress, should_stop, referer))
    if on_progress:
        on_progress(1.0)
    return saved, False
