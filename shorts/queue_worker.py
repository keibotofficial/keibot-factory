"""
shorts/queue_worker.py

Worker background untuk modul Shorts — Sinkron dengan Folder Audio Baru
(audio_non_cid / audio_cid), Background Shorts (shorts_bg / auto_crop),
Custom Thumbnail Shorts 9:16 (shorts_thumb), Metadata YouTube, Jadwal Publish,
dan Global Task Monitor.
"""

import os
import re
import json
import random
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.resource_gate import acquire_resource, release_resource
from core.json_store import lock_for, read_json, write_json, update_json
from core.job_state import JobCancelled, cancelled, terminal, channel_id
from core.jobs import cancel_jobs_in, check_job, update_job_in, recover_jobs
from .render_engine import render_job

DB_PATH = os.path.join(os.path.dirname(__file__), 'shorts_db.json')
CHANNELS_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'channels_db.json')
UPLOADS_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'shorts_output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

_db_lock = lock_for(DB_PATH)


def read_db():
    data = read_json(DB_PATH, [])
    if not isinstance(data, list):
        raise ValueError('Format database job tidak valid.')
    return data


def write_db(data):
    write_json(DB_PATH, data)


def update_db(mutate):
    return update_json(DB_PATH, [], mutate)


def cancel_jobs(job_id=None, yt_id=None):
    return cancel_jobs_in(DB_PATH, job_id=job_id, yt_id=yt_id)


def check_cancelled(job_id):
    try:
        check_job(DB_PATH, job_id)
        return False
    except JobCancelled:
        return True


def ensure_active(job_id):
    return check_job(DB_PATH, job_id)


def update_job(job_id, **fields):
    return update_job_in(DB_PATH, job_id, fields)


def _pick_random_asset(channel_id, category):
    """Mengambil 1 file acak dari folder kategori di uploads/<channel_id>/<category>."""
    folder = os.path.join(UPLOADS_ROOT, str(channel_id), category)
    if not os.path.isdir(folder):
        return None
    valid_exts = ('.mp3', '.wav', '.mp4', '.mov', '.webm', '.mkv', '.jpg', '.jpeg', '.png', '.webp', '.gif')
    files = [
        f for f in os.listdir(folder)
        if not f.startswith('.') and f.lower().endswith(valid_exts) and not f.endswith('_compressed.jpg')
    ]
    if not files:
        return None
    return os.path.join(folder, random.choice(files))


def _pick_shorts_background(channel_id, bg_source='shorts_bg'):
    """Otomatis ambil dari folder BG Shorts (shorts_bg) milik channel yang dipilih."""
    if bg_source == 'auto_crop':
        return _pick_random_asset(channel_id, 'backgrounds') or _pick_random_asset(channel_id, 'shorts_bg')
    return _pick_random_asset(channel_id, 'shorts_bg') or _pick_random_asset(channel_id, 'backgrounds')


def _pick_shorts_audio(channel_id, audio_source='non_cid'):
    """Pilih audio dari audio_non_cid, audio_cid, atau campuran keduanya."""
    if audio_source == 'cid':
        return _pick_random_asset(channel_id, 'audio_cid') or _pick_random_asset(channel_id, 'audio_non_cid')
    elif audio_source == 'mix':
        candidates = []
        p1 = _pick_random_asset(channel_id, 'audio_non_cid')
        p2 = _pick_random_asset(channel_id, 'audio_cid')
        if p1: candidates.append(p1)
        if p2: candidates.append(p2)
        return random.choice(candidates) if candidates else None
    else:
        return _pick_random_asset(channel_id, 'audio_non_cid') or _pick_random_asset(channel_id, 'audio_cid')


def _pick_and_validate_shorts_thumbnail(channel_id):
    """Mengambil Custom Thumbnail Shorts (9:16) dari folder shorts_thumb & kompres otomatis jika >2MB."""
    thumb_path = _pick_random_asset(channel_id, 'shorts_thumb')
    if not thumb_path:
        return None
    try:
        from PIL import Image as PILImage
        img = PILImage.open(thumb_path).convert('RGB')
        max_bytes = 2097152  # 2MB limit YouTube API
        if os.path.getsize(thumb_path) > max_bytes or thumb_path.lower().endswith('.webp'):
            scale = min(1.0, 1080 / img.width, 1920 / img.height)
            if scale < 1.0:
                img = img.resize((max(720, int(img.width * scale)), max(1280, int(img.height * scale))), PILImage.LANCZOS)
            base, _ = os.path.splitext(thumb_path)
            tmp_path = base + '_shorts_yt_compressed.jpg'
            quality = 90
            while quality >= 45:
                img.save(tmp_path, 'JPEG', quality=quality, optimize=True)
                if os.path.getsize(tmp_path) <= max_bytes:
                    break
                quality -= 8
            return tmp_path
    except Exception as e:
        print(f"⚠️ [Shorts Worker] Thumbnail compress warning: {e}")
    return thumb_path


def _get_channel(yt_id):
    if not os.path.exists(CHANNELS_DB):
        return None
    try:
        with open(CHANNELS_DB, 'r', encoding='utf-8') as f:
            channels = json.load(f)
        return next((c for c in channels if c.get('yt_id') == yt_id), None)
    except Exception:
        return None


# ============================================================
# UPLOAD PER-PLATFORM (LENGKAP METADATA, JADWAL PUBLISH & THUMBNAIL)
# ============================================================
def _upload_youtube(video_path, channel_id, job_title=None, job_desc=None, job_tags=None,
                    job_privacy='public', publish_date_str=None, use_shorts_thumb=True, **kwargs):
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.http import MediaFileUpload

    channel = _get_channel(channel_id)
    if not channel:
        raise Exception("Channel YouTube tidak ditemukan di channels_db.json")

    creds_list = channel.get('creds_list', [channel.get('creds_json')])
    last_err = "Token API Kosong"

    # Bersihkan & siapkan Tags (maks 400 karakter agar aman di YouTube API)
    raw_tags = str(job_tags or 'Shorts, YouTubeShorts, Music')
    clean_tags = raw_tags.replace('#', '').replace('<', '').replace('>', '').replace('"', '')
    temp_tags = [t.strip() for t in clean_tags.split(',') if t.strip()]
    tags_list = []
    char_count = 0
    for t in temp_tags:
        if char_count + len(t) <= 400:
            tags_list.append(t)
            char_count += len(t) + 1
    if not tags_list:
        tags_list = ['Shorts', 'Music']

    title = (job_title or "Musik Shorts Terbaik #Shorts").strip()
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"

    description = (job_desc or f"{title}\n\n#Shorts #Music #ViralShorts").strip()
    privacy_status = job_privacy if job_privacy in ('public', 'unlisted', 'private') else 'public'

    for cred_str in creds_list:
        if not cred_str:
            continue
        try:
            creds = Credentials.from_authorized_user_info(json.loads(cred_str))
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())

            youtube = build('youtube', 'v3', credentials=creds)

            body = {
                'snippet': {
                    'title': title[:100],
                    'description': description,
                    'tags': tags_list,
                    'categoryId': '10'
                },
                'status': {
                    'privacyStatus': privacy_status,
                    'selfDeclaredMadeForKids': False
                },
            }

            # 🔥 JADWAL PUBLISH OTOMATIS (Jika waktu tayang di masa depan, set private + publishAt UTC)
            if publish_date_str:
                try:
                    clean_dt_str = str(publish_date_str).strip().replace('T', ' ')[:16]
                    sch_obj = datetime.strptime(clean_dt_str, "%Y-%m-%d %H:%M")
                    if sch_obj.replace(tzinfo=ZoneInfo("Asia/Jakarta")) > datetime.now(ZoneInfo("Asia/Jakarta")):
                        wib = ZoneInfo("Asia/Jakarta")
                        sch_aware = sch_obj.replace(tzinfo=wib)
                        sch_utc = sch_aware.astimezone(timezone.utc)
                        body['status']['publishAt'] = sch_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                        body['status']['privacyStatus'] = 'private'
                except Exception as dt_err:
                    print(f"⚠️ [Shorts Worker] Format publish_date dilewati ({publish_date_str}): {dt_err}")

            media = MediaFileUpload(video_path, chunksize=1024 * 1024 * 5, resumable=True)
            req = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

            response = None
            while response is None:
                if kwargs.get('cancel_callback', lambda: False)():
                    raise JobCancelled('Dibatalkan')
                status, response = req.next_chunk()

            video_id = response.get('id')

            # Pasang Custom Thumbnail Shorts (9:16) jika tersedia di folder shorts_thumb
            if use_shorts_thumb and video_id:
                thumb_path = _pick_and_validate_shorts_thumbnail(channel_id)
                if thumb_path and os.path.exists(thumb_path):
                    try:
                        youtube.thumbnails().set(
                            videoId=video_id,
                            media_body=MediaFileUpload(thumb_path)
                        ).execute()
                    except Exception as th_e:
                        print(f"⚠️ [Shorts Worker] Gagal pasang custom thumbnail Shorts: {th_e}")

            return video_id
        except JobCancelled:
            raise
        except Exception as e:
            last_err = str(e)
            continue

    raise Exception(last_err)





def _upload_facebook(video_path, channel_id, job_title=None, job_desc=None, publish_date_str=None, **kwargs):
    """
    Mengunggah video 9:16 ke Facebook Page Reels menggunakan Meta Graph API v19.0 (3-Step Reels Publishing).
    Mendukung penjadwalan otomatis (scheduled_publish_time) jika jadwal > 15 menit ke depan.
    """
    import requests
    channel = _get_channel(channel_id)
    if not channel:
        raise Exception("Channel tidak ditemukan di database")

    chosen_page = kwargs.get('fb_page_id')
    if chosen_page:
        pages_path = os.path.join(os.path.dirname(CHANNELS_DB), 'facebook_pages.json')
        pages = read_json(pages_path, [])
        page = next((p for p in pages if str(p.get('page_id')) == str(chosen_page)), None)
        if not page:
            raise ValueError('Facebook Page yang dipilih tidak ditemukan.')
        page_id = str(page['page_id'])
        page_token = page.get('access_token', '').strip()
    else:
        fb_creds = channel.get('facebook_creds', {})
        page_id = fb_creds.get('page_id', '').strip()
        page_token = fb_creds.get('page_access_token', '').strip()

    if not page_id or not page_token:
        raise Exception("Page ID atau Page Access Token FB belum diatur di menu Settings -> Facebook.")

    file_size = os.path.getsize(video_path)
    caption = (job_title or "Reels Musik Terbaik").strip()
    if job_desc:
        caption = f"{caption}\n\n{job_desc.strip()}"

    # 1. Tahap START: Minta video_id & upload_url
    start_url = f"https://graph.facebook.com/v19.0/{page_id}/video_reels"
    start_res = requests.post(start_url, data={
        "upload_phase": "start",
        "access_token": page_token
    }, timeout=30).json()

    if "error" in start_res:
        raise Exception(f"FB Start Error: {start_res['error'].get('message')}")

    video_id = start_res.get("video_id")
    upload_url = start_res.get("upload_url")

    # 2. Tahap TRANSFER: Kirim binary MP4 ke rupload.facebook.com
    with open(video_path, "rb") as vf:
        video_bytes = vf.read()

    up_headers = {
        "Authorization": f"OAuth {page_token}",
        "offset": "0",
        "file_size": str(file_size)
    }
    up_res = requests.post(upload_url, headers=up_headers, data=video_bytes, timeout=300).json()
    if "error" in up_res:
        raise Exception(f"FB Upload Error: {up_res['error'].get('message')}")

    # 3. Tahap FINISH: Publish atau Jadwalkan Reels
    finish_params = {
        "access_token": page_token,
        "video_id": video_id,
        "upload_phase": "finish",
        "video_state": "PUBLISHED",
        "description": caption
    }

    # Cek apakah jadwal publish berada >15 menit di masa depan (Syarat Scheduled FB Reels)
    if publish_date_str:
        try:
            clean_dt_str = str(publish_date_str).strip().replace('T', ' ')[:16]
            sch_obj = datetime.strptime(clean_dt_str, "%Y-%m-%d %H:%M")
            wib = ZoneInfo("Asia/Jakarta")
            sch_aware = sch_obj.replace(tzinfo=wib)
            now_aware = datetime.now(wib)
            if (sch_aware - now_aware).total_seconds() >= 900:  # Minimal 15 menit ke depan
                finish_params["video_state"] = "SCHEDULED"
                finish_params["scheduled_publish_time"] = int(sch_aware.timestamp())
        except Exception:
            pass

    fin_res = requests.post(start_url, data=finish_params, timeout=60).json()
    if "error" in fin_res:
        raise Exception(f"FB Finish Error: {fin_res['error'].get('message')}")

    return video_id


_UPLOADERS = {
    'youtube': _upload_youtube,
    'facebook': _upload_facebook,
}


# ============================================================
# WORKER LOOP
# ============================================================
def shorts_worker_loop():
    recover_jobs(DB_PATH)
    print("🚀 [Shorts Worker] Background thread v2.0 dimulai...")
    while True:
        try:
            jobs = read_db()
            job = next((
                j for j in jobs
                if j.get('status') in ('queued', 'Menunggu Giliran (Queue) ⏳')
                or 'Queue' in str(j.get('status', ''))
            ), None)

            if job:
                job_id = job['id']
                opts = job.get('options', {})
                channel_id = opts.get('channelId') or job.get('yt_id')
                platforms = opts.get('platforms', ['youtube'])
                effects_config = opts.get('effects', [])
                vis_preset = opts.get('vis_preset', {})
                duration_sec = int(opts.get('durationSec', 30))
                bg_source = opts.get('bgSource', 'shorts_bg')
                audio_source = opts.get('audioSource', 'non_cid')
                use_shorts_thumb = opts.get('useShortsThumb', True)

                title_mode = opts.get('title_mode', 'title_bank')
                job_title = opts.get('title') or job.get('title', 'Shorts Video #Shorts')
                job_desc = opts.get('description', '')
                job_tags = opts.get('tags', 'shorts, music')
                job_privacy = opts.get('privacy', 'public')
                publish_date_str = opts.get('publish_date') or job.get('publish_date', '')

                update_job(job_id, status='Menunggu Resource CPU... ⏳', progress=2, stage='Menunggu slot CPU/RAM...')
                acquire_resource(worker_name="Shorts Worker", cancelled=lambda: check_cancelled(job_id))

                try:
                    bg_path = opts.get('bgPath') or _pick_shorts_background(channel_id, bg_source)
                    audio_path = opts.get('audioPath') or _pick_shorts_audio(channel_id, audio_source)

                    if not bg_path:
                        raise Exception("Background kosong! Upload dulu ke folder BG Shorts (9:16) di Settings.")
                    if not audio_path:
                        raise Exception("Audio kosong! Upload MP3 dulu ke folder Audio Non-CID / CID di Settings.")

                    # Bersihkan judul lagu untuk Floating Card (& untuk judul YT jika mode == 'audio_name')
                    raw_song = os.path.splitext(os.path.basename(audio_path))[0]
                    clean_song = re.sub(r'\[.*?\]|\(.*?\)', '', raw_song).strip()
                    clean_song = re.sub(r'^\d+[\.\)\-\s_]+', '', clean_song).strip(' -_') or raw_song

                    if title_mode == 'audio_name':
                        job_title = f"🎵 {clean_song} #Shorts"
                        update_job(job_id, title=job_title)

                    channel_info = _get_channel(channel_id) or {}
                    vis_cfg = {
                        "yt_id": channel_id,
                        "channel_thumbnail": channel_info.get('thumbnail', ''),
                        "use_floating_card": vis_preset.get('use_floating_card', True),
                        "floating_card_mode": vis_preset.get('floating_card_mode', 'intro_5s'),
                        "song_title": clean_song,
                        "layers": vis_preset.get('layers', effects_config),
                    }

                    output_name = f"shorts_{job_id}.mp4"
                    output_path = os.path.join(OUTPUT_DIR, output_name)

                    def _progress(stage, pct):
                        update_job(job_id, status=f"Rendering Shorts... {pct}% ⚡", stage=stage, progress=pct)

                    render_job(
                        bg_path=bg_path,
                        audio_path=audio_path,
                        output_path=output_path,
                        effects_config=effects_config,
                        vis_cfg=vis_cfg,
                        duration_range=(duration_sec, duration_sec),
                        cancel_callback=lambda: check_cancelled(job_id),
                        progress_callback=_progress
                    )

                    # Upload ke tiap platform yang dicentang
                    results = {}
                    errors = []
                    for platform in platforms:
                        ensure_active(job_id)
                        uploader = _UPLOADERS.get(platform)
                        if not uploader:
                            errors.append(f"{platform}: platform tidak dikenal")
                            continue
                        try:
                            update_job(job_id, status=f"Mengunggah ke {platform.upper()}... 🚀", stage=f'Upload ke {platform}...', progress=95)
                            result_id = uploader(
                                output_path,
                                channel_id,
                                job_title=job_title,
                                job_desc=job_desc,
                                job_tags=job_tags,
                                job_privacy=job_privacy,
                                publish_date_str=publish_date_str,
                                use_shorts_thumb=use_shorts_thumb,
                                fb_page_id=opts.get('fb_page_id'),
                                cancel_callback=lambda: check_cancelled(job_id),
                                
                            )
                            results[platform] = result_id
                        except NotImplementedError as e:
                            errors.append(f"{platform}: {str(e)}")
                        except Exception as e:
                            errors.append(f"{platform}: gagal upload — {str(e)}")

                    if 'youtube' in results:
                        vid_id = results['youtube']
                        final_status = f"Tayang! ✅ <a href='https://youtube.com/shorts/{vid_id}' target='_blank'>[Lihat Shorts]</a>"
                    elif results:
                        final_status = "Selesai! ✅" if not errors else "Selesai Sebagian ⚠️"
                    else:
                        final_status = f"Gagal ❌ ({'; '.join(errors)[:80]})"

                    update_job(
                        job_id,
                        status=final_status,
                        progress=100,
                        stage=f"Selesai (Jadwal: {publish_date_str})" if not errors else '; '.join(errors),
                        output_file=output_name,
                        upload_results=results,
                    )
                    print(f"✅ [Shorts Worker] Job {job_id} selesai.")

                except Exception as e:
                    print(f"❌ [Shorts Worker] Error job {job_id}: {e}")
                    update_job(job_id, status=f"Gagal ❌ ({str(e)[:70]})", stage=f'Error: {str(e)}')

                finally:
                    release_resource(worker_name="Shorts Worker")

        except Exception as main_e:
            print(f"⚠️ [Shorts Worker] Fatal error: {main_e}")

        time.sleep(2)


if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    threading.Thread(target=shorts_worker_loop, daemon=True).start()