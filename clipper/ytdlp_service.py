import datetime
import os
import shutil
from pathlib import Path

import yt_dlp


PROJECT_DIR = Path(__file__).resolve().parents[1]
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def find_cookie_file():
    """Cari cookie secara otomatis; frontend tidak perlu mengirim cookie."""
    candidates = [
        os.getenv("YTDLP_COOKIEFILE", "").strip(),
        str(PROJECT_DIR / "cookies.txt"),
        "/home/keiandfay/keibot/cookies.txt",
    ]
    for value in candidates:
        if value and Path(value).is_file() and Path(value).stat().st_size > 0:
            return value
    return None


def find_deno():
    return shutil.which("deno") or "/home/keiandfay/.deno/bin/deno"


def base_options():
    options = {
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "remote_components": {"ejs:github"},
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android_vr"],
            }
        },
    }

    deno = find_deno()
    if os.path.isfile(deno) and os.access(deno, os.X_OK):
        options["js_runtimes"] = {"deno": {"args": deno}}
        print(f"[yt-dlp] Deno aktif: {deno}")

    cookie_file = find_cookie_file()
    if cookie_file:
        options["cookiefile"] = cookie_file
        print(f"[yt-dlp] Cookie aktif: {cookie_file}")
    else:
        print("[yt-dlp] Cookie tidak ditemukan; mencoba tanpa cookie")

    return options


def get_metadata(url):
    options = base_options()
    options.update({"quiet": True, "no_warnings": True, "skip_download": True})

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

        duration = int(info.get("duration") or 0)
        resolutions = sorted({
            f"{fmt['height']}p"
            for fmt in info.get("formats", [])
            if fmt.get("vcodec") != "none" and fmt.get("height")
        }, key=lambda value: int(value[:-1]), reverse=True)

        return {
            "success": True,
            "data": {
                "id": info.get("id"),
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": duration,
                "durationLabel": str(datetime.timedelta(seconds=duration)),
                "channel": info.get("uploader"),
                "availableResolutions": resolutions,
                "hasSubtitles": bool(
                    info.get("subtitles") or info.get("automatic_captions")
                ),
            },
        }
    except Exception as exc:
        return {"success": False, "error": {"message": str(exc)}}


def download_source(url, video_id, resolution="720p"):
    height = int(str(resolution).lower().replace("p", "") or 720)
    output_path = DOWNLOADS_DIR / f"{video_id}_{height}p.mp4"

    if output_path.is_file() and output_path.stat().st_size > 0:
        return str(output_path)
    if output_path.exists():
        output_path.unlink()

    options = base_options()
    options.update({
        "format": (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        ),
        "outtmpl": str(output_path),
        "merge_output_format": "mp4",
        "ffmpeg_location": "/usr/bin/ffmpeg",
    })

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("File hasil download tidak ditemukan")
        return str(output_path)

    except Exception as exc:
        if output_path.exists():
            output_path.unlink()
        text = str(exc)
        if "403" in text or "Forbidden" in text:
            raise RuntimeError(
                "Download ditolak sumber (HTTP 403), meskipun cookie/runtime sudah dicoba. "
                "Jika video publik tetap gagal, IP VPS kemungkinan dibatasi sumber."
            ) from exc
        raise RuntimeError(f"Gagal mengunduh video: {text}") from exc


__all__ = ["get_metadata", "download_source"]
