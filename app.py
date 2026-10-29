import os, time, queue, threading, subprocess, random, json, shutil, math, re
import numpy as np
import cv2, librosa, imageio
import datetime as dt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
import uuid
from core.job_state import channel_id as task_channel_id, terminal as task_terminal
from core.json_store import read_json, write_json, update_json
from core.media import validate_video
from core.upload_state import DurableUpload, has_pending_upload, state_path
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session
import requests

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from core.resource_gate import acquire_resource, release_resource

# ==========================================
# 🛡️ SETUP & MONITORING
# ==========================================
def auto_setup_dependencies():
    for binary in ('ffmpeg', 'ffprobe'):
        if not shutil.which(binary):
            raise RuntimeError(f'{binary} belum tersedia. Pasang dependensi server sebelum menjalankan KeiBot.')

auto_setup_dependencies()

last_cpu_idle = 0
last_cpu_total = 0

def get_system_stats():
    global last_cpu_idle, last_cpu_total
    cpu_pct = 0.0
    try:
        with open('/proc/stat', 'r') as f:
            parts = [int(i) for i in f.readline().split()[1:8]]
        idle = parts[3] + parts[4]
        total = sum(parts)
        if last_cpu_total > 0:
            diff_idle = idle - last_cpu_idle
            diff_total = total - last_cpu_total
            if diff_total > 0:
                cpu_pct = round(100.0 * (1.0 - diff_idle / diff_total), 1)
        last_cpu_idle = idle
        last_cpu_total = total
        if cpu_pct < 0.0: cpu_pct = 0.0
        if cpu_pct > 100.0: cpu_pct = 100.0
    except: pass

    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        return {"cpu": cpu_pct, "ram_pct": mem.percent, "ram_used": round(mem.used / (1024**3), 2), "ram_total": round(mem.total / (1024**3), 2)}
    except: pass

    return {"cpu": cpu_pct, "ram_pct": 0.0, "ram_used": 0.0, "ram_total": 0.0}

# ==========================================
# 💾 DATABASE & FOLDER SYSTEM
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'))
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

from clipper.routes import clipper_bp
app.register_blueprint(clipper_bp, url_prefix='/api/clip')

from shorts.routes import shorts_bp
app.register_blueprint(shorts_bp, url_prefix='/api/shorts')
import shorts.queue_worker  # <-- Pastikan baris ini ada agar Shorts Worker hidup di background!

# 🔥 BLOK PENDAFTARAN MODUL LIVE (POSISI YANG BENAR) 🔥
from live.routes import live_bp
app.register_blueprint(live_bp, url_prefix='/api/live')
import live.queue_worker

def is_configured(): return os.path.exists(CONFIG_FILE)
def load_bot_config():
    data = read_json(CONFIG_FILE, {})
    if not isinstance(data, dict):
        raise ValueError('Format konfigurasi tidak valid.')
    return data

bot_config = load_bot_config()
app.secret_key = bot_config.get('secret_key', secrets.token_hex(24))

@app.before_request
def check_security():
    allowed_routes = ['login', 'setup', 'static', 'serve_uploads', 'device_login', 'poll_device_token']
    if request.endpoint in allowed_routes: return
    if not is_configured(): return redirect(url_for('setup'))
    if 'logged_in' not in session: return redirect(url_for('login'))

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if is_configured(): return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        pin = request.form.get('new_pin'); pin2 = request.form.get('confirm_pin')
        if not pin or len(pin) < 3: error = "PIN minimal 3 karakter."
        elif pin != pin2: error = "PIN tidak cocok!"
        else:
            new_secret = secrets.token_hex(24)
            write_json(CONFIG_FILE, {'admin_pin': pin, 'secret_key': new_secret})
            app.secret_key = new_secret; session['logged_in'] = True
            return redirect(url_for('hub'))
    return render_template('setup.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_configured(): return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        if request.form.get('password') == load_bot_config().get('admin_pin'):
            session['logged_in'] = True; return redirect(url_for('hub'))
        else: error = 'Akses Ditolak! PIN Salah.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None); return redirect(url_for('login'))

@app.route('/hub')
def hub():
    # Halaman untuk memilih masuk ke Factory atau Clipper
    return render_template('hub.html')

@app.route('/clipper')
def clipper_dashboard():
    # Halaman UI utama untuk Clipper
    return render_template('clipper.html')

@app.route('/settings')
def settings_page():
    return render_template('settings.html')

@app.route('/shorts')
def shorts_dashboard():
    return render_template('shorts.html')

@app.route('/live')
def live_dashboard():
    return render_template('live.html')

@app.route('/tasks')
def tasks_master_dashboard():
    return render_template('tasks.html')

@app.route('/analytics')
def analytics_master_dashboard():
    return render_template('analytics.html')

@app.route('/api/unified_tasks')
def api_unified_tasks():
    """Menggabungkan antrean & riwayat dari Factory, Shorts, dan Clipper lengkap dengan yt_id."""
    ch_map = {c['yt_id']: c['name'] for c in database_channel}
    ch_order = [c['yt_id'] for c in database_channel]
    
    # 1. Data Factory
    fac_active, fac_hist = [], []
    for t in active_tasks:
        yt_id = t.get('yt_id') or t.get('blueprint', {}).get('yt_id', '')
        fac_active.append({
            "id": t['id'], "module": "factory", "yt_id": yt_id,
            "title": t.get('title', 'Factory Video'),
            "channel": ch_map.get(yt_id, "Tanpa Channel"), "time": t.get('time', '-'),
            "thumb_status": t.get('thumb_status', ''),
            "status": t.get('status', 'Queue'), "can_stop": True,
            "platforms": ["youtube"]
        })
    for t in history_tasks:
        yt_id = t.get('yt_id') or t.get('blueprint', {}).get('yt_id', '')
        fac_hist.append({
            "id": t['id'], "module": "factory", "yt_id": yt_id,
            "title": t.get('title', 'Factory Video'),
            "channel": ch_map.get(yt_id, "Tanpa Channel"), "info": t.get('render_info', ''),
            "thumb_status": t.get('thumb_status', ''),
            "status": t.get('status', 'Selesai'),
            "platforms": ["youtube"]
        })

    # 2. Data Clipper
    clip_active, clip_hist = [], []
    clipper_db = os.path.join(BASE_DIR, 'clipper', 'clips_db.json')
    if os.path.exists(clipper_db):
        try:
            with open(clipper_db, 'r') as f:
                c_jobs = json.load(f)
            for j in c_jobs:
                st = str(j.get('status', ''))
                yt_id = j.get('yt_id', '')
                item = {
                    "id": j.get('id', '-'), "module": "clipper", "yt_id": yt_id,
                    "title": j.get('title') or j.get('url', 'Clipper Job'),
                    "channel": ch_map.get(yt_id, j.get('channel_name', 'Clipper Lokal')),
                    "time": j.get('publish_date') or j.get('created_at', '-'),
                    "info": "", "status": st, "can_stop": True,
                    "platforms": [j.get('options', {}).get('outputDest', 'youtube')]
                }
                # 🔥 FIX: Klasifikasi riwayat diperluas! (Menangkap tanda Seru, Stop, dan Dibatalkan)
                if any(x in st.lower() for x in ('done', 'error', 'selesai', 'gagal', 'dibatalkan')) or '✅' in st or '❌' in st or '⚠️' in st or '🛑' in st:
                    clip_hist.append(item)
                else:
                    clip_active.append(item)
        except Exception:
            pass

    # 3. Data Shorts
    short_active, short_hist = [], []
    shorts_db = os.path.join(BASE_DIR, 'shorts', 'shorts_db.json')
    if os.path.exists(shorts_db):
        try:
            with open(shorts_db, 'r') as f:
                s_jobs = json.load(f)
            for j in s_jobs:
                st = str(j.get('status', ''))
                yt_id = j.get('yt_id', '')
                item = {
                    "id": j.get('id', '-'), "module": "shorts", "yt_id": yt_id,
                    "title": j.get('title', 'Shorts Video'),
                    "channel": ch_map.get(yt_id, 'Shorts Channel'),
                    "time": j.get('publish_date', '-'),
                    "info": "", "status": st, "can_stop": True,
                    "platforms": j.get('options', {}).get('platforms', ['youtube'])
                }
                # 🔥 FIX: Klasifikasi riwayat diperluas!
                if any(x in st.lower() for x in ('done', 'error', 'selesai', 'gagal', 'dibatalkan')) or '✅' in st or '❌' in st or '⚠️' in st or '🛑' in st:
                    short_hist.append(item)
                else:
                    short_active.append(item)
        except Exception:
            pass

    return jsonify({
        "active": fac_active + short_active + clip_active,
        "history": fac_hist + short_hist + clip_hist,
        "ch_order": ch_order,
        "counts": {
            "all": len(fac_active) + len(short_active) + len(clip_active),
            "factory": len(fac_active),
            "shorts": len(short_active),
            "clipper": len(clip_active)
        }
    })

@app.route('/api/get_deep_analytics')
def api_get_deep_analytics():
    """Mengambil Statistik Channel, Estimasi Monetisasi/Earning, dan Top 5 Video Populer."""
    result = []
    for c in database_channel:
        views, subs, videos = 0, 0, 0
        top_videos = []
        uploads_pl = None
        try:
            creds_list = c.get('creds_list', [c.get('creds_json')])
            if creds_list and creds_list[0]:
                creds = Credentials.from_authorized_user_info(json.loads(creds_list[0]))
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                youtube = build('youtube', 'v3', credentials=creds)
                
                # Ambil Statistik & Playlist Uploads Channel
                res = youtube.channels().list(part="statistics,contentDetails", id=c['yt_id']).execute()
                if res.get('items'):
                    item = res['items'][0]
                    stats = item.get('statistics', {})
                    views = int(stats.get('viewCount', 0))
                    subs = int(stats.get('subscriberCount', 0))
                    videos = int(stats.get('videoCount', 0))
                    uploads_pl = item.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads')

                # Ambil Video Terbaru & Urutkan 5 Paling Populer
                if uploads_pl:
                    pl_res = youtube.playlistItems().list(part="contentDetails", playlistId=uploads_pl, maxResults=15).execute()
                    vid_ids = [it['contentDetails']['videoId'] for it in pl_res.get('items', [])]
                    if vid_ids:
                        v_res = youtube.videos().list(part="snippet,statistics", id=','.join(vid_ids)).execute()
                        v_list = []
                        for v in v_res.get('items', []):
                            v_stat = v.get('statistics', {})
                            v_snip = v.get('snippet', {})
                            v_list.append({
                                "id": v['id'],
                                "title": v_snip.get('title', 'Untitled'),
                                "published": v_snip.get('publishedAt', '')[:10],
                                "thumb": v_snip.get('thumbnails', {}).get('medium', {}).get('url', ''),
                                "views": int(v_stat.get('viewCount', 0)),
                                "likes": int(v_stat.get('likeCount', 0)),
                                "comments": int(v_stat.get('commentCount', 0))
                            })
                        v_list.sort(key=lambda x: x['views'], reverse=True)
                        top_videos = v_list[:5]
        except Exception:
            pass

        # Estimasi Jam Tayang (Rata-rata 4.5 menit per view untuk musik/VOD) & Estimasi Earning ($1.20 RPM)
        est_watch_hours = round((views * 4.5) / 60.0, 1)
        is_monetized = (subs >= 1000 and est_watch_hours >= 4000)
        est_earning_usd = round((views / 1000.0) * 1.20, 2) if is_monetized else round((views / 1000.0) * 0.45, 2)

        # Rekomendasi Langkah Otomatis (Actionable Insight)
        if is_monetized:
            advice = "✅ Channel memenuhi syarat monetisasi! Pertahankan jadwal upload rutin via Factory & perbanyak variasi lagu dari Top Video."
        elif subs < 1000 and est_watch_hours >= 4000:
            advice = f"⚡ Jam tayang sudah tembus! Kurang {1000 - subs} Subscribers lagi — genjot upload rutin di modul Shorts & Clipper untuk menjaring subs cepat."
        elif subs >= 1000 and est_watch_hours < 4000:
            advice = f"🏭 Subscribers sudah tembus 1K! Kurang ~{int(4000 - est_watch_hours)} jam tayang — perbanyak video durasi 1-3 jam di Factory atau nyalakan Live Streaming."
        else:
            advice = "🚀 Fokus kombinasi: Gunakan Shorts/Clipper untuk menaikkan Subscribers dan jadwalkan Batch Factory 2x sehari untuk menabung Jam Tayang."

        result.append({
            "yt_id": c["yt_id"], "name": c["name"], "thumbnail": c.get("thumbnail", ""),
            "views": views, "subs": subs, "videos": videos,
            "watch_hours": est_watch_hours, "is_monetized": is_monetized,
            "earning_usd": est_earning_usd, "top_videos": top_videos, "advice": advice
        })
    return jsonify(result)

BASE_UPLOAD = os.path.join(BASE_DIR, "uploads")
DB_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')
PRESETS_FILE = os.path.join(BASE_DIR, 'presets.json')
CLIENT_SECRETS_FILE = os.path.join(BASE_DIR, 'client_secret.json')
SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.upload']

os.makedirs(BASE_UPLOAD, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)

db_lock = threading.RLock()  # 🔥 FIX: RLock (bukan Lock biasa) supaya tidak deadlock saat
                              # ada 'with db_lock:' bersarang (mis. save_channels() dipanggil
                              # dari dalam blok 'with db_lock:' lain, seperti di batch_create()).

GALLERY_FOLDER_MAP = {
    'audio_non_cid': 'audio_non_cid',
    'audio_cid':     'audio_cid',
    'background':    'backgrounds',
    'backgrounds':   'backgrounds',
    'shorts_bg':     'shorts_bg',
    'shorts_thumb':  'shorts_thumb',
    'thumbnail':     'thumbnails',
    'thumbnails':    'thumbnails',
    'base_video':    'base_videos'
}

def resolve_folder(g_type: str) -> str:
    return GALLERY_FOLDER_MAP.get(str(g_type).strip().lower(), 'audios')

def load_tasks_db():
    data = read_json(TASKS_FILE, {"active": [], "history": []})
    if not isinstance(data, dict) or not isinstance(data.get('active'), list) or not isinstance(data.get('history'), list):
        raise ValueError('Format database task tidak valid; database tidak diubah.')
    return data

def save_tasks_db():
    with db_lock:
        write_json(TASKS_FILE, {"active": active_tasks, "history": history_tasks})

# ==========================================
# 🛡️ SISTEM KETAHANAN (ANTI-STUCK & ANTI-LOST)
# ==========================================
def send_tg_message(msg):
    """Kirim pesan ke Telegram kalau token & chat_id tersimpan. Aman gagal silent."""
    try:
        bot_config = load_bot_config()
        token, chat_id = bot_config.get('tg_token'), bot_config.get('tg_chat_id')
        if not token or not chat_id: return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception:
        pass

def startup_sanitizer():
    # Do not kill arbitrary FFmpeg processes or remove files owned by active jobs.
    # Each worker cleans up only its own temporary files.
    return

def queue_watchdog_worker():
    """🔥 BARU: Watchdog antrean — task stuck 'Rendering...' > 30 menit otomatis digagalkan
    & lanjut ke task berikutnya, plus notifikasi Telegram."""
    STUCK_LIMIT = 60 * 60  # 60 menit (1 jam)
    while True:
        time.sleep(60)
        now = time.time()
        stuck = []
        with db_lock:
            for d in active_tasks:
                st = d['status']
                # 🔥 BARU: semua stage dimonitor — termasuk "In Factory Queue"
                # (task bisa macet di antrean kalau worker error sebelum masuk render).
                # Pengecualian: status cooldown/dijadwalkan yang memang HARUS menunggu.
                # 🔥 FIX 2: Tambahkan "Menunggu" ke daftar pengecualian
                waiting_kind = "Ditunda" in st or "Dijadwalkan" in st or "Menunggu" in st
                
                stuck_kind = not waiting_kind and ("Queue" in st or "Rendering" in st or "Meracik" in st
                              or "Mengunggah" in st or "Upload" in st or "YouTube" in st
                              or "Antrean" in st or "Extract" in st)
                if stuck_kind and "updated_at" not in d:
                    d['updated_at'] = now
                if stuck_kind and d.get('updated_at', 0) and (now - d['updated_at']) > STUCK_LIMIT:
                    stuck.append(d['id'])
            if stuck:
                save_tasks_db()
        for sid in stuck:
            # 🔥 FIX: hentikan worker yang masih memproses task stuck
            # (tanpa ini, worker tetap jalan sampai selesai → notif SUKSES
            # terkirim padahal task sudah digagalkan watchdog → notif ganda).
            stop_flags[sid] = True
            proc = active_processes.pop(sid, None)
            if proc is not None:
                try: proc.terminate(); proc.kill()
                except Exception: pass
            with db_lock:
                for d in active_tasks:
                    if d['id'] == sid:
                        move_to_history(sid, f"Gagal ❌ (Stuck >1 jam, otomatis dilanjutkan)")
                        break
            send_tg_message(f"⚠️ *Watchdog*: Task #{sid} stuck >1 jam (tidak ada kemajuan).\nOtomatis digagalkan & antrean dilanjutkan ke task berikutnya.")

if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    threading.Thread(target=queue_watchdog_worker, daemon=True).start()

def telegram_status_report_worker():
    """🔥 UPDATE: Event-based notification — TIDAK lagi per jam.
    Notifikasi Telegram HANYA dikirim saat ada kejadian penting:
    • Task berhasil tayang / render selesai ✅
    • Task gagal ❌ (limit, error upload, dll)
    • Task dibatalkan/digagalkan watchdog 🛑
    Laporan digabung tiap menit (buffer) agar tidak spam.
    """
    global tg_event_queue     # pakai buffer modul (dideklarasikan di bawah worker)
    last_flush = time.time()
    FLUSH_INTERVAL = 60           # kirim kumpulan event maksimal tiap 1 menit
    MAX_FLUSH = 8                 # kalau event menumpuk >8, kirim segera
    while True:
        time.sleep(30)
        bot_config = load_bot_config()
        token, chat_id = bot_config.get('tg_token'), bot_config.get('tg_chat_id')
        if not token or not chat_id: continue
        # 🔥 Gabung event menjadi 1 pesan (anti-spam)
        if len(tg_event_queue) >= MAX_FLUSH or (tg_event_queue and time.time() - last_flush >= FLUSH_INTERVAL):
            events = tg_event_queue[:MAX_FLUSH]
            tg_event_queue = tg_event_queue[MAX_FLUSH:]
            last_flush = time.time()
            msg = "🔔 *KeiBot — Laporan Kejadian*\n\n" + "\n\n".join(events)
            try: send_tg_message(msg)
            except Exception: pass

def notify_event(emoji, title, status):
    """🔥 BARU: masukkan event notifikasi (selesai/gagal) ke buffer Telegram.
    Event digabung & dikirim maksimal tiap 1 menit agar tidak spam chat."""
    try:
        tg_event_queue.append(f"{emoji} *{title[:70]}*\nStatus: {status}")
    except Exception:
        pass

tg_event_queue = []  # 🔥 buffer event notifikasi Telegram (dibaca worker di atas)

telegram_status_report_worker_ref = threading.Thread(target=telegram_status_report_worker, daemon=True)
if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    telegram_status_report_worker_ref.start()

def load_channels():
    data = read_json(DB_FILE, [])
    if not isinstance(data, list):
        raise ValueError('Format database channel tidak valid.')
    return data

def save_channels(channels):
    with db_lock:
        write_json(DB_FILE, channels)

task_data = load_tasks_db()
active_tasks = task_data.get("active", [])
history_tasks = task_data.get("history", [])
database_channel = load_channels()

import itertools
render_queue = queue.PriorityQueue()
_queue_seq = itertools.count()  # tie-breaker biar item dgn jadwal sama tidak dibanding langsung sbg dict

def _queue_priority(blueprint):
    """Ambil timestamp dari publish_date blueprint agar antrean diproses
    berdasarkan tanggal & jam paling awal duluan.
    🔥 VIP PRIORITY: Jika ini adalah task Base Video Live, beri prioritas tertinggi (-1.0)."""
    if blueprint.get('is_live_base'):
        return -1.0  # Langsung masuk urutan paling depan antrean global!

    try:
        ts = datetime.strptime(blueprint['publish_date'], "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return float('inf')  # kalau tanggal tidak valid/kosong, taruh paling belakang
    overdue_boost = max(0.0, datetime.now().timestamp() - ts)
    if overdue_boost > 60:  # baru di-boost kalau lewat > 1 menit
        ts = max(0.0, ts - min(overdue_boost, 7 * 24 * 3600))
    return ts

def remove_queued_tasks(predicate):
    removed = []
    # Keep PriorityQueue accounting intact even when a worker is consuming it.
    with render_queue.mutex:
        import heapq
        kept = []
        for entry in render_queue.queue:
            if predicate(entry[2]):
                removed.append(entry[2])
            else:
                kept.append(entry)
        render_queue.queue[:] = kept
        heapq.heapify(render_queue.queue)
        render_queue.unfinished_tasks -= len(removed)
        if render_queue.unfinished_tasks == 0:
            render_queue.all_tasks_done.notify_all()
        render_queue.not_full.notify_all()
    return removed


def queue_put(blueprint):
    render_queue.put((_queue_priority(blueprint), next(_queue_seq), blueprint))

def queue_get():
    _priority, _seq, blueprint = render_queue.get_nowait()
    return blueprint
stop_flags = {}
active_processes = {}  # 🔥 task_id -> subprocess.Popen aktif, dipakai untuk force-stop instan
channel_cooldowns = {} 

def load_cooldowns():
    # 🔥 BARU: muat cooldown rate-limit dari channels_db.json agar TIDAK HILANG saat restart.
    # Sebelumnya cooldown hanya di memori → restart = cooldown hilang → task langsung
    # diproses lagi → kena rate-limit YouTube lagi → task "nyangkut" & rawan hilang.
    global channel_cooldowns
    for c in database_channel:
        cd = c.get('cooldown_until')
        if cd and cd > time.time():
            channel_cooldowns[c['yt_id']] = cd

def persist_cooldown(yt_id):
    # 🔥 BARU: simpan cooldown channel permanen ke database setiap kali cooldown dipasang.
    for c in database_channel:
        if c['yt_id'] == yt_id:
            c['cooldown_until'] = channel_cooldowns.get(yt_id, 0)
            try: save_channels(database_channel)
            except Exception: pass
            return

load_cooldowns()

# 🔥 SISTEM NOTIFIKASI LONCENG 🔥
system_notifications = []

def get_ffmpeg_path():
    local_exe = os.path.join(BASE_DIR, "ffmpeg.exe")
    if os.path.exists(local_exe): return local_exe
    found = shutil.which("ffmpeg")
    if found: return found
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
        if os.path.exists(p): return p
    raise FileNotFoundError("ffmpeg tidak ditemukan! Jalankan: apt-get install -y ffmpeg")

def get_ffprobe_path():
    found = shutil.which("ffprobe")
    if found: return found
    for p in ["/usr/bin/ffprobe", "/usr/local/bin/ffprobe", "/bin/ffprobe"]:
        if os.path.exists(p): return p
    return "ffprobe"

def wait_for_resources(task_id, max_ram_pct=85.0):
    while True:
        if stop_flags.get(task_id): return False
        stats = get_system_stats()
        if stats['ram_pct'] < max_ram_pct: return True
        with db_lock:
            for d in active_tasks:
                if d['id'] == task_id: d['status'] = f"Menunggu RAM Turun ({stats['ram_pct']}%) ⏳"
        save_tasks_db()
        time.sleep(10)

def move_to_history(task_id, final_status):
    global active_tasks, history_tasks
    with db_lock:
        for t in active_tasks:
            if t['id'] == task_id:
                start_t = t.get('started_at')
                if start_t:
                    import time as sys_time, datetime as sys_dt
                    dur_sec = int(sys_time.time() - start_t)
                    m, s = divmod(dur_sec, 60)
                    h, m = divmod(m, 60)
                    dur_str = f"{h}h {m}m" if h > 0 else f"{m}m {s}s"
                    st_str = sys_dt.datetime.fromtimestamp(start_t).strftime("%H:%M")
                    ed_str = sys_dt.datetime.now().strftime("%H:%M")
                    t['render_info'] = f"⏱ {st_str} - {ed_str} ({dur_str})"
                
                t['status'] = final_status
                history_tasks.insert(0, t)
                active_tasks.remove(t)
                if len(history_tasks) > 2000: history_tasks.pop() # Limit di perbesar
                break
    save_tasks_db()

def get_fresh_credentials(channel_data):
    creds_str = channel_data.get('creds_list', [channel_data.get('creds_json')])[0]
    creds = Credentials.from_authorized_user_info(json.loads(creds_str))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ==========================================
# 🚨 SATPAM API KEY (AUTO-CHECKER)
# ==========================================
def api_key_checker_worker():
    global system_notifications, database_channel
    while True:
        time.sleep(10) 
        new_notifs = []
        for c in database_channel:
            creds_list = c.get('creds_list', [c.get('creds_json', '')])
            for idx, cred_str in enumerate(creds_list):
                if not cred_str: continue
                try:
                    creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                except Exception as e:
                    msg = f"⚠️ API Key #{idx+1} untuk Channel '{c.get('name','Unknown')}' EXPIRED! Silakan hapus dan tautkan ulang."
                    if not any(n['msg'] == msg for n in system_notifications):
                        new_notifs.append({"msg": msg, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
        
        if new_notifs:
            with db_lock:
                system_notifications.extend(new_notifs)
                
        time.sleep(43200)

if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    threading.Thread(target=api_key_checker_worker, daemon=True).start()

# ==========================================
# 🏭 GALLERY & ASSET MANAGER
# ==========================================
def channel_gallery_path(yt_id):
    if not isinstance(yt_id, str) or not yt_id or yt_id in ('.', '..') or os.path.basename(yt_id) != yt_id:
        raise ValueError('ID channel tidak valid.')
    if not any(c.get('yt_id') == yt_id for c in database_channel):
        raise ValueError('Channel tidak terdaftar.')
    root = os.path.realpath(BASE_UPLOAD)
    path = os.path.realpath(os.path.join(root, yt_id))
    if os.path.commonpath([root, path]) != root or path == root:
        raise ValueError('Folder channel di luar Gallery.')
    return path


def get_channel_folder(yt_id, sub):
    root = channel_gallery_path(yt_id)
    path = os.path.realpath(os.path.join(root, sub))
    if os.path.commonpath([root, path]) != root:
        raise ValueError('Folder aset tidak valid.')
    os.makedirs(path, exist_ok=True)
    return path

def get_multi_backgrounds(yt_id, count=1):
    path = get_channel_folder(yt_id, "backgrounds")
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.mp4', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mov'))]
    if not files: return []
    random.shuffle(files)
    
    selected = []
    while len(selected) < count and files:
        for f in files:
            selected.append(f)
            if len(selected) == count: break
    return selected

def get_mixed_audios(yt_id, count_non, count_cid):
    p_non = get_channel_folder(yt_id, "audio_non_cid")
    p_cid = get_channel_folder(yt_id, "audio_cid")
    
    f_non = [os.path.join(p_non, f) for f in os.listdir(p_non) if f.lower().endswith(('.mp3', '.wav'))]
    f_cid = [os.path.join(p_cid, f) for f in os.listdir(p_cid) if f.lower().endswith(('.mp3', '.wav'))]
    
    random.shuffle(f_non)
    random.shuffle(f_cid)
    
    sel_non = f_non[:int(count_non)]
    sel_cid = f_cid[:int(count_cid)]
    
    combined = sel_non + sel_cid
    random.shuffle(combined) # Acak ulang gabungannya (Final Shuffle)
    return combined

def get_and_consume_thumbnail(yt_id):
    """🔥 UPDATE: pilih thumbnail acak (bukan selalu file pertama),
    validasi ukuran minimal 1280x720 agar YouTube tidak menolak diam-diam."""
    path = get_channel_folder(yt_id, "thumbnails")
    files = [f for f in os.listdir(path) if f.lower().endswith(('.jpg', '.png', '.jpeg'))]
    if not files: return None
    # Pilih acak dari folder (lebih bervariasi tiap video, file tidak dihapus/dipakai ulang)
    chosen = random.choice(files)
    thumb_path = os.path.join(path, chosen)
    # 🔥 BARU: validasi ukuran — YouTube mewajibkan >= 1280x720 DAN <= 2MB (2097152 byte)
    try:
        from PIL import Image
        img = Image.open(thumb_path)
        img.load()
        if img.width < 1280 or img.height < 720:
            # File tidak memenuhi standar YouTube — skip & catat di Task Monitor
            return None
    except Exception:
        return None
    # 🔥 BARU: kalau file > 2MB, kompres otomatis menjadi JPG (batas YouTube: 2097152 byte)
    max_bytes = 2097152
    try:
        if os.path.getsize(thumb_path) > max_bytes:
            from PIL import Image as PILImage
            img2 = PILImage.open(thumb_path).convert('RGB')
            # YouTube hanya butuh max 1920 lebar untuk thumbnail; downscale cukup
            scale = min(1.0, 1920 / img2.width, 1080 / img2.height)
            if scale < 1.0:
                img2 = img2.resize((max(1280, int(img2.width * scale)), max(720, int(img2.height * scale))), PILImage.LANCZOS)
            # Tulis ke file sementara .jpg lalu pakai itu
            base, _ = os.path.splitext(thumb_path)
            tmp_path = base + '_yt_compressed.jpg'
            quality = 92
            while quality >= 40:
                img2.save(tmp_path, 'JPEG', quality=quality, optimize=True)
                if os.path.getsize(tmp_path) <= max_bytes:
                    break
                quality -= 8
            else:
                # Fallback terakhir: turunkan skala lagi
                img3 = img2.resize((1920, 1080), PILImage.LANCZOS)
                img3.save(tmp_path, 'JPEG', quality=40, optimize=True)
            return tmp_path
    except Exception as e:
        print(f"Thumbnail compress error: {e}")
    return thumb_path

def get_and_consume_shorts_thumbnail(yt_id):
    """Mengambil Custom Thumbnail Shorts (9:16) secara acak, validasi min 720x1280 & max 2MB."""
    path = get_channel_folder(yt_id, "shorts_thumb")
    files = [f for f in os.listdir(path) if f.lower().endswith(('.jpg', '.png', '.jpeg', '.webp'))]
    if not files: return None
    chosen = random.choice(files)
    thumb_path = os.path.join(path, chosen)
    
    try:
        from PIL import Image as PILImage
        img = PILImage.open(thumb_path).convert('RGB')
        # Validasi resolusi vertikal Shorts (minimal lebar 720 & tinggi 1280)
        if img.width < 720 or img.height < 1280:
            return None
            
        max_bytes = 2097152 # Batas 2MB YouTube API
        if os.path.getsize(thumb_path) > max_bytes or thumb_path.lower().endswith('.webp'):
            scale = min(1.0, 1080 / img.width, 1920 / img.height)
            if scale < 1.0:
                img = img.resize((max(720, int(img.width * scale)), max(1280, int(img.height * scale))), PILImage.LANCZOS)
            base, _ = os.path.splitext(thumb_path)
            tmp_path = base + '_shorts_compressed.jpg'
            quality = 92
            while quality >= 45:
                img.save(tmp_path, 'JPEG', quality=quality, optimize=True)
                if os.path.getsize(tmp_path) <= max_bytes:
                    break
                quality -= 8
            return tmp_path
    except Exception as e:
        print(f"Shorts thumbnail error: {e}")
        return None
    return thumb_path

def get_random_preset(allowed_names=None):
    if not os.path.exists(PRESETS_FILE): return None
    try:
        with open(PRESETS_FILE, 'r') as f: presets = json.load(f)
        if not presets: return None
        if allowed_names:
            filtered = {k: v for k, v in presets.items() if k in allowed_names}
            if filtered: return random.choice(list(filtered.values()))
        return random.choice(list(presets.values()))
    except: return None

def safe_num(val, default):
    try: return float(val) if val != "" and val is not None else default
    except: return default

# ==========================================
# ⚙️ CORE ENGINE (VISUALIZER HULK V2.0)
# ==========================================
def get_gradient_color(c1, c2, ratio):
    import numpy as np
    ratio = np.clip(ratio, 0.0, 1.0)
    return (
        int(c2[0] + (c1[0] - c2[0]) * ratio),
        int(c2[1] + (c1[1] - c2[1]) * ratio),
        int(c2[2] + (c1[2] - c2[2]) * ratio)
    )

def get_rainbow_color(ratio):
    import numpy as np, cv2
    ratio = np.clip(ratio, 0.0, 1.0)
    hue = int(ratio * 179)
    hsv_pixel = np.uint8([[[hue, 255, 255]]])
    # 🔥 FIX: OpenCV butuh BGR, bukan RGB
    bgr_pixel = cv2.cvtColor(hsv_pixel, cv2.COLOR_HSV2BGR)[0][0]
    return (int(bgr_pixel[0]), int(bgr_pixel[1]), int(bgr_pixel[2]))

def draw_gradient_polygon_vertical(frame, pts, c1_bgr, c2_bgr, color_mode="Gradien Custom", flash=0.0):
    import numpy as np, cv2
    pts = np.array(pts, dtype=np.int32)
    if len(pts) < 3: return
    x_min, y_min = np.min(pts, axis=0)
    x_max, y_max = np.max(pts, axis=0)
    h_frame, w_frame = frame.shape[:2]
    x_min, y_min = max(0, int(x_min)), max(0, int(y_min))
    x_max, y_max = min(w_frame, int(x_max)), min(h_frame, int(y_max))
    patch_h = y_max - y_min
    patch_w = x_max - x_min
    if patch_h <= 0 or patch_w <= 0: return

    y_coords = np.arange(patch_h, dtype=np.float32)
    ratio = 1.0 - (y_coords / max(1.0, float(patch_h - 1)))
    gradient_patch = np.zeros((patch_h, patch_w, 3), dtype=np.uint8)
    
    if color_mode == "Dynamic RGB Shift":
        hues = (ratio * 179).astype(np.uint8)
        sats = np.full((patch_h,), 255, dtype=np.uint8)
        vals = np.full((patch_h,), 255, dtype=np.uint8)
        hsv_col = np.stack([hues, sats, vals], axis=1).reshape((patch_h, 1, 3))
        # 🔥 FIX: OpenCV butuh BGR, bukan RGB
        gradient_patch[:, :] = cv2.cvtColor(hsv_col, cv2.COLOR_HSV2BGR)
    elif color_mode in ("Solid / Gradien", "Dual-Color Flash"):
        c1 = np.array(c1_bgr, dtype=np.float32)
        c2 = np.array(c2_bgr, dtype=np.float32)
        if color_mode == "Dual-Color Flash":
            c1 = c1 * (1.0 + flash * 0.8)
            c2 = c2 * (1.0 + flash * 0.8)
        colors_col = c2 + np.outer(ratio, (c1 - c2))
        colors_col = np.clip(colors_col, 0, 255).astype(np.uint8)
        gradient_patch[:, :] = colors_col[:, np.newaxis, :]
    else:
        gradient_patch[:, :] = c1_bgr

    mask = np.zeros((patch_h, patch_w), dtype=np.uint8)
    pts_shifted = pts - np.array([x_min, y_min])
    cv2.fillPoly(mask, [pts_shifted], 255)
    frame_crop = frame[y_min:y_max, x_min:x_max]
    mask_3ch = mask[:, :, np.newaxis] > 0
    np.copyto(frame_crop, gradient_patch, where=mask_3ch)

def draw_rainbow_glow_polygon(frame, pts, cx, cy, hue_offset=0.0, blur_ksize=31, intensity=1.0):
    import numpy as np, cv2
    pts = np.array(pts, dtype=np.int32)
    if len(pts) < 3: return
    h_frame, w_frame = frame.shape[:2]
    pad = int(blur_ksize)
    x_min = max(0, int(np.min(pts[:, 0])) - pad)
    y_min = max(0, int(np.min(pts[:, 1])) - pad)
    x_max = min(w_frame, int(np.max(pts[:, 0])) + pad)
    y_max = min(h_frame, int(np.max(pts[:, 1])) + pad)
    pw, ph = x_max - x_min, y_max - y_min
    if pw <= 0 or ph <= 0: return

    mask = np.zeros((ph, pw), dtype=np.uint8)
    pts_shifted = pts - np.array([x_min, y_min])
    cv2.fillPoly(mask, [pts_shifted], 255)

    local_cx, local_cy = cx - x_min, cy - y_min
    yy, xx = np.mgrid[0:ph, 0:pw]
    angle = np.arctan2(yy - local_cy, xx - local_cx)
    hue = (((angle + np.pi) / (2 * np.pi) * 179.0) + hue_offset) % 179.0
    hue = hue.astype(np.uint8)
    sat = np.full((ph, pw), 255, dtype=np.uint8)
    val = np.full((ph, pw), 255, dtype=np.uint8)
    hsv = np.stack([hue, sat, val], axis=-1)
    
    # 🔥 FIX: OpenCV butuh BGR, bukan RGB
    rainbow_bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    layer = np.zeros((ph, pw, 3), dtype=np.uint8)
    mask_bool = mask > 0
    layer[mask_bool] = rainbow_bgr[mask_bool]

    k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
    k = max(3, k)
    layer = cv2.GaussianBlur(layer, (k, k), 0)

    roi = frame[y_min:y_max, x_min:x_max]
    blended = cv2.add(roi, (layer.astype(np.float32) * intensity).clip(0, 255).astype(np.uint8))
    frame[y_min:y_max, x_min:x_max] = blended

def draw_dualcolor_glow_polygon(frame, pts, color1, color2, blur_ksize=31, intensity=1.0, flash=0.0):
    import numpy as np
    import cv2
    pts = np.array(pts, dtype=np.int32)
    if len(pts) < 3: return
    h_frame, w_frame = frame.shape[:2]
    pad = int(blur_ksize)
    x_min = max(0, int(np.min(pts[:, 0])) - pad)
    y_min = max(0, int(np.min(pts[:, 1])) - pad)
    x_max = min(w_frame, int(np.max(pts[:, 0])) + pad)
    y_max = min(h_frame, int(np.max(pts[:, 1])) + pad)
    pw, ph = x_max - x_min, y_max - y_min
    if pw <= 0 or ph <= 0: return

    mask = np.zeros((ph, pw), dtype=np.uint8)
    pts_shifted = pts - np.array([x_min, y_min])
    cv2.fillPoly(mask, [pts_shifted], 255)

    ratio = np.linspace(0.0, 1.0, ph, dtype=np.float32).reshape(ph, 1, 1)
    c1_arr = np.array(color1, dtype=np.float32).reshape(1, 1, 3)
    c2_arr = np.array(color2, dtype=np.float32).reshape(1, 1, 3)
    grad_row = c1_arr + (c2_arr - c1_arr) * ratio
    grad_img = np.repeat(grad_row, pw, axis=1).astype(np.float32)

    boost = 1.0 + max(0.0, flash) * 0.8
    grad_img = np.clip(grad_img * boost, 0, 255).astype(np.uint8)

    layer = np.zeros((ph, pw, 3), dtype=np.uint8)
    mask_bool = mask > 0
    layer[mask_bool] = grad_img[mask_bool]

    k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
    k = max(3, k)
    layer = cv2.GaussianBlur(layer, (k, k), 0)

    roi = frame[y_min:y_max, x_min:x_max]
    blended = cv2.add(roi, (layer.astype(np.float32) * intensity).clip(0, 255).astype(np.uint8))
    frame[y_min:y_max, x_min:x_max] = blended

class BackgroundManager:
    def __init__(self, bg_paths, width, height, fps=30):
        import cv2
        import numpy as np
        self.bg_paths = bg_paths
        self.width = width
        self.height = height
        self.fps = fps
        self.current_idx = 0
        self.frame_number = 0
        self.cap = None
        self.is_video = False
        self.static_frame = None
        self._load_current()

    def _load_current(self):
        import cv2
        import numpy as np
        if self.cap:
            self.cap.release()
            self.cap = None

        if not self.bg_paths:
            self.static_frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            self.is_video = False
            return

        path = self.bg_paths[self.current_idx]
        ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
        
        if ext in ('mp4', 'webm', 'mov', 'avi', 'mkv'):
            self.is_video = True
            self.cap = cv2.VideoCapture(path)
            if not self.cap.isOpened():
                self.is_video = False
                self.static_frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        else:
            self.is_video = False
            img = cv2.imread(path)
            if img is None:
                self.static_frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            else:
                self.static_frame = self._resize_and_crop(img)

    def _resize_and_crop(self, frame):
        import cv2
        h, w = frame.shape[:2]
        target_ratio = self.width / self.height
        frame_ratio = w / h
        
        if frame_ratio > target_ratio:
            new_w = int(h * target_ratio)
            start_x = (w - new_w) // 2
            frame = frame[:, start_x:start_x+new_w]
        else:
            new_h = int(w / target_ratio)
            start_y = (h - new_h) // 2
            frame = frame[start_y:start_y+new_h, :]
            
        return cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)

    def get_frame(self, _attempts=0):
        import cv2
        import numpy as np
        if not self.is_video and len(self.bg_paths) > 1 and self.frame_number >= self.fps * 30:
            self.current_idx = (self.current_idx + 1) % len(self.bg_paths)
            self.frame_number = 0
            self._load_current()
        self.frame_number += 1
        if self.is_video and self.cap:
            ret, frame = self.cap.read()
            if not ret:
                if len(self.bg_paths) > 1:
                    self.current_idx = (self.current_idx + 1) % len(self.bg_paths)
                    self.frame_number = 0
                    self._load_current()
                    if _attempts >= len(self.bg_paths):
                        return np.zeros((self.height, self.width, 3), dtype=np.uint8)
                    return self.get_frame(_attempts + 1)
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = self.cap.read()
                if not ret:
                    return np.zeros((self.height, self.width, 3), dtype=np.uint8)
            return self._resize_and_crop(frame)
        else:
            if self.static_frame is not None:
                return self.static_frame.copy()
            return np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def close(self):
        if self.cap:
            self.cap.release()

class AudioBrain:
    def __init__(self):
        import numpy as np
        self.y = None; self.sr = None; self.has_audio = False
        self.duration = 0.0

    def load(self, path, max_duration=None):
        try:
            import librosa
            self.y, self.sr = librosa.load(path, sr=22050, mono=True, duration=max_duration)
            self.duration = len(self.y) / self.sr
            self.has_audio = True
        except Exception as e: print(f"Audio Error: {e}")

    def get_data(self, t, n_bars=64): 
        import numpy as np
        n_bars = int(n_bars) 
        if not self.has_audio: return 0.0, False, np.zeros(n_bars)
        idx = int(t * self.sr)
        if idx >= len(self.y): return 0.0, False, np.zeros(n_bars)
        
        bass_val = 0.0
        hit = False
        final_bars = np.zeros(n_bars)
        try:
            n_fft = 2048
            fft_data = self.y[idx:idx+n_fft]
            if len(fft_data) == n_fft:
                windowed_data = fft_data * np.hanning(n_fft)
                spec = np.abs(np.fft.rfft(windowed_data))
                
                # Ambil energi murni Sub-Bass & Kick Drum (Bin 1 s/d 8)
                raw_bass = float(np.max(spec[1:8]) * 0.65 + np.mean(spec[1:8]) * 0.35) / 25.0
                bass_val = float(np.clip(raw_bass, 0.0, 1.5))

                # Envelope Follower untuk menangkap setiap ketukan tanpa terlewat
                fast_b = getattr(self, 'fast_bass', 0.0)
                slow_b = getattr(self, 'slow_bass', 0.0)
                
                fast_b = (fast_b * 0.2 + bass_val * 0.8) if bass_val > fast_b else (fast_b * 0.75 + bass_val * 0.25)
                slow_b = (slow_b * 0.92) + (bass_val * 0.08)
                self.fast_bass = fast_b
                self.slow_bass = slow_b

                onset = max(0.0, fast_b - (slow_b * 0.85))
                bass_val = float(np.clip((onset * 2.8) + ((bass_val ** 2.5) * 0.5), 0.0, 1.0))
                hit = True if bass_val > 0.25 else False

                usable = spec[2:200] 
                ls = len(usable)
                if ls > 0:
                    half_n = max(1, n_bars // 2) 
                    raw_bars = np.zeros(half_n)
                    for i in range(half_n):
                        s = int((i / half_n) * ls)
                        e = int(((i + 1) / half_n) * ls)
                        if e <= s: e = s + 1
                        if e > ls: e = ls
                        raw_bars[i] = np.mean(usable[s:e]) / 15.0 if e > s else 0
                    
                    smooth_half = np.convolve(raw_bars, np.ones(3)/3, mode='same')
                    final_bars = np.concatenate((smooth_half[::-1], smooth_half))
                    if len(final_bars) < n_bars: final_bars = np.pad(final_bars, (0, n_bars - len(final_bars)), 'constant')
                    elif len(final_bars) > n_bars: final_bars = final_bars[:n_bars]
        except Exception: pass 
        return bass_val, hit, final_bars

class VisualEngine:
    def __init__(self, c_bot, c_top, c_part):
        self.col_top = (int(c_top[2]), int(c_top[1]), int(c_top[0]))
        self.frame_count = 0
        self.last_beat_t = -999.0
        self.beat_intensity = 0.0
        self.layer_states = {}

    def process(self, frame, vol, is_hit, bars, cfg):
        import numpy as np
        import cv2
        import random

        self.frame_count += 1
        t = self.frame_count / 30.0

        # 🔥 SMOOTH BEAT ENVELOPE (Naik sigap, turun mulus tanpa patah-patah)
        target_beat = float(np.clip(vol, 0.0, 1.0))
        if target_beat > self.beat_intensity:
            self.beat_intensity = (self.beat_intensity * 0.30) + (target_beat * 0.70)
        else:
            self.beat_intensity = (self.beat_intensity * 0.80) + (target_beat * 0.20)

        h, w = frame.shape[:2]
        current_spectrum = np.clip(bars * 255, 0, 255)
        
        layers = cfg.get('layers', [])
        is_double = cfg.get('vis_instance', 'single') == 'double'
        instances = 2 if is_double else 1

        if getattr(self, 'rainbow_v', None) is None:
            self.rainbow_v = []
            for d in range(1000):
                hue = int((d / 999.0) * 179) 
                bgr = cv2.cvtColor(np.uint8([[[hue, 255, 255]]]), cv2.COLOR_HSV2BGR)[0][0]
                self.rainbow_v.append((int(bgr[0]), int(bgr[1]), int(bgr[2])))

        for idx, layer in enumerate(layers):
            l_name = layer.get('name', '')
            l_id = layer.get('id', f"{l_name}_{idx}")
            
            if l_id not in self.layer_states:
                self.layer_states[l_id] = {'particles': [], 'last_t': t}
            state = self.layer_states[l_id]
            
            intens = float(layer.get('intensity', 50))
            val = intens / 100.0
            
            c1_hex = layer.get('color1', '#10b981')
            c2_hex = layer.get('color2', '#00e5ff')
            c1 = tuple(int(c1_hex.lstrip('#')[i:i+2], 16) for i in (4, 2, 0))
            c2 = tuple(int(c2_hex.lstrip('#')[i:i+2], 16) for i in (4, 2, 0))
            
            c_mode = layer.get('color_mode', 'Solid / Gradien')
            size_factor = float(layer.get('size', 60)) / 100.0
            px = float(layer.get('pos_x', 50))
            py = float(layer.get('pos_y', 85))

            # ----------------------------------------------------
            # 1. EFEK SINEMATIK BACKGROUND (ZOOM & GLITCH IKUT BEAT)
            # ----------------------------------------------------
            if "Color Boost" in l_name:
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
                hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (1.0 + val), 0, 255)
                frame = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

            elif "Glitch Beat" in l_name and self.beat_intensity > 0.05:
                shift = int(val * 28 * self.beat_intensity)
                if 0 < shift < w:
                    frame_copy = frame.copy()
                    frame[:, shift:, 2] = frame_copy[:, :-shift, 2] # Geser Red
                    frame[:, :-shift, 0] = frame_copy[:, shift:, 0] # Geser Blue

            elif "Zoom Jedag-Jedug" in l_name and self.beat_intensity > 0.005:
                # 🔥 SUB-PIXEL SMOOTH ZOOM: Menggunakan matriks float agar tidak bergetar/patah-patah
                zoom = 1.0 + (val * 0.38 * self.beat_intensity)
                M = np.float32([
                    [zoom, 0, (1.0 - zoom) * (w / 2.0)],
                    [0, zoom, (1.0 - zoom) * (h / 2.0)]
                ])
                frame = cv2.warpAffine(frame, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
                
            # ----------------------------------------------------
            # 2. VISUALIZER & PARTIKEL BEAT
            # ----------------------------------------------------
            for inst in range(instances):
                cur_px = 25.0 if inst == 0 and instances == 2 else (75.0 if inst == 1 and instances == 2 else px)
                cur_size_factor = size_factor * 0.45 if instances == 2 else size_factor
                
                cx = int(w * (cur_px / 100.0))
                cy = int(h * (py / 100.0))

                if l_name in ("Visualizer (Bar Bawah)", "Visualizer (Radial Bar Biasa)", "Visualizer (Waveform Garis)"):
                    num_bars = max(10, int(layer.get('num_bars', 64)))
                    scale_res = w / 640.0
                    max_h = h * (float(layer.get('height_scale', 50)) / 100.0) * 0.7

                    spec_len = len(current_spectrum)
                    half_idx = spec_len // 2
                    straight_spectrum = current_spectrum[half_idx:] 
                    cutoff = max(1, len(straight_spectrum))

                    state_id = f"smooth_raw_{l_id}_{inst}"
                    if state_id not in self.layer_states or len(self.layer_states[state_id]) != num_bars:
                        self.layer_states[state_id] = np.zeros(num_bars)
                    bar_h_arr = self.layer_states[state_id]

                    for i in range(num_bars):
                        pseudo_rand = abs(np.sin((i + 1) * 12.9898) * 43758.5453) % 1.0
                        source_idx = max(0, min(cutoff - 1, int(pseudo_rand * cutoff)))
                        raw_norm = float(straight_spectrum[source_idx]) / 255.0
                        
                        bar_var = 0.5 + ((abs(np.cos((i + 1) * 78.233) * 43758.5453) % 1.0) * 0.5)
                        raw_norm = min(1.0, raw_norm * bar_var)
                        target = (raw_norm ** 2.5) * 0.85
                        
                        if target > bar_h_arr[i]:
                            bar_h_arr[i] = (bar_h_arr[i] * 0.25) + (target * 0.75)
                        else:
                            bar_h_arr[i] = (bar_h_arr[i] * 0.65) + (target * 0.35)

                    # A. BAR BAWAH
                    if l_name == "Visualizer (Bar Bawah)":
                        total_width = float(w * cur_size_factor)
                        start_x = cx - (total_width / 2.0)
                        base_y = int(cy)
                        slot_w = total_width / float(num_bars)
                        
                        raw_gap = float(layer.get('bar_gap', 3)) * scale_res
                        space = min(raw_gap, max(0.0, slot_w - 1.0))
                        max_bar_w = max(1.0, slot_w - space)
                        thick_ratio = max(0.05, float(layer.get('thickness', 30)) / 100.0)
                        bar_w = max(1, int(round(max_bar_w * thick_ratio)))
                        bar_offset = (slot_w - bar_w) / 2.0

                        for i in range(num_bars):
                            ratio = i / max(1, num_bars - 1)
                            height = int(bar_h_arr[i] * max_h * (1.0 + self.beat_intensity * val * 0.35))
                            height = max(2, height)
                            
                            x1 = int(round(start_x + (slot_w * i) + bar_offset))
                            x2 = x1 + bar_w
                            y1, y2 = base_y - height, base_y
                            
                            x1_safe, x2_safe = max(0, min(w, x1)), max(0, min(w, x2))
                            y1_safe, y2_safe = max(0, min(h, y1)), max(0, min(h, y2))
                            
                            if x2_safe > x1_safe and y2_safe > y1_safe:
                                real_h = max(1, height)
                                for y in range(y1_safe, y2_safe):
                                    v_ratio = max(0.0, min(1.0, (y2_safe - y) / float(real_h)))
                                    if c_mode == "Dynamic RGB Shift":
                                        idx_c = int(((ratio + t * 0.2) % 1.0) * 999)
                                        color = self.rainbow_v[idx_c]
                                    elif c_mode == "Dual-Color Flash":
                                        color = c1 if self.beat_intensity > 0.5 else c2
                                    else:
                                        color = get_gradient_color(c2, c1, v_ratio)
                                    cv2.line(frame, (x1_safe, y), (x2_safe - 1, y), color, 1)

                    # B. RADIAL BAR (Lengkap dengan Foto Profil Channel di Tengah)
                    elif l_name == "Visualizer (Radial Bar Biasa)":
                        base_radius = min(w, h) * 0.32 * cur_size_factor
                        pulse_r = base_radius + (self.beat_intensity * 12.0 * val * scale_res)
                        max_radial_h = max_h * 0.65

                        circumference = 2.0 * np.pi * pulse_r
                        slot_arc = circumference / float(num_bars)
                        raw_gap = float(layer.get('bar_gap', 3)) * scale_res
                        space = min(raw_gap, max(0.0, slot_arc - 1.0))
                        max_bar_w = max(1.0, slot_arc - space)
                        thick_ratio = max(0.05, float(layer.get('thickness', 30)) / 100.0)
                        bar_w = max(1, int(round(max_bar_w * thick_ratio)))

                        for i in range(num_bars):
                            ratio = i / max(1, num_bars - 1)
                            height = max(2, int(bar_h_arr[i] * max_radial_h * (1.0 + self.beat_intensity * val * 0.35)))
                            angle = (i / float(num_bars)) * 2.0 * np.pi - (np.pi / 2.0)
                            cos_a, sin_a = np.cos(angle), np.sin(angle)

                            steps = 8
                            for s in range(steps):
                                r_start = pulse_r + (height * (s / float(steps)))
                                r_end = pulse_r + (height * ((s + 1) / float(steps)))
                                pt1 = (int(cx + cos_a * r_start), int(cy + sin_a * r_start))
                                pt2 = (int(cx + cos_a * r_end), int(cy + sin_a * r_end))
                                v_ratio = (s + 0.5) / float(steps)

                                if c_mode == "Dynamic RGB Shift":
                                    idx_c = int(((ratio + t * 0.2) % 1.0) * 999)
                                    color = self.rainbow_v[idx_c]
                                elif c_mode == "Dual-Color Flash":
                                    color = c1 if self.beat_intensity > 0.5 else c2
                                else:
                                    color = get_gradient_color(c2, c1, v_ratio)
                                cv2.line(frame, pt1, pt2, color, bar_w, cv2.LINE_AA)

                        # 🔥 RENDER FOTO PROFIL CHANNEL DI TENGAH LINGKARAN
                        inner_r = max(4, int(pulse_r - 3 * scale_res))
                        if layer.get('use_logo', True):
                            # Cache gambar logo di memori sekali saja agar render video tetap ngebut
                            if not hasattr(self, 'cached_logo_bgr'):
                                self.cached_logo_bgr = None
                                yt_id_cfg = cfg.get('yt_id')
                                ch_info = next((c for c in database_channel if c.get('yt_id') == yt_id_cfg), None)
                                if ch_info and ch_info.get('thumbnail'):
                                    try:
                                        resp = requests.get(ch_info['thumbnail'], timeout=5)
                                        if resp.status_code == 200:
                                            arr = np.frombuffer(resp.content, np.uint8)
                                            self.cached_logo_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                                    except Exception:
                                        pass
                                if self.cached_logo_bgr is None:
                                    logo_fallback = os.path.join(BASE_DIR, 'static', 'logo.png')
                                    if os.path.exists(logo_fallback):
                                        self.cached_logo_bgr = cv2.imread(logo_fallback, cv2.IMREAD_COLOR)

                            if self.cached_logo_bgr is not None:
                                diam = inner_r * 2
                                x1_l, y1_l = cx - inner_r, cy - inner_r
                                x2_l, y2_l = x1_l + diam, y1_l + diam
                                
                                # Pastikan area lingkaran berada di dalam batas layar
                                if x1_l >= 0 and y1_l >= 0 and x2_l <= w and y2_l <= h and diam > 4:
                                    logo_res = cv2.resize(self.cached_logo_bgr, (diam, diam), interpolation=cv2.INTER_AREA)
                                    mask = np.zeros((diam, diam), dtype=np.uint8)
                                    cv2.circle(mask, (inner_r, inner_r), inner_r, 255, -1, cv2.LINE_AA)
                                    roi = frame[y1_l:y2_l, x1_l:x2_l]
                                    mask_bool = mask > 0
                                    roi[mask_bool] = logo_res[mask_bool]

                        # Cincin bingkai (border) mengelilingi foto profil
                        cv2.circle(frame, (cx, cy), inner_r, c1, max(1, int(2.5 * scale_res)), cv2.LINE_AA)

                    # C. WAVEFORM GARIS
                    elif l_name == "Visualizer (Waveform Garis)":
                        total_width = float(w * cur_size_factor)
                        start_x = cx - (total_width / 2.0)
                        slot_w = total_width / float(max(1, num_bars - 1))
                        line_w = max(2, int(round((float(layer.get('thickness', 30)) / 100.0) * 12.0 * scale_res)))

                        pts = []
                        max_peak = 2
                        for i in range(num_bars):
                            height = max(2, int(bar_h_arr[i] * max_h * (1.0 + self.beat_intensity * val * 0.35)))
                            if height > max_peak: max_peak = height
                            pts.append((int(round(start_x + slot_w * i)), int(cy - height)))

                        for i in range(len(pts) - 1):
                            ratio = i / max(1, len(pts) - 1)
                            avg_h = (cy - (pts[i][1] + pts[i+1][1]) * 0.5)
                            v_ratio = max(0.0, min(1.0, avg_h / float(max(1, max_peak))))
                            if c_mode == "Dynamic RGB Shift":
                                idx_c = int(((ratio + t * 0.2) % 1.0) * 999)
                                color = self.rainbow_v[idx_c]
                            elif c_mode == "Dual-Color Flash":
                                color = c1 if self.beat_intensity > 0.5 else c2
                            else:
                                color = get_gradient_color(c2, c1, v_ratio)
                            cv2.line(frame, pts[i], pts[i+1], color, line_w, cv2.LINE_AA)

                # D. PARTIKEL (RISING EMBERS): MURNI MENGIKUTI BEAT
                # D. PARTIKEL BARU: FREE-FLOWING 360° & RANDOM BEAT POP
                # D. PARTIKEL: MELAYANG STABIL (FLOWING) & BELOK ARAH ACAK SAAT BEAT (TANPA NGEBUT)
                elif "Particle" in l_name:
                    scale_res = w / 640.0
                    target_count = max(10, int(layer.get('part_count', 60)))
                    size_scale = (float(layer.get('size', 50)) / 50.0) * scale_res
                    flow_speed = max(0.15, float(layer.get('flow_speed', 40)) / 40.0)
                    p_style = layer.get('part_style', 'flow_beat')

                    def _create_floating_particle(is_beat_spawn=False):
                        init_angle = random.uniform(0, 2 * np.pi)
                        return {
                            'x': random.uniform(0, w),
                            'y': random.uniform(0, h),
                            'angle': init_angle,
                            'target_angle': init_angle,
                            'base_speed': random.uniform(0.4, 1.2) * scale_res,
                            'wobble_freq': random.uniform(0.5, 1.2),
                            'wobble_amp': random.uniform(0.25, 0.6),
                            'phase': random.uniform(0, 2 * np.pi),
                            'base_size': random.uniform(1.8, 3.5) * size_scale,
                            'ratio': random.random(),
                            'life': 0.05 if is_beat_spawn else random.uniform(0.2, 0.9),
                            'fade_in': is_beat_spawn,
                            'decay': random.uniform(0.008, 0.020)
                        }

                    last_redir_t = state.get('last_redirect_t', -999.0)
                    is_beat_trigger = False
                    if self.beat_intensity > 0.38 and (t - last_redir_t) > 0.18:
                        is_beat_trigger = True
                        state['last_redirect_t'] = t

                    if p_style == 'flow_beat':
                        while len(state['particles']) < target_count:
                            state['particles'].append(_create_floating_particle(False))
                        if len(state['particles']) > target_count:
                            state['particles'] = state['particles'][:target_count]

                        # 🔥 SAAT BEAT: Belokkan arah melayang secara acak tanpa mempercepat laju
                        if is_beat_trigger:
                            for p in state['particles']:
                                if random.random() <= (0.3 + val * 0.7):
                                    turn_range = (0.4 + val * 0.8) * 2.0 * np.pi
                                    p['target_angle'] += (random.random() - 0.5) * turn_range
                    else:
                        while len(state['particles']) < int(target_count * 0.2):
                            state['particles'].append(_create_floating_particle(False))
                        if is_beat_trigger and len(state['particles']) < target_count + 20:
                            spawn_n = max(2, int(np.ceil(target_count * 0.15 * val)))
                            for _ in range(spawn_n):
                                state['particles'].append(_create_floating_particle(True))

                    alive_particles = []
                    for p in state['particles']:
                        # Smooth steering menuju arah baru
                        p['angle'] += (p['target_angle'] - p['angle']) * 0.12
                        drift_angle = p['angle'] + np.sin(t * p['wobble_freq'] + p['phase']) * p['wobble_amp']
                        move_step = p['base_speed'] * flow_speed

                        # Laju melayang selalu stabil sesuai flow_speed
                        p['x'] += np.cos(drift_angle) * move_step
                        p['y'] += np.sin(drift_angle) * move_step

                        if p_style == 'beat_spawn':
                            if p['fade_in']:
                                p['life'] += 0.08
                                if p['life'] >= 1.0:
                                    p['life'] = 1.0
                                    p['fade_in'] = False
                            else:
                                p['life'] -= p['decay']
                            if p['life'] <= 0.02:
                                continue
                        else:
                            p['life'] = 0.75 + 0.25 * np.sin(t * 1.5 + p['phase'])

                        if p['x'] < -15: p['x'] = w + 12
                        elif p['x'] > w + 15: p['x'] = -12
                        if p['y'] < -15: p['y'] = h + 12
                        elif p['y'] > h + 15: p['y'] = -12

                        if c_mode == "Dynamic RGB Shift":
                            idx_c = int(((p['ratio'] + t * 0.15) % 1.0) * 999)
                            color = self.rainbow_v[idx_c]
                        elif c_mode == "Dual-Color Flash":
                            color = c1 if self.beat_intensity > 0.4 else c2
                        else:
                            color = get_gradient_color(c2, c1, p['ratio'])

                        alpha = max(0.1, min(1.0, float(p['life']) * (0.65 + self.beat_intensity * 0.35)))
                        r = max(1, int(p['base_size'] * (1.0 + self.beat_intensity * 0.25)))
                        draw_col = tuple(int(min(255, ch_c * alpha)) for ch_c in color)
                        cv2.circle(frame, (int(p['x']), int(p['y'])), r, draw_col, -1, cv2.LINE_AA)
                        alive_particles.append(p)

                    state['particles'] = alive_particles

        # 3. MODERN GLASSMORPHISM FLOATING CARD (Transparan Buram Elegan + Mode 5 Detik / Full)
        if cfg.get('use_floating_card', False):
            current_sec = self.frame_count / 30.0
            current_title = ""
            track_elapsed = 0.0
            card_mode = cfg.get('floating_card_mode', 'intro_5s')

            for track in cfg.get('track_schedule', []):
                if track['start'] <= current_sec < track['end']:
                    current_title = track['title']
                    track_elapsed = current_sec - track['start']
                    break

            if current_title:
                card_alpha = 1.0
                slide_offset = 0
                if card_mode == 'intro_5s':
                    if track_elapsed > 5.5:
                        card_alpha = 0.0
                    elif track_elapsed < 0.7:
                        card_alpha = track_elapsed / 0.7
                        slide_offset = int((1.0 - card_alpha) * -35)
                    elif track_elapsed > 4.5:
                        card_alpha = max(0.0, 1.0 - ((track_elapsed - 4.5) / 1.0))
                        slide_offset = int((1.0 - card_alpha) * -20)

                if card_alpha > 0.02:
                    font = cv2.FONT_HERSHEY_SIMPLEX
                    (tw_sub, _), _ = cv2.getTextSize("NOW PLAYING", font, 0.42, 1)
                    (tw_main, th_main), _ = cv2.getTextSize(current_title, font, 0.65, 2)

                    card_x = max(10, 36 + slide_offset)
                    card_y = 32
                    card_w = max(tw_sub, tw_main) + 92
                    card_h = 68
                    x2 = min(w - 10, card_x + card_w)
                    y2 = min(h - 10, card_y + card_h)
                    real_w, real_h = x2 - card_x, y2 - card_y

                    if real_w > 40 and real_h > 20:
                        # 1. Buat Mask Kapsul Membulat (Rounded Rectangle)
                        rad = 16
                        mask = np.zeros((real_h, real_w), dtype=np.uint8)
                        cv2.rectangle(mask, (rad, 0), (real_w - rad, real_h), 255, -1)
                        cv2.rectangle(mask, (0, rad), (real_w, real_h - rad), 255, -1)
                        for cx_r, cy_r in [(rad, rad), (real_w - rad, rad), (rad, real_h - rad), (real_w - rad, real_h - rad)]:
                            cv2.circle(mask, (cx_r, cy_r), rad, 255, -1, cv2.LINE_AA)

                        # 2. Efek Frosted Glass: Blur area video di balik kartu + Tint Kaca Gelap Transparan
                        roi = frame[card_y:y2, card_x:x2].copy()
                        blurred_roi = cv2.GaussianBlur(roi, (25, 25), 0)
                        glass_tint = np.full_like(roi, (32, 22, 14)) # BGR Dark Slate Glass
                        frosted = cv2.addWeighted(blurred_roi, 0.55, glass_tint, 0.45, 0)

                        # 3. Gambar Border Kaca Tipis, Mini Equalizer, & Teks di atas lapisan Frosted
                        border_canvas = frosted.copy()
                        cv2.rectangle(border_canvas, (rad, 0), (real_w - rad, 1), (235, 225, 210), -1)
                        cv2.rectangle(border_canvas, (rad, real_h - 2), (real_w - rad, real_h - 1), (180, 170, 150), -1)
                        frosted = cv2.addWeighted(frosted, 0.7, border_canvas, 0.3, 0)

                        # Mini Equalizer Bars di sisi kiri kartu (Bergerak mengikuti beat)
                        eq_base_x = 22
                        eq_base_y = real_h - 16
                        accent_bgr = (255, 212, 0) # Cyan khas KeiBot (#00d4ff dalam BGR)
                        for b in range(3):
                            wave = np.sin(t * 8.0 + b * 1.5) * 3.0
                            bh = int(8 + (self.beat_intensity * (18 if b == 1 else 12)) + wave)
                            bh = max(4, min(34, bh))
                            bx1 = eq_base_x + b * 9
                            cv2.rectangle(frosted, (bx1, eq_base_y - bh), (bx1 + 5, eq_base_y), accent_bgr, -1, cv2.LINE_AA)

                        # Teks "NOW PLAYING" & Judul Lagu
                        cv2.putText(frosted, "NOW PLAYING", (58, 25), font, 0.40, (255, 220, 100), 1, cv2.LINE_AA)
                        cv2.putText(frosted, current_title, (58, 50), font, 0.65, (250, 250, 250), 2, cv2.LINE_AA)

                        # 4. Gabungkan ke Frame Utama sesuai transparansi Fade-In / Fade-Out
                        blended_roi = cv2.addWeighted(roi, 1.0 - (card_alpha * 0.88), frosted, card_alpha * 0.88, 0)
                        mask_bool = mask > 0
                        roi[mask_bool] = blended_roi[mask_bool]
                        frame[card_y:y2, card_x:x2] = roi

        return frame

def hex_to_rgb(hex_color):
    """Mengubah kode warna Hex (#RRGGBB) menjadi tuple RGB (R, G, B)"""
    hex_color = str(hex_color).strip().lstrip('#')
    if len(hex_color) != 6:
        return (255, 255, 255) # Default putih jika format salah
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def render_video_core(task_id, audio_path, bg_paths, output_path, duration, cfg):
    # 🔥 1. RESOLUSI FHD 1080p (Lebih Tajam & Jernih) 🔥
    # 🔥 PERINTAH 3: RESOLUSI 720p 🔥
    w, h = 1280, 720; fps = 30; total_f = int(duration * fps)
    
    c_bot = hex_to_rgb(cfg.get('color_bot', '#10b981'))
    c_top = hex_to_rgb(cfg.get('color_top', '#0ea5e9'))
    c_part = hex_to_rgb(cfg.get('color_part', '#ffffff'))
    bar_c = int(cfg.get('bar_count', 64))
    
    vis = VisualEngine(c_bot, c_top, c_part)
    bg = BackgroundManager(bg_paths, w, h, fps=fps)
    audio = AudioBrain(); audio.load(audio_path)
    
    with db_lock:
        for d in active_tasks:
            if d['id'] == task_id:
                d['status'] = "Rendering 720p... ⚡"
                d['updated_at'] = time.time()  # 🔥 BARU: timestamp untuk watchdog stuck
                d['started_at'] = time.time() # 🔥 CATAT WAKTU START
    save_tasks_db()

    cmd = [
        get_ffmpeg_path(), '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(fps), 
        '-i', '-', 
        '-i', audio_path, 
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-threads', '0', '-pix_fmt', 'yuv420p', output_path
    ]
    
    partial_output = output_path + '.rendering.mp4'
    cmd[-1] = partial_output
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    active_processes[task_id] = proc
    
    try:
        last_ping = time.time()
        for f in range(total_f):
            if stop_flags.get(task_id):
                raise Exception("Dibatalkan")
                
            v, is_hit, bars = audio.get_data(f/fps, bar_c)
            frame = vis.process(bg.get_frame(), v, is_hit, bars, cfg)
            proc.stdin.write(frame.tobytes())
            
            # 🔥 PING WATCHDOG: Lapor ke pos satpam setiap 60 detik agar tidak ditebas
            now_t = time.time()
            if now_t - last_ping > 60:
                last_ping = now_t
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['updated_at'] = now_t
                            
    except BaseException:
        proc.kill()
        raise
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        result = proc.wait()
        bg.close()
        active_processes.pop(task_id, None)
        if result != 0 and os.path.exists(partial_output):
            os.remove(partial_output)
    if result != 0:
        raise RuntimeError('FFmpeg gagal merender video.')
    validate_video(partial_output)
    os.replace(partial_output, output_path)

# ==========================================
# 🚀 BACKGROUND WORKER (SUDAH DI-TUNE UP NGEBUT)
# ==========================================
def background_worker():
    global channel_cooldowns
    while True:
        task = None
        temp_cooldowns = []
        
        # 🔥 1. GALI ANTREAN: Cari task pertama yang AMAN dari cooldown
        while not render_queue.empty():
            try:
                ptask = queue_get()
            except queue.Empty:
                break
            pyt_id = ptask['yt_id']
            if stop_flags.get(ptask['id']):
                render_queue.task_done()
                stop_flags.pop(ptask['id'], None)
                continue
            
            # Jika ini adalah task VIP Live Base, abaikan cooldown upload YT (karena hanya merender lokal)
            if not ptask.get('is_live_base') and pyt_id in channel_cooldowns and time.time() < channel_cooldowns[pyt_id]:
                sisa_menit = max(1, int((channel_cooldowns[pyt_id] - time.time()) / 60))
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == ptask['id']:
                            d['status'] = f"Antrean Ditunda (Cooldown YT {sisa_menit} mnt) ⏳"
                            d['updated_at'] = time.time()
                temp_cooldowns.append(ptask)
            else:
                task = ptask
                break
        
        # 🔥 2. Kembalikan task yang kena cooldown tadi ke dalam antrean (tetap prioritas)
        if temp_cooldowns:
            save_tasks_db()
            for t in temp_cooldowns:
                queue_put(t)
                render_queue.task_done()
                
        # 🔥 3. Jika semua antrean ternyata sedang cooldown, istirahat sejenak
        if task is None:
            time.sleep(10)
            continue
            
        # --- MULAI PROSES TASK YANG AMAN ---
        task_id = task['id']
        yt_id = task['yt_id']
        ch_name = 'KeiBot FM'
        channel_data = next((c for c in database_channel if c['yt_id'] == yt_id), None)
        if channel_data:
            ch_name = channel_data.get('name', ch_name)
        
        if not task.get('is_live_base'):
            saved_upload = read_json(state_path(task_id, yt_id), {})
            if saved_upload.get('video_id'):
                video_id = saved_upload['video_id']
                move_to_history(task_id, f"Tayang! ✅ <a href='https://youtu.be/{video_id}' target='_blank'>[Lihat]</a>")
                render_queue.task_done()
                continue

        # Bersihkan status cooldown jika sudah waktunya kedaluwarsa
        if yt_id in channel_cooldowns and time.time() >= channel_cooldowns[yt_id]:
            del channel_cooldowns[yt_id]
            try: persist_cooldown(yt_id)
            except Exception: pass

        # ==========================================================
        # 🔥 PENANGANAN KHUSUS VIP LIVE BASE VIDEO (MESIN FACTORY)
        # ==========================================================
        if task.get('is_live_base'):
            output_override = task.get('output_override')
            temp_txt = os.path.join(BASE_UPLOAD, f"temp_live_audio_{task_id}.txt")
            merged_audio = os.path.join(BASE_UPLOAD, f"merged_audio_{task_id}.mp3")
            
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id:
                        d['status'] = "Merakit Base Video Live (VIP)... ⚡"
                        d['updated_at'] = time.time()
                        d['started_at'] = time.time()
            save_tasks_db()

            try:
                acquire_resource("Live Base Worker", cancelled=lambda: stop_flags.get(task_id))
                if stop_flags.get(task_id):
                    raise Exception("Dibatalkan oleh pengguna")

                try:
                    from live.routes import update_live_item
                    update_live_item(task_id, {
                        "status": "RENDERING_BASE",
                        "message": "⚡ Sedang merender Base Video dengan Visualizer Factory..."
                    })
                except Exception:
                    pass

                audios = task.get('selected_audios', [])
                bgs = task.get('selected_bgs', [])
                preset_name = task.get('vis_preset', '')

                track_schedule = []
                current_sec = 0.0
                valid_audios = 0

                with open(temp_txt, 'w', encoding='utf-8') as f:
                    for audio_name in audios:
                        p1 = os.path.join(BASE_UPLOAD, yt_id, 'audio_non_cid', audio_name)
                        ap = p1 if os.path.exists(p1) else os.path.join(BASE_UPLOAD, yt_id, 'audio_cid', audio_name)
                        if os.path.exists(ap):
                            safe_path = os.path.abspath(ap).replace('\\', '/').replace("'", "'\\''")
                            f.write(f"file '{safe_path}'\n")
                            valid_audios += 1
                            
                            probe_a = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', ap], capture_output=True, text=True)
                            try: dur_a = float(probe_a.stdout.strip())
                            except: dur_a = 0.0
                            
                            raw_title = os.path.splitext(os.path.basename(ap))[0]
                            clean_t = re.sub(r'^\d+[\.\)\-\s]+', '', raw_title)
                            clean_t = re.sub(r'\(.*?\)|\[.*?\]', '', clean_t).strip(' -_') or raw_title
                            track_schedule.append({'title': clean_t, 'path': safe_path, 'start': current_sec, 'end': current_sec + dur_a, 'duration': dur_a})
                            current_sec += dur_a

                if valid_audios == 0:
                    raise Exception("File audio yang dipilih tidak ditemukan di folder channel!")

                subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', temp_txt, '-c:a', 'libmp3lame', '-ar', '44100', merged_audio], check=True, capture_output=True)

                probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', merged_audio], capture_output=True, text=True, check=True)
                duration = float(probe.stdout.strip())

                cfg = get_random_preset([preset_name]) if preset_name else get_random_preset()
                if not isinstance(cfg, dict):
                    cfg = {"color_bot": "#00d4ff", "color_top": "#7c5cfc", "color_part": "#ffffff", "bar_count": 64}
                
                channel_data = next((c for c in database_channel if c['yt_id'] == yt_id), None)
                cfg['yt_id'] = yt_id
                cfg['use_floating_card'] = True
                cfg['floating_card_mode'] = 'intro_5s'
                cfg['track_schedule'] = track_schedule
                cfg['channel_name'] = channel_data['name'] if channel_data else "KeiBot Live"

                bg_paths_full = [os.path.join(BASE_UPLOAD, yt_id, 'backgrounds', b) for b in bgs if os.path.exists(os.path.join(BASE_UPLOAD, yt_id, 'backgrounds', b))]
                if not bg_paths_full:
                    bg_paths_full = get_multi_backgrounds(yt_id, count=1)
                if not bg_paths_full:
                    raise Exception("Background tidak ditemukan!")

                os.makedirs(os.path.dirname(output_override), exist_ok=True)

                render_video_core(task_id, merged_audio, bg_paths_full, output_override, duration, cfg)

                move_to_history(task_id, "Base Video Live Siap! ✅ (Menunggu Jadwal Live)")

                try:
                    from live.routes import update_live_item
                    update_live_item(task_id, {
                        "status": "READY_TO_LOCK",
                        "message": "✅ Base Video siap! Server bebas (Menunggu H-15 menit sebelum jam tayang)."
                    })
                except Exception:
                    pass

            except Exception as e:
                move_to_history(task_id, f"Gagal Base Live ❌ ({str(e)})")
                try:
                    from live.routes import update_live_item
                    update_live_item(task_id, {
                        "status": "ERROR",
                        "message": f"Gagal merakit Base Video: {str(e)}"
                    })
                except Exception:
                    pass
            finally:
                release_resource("Live Base Worker")
                for tf in (temp_txt, merged_audio):
                    try: os.remove(tf)
                    except: pass
                stop_flags.pop(task_id, None)
                active_processes.pop(task_id, None)
                render_queue.task_done()
            continue
        # ==========================================================
        # AKHIR BLOK KHUSUS VIP LIVE BASE VIDEO
        # ==========================================================
        
        with db_lock:
            for d in active_tasks:
                if d['id'] == task_id:
                    d['status'] = "Mulai Diproses... ⚙️"
                    d['updated_at'] = time.time()
        save_tasks_db()
        
        temp_files = [
            os.path.join(BASE_UPLOAD, f"temp_a_{task_id}.mp3"),
            os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt"),
            os.path.join(BASE_UPLOAD, f"temp_v_{task_id}.mp4"),
            os.path.join(BASE_UPLOAD, f"temp_mv_{task_id}.mp4"),
            os.path.join(BASE_UPLOAD, f"temp_sv_{task_id}.txt"),
            os.path.join(BASE_UPLOAD, f"loop_{task_id}.txt"),
            os.path.join(BASE_DIR, f"static/final_{task_id}.mp4"),
        ]
        try:
            # Pasang antrean resource gate di sini
            acquire_resource("Factory Worker", cancelled=lambda: stop_flags.get(task_id))
            if stop_flags.get(task_id): 
                raise Exception("Dibatalkan")
                
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id:
                        d['status'] = "Meracik Aset Gallery... ⚙️"
                        d['updated_at'] = time.time()  # 🔥 BARU: timestamp untuk watchdog stuck
            save_tasks_db()

            final_video = os.path.join(BASE_DIR, f"static/final_{task_id}.mp4")
            upload_record = read_json(state_path(task_id, yt_id), {})
            if not os.path.isfile(final_video) and upload_record.get('uri') and not upload_record.get('video_id'):
                raise RuntimeError('File sesi upload sebelumnya tidak ada; perlu rekonsiliasi sebelum upload ulang.')
            if not (os.path.isfile(final_video) and upload_record):
                # ============== AWAL BLOK SUMBER KONTEN (UPDATE) ==============
                source_mode = task.get('source_mode', 'mix')
                base_audio = os.path.join(BASE_UPLOAD, f"temp_a_{task_id}.mp3")
                track_schedule = []

                if source_mode == 'single_video':
                    # --- JALUR BARU: MODE VIDEO UTUH ---
                    with db_lock:
                        for d in active_tasks:
                            if d['id'] == task_id: d['status'] = "Mencari & Mengekstrak Audio dari Video... ⚙️"
                    save_tasks_db()

                    # Ambil dari folder khusus base_videos yang baru
                    base_videos_dir = get_channel_folder(yt_id, "base_videos")
                    video_paths = [os.path.join(base_videos_dir, f) for f in os.listdir(base_videos_dir) if f.lower().endswith(('.mp4', '.mov', '.webm', '.mkv'))]
                    if not video_paths: raise Exception("Gallery Video+Audio kosong! Upload MP4 sumber ke tab tersebut.")
                
                    # 🔥 BARU: Jumlah video acak per final video (default 1 = perilaku lama)
                    vid_req = int(task.get('vid_per_video', 1))
                    vid_req = max(1, min(vid_req, len(video_paths)))
                
                    # 🔥 BARU: Pilih N video acak BEDA untuk task ini (diacak ulang tiap render).
                    # Jadi kalau Final Video 30, masing-masing mendapat 5 video acak berbeda.
                    selected_videos = random.sample(video_paths, vid_req)
                    source_video = selected_videos[0]
                    bg_paths = [source_video] # Video pertama sebagai background utama
                
                    # 🔥 BARU: Concat video utuh (video + audio + subtitle burned jadi satu).
                    # Karena subtitle sudah di-render permanen di dalam MP4, video digabung
                    # tanpa re-encode (copy) agar kualitas & subtitle tidak berubah.
                    if vid_req > 1:
                        concat_txt = os.path.join(BASE_UPLOAD, f"temp_sv_{task_id}.txt")
                        with open(concat_txt, 'w', encoding='utf-8') as f:
                            for vp in selected_videos:
                                safe_path = os.path.abspath(vp).replace('\\', '/').replace("'", "'\\''")
                                f.write(f"file '{safe_path}'\n")
                        merged_video = os.path.join(BASE_UPLOAD, f"temp_mv_{task_id}.mp4")
                        try:
                            subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0',
                                            '-i', concat_txt, '-c', 'copy', merged_video],
                                           check=True, capture_output=True)
                        except subprocess.CalledProcessError:
                            raise Exception("Gagal menggabungkan video acak! Pastikan semua video MP4 punya format/kodek sejenis (misal resolusi & fps sama).")
                        try: os.remove(concat_txt)
                        except: pass
                    else:
                        merged_video = source_video # 1 video: pakai langsung tanpa concat
                
                    bg_paths = [merged_video]

                    # Ekstrak audio dari video gabungan ke temp_a.mp3 (untuk visualizer)
                    try:
                        subprocess.run([get_ffmpeg_path(), '-y', '-i', merged_video, '-vn', '-q:a', '0', base_audio], check=True, capture_output=True)
                    except subprocess.CalledProcessError:
                        raise Exception(f"Gagal ekstrak audio! Video gabungan mungkin tidak bersuara.")

                    probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', base_audio], capture_output=True, text=True, check=True)
                    try: base_duration_sec = float(probe.stdout.strip())
                    except: raise Exception("Gagal membaca durasi audio dari video utama!")

                    # 🔥 BARU: track_schedule berisi SEMUA video acak (urutan concat),
                    # supaya Floating Card berganti judul mengikuti nama file tiap segmen.
                    # Judul dibersihkan dari nomor depan, misal '06. Langkah Sabar.mp4' → 'Langkah Sabar'
                    current_sec = 0.0
                    for vp in selected_videos:
                        probe_vp = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', vp], capture_output=True, text=True)
                        try: dur_vp = float(probe_vp.stdout.strip())
                        except: dur_vp = 0.0
                        raw_title = os.path.splitext(os.path.basename(vp))[0]
                        title_vid = re.sub(r'^\d+[\.\)\-\s]*', '', raw_title).strip() if raw_title else raw_title
                        if not title_vid: title_vid = raw_title
                        track_schedule.append({'title': title_vid, 'path': os.path.abspath(vp), 'start': current_sec, 'end': current_sec + dur_vp, 'duration': dur_vp})
                        current_sec += dur_vp

                else:
                    # --- JALUR LAMA: MODE KLASIK (MIX MP3) ---
                    mp3_non = int(task.get('mp3_non_cid_count', 5))
                    mp3_cid = int(task.get('mp3_cid_count', 0))
                    selected_audios = get_mixed_audios(yt_id, mp3_non, mp3_cid)
                    if not selected_audios: raise Exception("Gallery Audio (Non-CID / CID) Kosong! Upload minimal 1 file.")

                    current_sec = 0.0
                    c_txt = os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt")
                
                    with open(c_txt, 'w', encoding='utf-8') as f:
                        for ap in selected_audios:
                            safe_path = os.path.abspath(ap).replace('\\', '/').replace("'", "'\\''")
                            f.write(f"file '{safe_path}'\n")
                            probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', ap], capture_output=True, text=True)
                            try: dur = float(probe.stdout.strip())
                            except: dur = 0.0
                        
                            # 🔥 PEMBERSIH JUDUL OTOMATIS (Audio MP3)
                            raw_title = os.path.splitext(os.path.basename(ap))[0]
                            title = re.sub(r'^\d+[\.\)\-\s]+', '', raw_title)
                            title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip(' -_')
                            if not title: title = raw_title
                        
                            track_schedule.append({'title': title, 'path': safe_path, 'start': current_sec, 'end': current_sec + dur, 'duration': dur})
                            current_sec += dur

                    subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', c_txt, '-c:a', 'libmp3lame', '-ar', '44100', base_audio], check=True)

                    probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', base_audio], capture_output=True, text=True, check=True)
                    base_duration_sec = float(probe.stdout.strip())
                
                    bg_count = int(task.get('bg_count', 1))
                    bg_paths = get_multi_backgrounds(yt_id, count=bg_count)
                    if not bg_paths: raise Exception("Gallery Background Kosong!")

                if base_duration_sec <= 0: raise Exception("Durasi sumber media tidak valid!")

                channel_data = next((c for c in database_channel if c['yt_id'] == yt_id), None)
                ch_name = channel_data['name'] if channel_data else "KeiBot FM"
                # ============== AKHIR BLOK SUMBER KONTEN (UPDATE) ==============

                preset = task.get('vis_preset')
                allowed_presets = task.get('vis_presets_allowed', [])
                if task.get('vis_mode') == 'random' or preset == 'random':
                    preset = get_random_preset(allowed_presets)
                if not isinstance(preset, dict):
                    preset = {"color_bot": "#00d4ff", "color_top": "#7c5cfc", "color_part": "#ffffff", 
                             "pos_x": 50, "pos_y": 85, "width_pct": 60, "max_height": 40, "idle_height": 5, 
                             "bar_count": 64, "reactivity": 0.66, "gravity": 0.08, "spacing": 3, 
                             "part_amount": 3, "part_speed": 1.0, "bar_style": "bottom"}

                preset['yt_id'] = yt_id 
                preset['use_floating_card'] = task.get('use_floating_card', False)
                preset['floating_card_mode'] = task.get('floating_card_mode', preset.get('floating_card_mode', 'intro_5s'))
                preset['track_schedule'] = track_schedule
                preset['channel_name'] = ch_name
                preset['source_mode'] = source_mode

                base_video = os.path.join(BASE_UPLOAD, f"temp_v_{task_id}.mp4")
                final_video = os.path.join(BASE_DIR, f"static/final_{task_id}.mp4")

                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = "Rendering Base FFmpeg... ⚡"
                save_tasks_db()

                render_video_core(task_id, base_audio, bg_paths, base_video, base_duration_sec, preset)
                if stop_flags.get(task_id): raise Exception("Dibatalkan")

                target_hours = float(task.get('target_duration_hours', 1))
                target_sec = target_hours * 3600
            
                loop_count = math.ceil(target_sec / base_duration_sec)

                if loop_count > 1:
                    with db_lock:
                        for d in active_tasks:
                            if d['id'] == task_id: d['status'] = f"Auto-Looping {loop_count}x ke {target_hours} Jam... 🚀"
                    save_tasks_db()

                    loop_txt = os.path.join(BASE_UPLOAD, f"loop_{task_id}.txt")
                    with open(loop_txt, 'w', encoding='utf-8') as f:
                        for _ in range(loop_count):
                            safe_path_vid = os.path.abspath(base_video).replace('\\', '/')
                            f.write(f"file '{safe_path_vid}'\n")

                    if stop_flags.get(task_id): raise Exception("Dibatalkan")
                
                    try:
                        # Kembali ke concat tapi dengan pengaman timestamp (+genpts)
                        subprocess.run([get_ffmpeg_path(), '-y', '-fflags', '+genpts', '-f', 'concat', '-safe', '0', '-i', loop_txt, '-c', 'copy', '-t', str(target_sec), final_video], check=True, capture_output=True, text=True)
                    except subprocess.CalledProcessError as e:
                        # Ambil 6 baris terakhir agar error aslinya tidak tertutup statistik ukuran
                        err_lines = e.stderr.strip().split('\n')
                        real_err = " | ".join(err_lines[-6:]) 
                        raise Exception(f"FFmpeg Concat Error: {real_err}")
                else:
                    if stop_flags.get(task_id): raise Exception("Dibatalkan")
                
                    try:
                        subprocess.run([get_ffmpeg_path(), '-y', '-i', base_video, '-c', 'copy', '-t', str(target_sec), final_video], check=True, capture_output=True, text=True)
                    except subprocess.CalledProcessError as e:
                        err_lines = e.stderr.strip().split('\n')
                        real_err = " | ".join(err_lines[-6:])
                        raise Exception(f"FFmpeg Final Error: {real_err}")

            if channel_data:
                creds_list = channel_data.get('creds_list', [channel_data.get('creds_json')])
                upload_berhasil = False
                pesan_error = "Token API Tidak Ditemukan/Kosong!" 
                
                for index_kunci, cred_str in enumerate(creds_list):
                    if not cred_str: continue
                    try:
                        creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                        if creds.expired and creds.refresh_token: 
                            creds.refresh(Request())
                            
                        youtube = build('youtube', 'v3', credentials=creds)
                        prior_upload = read_json(state_path(task_id, yt_id), {})
                        if prior_upload.get('uri') and prior_upload.get('credential_index', index_kunci) != index_kunci:
                            continue
                        try: sch_obj = datetime.strptime(task['publish_date'], "%Y-%m-%d %H:%M")
                        except: raise Exception("Format tanggal salah")
                        
                        raw_tags = task.get('tags', '')
                        clean_tags = raw_tags.replace('#', '').replace('<', '').replace('>', '').replace('"', '')
                        temp_tags = [t.strip() for t in clean_tags.split(',') if t.strip()]
                        
                        tags_list = []
                        char_count = 0
                        for t in temp_tags:
                            if char_count + len(t) <= 400:
                                tags_list.append(t)
                                char_count += len(t) + 1
                        
                        if not tags_list: tags_list = ['wavepush']
                        
                        body = {
                            'snippet': {'title': str(task['title'])[:100], 'description': task.get('description', ''), 'tags': tags_list, 'categoryId': '10'},
                            'status': {'privacyStatus': task.get('privacy', 'public')}
                        }
                        if sch_obj.replace(tzinfo=ZoneInfo("Asia/Jakarta")) > datetime.now(ZoneInfo("Asia/Jakarta")):
                            wib = ZoneInfo("Asia/Jakarta")
                            sch_aware = sch_obj.replace(tzinfo=wib)
                            sch_utc = sch_aware.astimezone(timezone.utc)
                            body['status']['publishAt'] = sch_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                            body['status']['privacyStatus'] = 'private'
                            
                        media = MediaFileUpload(final_video, chunksize=1024*1024*5, resumable=True)
                        req = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
                        durable_upload = DurableUpload(req, task_id, yt_id, final_video, credential_index=index_kunci)
                        resp = None
                        
                        max_retries = 5
                        retry_count = 0
                        
                        while resp is None:
                            if stop_flags.get(task_id): raise Exception("Dibatalkan")
                            try:
                                status, resp = durable_upload.next_chunk()
                                if status:
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id:
                                                d['status'] = f"Mengunggah (Key {index_kunci+1})... {int(status.progress()*100)}% 🚀"
                                                d['updated_at'] = time.time()
                                    save_tasks_db()
                                retry_count = 0 
                            except HttpError as e:
                                if e.resp.status < 500:
                                    raise e
                                else:
                                    retry_count += 1
                                    if retry_count > max_retries: 
                                        raise Exception("Server YouTube Down/Timeout setelah 5x percobaan.")
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['status'] = f"Koneksi Sinyal Lemah, Auto-Retry ({retry_count}/{max_retries})... 🔌"
                                    save_tasks_db()
                                    time.sleep(10)
                            except Exception as e:
                                retry_count += 1
                                if retry_count > max_retries: 
                                    raise Exception("Koneksi VPS Putus setelah dicoba 5x berturut-turut.")
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id: d['status'] = f"Koneksi VPS Putus, Auto-Retry ({retry_count}/{max_retries})... 🔌"
                                save_tasks_db()
                                time.sleep(10)
                        
                        video_id = resp.get('id')
                        
                        thumb_path = get_and_consume_thumbnail(yt_id)
                        if thumb_path and os.path.exists(thumb_path):
                            try:
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id: d['status'] = "Memasang Thumbnail... 🖼️"
                                save_tasks_db()
                                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id: d['thumb_status'] = "Terpasang ✓"
                                # 🔥 UPDATE: file TIDAK dihapus lagi — thumbnail dipilih acak tiap video,
                                # jadi file bisa dipakai ulang oleh video berikutnya
                            except Exception as thumb_err:
                                # 🔥 BARU: error thumbnail kini tercatat di Task Monitor, tidak lagi diam
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id: d['thumb_status'] = f"Gagal: {str(thumb_err)[:80]}"
                                save_tasks_db()
                        else:
                            # 🔥 BARU: kalau folder kosong / semua gambar < 1280x720, catat status
                            with db_lock:
                                for d in active_tasks:
                                    if d['id'] == task_id: d['thumb_status'] = "Tidak ada thumbnail valid (min 1280×720)"
                            save_tasks_db()
                                
                        try:
                            if task.get('playlist_id'):
                                youtube.playlistItems().insert(part='snippet', body={'snippet': {'playlistId': task['playlist_id'], 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()
                        except: pass
                        move_to_history(task_id, f"Tayang! ✅ <a href='https://youtu.be/{video_id}' target='_blank'>[Lihat]</a>")
                        notify_event("✅", task['title'], f"Tayang di YouTube {ch_name}\nJadwal tayang: {task['publish_date']}")
                        upload_berhasil = True
                        break
                        
                    except HttpError as e:
                        try:
                            err_info = json.loads(e.content.decode('utf-8'))
                            reason = err_info['error']['errors'][0]['reason']
                        except:
                            reason = str(e)
                            
                        if "quotaExceeded" in reason:
                            pesan_error = f"Limit Kuota Harian API Habis!"
                            continue 
                        elif "uploadLimitExceeded" in reason:
                            pesan_error = "Limit Upload Harian Channel Tercapai!"
                            channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                            try: persist_cooldown(yt_id)
                            except Exception: pass
                            break
                        elif "rateLimitExceeded" in reason:
                            pesan_error = "Rate Limit (Terlalu Cepat) - Auto Cooldown 30 Menit"
                            channel_cooldowns[yt_id] = time.time() + 1800
                            try: persist_cooldown(yt_id)
                            except Exception: pass
                            break
                        else:
                            pesan_error = f"Ditolak YT: {reason}"
                            break
                    except Exception as e:
                        err_str = str(e).lower()
                        if "invalid_grant" in err_str or "expired" in err_str or "revoked" in err_str:
                            pesan_error = "Sesi Kedaluwarsa (Tautkan Ulang!)"
                            channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                            try: persist_cooldown(yt_id)
                            except Exception: pass
                        elif "timeout" in err_str or "connection" in err_str or "broken" in err_str:
                            pesan_error = "Koneksi VPS Putus/Timeout"
                        else:
                            pesan_error = f"Error: {str(e)[:40]}"
                        break
                        
                if not upload_berhasil:
                    if "API Habis" in pesan_error:
                        channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                        try: persist_cooldown(yt_id)
                        except Exception: pass
                    raise Exception(pesan_error)
            else:
                move_to_history(task_id, f"Render Selesai ✅ <a href='/static/final_{task_id}.mp4' target='_blank'>[Download]</a>")
                notify_event("✅", task['title'], f"Render selesai, di-upload ke YouTube {ch_name}")
        
        except Exception as e:
            err_msg = str(e)
            if "Limit" in err_msg or "Cooldown" in err_msg or "Habis" in err_msg:
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['status'] = f"Gagal Upload, Antre Ulang ({err_msg}) ⏳"
                save_tasks_db()
                queue_put(task)
            else:
                move_to_history(task_id, f"Gagal ❌ ({err_msg})")
                # 🔥 FIX: kalau digagalkan watchdog (stop flag), JANGAN kirim
                # notif gagal lagi — watchdog sudah mengirim notifikasinya sendiri.
                if not stop_flags.get(task_id):
                    notify_event("❌", task.get('title', 'Factory Video'), f"Gagal upload di channel {ch_name}\nPenyebab: {err_msg[:120]}")
        finally:
            # Lepas antrean agar Clipper bisa jalan
            release_resource("Factory Worker")
            
            for path in temp_files:
                if path == os.path.join(BASE_DIR, f"static/final_{task_id}.mp4") and has_pending_upload(task_id, yt_id):
                    continue
                try: os.remove(path)
                except: pass
            stop_flags.pop(task_id, None)
            active_processes.pop(task_id, None)
            render_queue.task_done()



# ==========================================
# 📊 API ENDPOINTS (LANJUTAN)
# ==========================================
@app.route('/')
def main_hub_landing():
    # Halaman pertama yang terbuka saat IP diakses
    return render_template('hub.html')

@app.route('/factory')
def factory_main_dashboard():
    # Dashboard Factory
    return render_template('index.html')

@app.route('/clipper')
def clipper_main_dashboard():
    # Dashboard Clipper
    return render_template('clipper.html')

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    return jsonify(system_notifications)

@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    global system_notifications
    with db_lock:
        system_notifications.clear()
    return jsonify({"status": "success"})

@app.route('/api/get_dashboard_stats')
def get_dashboard_stats():
    sys = get_system_stats()
    return jsonify({
        "channels": len(database_channel), "active_tasks": len(active_tasks), "history_tasks": len(history_tasks),
        "sys_cpu": sys["cpu"], "sys_ram_pct": sys["ram_pct"], "sys_ram_text": f"{sys['ram_used']}GB / {sys['ram_total']}GB"
    })

@app.route('/api/get_youtube_analytics')
def get_youtube_analytics():
    data = []
    for c in database_channel:
        views, subs, videos = 0, 0, 0
        try:
            creds_list = c.get('creds_list', [c.get('creds_json')])
            if creds_list and creds_list[0]:
                creds = Credentials.from_authorized_user_info(json.loads(creds_list[0]))
                if creds.expired and creds.refresh_token: creds.refresh(Request())
                youtube = build('youtube', 'v3', credentials=creds)
                res = youtube.channels().list(part="statistics", id=c['yt_id']).execute()
                if res.get('items'):
                    stats = res['items'][0]['statistics']
                    views = int(stats.get('viewCount', 0))
                    subs = int(stats.get('subscriberCount', 0))
                    videos = int(stats.get('videoCount', 0))
        except Exception as e:
            pass
        data.append({"yt_id": c["yt_id"], "name": c["name"], "views": views, "subs": subs, "watch_hours": 0, "videos": videos})
    return jsonify(data)

@app.route('/api/get_schedule')
def get_schedule(): return jsonify({"active": active_tasks, "history": history_tasks})

@app.route('/api/recover_failed_tasks', methods=['GET', 'POST'])
def recover_failed_tasks():
    # 🔥 BARU: memulihkan task yang digagalkan watchdog (belum diproses)
    # dari history kembali ke antrean aktif — jadwal tayang & judul tetap utuh.
    global active_tasks, history_tasks
    recovered = []
    with db_lock:
        still_needed = []
        for t in history_tasks:
            bp = t.get('blueprint')
            if bp and "Stuck" in t.get('status', ''):
                # 🔥 FIX: Pakaikan pelindung "Menunggu" saat dipulihkan
                t['status'] = "Menunggu Giliran (Queue) ⏳ (Dipulihkan)"
                t['updated_at'] = time.time()
                active_tasks.append(t)
                queue_put(bp)   # masuk mesin render beneran
                recovered.append(t.get('title', '?')[:60])
            else:
                still_needed.append(t)
        history_tasks = still_needed
        active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
        save_tasks_db()
    for r in recovered:
        pass
    if recovered:
        send_tg_message("🔄 *Task dipulihkan*\n" + "\n".join("• " + r for r in recovered[:10]) +
                        ("\n…dst" if len(recovered) > 10 else "") + "\nTask kembali ke antrean render.")
    return jsonify({"status": "success", "recovered": len(recovered),
                    "titles": recovered})

@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    global history_tasks
    with db_lock: history_tasks.clear()
    save_tasks_db()
    
    from shorts.queue_worker import update_db as update_shorts
    from clipper.queue_worker import update_db as update_clips
    for update in (update_shorts, update_clips):
        update(lambda jobs: [job for job in jobs if not task_terminal(job)])

    return jsonify({"status": "success", "message": "Semua riwayat (Factory, Shorts, Clipper) dibersihkan!"})

@app.route('/api/get_channels')
def get_channels():
    safe_c = [{"id": c["id"], "name": c["name"], "yt_id": c["yt_id"], "thumbnail": c["thumbnail"], "status": c["status"], "title_bank": c.get("title_bank", [])} for c in database_channel]
    return jsonify(safe_c)

from flask import send_file # Pastikan send_file di-import di baris paling atas jika belum ada

@app.route('/api/export_channels', methods=['GET'])
def export_channels():
    """Mengunduh database channel saat ini sebagai file JSON."""
    if not os.path.exists(DB_FILE):
        return jsonify({"status": "error", "message": "Database belum terbentuk."}), 404
    
    timestamp = int(time.time())
    return send_file(DB_FILE, as_attachment=True, download_name=f"keibot_channels_backup_{timestamp}.json")

@app.route('/api/import_channels', methods=['POST'])
def import_channels():
    """Mengunggah dan menimpa database channel dengan file JSON cadangan."""
    global database_channel
    file = request.files.get('file')
    
    if not file or not file.filename.endswith('.json'):
        return jsonify({"status": "error", "message": "File tidak valid! Harus berformat .json"}), 400
    
    try:
        new_data = json.load(file)
        if not isinstance(new_data, list):
            return jsonify({"status": "error", "message": "Format file rusak. Data channel harus berupa List/Array."}), 400
            
        required = ('id', 'name', 'yt_id', 'thumbnail', 'status')
        if any(not isinstance(c, dict) or any(k not in c for k in required) for c in new_data):
            return jsonify({'status': 'error', 'message': 'Struktur channel cadangan tidak lengkap.'}), 400
        ids = [c['yt_id'] for c in new_data]
        if len(ids) != len(set(ids)) or any(not isinstance(v, str) or not v or os.path.basename(v) != v or v in ('.', '..') for v in ids):
            return jsonify({'status': 'error', 'message': 'ID channel cadangan tidak valid/duplikat.'}), 400
        with db_lock:
            if any(task_channel_id(t) not in ids for t in active_tasks if not task_terminal(t)):
                return jsonify({'status': 'error', 'message': 'Cadangan tidak mencakup channel antrean aktif.'}), 409
            database_channel = new_data
            # Langsung simpan secara permanen menimpa channels_db.json lama
            save_channels(database_channel)
            
        return jsonify({"status": "success", "message": f"{len(database_channel)} channel berhasil diimpor!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal membaca file: {str(e)}"}), 500

@app.route('/api/delete_channel', methods=['POST'])
def delete_channel():
    yt_id = request.form.get('yt_id')
    global database_channel, active_tasks, history_tasks
    try:
        gallery_path = channel_gallery_path(yt_id)
    except ValueError as error:
        return jsonify({'status': 'error', 'message': str(error)}), 400
    
    # 1. Batalkan semua antreannya di background
    with db_lock:
        tasks_to_remove = [t for t in active_tasks if task_channel_id(t) == yt_id]
        for t in tasks_to_remove:
            task_id = t['id']
            if task_id in active_processes:
                proc = active_processes[task_id]
                if proc and proc.poll() is None:
                    kill_process_tree(proc.pid)
            stop_flags[task_id] = True
            t['status'] = "Dibatalkan (Channel Dihapus) 🗑️"
            active_tasks.remove(t)
            history_tasks.insert(0, t)
            
        remove_queued_tasks(lambda item: task_channel_id(item) == yt_id)
    save_tasks_db()

    from shorts.queue_worker import cancel_jobs as cancel_shorts
    from clipper.queue_worker import cancel_jobs as cancel_clips
    cancel_shorts(yt_id=yt_id)
    cancel_clips(yt_id=yt_id)

    # 2. Sapu bersih folder & fisik dari Gallery VPS
    gallery_path = channel_gallery_path(yt_id)
    if os.path.exists(gallery_path):
        import shutil
        shutil.rmtree(gallery_path, ignore_errors=True)

    # 3. Hapus profil channel dari Database
    database_channel = [c for c in database_channel if c['yt_id'] != yt_id]
    save_channels(database_channel)
    
    return jsonify({"status": "success", "message": "Channel, aset Gallery, dan antrean berhasil disapu bersih!"})

# --- PRESET API ---
@app.route('/api/save_preset', methods=['POST'])
def save_preset():
    data = request.json
    try:
        presets = {}
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                try: presets = json.load(f)
                except: pass
        presets.update(data)
        with open(PRESETS_FILE, 'w') as f: json.dump(presets, f, indent=4)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@app.route('/api/get_presets', methods=['GET'])
def get_presets():
    if os.path.exists(PRESETS_FILE):
        with open(PRESETS_FILE, 'r') as f:
            try: 
                return jsonify(json.load(f))
            except: 
                pass
    return jsonify({})

@app.route('/api/delete_preset', methods=['POST'])
def delete_preset():
    data = request.json
    preset_name = data.get('name')
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                presets = json.load(f)
                
            if preset_name in presets:
                del presets[preset_name]
                
                with open(PRESETS_FILE, 'w') as f: 
                    json.dump(presets, f, indent=4)
                    
                return jsonify({"status": "success"})
                
        return jsonify({"status": "error", "message": "Preset tidak ditemukan"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ============================================================
# 🖼️ GALLERY ENDPOINTS
# ============================================================
@app.route('/api/get_asset_counts')
def get_asset_counts():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify({"audio_non_cid": 0, "audio_cid": 0, "backgrounds": 0, "shorts_bg": 0, "thumbnails": 0, "base_videos": 0})
    def count_files(sub):
        path = get_channel_folder(yt_id, sub)
        return len([f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))])
    return jsonify({
        "audio_non_cid": count_files("audio_non_cid"), 
        "audio_cid": count_files("audio_cid"), 
        "backgrounds": count_files("backgrounds"), 
        "shorts_bg": count_files("shorts_bg"),
        "shorts_thumb": count_files("shorts_thumb"),
        "thumbnails": count_files("thumbnails"),
        "base_videos": count_files("base_videos")
    })

@app.route('/api/get_gallery', methods=['GET'])
def get_gallery():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify({"audio": [], "background": [], "thumbnails": [], "base_video": []})
    def get_files_data(sub):
        path = get_channel_folder(yt_id, sub)
        res = []
        if os.path.exists(path):
            for f in os.listdir(path):
                fp = os.path.join(path, f)
                if os.path.isfile(fp):
                    size_mb = round(os.path.getsize(fp) / (1024*1024), 2)
                    item = {"name": f, "size": f"{size_mb} MB"}
                    ext = f.rsplit('.', 1)[-1].lower() if '.' in f else ''
                    if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
                        item['url'] = f"/uploads/{yt_id}/{sub}/{f}"
                    elif ext in ('mp4', 'webm', 'mov'):
                        item['url'] = f"/uploads/{yt_id}/{sub}/{f}"
                    res.append(item)
        return res
    return jsonify({
        "audio_non_cid": get_files_data("audio_non_cid"),
        "audio_cid":     get_files_data("audio_cid"),
        "background":    get_files_data("backgrounds"),
        "shorts_bg":     get_files_data("shorts_bg"),
        "shorts_thumb":  get_files_data("shorts_thumb"),
        "thumbnails":    get_files_data("thumbnails"),
        "base_video":    get_files_data("base_videos")
    })

@app.route('/api/upload_gallery', methods=['POST'])
def upload_gallery():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()

    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id tidak boleh kosong!"}), 400
    if not g_type:
        return jsonify({"status": "error", "message": "type tidak boleh kosong!"}), 400

    folder_name = resolve_folder(g_type)
    folder      = get_channel_folder(yt_id, folder_name)

    files = (request.files.getlist('files[]')
             or request.files.getlist('files')
             or request.files.getlist('file')
             or list(request.files.values()))

    if not files:
        return jsonify({"status": "error", "message": "Tidak ada file yang diterima!"}), 400

    saved, errors = 0, []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            safe_name = os.path.basename(f.filename)
            dest = os.path.join(folder, safe_name)
            f.save(dest)
            saved += 1
        except Exception as e:
            errors.append(f"{f.filename}: {str(e)}")

    if saved == 0:
        return jsonify({"status": "error", "message": "Tidak ada file yang berhasil disimpan. " + "; ".join(errors)}), 500

    msg = f"{saved} file berhasil diupload ke '{folder_name}'"
    if errors:
        msg += f" ({len(errors)} gagal: {'; '.join(errors[:3])})"
    return jsonify({"status": "success", "message": msg})

@app.route('/api/delete_gallery_file', methods=['POST'])
def delete_gallery_file():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()
    name   = request.form.get('name',  '').strip()

    folder_name = resolve_folder(g_type)
    path = os.path.join(get_channel_folder(yt_id, folder_name), os.path.basename(name))

    if os.path.exists(path):
        os.remove(path)
        return jsonify({"status": "success", "message": "File dihapus!"})
    return jsonify({"status": "error", "message": f"File tidak ditemukan: {path}"})

@app.route('/api/clear_gallery_folder', methods=['POST'])
def clear_gallery_folder():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()

    folder_name = resolve_folder(g_type)
    path = get_channel_folder(yt_id, folder_name)

    deleted = 0
    if os.path.exists(path):
        for f in os.listdir(path):
            fp = os.path.join(path, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    deleted += 1
                except: pass

    return jsonify({"status": "success", "message": f"{deleted} file berhasil disapu bersih!"})

# ============================================================
# 📝 TITLE BANK ENDPOINT
# ============================================================
@app.route('/api/upload_title_bank', methods=['POST'])
def upload_title_bank():
    yt_id = (request.form.get('yt_id') or request.args.get('yt_id') or '').strip()
    txt_file = request.files.get('txt_file') or request.files.get('file')

    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id tidak ditemukan. Pastikan channel sudah dipilih."}), 400
    if not txt_file:
        return jsonify({"status": "error", "message": "File .txt tidak ditemukan dalam request."}), 400

    try:
        raw_bytes = txt_file.read()
        try:   content = raw_bytes.decode('utf-8')
        except: content = raw_bytes.decode('latin-1', errors='ignore')

        lines = [line.strip() for line in content.split('\n') if line.strip()]
        global database_channel

        # 🔥 FIX: file .txt kosong = perintah CLEAR Title Bank (hapus semua judul),
        # bukan error. Kalau di-reject sebagai error, bank tidak pernah dihapus
        # dari database -> judul muncul lagi setelah refresh.
        if not lines:
            for c in database_channel:
                if c['yt_id'] == yt_id:
                    c['title_bank'] = []
                    save_channels(database_channel)
                    return jsonify({"status": "success", "message": "Title Bank dikosongkan!", "total": 0})
            return jsonify({"status": "error", "message": f"Channel dengan yt_id '{yt_id}' tidak ditemukan di database."}), 404

        channel_found = False
        for c in database_channel:
            if c['yt_id'] == yt_id:
                existing = c.get('title_bank', [])
                merged   = list(dict.fromkeys(existing + lines))
                c['title_bank'] = merged
                channel_found = True
                save_channels(database_channel)
                return jsonify({
                    "status":  "success",
                    "message": f"{len(lines)} judul diimport! Total bank: {len(merged)} judul.",
                    "total":   len(merged),
                })

        if not channel_found:
            return jsonify({"status": "error", "message": f"Channel dengan yt_id '{yt_id}' tidak ditemukan di database."}), 404

    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal memproses file: {str(e)}"}), 500

@app.route('/api/get_playlists', methods=['GET'])
def get_playlists():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify([])
    channel = next((c for c in database_channel if c['yt_id'] == yt_id), None)
    if not channel: return jsonify([])
    try:
        creds = get_fresh_credentials(channel)
        youtube = build('youtube', 'v3', credentials=creds)
        res = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        return jsonify([{"id": p['id'], "title": p['snippet']['title']} for p in res.get('items', [])])
    except: return jsonify([])

# 🛠️ FUNGSI BANTUAN: Membunuh proses FFmpeg/Worker sampai ke akar
def kill_process_tree(pid):
    try:
        import psutil
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except Exception:
        pass

@app.route('/api/stop_task/<task_id>', methods=['POST'])
def stop_task(task_id):
    # Coba konversi ke integer untuk Factory, biarkan string untuk Shorts
    try: fac_id = int(task_id)
    except: fac_id = task_id
    
    # 1. Stop Factory
    stop_flags[fac_id] = True
    proc = active_processes.pop(fac_id, None)
    if proc and proc.poll() is None:
        kill_process_tree(proc.pid)

    NOT_YET_RUNNING_MARKERS = ("In Factory Queue", "Antrean Ditunda", "Menunggu Giliran", "Dijadwalkan")
    with db_lock:
        target = next((t for t in active_tasks if str(t['id']) == str(task_id)), None)
    if target and any(m in target['status'] for m in NOT_YET_RUNNING_MARKERS):
        remove_queued_tasks(lambda bp: str(bp["id"]) == str(task_id))
        move_to_history(fac_id, "Dibatalkan (Manual) 🛑")
    else:
        save_tasks_db()
        
    from shorts.queue_worker import cancel_jobs as cancel_shorts
    from clipper.queue_worker import cancel_jobs as cancel_clips
    cancel_shorts(job_id=task_id)
    cancel_clips(job_id=task_id)

    return jsonify({"status": "success", "message": "Task berhasil dibatalkan/dihentikan paksa!"})

@app.route('/api/stop_channel/<yt_id>', methods=['POST'])
def stop_channel(yt_id):
    global active_tasks, history_tasks
    # 1. Stop Factory
    with db_lock:
        tasks_to_remove = [t for t in active_tasks if task_channel_id(t) == yt_id]
        for t in tasks_to_remove:
            task_id = t['id']
            if task_id in active_processes:
                proc = active_processes[task_id]
                if proc and proc.poll() is None:
                    kill_process_tree(proc.pid)
            stop_flags[task_id] = True
            t['status'] = "Dibatalkan (Stop Channel) 🛑"
            active_tasks.remove(t)
            history_tasks.insert(0, t)
            
        remove_queued_tasks(lambda item: task_channel_id(item) == yt_id)
    save_tasks_db()
    
    from shorts.queue_worker import cancel_jobs as cancel_shorts
    from clipper.queue_worker import cancel_jobs as cancel_clips
    cancel_shorts(yt_id=yt_id)
    cancel_clips(yt_id=yt_id)

    return jsonify({"status": "success", "message": "Semua antrean channel dihentikan!"})

@app.route('/api/check_secret')
def check_secret():
    try: return jsonify({"exists": os.path.exists(CLIENT_SECRETS_FILE)})
    except: return jsonify({"exists": False})

@app.route('/api/upload_secret', methods=['POST'])
def upload_secret():
    try:
        file = request.files.get('secret_file')
        if file and file.filename.endswith('.json'):
            file.save(CLIENT_SECRETS_FILE)
            return jsonify({"status": "success", "message": "API Key diunggah!"})
        return jsonify({"status": "error", "message": "Harus .json!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Izin ditolak server: {str(e)}"})

@app.route('/api/generate_tv_link')
def generate_tv_link():
    if not os.path.exists(CLIENT_SECRETS_FILE): return jsonify({"auth_url": "", "error": "File client_secret.json belum ada!"})
    return jsonify({"auth_url": f"http://{request.host}/device_login"})

@app.route('/device_login')
def device_login():
    if not os.path.exists(CLIENT_SECRETS_FILE): return "File rahasia tidak ditemukan!"
    with open(CLIENT_SECRETS_FILE, 'r') as f:
        secret_data = json.load(f); client_config = secret_data.get('installed', secret_data.get('web', {})); client_id = client_config.get('client_id')
    res = requests.post('https://oauth2.googleapis.com/device/code', data={'client_id': client_id, 'scope': ' '.join(SCOPES)}).json()
    if 'error' in res: return f"Error Google: {res['error']}"
    html = f"""
    <html><head><title>Aktivasi YouTube</title>
    <style>
        body {{ font-family: 'Segoe UI', Arial; text-align: center; background: #eef2f6; color: #1e293b; padding-top: 10vh; }}
        .box {{ background: #ffffff; width: 550px; margin: auto; padding: 40px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        .step {{ text-align: left; margin-bottom: 25px; font-size: 14px; color: #64748b; font-weight:600; }}
        .input-group {{ display: flex; margin-top: 10px; }}
        .input-group input {{ flex: 1; padding: 15px; font-size: 16px; font-weight: bold; background: #f8fafc; color: #10b981; border: 1px solid #e2e8f0; border-radius: 8px 0 0 8px; text-align: center; outline:none; }}
        .input-group button {{ padding: 15px 25px; font-size: 14px; font-weight: bold; background: #10b981; color: white; border: none; border-radius: 0 8px 8px 0; cursor: pointer; transition: 0.3s; }}
    </style></head><body>
        <div class="box">
            <h2 style="margin-top:0;">🔗 Tautkan Channel Baru</h2>
            <div class="step"><b>Langkah 1:</b> Copy link ini dan Paste di browser target:
                <div class="input-group"><input type="text" id="glink" value="{res['verification_url']}" readonly><button onclick="document.getElementById('glink').select();document.execCommand('copy');">Copy Link</button></div>
            </div>
            <div class="step"><b>Langkah 2:</b> Masukkan Kode Rahasia ini:
                <div class="input-group"><input type="text" id="gcode" value="{res['user_code']}" readonly><button onclick="document.getElementById('gcode').select();document.execCommand('copy');">Copy Kode</button></div>
            </div>
            <div id="status" style="margin-top:30px; font-weight:bold;">⏳ Menunggu Anda memasukkan kode...</div>
        </div>
        <script>
            function poll() {{ fetch('/api/poll_device_token', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{device_code: '{res['device_code']}'}}) }}).then(r => r.json()).then(data => {{ if(data.status === 'success') {{ document.getElementById('status').innerHTML = "🎉 Berhasil! Mengalihkan..."; setTimeout(() => {{ window.location.href = '/'; }}, 2000); }} else if(data.status === 'pending') {{ setTimeout(poll, data.interval || 5000); }} }}); }}
            setTimeout(poll, 5000);
        </script>
    </body></html>
    """
    return html

@app.route('/api/poll_device_token', methods=['POST'])
def poll_device_token():
    device_code = request.json.get('device_code')
    with open(CLIENT_SECRETS_FILE, 'r') as f:
        s_data = json.load(f); conf = s_data.get('installed', s_data.get('web', {})); c_id = conf.get('client_id'); c_sec = conf.get('client_secret')
    res = requests.post('https://oauth2.googleapis.com/token', data={'client_id': c_id, 'client_secret': c_sec, 'device_code': device_code, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}).json()
    if 'error' in res:
        err = res['error']
        if err == 'authorization_pending': return jsonify({"status": "pending", "interval": 5000})
        elif err == 'slow_down': return jsonify({"status": "pending", "interval": 10000})
        else: return jsonify({"status": "error", "error": err})
    creds = Credentials(token=res['access_token'], refresh_token=res.get('refresh_token'), token_uri='https://oauth2.googleapis.com/token', client_id=c_id, client_secret=c_sec, scopes=SCOPES)
    youtube = build('youtube', 'v3', credentials=creds); chan_res = youtube.channels().list(part="snippet", mine=True).execute()
    if chan_res['items']:
        item = chan_res['items'][0]; global database_channel
        c_idx = next((i for i, c in enumerate(database_channel) if c['yt_id'] == item['id']), None)
        if c_idx is None:
            new_c = {"id": len(database_channel)+1, "name": item['snippet']['title'], "yt_id": item['id'], "thumbnail": item['snippet']['thumbnails']['default']['url'], "status": "Connected 🟢 (1 Key)", "creds_list": [creds.to_json()]}
            database_channel.append(new_c)
        else:
            if 'creds_list' not in database_channel[c_idx]:
                database_channel[c_idx]['creds_list'] = [database_channel[c_idx].get('creds_json', '')]
            if creds.to_json() not in database_channel[c_idx]['creds_list']:
                database_channel[c_idx]['creds_list'].append(creds.to_json())
            database_channel[c_idx]['status'] = f"Connected 🟢 ({len(database_channel[c_idx]['creds_list'])} Keys)"
        save_channels(database_channel)
    return jsonify({"status": "success"})

# --- BATCH CREATOR ---
@app.route('/api/batch_create', methods=['POST'])
def batch_create():
    data = request.json
    yt_id = data.get('yt_id')
    count = data.get('count', 1)
    
    titles = data.get('generated_titles', [])
    # 🔥 TAMBAHKAN BARIS INI UNTUK MENANGKAP DESKRIPSI ACAK 🔥
    descs = data.get('generated_descriptions', []) 
    
    durations_array = data.get('target_durations_array', [])
    
    try:
        base_date = datetime.strptime(data['start_date'], '%Y-%m-%dT%H:%M')
    except:
        return jsonify({"status": "error", "message": "Format tanggal salah"}), 400
        
    interval_days = float(data.get('interval_days', 1))
    publish_dates = data.get('generated_publish_dates', [])

    if publish_dates and len(publish_dates) > 0:
        last_date_str = publish_dates[-1]
    else:
        final_v_date = base_date + timedelta(days=(count - 1) * interval_days)
        last_date_str = final_v_date.strftime('%Y-%m-%d %H:%M')
        
    with db_lock:
        for c in database_channel:
            if c['yt_id'] == yt_id:
                c['last_scheduled_date'] = last_date_str
                c['reminder_sent'] = False  
                break
        save_channels(database_channel)
        
    for i in range(count):
        t_id = secrets.randbits(52)
        
        if publish_dates and i < len(publish_dates):
            v_date_str = publish_dates[i]
        else:
            v_date = base_date + timedelta(days=i * interval_days)
            v_date_str = v_date.strftime('%Y-%m-%d %H:%M')
        
        if i < len(durations_array):
            vid_duration = durations_array[i]
        else:
            vid_duration = data.get('target_duration_hours', 1)
            
        blueprint = {
            "id": t_id, "yt_id": yt_id, "title": titles[i] if i < len(titles) else f"Auto Video #{i+1}",
            "publish_date": v_date_str,
            "mp3_non_cid_count": data.get('mp3_non_cid_count', 5), 
            "mp3_cid_count": data.get('mp3_cid_count', 0), 
            "vid_per_video": data.get('vid_per_video', 1), 
            "bg_count": data.get('bg_count', 1), 
            "target_duration_hours": vid_duration,
            "vis_mode": data.get('vis_mode'), "vis_preset": data.get('vis_preset'),
            "vis_presets_allowed": data.get('vis_presets_allowed', []), 
            
            # 🔥 UBAH BARIS DESKRIPSI INI 🔥
            "description": descs[i] if i < len(descs) else data.get('description', ''), 
            
            "tags": data.get('tags', ''), "privacy": data.get('privacy', 'public'), "playlist_id": data.get('playlist_id', ''),
            "use_floating_card": data.get('use_floating_card', False),
            "floating_card_mode": data.get('floating_card_mode', 'intro_5s'),
            "source_mode": data.get('source_mode', 'mix')  # 🔥 BARU: simpan jalur render engine di blueprint
        }
        with db_lock:
            active_tasks.append({
                "id": t_id,
                "yt_id": yt_id,
                "title": blueprint['title'], 
                "time": blueprint['publish_date'], 
                # 🔥 FIX 1: Frontend membaca "Queue" (Kuning), Watchdog membaca "Menunggu" (Aman)
                "status": "Menunggu Giliran (Queue) ⏳", 
                "type": "📺 VOD",
                "blueprint": blueprint 
            })
        queue_put(blueprint)
        
    save_tasks_db()
    
    return jsonify({"status": "success", "message": f"{count} Video diproses!"})
    
@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    return send_from_directory(BASE_UPLOAD, filename)

# ==========================================
# 📱 TELEGRAM REMINDER WORKER
# ==========================================
def telegram_reminder_worker():
    global database_channel
    while True:
        time.sleep(3600)  # Mengecek jadwal setiap 1 jam
        
        bot_config = load_bot_config()
        token = bot_config.get('tg_token')
        chat_id = bot_config.get('tg_chat_id')
        
        if not token or not chat_id: 
            continue
            
        now = datetime.now()
        changed = False
        
        with db_lock:
            for c in database_channel:
                last_date_str = c.get('last_scheduled_date')
                # Lewati jika belum ada jadwal atau pengingat sudah dikirim
                if not last_date_str or c.get('reminder_sent', False):
                    continue
                    
                try:
                    last_date = datetime.strptime(last_date_str, '%Y-%m-%d %H:%M')
                    time_diff = last_date - now
                    
                    # Jika sisa waktu <= 24 Jam (dan belum kelewat jauh)
                    if timedelta(hours=0) < time_diff <= timedelta(hours=24):
                        msg = f"⚠️ Notifikasi KeiBot Factory\n\nChannel: *{c['name']}*\nStok jadwal video hampir habis!\nJadwal terakhir: {last_date_str}\n\nWaktunya buat video baru, Bos K!"
                        try:
                            # Kirim pesan ke Telegram
                            requests.post(
                                f"https://api.telegram.org/bot{token}/sendMessage", 
                                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
                            )
                            c['reminder_sent'] = True
                            changed = True
                        except Exception as e:
                            print(f"Gagal kirim Telegram: {e}")
                except Exception as e:
                    pass
                    
            if changed:
                save_channels(database_channel)

# Jalankan worker di background
if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    threading.Thread(target=telegram_reminder_worker, daemon=True).start()

def scheduled_task_activator():
    # 🔥 BARU: mengaktifkan task "Dijadwalkan ... ⏳ (Tahan Restart ✓)" saat waktunya tiba.
    # Task berjadwal jauh sengaja TIDAK dirender saat startup (biar aman dari restart).
    # Worker ini cek tiap 2 menit: begitu publish_date masuk dalam ≤60 menit,
    # task dikembalikan ke mesin render.
    while True:
        time.sleep(120)
        now = datetime.now()
        activated = []
        with db_lock:
            for t in active_tasks:
                if "blueprint" not in t or "Dijadwalkan" not in t.get('status', ''):
                    continue
                try:
                    sch = datetime.strptime(t['blueprint']['publish_date'], "%Y-%m-%d %H:%M")
                except Exception:
                    continue
                diff = sch - now
                if diff <= timedelta(hours=1):
                    t['status'] = "In Factory Queue ⚙️ (Dilanjutkan)"
                    try:
                        queue_put(t["blueprint"])
                        activated.append(t['title'][:50])
                    except Exception:
                        pass
        if activated:
            save_tasks_db()
            msg = "⏳ *Task terjadwal diaktifkan:*\n" + "\n".join("• " + a for a in activated)
            send_tg_message(msg)

if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    threading.Thread(target=scheduled_task_activator, daemon=True).start()

# --- Endpoint Telegram Settings ---
@app.route('/api/save_telegram', methods=['POST'])
def save_telegram():
    data = request.json
    bot_config = load_bot_config()
    if data.get('token') != '********':
        bot_config['tg_token'] = data.get('token', '')
    bot_config['tg_chat_id'] = data.get('chat_id', '')
    
    write_json(CONFIG_FILE, bot_config)
    
    # Tes kirim pesan saat disimpan
    if bot_config['tg_token'] and bot_config['tg_chat_id']:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_config['tg_token']}/sendMessage", 
                json={"chat_id": bot_config['tg_chat_id'], "text": "✅ KeiBot: Notifikasi Telegram berhasil dihubungkan!"}
            )
        except: 
            pass
            
    return jsonify({"status": "success", "message": "Konfigurasi Telegram disimpan!"})

@app.route('/api/get_telegram', methods=['GET'])
def get_telegram():
    bot_config = load_bot_config()
    return jsonify({
        "token": "********" if bot_config.get('tg_token') else "", 
        "chat_id": bot_config.get('tg_chat_id', '')
    })

# ============================================================
# 📱 TIKTOK & FACEBOOK REELS CREDENTIALS API
# ============================================================
@app.route('/api/save_social_creds', methods=['POST'])
def save_social_creds():
    data = request.json or {}
    yt_id = data.get('yt_id', '').strip()
    platform = data.get('platform', '').strip().lower() # 'tiktok' atau 'facebook'
    
    if not yt_id or platform not in ('tiktok', 'facebook'):
        return jsonify({"status": "error", "message": "Channel dan platform wajib dipilih!"}), 400

    with db_lock:
        found = False
        for c in database_channel:
            if c.get('yt_id') == yt_id:
                if platform == 'tiktok':
                    c['tiktok_creds'] = {
                        "access_token": c.get('tiktok_creds', {}).get('access_token', '') if data.get('access_token') == '********' else data.get('access_token', '').strip(),
                        "open_id": data.get('open_id', '').strip(),
                        "privacy_level": data.get('privacy_level', 'PUBLIC_TO_EVERYONE'),
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")
                    }
                elif platform == 'facebook':
                    c['facebook_creds'] = {
                        "page_id": data.get('page_id', '').strip(),
                        "page_access_token": c.get('facebook_creds', {}).get('page_access_token', '') if data.get('page_access_token') == '********' else data.get('page_access_token', '').strip(),
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")
                    }
                found = True
                break
        if not found:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan!"}), 404
        save_channels(database_channel)

    return jsonify({"status": "success", "message": f"Kredensial {platform.upper()} berhasil disimpan untuk channel ini!"})

@app.route('/api/get_social_creds', methods=['GET'])
def get_social_creds():
    yt_id = request.args.get('yt_id', '').strip()
    channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if not channel:
        return jsonify({"tiktok": {}, "facebook": {}})
    tiktok = dict(channel.get('tiktok_creds', {}))
    facebook = dict(channel.get('facebook_creds', {}))
    for value in (tiktok, facebook):
        for key in ('access_token', 'page_access_token'):
            if value.get(key):
                value[key] = '********'
    return jsonify({'tiktok': tiktok, 'facebook': facebook})

@app.route('/api/hub_status')
def get_hub_status():
    try:
        factory_active = sum(not task_terminal(t) for t in active_tasks)

        clipper_active = 0
        clipper_db_path = os.path.join(BASE_DIR, 'clipper', 'clips_db.json')
        if os.path.exists(clipper_db_path):
            with open(clipper_db_path, 'r') as f:
                c_jobs = json.load(f)
                clipper_active = sum(not task_terminal(j) for j in c_jobs)

        shorts_active = 0
        shorts_db_path = os.path.join(BASE_DIR, 'shorts', 'shorts_db.json')
        if os.path.exists(shorts_db_path):
            with open(shorts_db_path, 'r') as f:
                s_jobs = json.load(f)
                shorts_active = sum(not task_terminal(j) for j in s_jobs)

        # Cek status Live Streaming (Multi-Jadwal)
        live_is_active = False
        live_db_path = os.path.join(BASE_DIR, 'live', 'live_db.json')
        if os.path.exists(live_db_path):
            try:
                with open(live_db_path, 'r') as f:
                    ldb = json.load(f)
                    if isinstance(ldb, dict):
                        ldb = [ldb]
                    if isinstance(ldb, list):
                        live_is_active = any(i.get('status') in ('LIVE', 'WAITING_TO_START', 'RENDERING_BASE') for i in ldb)
            except Exception:
                pass

        sys_stats = get_system_stats()

        return jsonify({
            "factory": factory_active,
            "clipper": clipper_active,
            "shorts": shorts_active,
            "history": len(history_tasks),
            "channels": len(database_channel),
            "live_active": live_is_active,
            "cpu": f"{sys_stats['cpu']}%"
        })
    except Exception:
        return jsonify({"factory": 0, "clipper": 0, "shorts": 0, "live_active": False, "cpu": "0%"})

# ==========================================
# 🚀 STARTUP & RECOVERY SCRIPT
# ==========================================
def _reschedule_task(t):
    yt_id = task_channel_id(t)
    if channel_cooldowns.get(yt_id, 0) > time.time():
        minutes = max(1, int((channel_cooldowns[yt_id] - time.time()) / 60))
        t['status'] = f'Antrean Ditunda (Cooldown YT {minutes} mnt) ⏳'
    else:
        t['status'] = 'Menunggu Giliran (Queue) ⏳ (Dilanjutkan)'
    t['updated_at'] = time.time()
    queue_put(t["blueprint"])

# 🔥 SISTEM PENYAPU OTOMATIS: Bersihkan hantu sebelum masuk antrean
if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    valid_yt_ids = {c['yt_id'] for c in database_channel}
    tasks_to_keep = []

    for t in active_tasks:
        is_final = task_terminal(t)
    
        # Cek apakah channel induknya masih ada
        yt_id = t.get('yt_id') or (t.get('blueprint', {}).get('yt_id'))
    
        if yt_id and yt_id not in valid_yt_ids and not is_final:
            # Langsung batalkan dan buang ke history saat itu juga
            t['status'] = "Dibatalkan (Channel Dihapus) 🛑"
            history_tasks.insert(0, t)
        else:
            tasks_to_keep.append(t)
            if not is_final:
                if "blueprint" in t:
                    _reschedule_task(t)
                else:
                    t['status'] = "Menunggu (Data Incomplete) ⚠️"

    active_tasks = [t for t in tasks_to_keep if "Dibatalkan" not in t['status']]
    save_tasks_db()
    
    startup_sanitizer()
    send_tg_message("🚀 *KeiBot Factory aktif kembali*\nTask dilanjutkan otomatis. "
                    f"Antrean: {len(active_tasks)} | Riwayat: {len(history_tasks)}")

# ==========================================
# 🌐 GLOBAL FACEBOOK PAGES API
# ==========================================
FB_PAGES_FILE = os.path.join(BASE_DIR, 'facebook_pages.json')

def load_fb_pages():
    data = read_json(FB_PAGES_FILE, [])
    if not isinstance(data, list):
        raise ValueError('Format database Facebook Page tidak valid.')
    return data

@app.route('/api/get_fb_pages', methods=['GET'])
def api_get_fb_pages():
    return jsonify([{k: p.get(k, '') for k in ('name', 'page_id')} for p in load_fb_pages()])

@app.route('/api/save_fb_page', methods=['POST'])
def api_save_fb_page():
    data = request.json or {}
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in ('page_id', 'name', 'access_token')):
        return jsonify({'status': 'error', 'message': 'Nama, Page ID, dan token wajib diisi.'}), 400
    def mutate(pages):
        page = next((p for p in pages if p['page_id'] == data['page_id']), None)
        if page is None:
            pages.append({k: data[k].strip() for k in ('page_id', 'name', 'access_token')})
        else:
            page.update({k: data[k].strip() for k in ('name', 'access_token')})
    update_json(FB_PAGES_FILE, [], mutate)
    return jsonify({'status': 'success', 'message': 'Facebook Page berhasil disimpan!'})

@app.route('/api/delete_fb_page', methods=['POST'])
def api_delete_fb_page():
    data = request.json or {}
    update_json(FB_PAGES_FILE, [], lambda pages: [p for p in pages if p.get('page_id') != data.get('page_id')])
    return jsonify({'status': 'success', 'message': 'Page dihapus!'})



if os.environ.get('KEIBOT_START_WORKERS', '1') == '1':
    threading.Thread(target=background_worker, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)