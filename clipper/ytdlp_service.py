import datetime
import os
import subprocess
from pathlib import Path
from core.media import executable, validate_video

try:
    from pytubefix import YouTube
except ImportError:
    YouTube = None

PROJECT_DIR = Path(__file__).resolve().parents[1]
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

def _cookie_options():
    cookie_path = PROJECT_DIR / 'cookies.txt'
    if cookie_path.is_file() and cookie_path.stat().st_size:
        return {'cookiefile': str(cookie_path), 'quiet': True, 'no_warnings': True,
                'noplaylist': True, 'socket_timeout': 30}
    return None


def get_metadata(url):
    try:
        cookie_options = _cookie_options()
        if cookie_options:
            from yt_dlp import YoutubeDL
            with YoutubeDL(cookie_options) as downloader:
                info = downloader.extract_info(url, download=False)
            duration = info.get('duration') or 0
            resolutions = sorted({int(f['height']) for f in info.get('formats', []) if f.get('height')}, reverse=True)
            return {'success': True, 'data': {
                'id': info['id'], 'title': info.get('title', ''), 'thumbnail': info.get('thumbnail', ''),
                'duration': duration, 'durationLabel': str(datetime.timedelta(seconds=duration)),
                'channel': info.get('uploader', ''), 'availableResolutions': [f'{h}p' for h in resolutions],
                'hasSubtitles': bool(info.get('subtitles') or info.get('automatic_captions'))}}
        # Menggunakan client WEB yang lebih kebal terhadap blokir bot
        yt = YouTube(url, client='WEB')
        duration = yt.length

        # Ambil daftar resolusi yang tersedia dan urutkan dari yang terbesar
        resolutions = []
        for stream in yt.streams.filter(type="video"):
            if stream.resolution and stream.resolution not in resolutions:
                resolutions.append(stream.resolution)
        resolutions.sort(key=lambda x: int(x[:-1]) if x[:-1].isdigit() else 0, reverse=True)

        return {
            "success": True,
            "data": {
                "id": yt.video_id,
                "title": yt.title,
                "thumbnail": yt.thumbnail_url,
                "duration": duration,
                "durationLabel": str(datetime.timedelta(seconds=duration)),
                "channel": yt.author,
                "availableResolutions": resolutions,
                "hasSubtitles": len(yt.captions) > 0,
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

    try:
        cookie_options = _cookie_options()
        if cookie_options:
            from yt_dlp import YoutubeDL
            cookie_options.update({'format': f'bestvideo[height<={height}]+bestaudio/best[height<={height}]',
                                   'outtmpl': str(output_path), 'merge_output_format': 'mp4',
                                   'ffmpeg_location': executable('ffmpeg')})
            with YoutubeDL(cookie_options) as downloader:
                downloader.download([url])
            validate_video(output_path)
            return str(output_path)
        yt = YouTube(url, client='WEB')

        # Cari stream video dengan resolusi yang diminta, atau fallback ke resolusi tertinggi
        video_stream = yt.streams.filter(type="video", res=f"{height}p").first()
        if not video_stream:
            video_stream = yt.streams.filter(type="video").order_by('resolution').desc().first()

        # Ambil stream audio kualitas terbaik
        audio_stream = yt.streams.get_audio_only()

        if not video_stream or not audio_stream:
            raise RuntimeError("Tidak dapat menemukan stream video/audio yang valid.")

        v_path = DOWNLOADS_DIR / f"{video_id}_v.mp4"
        a_path = DOWNLOADS_DIR / f"{video_id}_a.mp4"

        # Download video dan audio secara terpisah (metode paling stabil di pytubefix)
        video_stream.download(output_path=str(DOWNLOADS_DIR), filename=f"{video_id}_v.mp4")
        audio_stream.download(output_path=str(DOWNLOADS_DIR), filename=f"{video_id}_a.mp4")

        # Gabungkan video dan audio kembali menjadi satu menggunakan FFmpeg bawaan sistem
        ffmpeg_path = executable('ffmpeg')
        subprocess.run([
            ffmpeg_path, "-y",
            "-i", str(v_path),
            "-i", str(a_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            str(output_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Bersihkan file sampah hasil pecahan
        if v_path.exists(): v_path.unlink()
        if a_path.exists(): a_path.unlink()

        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("File hasil merge FFmpeg tidak ditemukan")
            
        return str(output_path)

    except Exception as exc:
        if output_path.exists():
            output_path.unlink()
        raise RuntimeError(f"Gagal mengunduh video dengan pytubefix: {str(exc)}") from exc

__all__ = ["get_metadata", "download_source"]