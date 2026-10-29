"""
live/queue_worker.py

Mandor Jadwal Live Streaming 24/7:
- Membiarkan server bebas (tanpa lock) selama menunggu jadwal.
- Mengaktifkan Priority-Lock tepat H-15 menit sebelum jadwal mulai.
- Mengambil Stream Key & menyetel Thumbnail via YouTube API.
- Memutar Base Video secara looping (-stream_loop -1) dengan CPU ~2%.
- Memantau statistik Viewers & Likes dan menghentikan siaran otomatis saat durasi habis.
"""

import os
import json
import time
import random
import threading
import subprocess
import copy
from core.json_store import merge_snapshot, update_json
from core.media import validate_video
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload

from core.resource_gate import set_live_active
from live.routes import read_db, write_db, _live_db_lock, DB_PATH

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')
CHANNELS_DB = os.path.join(BASE_DIR, 'channels_db.json')

# Menyimpan proses FFmpeg per jadwal: { live_id: subprocess.Popen }
ffmpeg_processes = {}
_last_stats_poll = {}

# ==========================================
# 🔴 GOOGLE YOUTUBE LIVE API HELPERS
# ==========================================
def get_fresh_credentials(yt_id):
    if not os.path.exists(CHANNELS_DB):
        return None
    try:
        with open(CHANNELS_DB, 'r', encoding='utf-8') as f:
            channels = json.load(f)
        channel = next((c for c in channels if c.get('yt_id') == yt_id), None)
        if not channel:
            return None
        creds_list = channel.get('creds_list', [channel.get('creds_json')])
        for cred_str in creds_list:
            if not cred_str:
                continue
            try:
                creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                return creds
            except Exception:
                continue
    except Exception:
        pass
    return None

def resolve_thumbnail_path(yt_id, thumb_name=""):
    thumb_dir = os.path.join(UPLOADS_DIR, yt_id, 'thumbnails')
    if not os.path.exists(thumb_dir):
        return None
    if thumb_name:
        specific = os.path.join(thumb_dir, thumb_name)
        if os.path.exists(specific):
            return specific
    files = [os.path.join(thumb_dir, f) for f in os.listdir(thumb_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
    return random.choice(files) if files else None

def start_youtube_broadcast(item):
    yt_id = item['yt_id']
    title = item.get('title', '🔴 Live Streaming')
    desc = item.get('desc', '')
    privacy = item.get('privacy', 'public')
    start_time_str = item['start_time']
    thumb_name = item.get('thumb', '')

    creds = get_fresh_credentials(yt_id)
    if not creds:
        raise Exception("Token OAuth Channel tidak ditemukan atau kedaluwarsa!")
        
    youtube = build('youtube', 'v3', credentials=creds)
    
    # Konversi Waktu Lokal (WIB) ke UTC sesuai syarat YouTube API
    local_dt = datetime.strptime(start_time_str[:16], "%Y-%m-%dT%H:%M")
    local_dt = local_dt.replace(tzinfo=ZoneInfo("Asia/Jakarta"))
    utc_dt = local_dt.astimezone(timezone.utc)
    # Jika waktu mulai sedikit terlewat saat setup API, majukan 1 menit dari sekarang agar tidak ditolak YT
    now_utc = datetime.now(timezone.utc)
    if utc_dt < now_utc:
        utc_dt = now_utc + timedelta(minutes=1)
    iso_start = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    print(f"📡 [Live API] Membuat Stream Key RTMP untuk '{title}'...")
    stream_res = youtube.liveStreams().insert(
        part="snippet,cdn",
        body={
            "snippet": {"title": f"KeiBot Stream: {title[:50]}"},
            "cdn": {
                "frameRate": "30fps", 
                "ingestionType": "rtmp", 
                "resolution": "720p"
            }
        }
    ).execute()
    
    stream_id = stream_res['id']
    stream_key = stream_res['cdn']['ingestionInfo']['streamName']
    
    print(f"📡 [Live API] Membuat Ruang Live Broadcast...")
    broadcast_res = youtube.liveBroadcasts().insert(
        part="snippet,status,contentDetails",
        body={
            "snippet": {
                "title": title, 
                "description": desc, 
                "scheduledStartTime": iso_start
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False
            },
            "contentDetails": {
                "enableAutoStart": True,
                "enableAutoStop": True,
                "monitorStream": {"enableMonitorStream": False}
            }
        }
    ).execute()
    
    broadcast_id = broadcast_res['id']
    
    # Pasang Thumbnail jika tersedia
    thumb_path = resolve_thumbnail_path(yt_id, thumb_name)
    if thumb_path and os.path.exists(thumb_path):
        try:
            youtube.thumbnails().set(
                videoId=broadcast_id, 
                media_body=MediaFileUpload(thumb_path)
            ).execute()
        except Exception as te:
            print(f"⚠️ [Live API] Info Thumbnail: {te}")
    
    print(f"📡 [Live API] Mengikat Broadcast ID {broadcast_id} ke Stream ID {stream_id}...")
    youtube.liveBroadcasts().bind(
        id=broadcast_id, 
        part="id,contentDetails", 
        streamId=stream_id
    ).execute()
    
    return stream_key, broadcast_id

def stop_youtube_broadcast(yt_id, broadcast_id):
    if not broadcast_id:
        return
    try:
        creds = get_fresh_credentials(yt_id)
        if not creds:
            return
        youtube = build('youtube', 'v3', credentials=creds)
        youtube.liveBroadcasts().transition(
            broadcastStatus="complete", 
            id=broadcast_id, 
            part="id,status"
        ).execute()
    except Exception as e:
        print(f"ℹ️ [Live API] Broadcast {broadcast_id} selesai/ditutup: {e}")

def fetch_live_metrics(yt_id, broadcast_id):
    """Mengambil jumlah Viewers aktif dan Likes dari YouTube."""
    if not broadcast_id:
        return None, None
    try:
        creds = get_fresh_credentials(yt_id)
        if not creds:
            return None, None
        youtube = build('youtube', 'v3', credentials=creds)
        res = youtube.videos().list(
            part="liveStreamingDetails,statistics", 
            id=broadcast_id
        ).execute()
        items = res.get('items', [])
        if items:
            v = items[0]
            viewers = int(v.get('liveStreamingDetails', {}).get('concurrentViewers', 0))
            likes = int(v.get('statistics', {}).get('likeCount', 0))
            return viewers, likes
    except Exception:
        pass
    return None, None

def sync_priority_lock(items):
    """
    Menyalakan Priority-Lock HANYA jika ada jadwal yang sudah masuk H-15 menit
    (WAITING_TO_START) atau sedang mengudara (LIVE).
    Jika jadwal masih jauh, gembok dibuka agar Factory/Shorts tetap jalan!
    """
    should_lock = any(i.get('status') in ('WAITING_TO_START', 'LIVE') for i in items)
    set_live_active(should_lock)

# ==========================================
# 🚀 MAIN LOOP WORKER 24/7
# ==========================================
def recover_live_state():
    with _live_db_lock:
        items = read_db()
        for item in items:
            if item.get('status') == 'LIVE':
                item['status'] = 'WAITING_TO_START' if item.get('stream_key') else 'ERROR'
                item['message'] = 'Memulihkan pengirim Live setelah restart.'
        write_db(items)


def live_worker_loop():
    recover_live_state()
    print("🚀 [Live Worker] Mandor Live Streaming Multi-Channel aktif...")
    
    while True:
        try:
            items = read_db()
            snapshot = copy.deepcopy(items)
            def save_items():
                nonlocal snapshot
                current = merge_snapshot(DB_PATH, snapshot, items)
                snapshot = copy.deepcopy(items)
                return current
            if not items:
                set_live_active(False)
                time.sleep(5)
                continue

            now = datetime.now(ZoneInfo("Asia/Jakarta"))
            db_changed = False

            for item in items:
                lid = item.get('id')
                status = item.get('status')
                bpath = item.get('base_video_path', '')

                # 1. HANDLE PERINTAH BATAL / FORCE STOP
                if status == 'FORCE_STOP':
                    print(f"🛑 [Live Worker] Menghentikan jadwal #{lid}...")
                    proc = ffmpeg_processes.pop(lid, None)
                    if proc:
                        try:
                            proc.terminate()
                            proc.kill()
                        except Exception:
                            pass
                    stop_youtube_broadcast(item.get('yt_id'), item.get('broadcast_id'))
                    item['status'] = 'STOPPED'
                    item['message'] = 'Siaran telah dihentikan oleh pengguna.'
                    db_changed = True
                    continue

                # 2. CEK APAKAH BASE VIDEO SUDAH SELESAI DIRENDER OLEH FACTORY
                if status in ('QUEUED_RENDER', 'RENDERING_BASE', 'SCHEDULED'):
                    if bpath and os.path.isfile(bpath) and not os.path.exists(bpath + '.rendering.mp4'):
                        validate_video(bpath)
                        item['status'] = 'READY_TO_LOCK'
                        item['message'] = 'Base Video siap! Server bebas (Menunggu H-15 menit sebelum tayang).'
                        db_changed = True
                        continue

                # 3. JIKA BASE VIDEO SUDAH SIAP -> PANTAU H-15 MENIT UNTUK KUNCI SERVER
                if item.get('status') == 'READY_TO_LOCK':
                    start_dt = datetime.strptime(item['start_time'][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("Asia/Jakarta"))
                    if now >= start_dt - timedelta(minutes=15):
                        print(f"🔐 [Live Worker] H-15 Menit untuk '{item.get('title')}'. Mengunci Resource & Setup API...")
                        item['status'] = 'WAITING_TO_START'
                        item['message'] = '🔐 H-15 Menit: Priority-Lock aktif! Menyiapkan Stream Key YouTube...'
                        save_items()
                        sync_priority_lock(items)

                        try:
                            s_key, b_id = start_youtube_broadcast(item)
                            item['stream_key'] = s_key
                            item['broadcast_id'] = b_id
                            item['message'] = '🔐 Siap Mengudara! Menunggu detik jam tayang...'
                        except Exception as api_err:
                            item['status'] = 'ERROR'
                            item['message'] = f'Gagal Setup API YouTube: {str(api_err)[:100]}'
                        db_changed = True

                # 4. MULAI STREAMING FFMPEG SAAT JAM TAYANG TIBA
                elif item.get('status') == 'WAITING_TO_START':
                    start_dt = datetime.strptime(item['start_time'][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("Asia/Jakarta"))
                    if now >= start_dt:
                        end_dt = start_dt + timedelta(hours=float(item.get('duration_hours', 4)))
                        if now >= end_dt:
                            stop_youtube_broadcast(item.get('yt_id'), item.get('broadcast_id'))
                            item['status'] = 'FINISHED'
                            item['message'] = 'Durasi jadwal telah berakhir.'
                            db_changed = True
                            continue
                        current_item = next((i for i in read_db() if i.get('id') == lid), None)
                        if not current_item or current_item.get('status') == 'FORCE_STOP':
                            continue
                        if not bpath or not os.path.exists(bpath):
                            item['status'] = 'ERROR'
                            item['message'] = 'File Base Video tidak ditemukan saat jam tayang tiba!'
                            db_changed = True
                            continue

                        s_key = item.get('stream_key')
                        if not s_key:
                            item['status'] = 'ERROR'
                            item['message'] = 'Stream Key kosong!'
                            db_changed = True
                            continue

                        print(f"📡 [Live Worker] ON AIR! Memulai FFmpeg Loop untuk #{lid}...")
                        cmd = [
                            "ffmpeg", "-re", "-stream_loop", "-1",
                            "-i", bpath,
                            "-c", "copy", "-f", "flv",
                            f"rtmp://a.rtmp.youtube.com/live2/{s_key}"
                        ]
                        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        ffmpeg_processes[lid] = proc

                        item['status'] = 'LIVE'
                        item['started_real_at'] = time.time()
                        item['message'] = '🔴 ON AIR — Looping Base Video berjalan stabil (CPU ~2%).'
                        db_changed = True

                # 5. PANTAU DURASI & STATISTIK SAAT SEDANG LIVE
                elif item.get('status') == 'LIVE':
                    start_dt = datetime.strptime(item['start_time'][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("Asia/Jakarta"))
                    end_dt = start_dt + timedelta(hours=float(item.get('duration_hours', 4)))

                    # Auto-restart FFmpeg jika tiba-tiba putus padahal durasi belum habis
                    proc = ffmpeg_processes.get(lid)
                    if (proc is None or proc.poll() is not None) and now < end_dt:
                        print(f"⚠️ [Live Worker] FFmpeg #{lid} terputus, menyambung ulang otomatis...")
                        cmd = [
                            "ffmpeg", "-re", "-stream_loop", "-1",
                            "-i", bpath,
                            "-c", "copy", "-f", "flv",
                            f"rtmp://a.rtmp.youtube.com/live2/{item.get('stream_key')}"
                        ]
                        ffmpeg_processes[lid] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    # Cek apakah waktu siaran sudah selesai
                    if now >= end_dt:
                        print(f"🏁 [Live Worker] Durasi siaran #{lid} selesai. Mematikan stream...")
                        if proc:
                            try:
                                proc.terminate()
                                proc.kill()
                            except Exception:
                                pass
                            ffmpeg_processes.pop(lid, None)

                        stop_youtube_broadcast(item.get('yt_id'), item.get('broadcast_id'))
                        item['status'] = 'FINISHED'
                        item['message'] = '✅ Siaran selesai sesuai durasi jadwal.'
                        
                        # Hapus file Base Video otomatis setelah selesai agar disk VPS hemat
                        if bpath and os.path.exists(bpath):
                            try: os.remove(bpath)
                            except Exception: pass
                            
                        db_changed = True
                    else:
                        # Update angka Viewers & Likes setiap 60 detik
                        last_p = _last_stats_poll.get(lid, 0)
                        if time.time() - last_p > 60:
                            _last_stats_poll[lid] = time.time()
                            v_cnt, l_cnt = fetch_live_metrics(item.get('yt_id'), item.get('broadcast_id'))
                            if v_cnt is not None:
                                item['viewers'] = v_cnt
                                item['likes'] = l_cnt
                                db_changed = True

            if db_changed:
                save_items()

            # Selalu sinkronkan status Priority-Lock dengan keadaan jadwal terbaru
            sync_priority_lock(read_db())

        except Exception as e:
            print(f"❌ [Live Worker] Error: {e}")
            
        time.sleep(5)

# Jalankan thread otomatis saat modul di-import
if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    threading.Thread(target=live_worker_loop, daemon=True).start()