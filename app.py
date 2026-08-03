import os, time, queue, threading, subprocess, random, json, shutil, math
import numpy as np
import cv2, librosa, imageio
import datetime as dt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session
import requests

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ==========================================
# 🛡️ SETUP & MONITORING
# ==========================================
def auto_setup_dependencies():
    ffmpeg_found = shutil.which("ffmpeg") or os.path.exists("/usr/bin/ffmpeg")
    if not ffmpeg_found:
        print("⚙️ KEIBOT: ffmpeg tidak ditemukan, mencoba install otomatis...")
        ret = os.system("apt-get update -qq && apt-get install -y ffmpeg")
        if ret == 0: print("✅ ffmpeg berhasil diinstall!")
        else: print("❌ Gagal install ffmpeg otomatis. Jalankan manual: apt-get install -y ffmpeg")
    else:
        path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        print(f"✅ ffmpeg ditemukan: {path}")

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

def is_configured(): return os.path.exists(CONFIG_FILE)
def load_bot_config():
    if is_configured():
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return {}

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
            with open(CONFIG_FILE, 'w') as f: json.dump({"admin_pin": pin, "secret_key": new_secret}, f, indent=4)
            app.secret_key = new_secret; session['logged_in'] = True
            return redirect(url_for('index'))
    return render_template('setup.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_configured(): return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        if request.form.get('password') == load_bot_config().get('admin_pin'):
            session['logged_in'] = True; return redirect(url_for('index'))
        else: error = 'Akses Ditolak! PIN Salah.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None); return redirect(url_for('login'))

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
    'audio':      'audios',
    'audios':     'audios',
    'background': 'backgrounds',
    'backgrounds':'backgrounds',
    'thumbnail':  'thumbnails',
    'thumbnails': 'thumbnails',
    'base_video': 'base_videos'  # <--- TAMBAHAN FOLDER BARU
}

def resolve_folder(g_type: str) -> str:
    return GALLERY_FOLDER_MAP.get(str(g_type).strip().lower(), 'audios')

def load_tasks_db():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, 'r') as f: return json.load(f)
        except: return {"active": [], "history": []}
    return {"active": [], "history": []}

def save_tasks_db():
    with db_lock:
        data = {"active": active_tasks, "history": history_tasks}
        with open(TASKS_FILE, 'w') as f: json.dump(data, f, indent=4)

def load_channels():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f: return json.load(f)
        except: return []
    return []

def save_channels(channels):
    with db_lock:
        with open(DB_FILE, 'w') as f: json.dump(channels, f, indent=4)

task_data = load_tasks_db()
active_tasks = task_data.get("active", [])
history_tasks = task_data.get("history", [])
database_channel = load_channels()

render_queue = queue.Queue()
stop_flags = {}
channel_cooldowns = {} 

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
                t['status'] = final_status
                history_tasks.insert(0, t)
                active_tasks.remove(t)
                if len(history_tasks) > 50: history_tasks.pop()
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

threading.Thread(target=api_key_checker_worker, daemon=True).start()

# ==========================================
# 🏭 GALLERY & ASSET MANAGER
# ==========================================
def get_channel_folder(yt_id, sub):
    path = os.path.join(BASE_UPLOAD, yt_id, sub)
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

def get_all_audios(yt_id):
    path = get_channel_folder(yt_id, "audios")
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.mp3', '.wav'))]
    random.shuffle(files)
    return files

def get_and_consume_thumbnail(yt_id):
    path = get_channel_folder(yt_id, "thumbnails")
    files = sorted([f for f in os.listdir(path) if f.lower().endswith(('.jpg', '.png', '.jpeg'))])
    if not files: return None
    return os.path.join(path, files[0])

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
# ⚙️ CORE ENGINE (VISUALIZER DENGAN 7 MODE)
# ==========================================
class AudioBrain:
    def __init__(self):
        import numpy as np
        self.y = None; self.sr = None; self.onset_env = None; self.has_audio = False
        self.duration = 0.0

    def load(self, path, max_duration=None):
        try:
            import librosa
            self.y, self.sr = librosa.load(path, sr=22050, mono=True, duration=max_duration)
            self.onset_env = librosa.onset.onset_strength(y=self.y, sr=self.sr)
            self.duration = len(self.y) / self.sr
            self.has_audio = True
        except Exception as e:
            print(f"Audio Error: {e}")

    def get_data(self, t, n_bars=64): 
        import numpy as np
        n_bars = int(n_bars) # Pastikan selalu angka bulat
        if not self.has_audio: return 0.0, False, np.zeros(n_bars)
        
        idx = int(t * self.sr)
        if idx >= len(self.y): return 0.0, False, np.zeros(n_bars)

        try: 
            chunk = self.y[idx:idx+1024]
            vol = np.sqrt(np.mean(chunk**2)) * 10 if len(chunk) > 0 else 0
        except: 
            vol = 0
        
        hit = False
        try:
            onset_idx = int(idx/512)
            if onset_idx < len(self.onset_env) and self.onset_env[onset_idx] > 2.0: 
                hit = True
        except: pass

        final_bars = np.zeros(n_bars)
        try:
            n_fft = 2048
            fft_data = self.y[idx:idx+n_fft]
            if len(fft_data) == n_fft:
                windowed_data = fft_data * np.hanning(n_fft)
                spec = np.abs(np.fft.rfft(windowed_data))
                usable = spec[2:200] 
                ls = len(usable)
                
                if ls > 0:
                    half_n = max(1, n_bars // 2) # Pengaman dari pembagian nol
                    raw_bars = np.zeros(half_n)
                    for i in range(half_n):
                        s = int((i / half_n) * ls)
                        e = int(((i + 1) / half_n) * ls)
                        if e <= s: e = s + 1
                        if e > ls: e = ls
                        raw_bars[i] = np.mean(usable[s:e]) / 15.0 if e > s else 0
                    
                    smooth_half = np.convolve(raw_bars, np.ones(3)/3, mode='same')
                    final_bars = np.concatenate((smooth_half[::-1], smooth_half))
                    
                    # Pengaman dimensi array anti-crash
                    if len(final_bars) < n_bars: 
                        final_bars = np.pad(final_bars, (0, n_bars - len(final_bars)), 'constant')
                    elif len(final_bars) > n_bars: 
                        final_bars = final_bars[:n_bars]
        except Exception: 
            pass # Abaikan error matematika agar server tidak mati
                
        return vol, hit, final_bars

class BackgroundManager:
    def __init__(self, bg_paths, w, h):
        self.bg_paths = bg_paths; self.w = w; self.h = h; self.idx = 0; self.reader = None; self.static_bg = None; self.load_current()
        
    def load_current(self):
        if self.reader: self.reader.close()
        path = self.bg_paths[self.idx]
        if path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')): 
            img = cv2.imread(path)
            if img is not None:
                self.static_bg = cv2.resize(img, (self.w, self.h))
            else:
                self.static_bg = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        else: 
            self.reader = imageio.get_reader(path, 'ffmpeg')
            
    def get_frame(self):
        if self.static_bg is not None: return self.static_bg.copy()
        try: return cv2.resize(cv2.cvtColor(self.reader.get_next_data(), cv2.COLOR_RGB2BGR), (self.w, self.h))
        except: self.idx = (self.idx + 1) % len(self.bg_paths); self.load_current(); return self.get_frame()
        
    def close(self):
        if self.reader: self.reader.close()

class VisualEngine:
    def __init__(self, c_bot, c_top, c_part):
        import numpy as np
        self.col_bot = (c_bot[2], c_bot[1], c_bot[0])
        self.col_top = (c_top[2], c_top[1], c_top[0])
        self.col_part = (c_part[2], c_part[1], c_part[0])
        self.bar_h = None
        self.particles = []
        
        # Generator Gradien Kustom Asli Bos K
        self.grad = np.zeros((1000, 1, 3), dtype=np.uint8)
        for c in range(3): 
            self.grad[:, 0, c] = np.linspace(self.col_top[c], self.col_bot[c], 1000)
            
        self.bar_h_h = None
        self.radial_h = None
        self.wave_points = None
        self.line_h = None
        self.band_h = None
        self.frame_count = 0
        self.rainbow_palette = []

    def _init_rainbow(self, n):
        import numpy as np
        import cv2
        if len(self.rainbow_palette) != n:
            self.rainbow_palette = []
            for i in range(n):
                hue = int((i / n) * 179) # Rentang Hue OpenCV 0-179
                color_hsv = np.uint8([[[hue, 255, 255]]])
                bgr = cv2.cvtColor(color_hsv, cv2.COLOR_HSV2BGR)[0][0]
                self.rainbow_palette.append((int(bgr[0]), int(bgr[1]), int(bgr[2])))

    def get_color(self, i, n, mode):
        if mode == 'rainbow':
            if not self.rainbow_palette: return (255,255,255)
            # 🔥 SUNTIK KEBAL: Sesuaikan rasio agar tidak pernah Out of Range
            ratio = max(0.0, min(1.0, i / max(1, n)))
            idx = int(ratio * (len(self.rainbow_palette) - 1))
            idx = max(0, min(len(self.rainbow_palette) - 1, idx))
            return self.rainbow_palette[idx]
        else:
            ratio = max(0.0, min(1.0, i / max(1, n)))
            idx = max(0, min(999, int(ratio * 999)))
            c = self.grad[idx, 0]
            return (int(c[0]), int(c[1]), int(c[2]))

    # ========== VERTICAL BARS (BOTTOM & CENTER) ==========
    def draw_vertical_bars(self, frame, glow_canvas, bars, cfg, current_px, current_py, current_wp):
        import cv2, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        max_h = h * (cfg.get('max_height', 40) / 100)
        idle, space = int(cfg.get('idle_height', 5)), int(cfg.get('spacing', 3))
        bar_style = cfg.get('bar_style', 'bottom')
        color_mode = cfg.get('color_mode', 'gradient')
        
        tot_w = w * current_wp
        bar_w = max(1, int((tot_w - (space * (n - 1))) / n))
        start_x = int(w * current_px) - int(tot_w / 2)
        base_y = int(h * current_py)
        
        if self.bar_h is None or len(self.bar_h) != n: self.bar_h = np.zeros(n)
        
        for i in range(n):
            target = bars[i] * cfg.get('reactivity', 0.66)
            self.bar_h[i] = (self.bar_h[i] * 0.85) + (target * 0.15)
            height = max(2, int(max(idle, self.bar_h[i] * max_h)))
            
            x1 = start_x + i * (bar_w + space)
            x2 = x1 + bar_w
            y1 = (base_y - (height // 2)) if bar_style == 'center' else base_y - height
            y2 = (base_y + (height // 2)) if bar_style == 'center' else base_y
            
            x1_safe, x2_safe = max(0, min(w, x1)), max(0, min(w, x2))
            y1_safe, y2_safe = max(0, min(h, y1)), max(0, min(h, y2))
            
            if x2_safe > x1_safe and y2_safe > y1_safe:
                # 🔥 BARU: Gradasi Warna Tiang dari Bawah ke Atas 🔥
                for y in range(y1_safe, y2_safe):
                    # Hitung jarak piksel ini dari garis bawah (base_y)
                    dist = abs(base_y - y) if bar_style == 'center' else (base_y - y)
                    color = self.get_color(dist, max_h, color_mode)
                    # Gambar per baris piksel
                    cv2.line(glow_canvas, (x1_safe, y), (x2_safe - 1, y), color, 1)
                    
                core_w = max(1, int((x2_safe - x1_safe) * 0.5))
                offset = ((x2_safe - x1_safe) - core_w) // 2
                cv2.rectangle(frame, (x1_safe + offset, y1_safe), (x1_safe + offset + core_w, y2_safe), (255,255,255), -1)

    # ========== HORIZONTAL BARS ==========
    def draw_horizontal_bars(self, frame, glow_canvas, bars, cfg, current_px, current_py, current_wp):
        import cv2, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        max_h = h * (cfg.get('max_height', 40) / 100)
        idle, space = int(cfg.get('idle_height', 5)), int(cfg.get('spacing', 3))
        color_mode = cfg.get('color_mode', 'gradient')
        
        tot_h = h * current_wp
        bar_h = max(1, int((tot_h - (space * (n - 1))) / n))
        start_y = int(h * current_py) - int(tot_h / 2)
        base_x = int(w * current_px)
        
        if self.bar_h_h is None or len(self.bar_h_h) != n: self.bar_h_h = np.zeros(n)
        
        for i in range(n):
            target = bars[i] * cfg.get('reactivity', 0.66)
            self.bar_h_h[i] = (self.bar_h_h[i] * 0.85) + (target * 0.15)
            width = max(2, int(max(idle, self.bar_h_h[i] * max_h)))
            
            y1, y2 = start_y + i * (bar_h + space), start_y + i * (bar_h + space) + bar_h
            x1, x2 = base_x - width, base_x
            
            x1_safe, x2_safe = max(0, min(w, x1)), max(0, min(w, x2))
            y1_safe, y2_safe = max(0, min(h, y1)), max(0, min(h, y2))
            
            if x2_safe > x1_safe and y2_safe > y1_safe:
                color = self.get_color(i, n, color_mode)
                cv2.rectangle(glow_canvas, (x1_safe, y1_safe), (x2_safe, y2_safe), color, -1)
                core_h = max(1, int((y2_safe - y1_safe) * 0.5))
                offset = ((y2_safe - y1_safe) - core_h) // 2
                cv2.rectangle(frame, (x1_safe, y1_safe + offset), (x2_safe, y1_safe + offset + core_h), (255,255,255), -1)

    
    # ========== CIRCULAR (MELINGKAR - GAYA LED) ==========
    def draw_circle_bars(self, frame, glow_canvas, bars, cfg, rotation, current_px, current_py, current_wp, vol=0):
        import cv2, math, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        if n == 0: return

        max_h = h * (cfg.get('max_height', 40) / 100)
        idle = int(cfg.get('idle_height', 5))
        idle = min(idle, max_h)
        color_mode = cfg.get('color_mode', 'gradient')

        tot_w = w * current_wp
        bar_w = max(1, int((tot_w - (cfg.get('spacing', 3) * (n - 1))) / n))
        center_x, center_y = int(w * current_px), int(h * current_py)
        base_radius = min(tot_w, max_h) * 0.35

        if self.bar_h is None or len(self.bar_h) != n:
            self.bar_h = np.zeros(n)

        beat_scale = 1.0 + (min(vol, 3.0) * 0.15) if cfg.get('enable_logo_pulse', True) else 1.0
        dynamic_radius = base_radius * beat_scale

        ring_color = self.get_color(int(n / 2), n, color_mode)
        cv2.circle(glow_canvas, (center_x, center_y), int(dynamic_radius), ring_color, 4, cv2.LINE_AA)
        cv2.circle(frame, (center_x, center_y), int(dynamic_radius), (255, 255, 255), 2, cv2.LINE_AA)

        # 🔥 PERBAIKAN: RUMUS SKALA 1080p AGAR LED TIDAK MENGGUMPAL 🔥
        scale_f = h / 1080.0
        seg_h = max(2, int(18 * scale_f))     # Diperpanjang agar proporsional di 1080p
        seg_gap = max(1, int(10 * scale_f))   # Jarak direnggangkan agar tidak nempel
        max_segs = max(1, int(max_h / (seg_h + seg_gap)))
        
        # Ketebalan ditipiskan agar tidak tabrakan menjadi gumpalan
        thick_glow = max(2, int(bar_w * 0.4))
        thick_core = max(1, int(bar_w * 0.2))

        for i in range(n):
            target = bars[i] * cfg.get('reactivity', 0.66)
            self.bar_h[i] = (self.bar_h[i] * 0.85) + (target * 0.15)
            height = max(int(15 * scale_f), int(max(idle, self.bar_h[i] * max_h)))
            
            num_segs = int(height / (seg_h + seg_gap))
            if num_segs < 1: num_segs = 1

            angle = (i / n) * math.pi * 2 + (math.pi / 2) + rotation

            for s in range(num_segs):
                r1 = dynamic_radius + (s * (seg_h + seg_gap))
                r2 = r1 + seg_h
                
                x1 = int(center_x + math.cos(angle) * r1)
                y1 = int(center_y + math.sin(angle) * r1)
                x2 = int(center_x + math.cos(angle) * r2)
                y2 = int(center_y + math.sin(angle) * r2)

                color = self.get_color(s, max_segs, color_mode)

                cv2.line(glow_canvas, (x1, y1), (x2, y2), color, thick_glow, cv2.LINE_AA)
                cv2.line(frame, (x1, y1), (x2, y2), color, thick_core, cv2.LINE_AA)

        print(">> Circle bars DIPASTIKAN muncul. Jumlah bar:", n)

    
    
    # ========== LED EQUALIZER (BALOK PUTUS-PUTUS) ==========
    def draw_led_eq(self, frame, glow_canvas, bars, cfg, current_px, current_py, current_wp):
        import cv2, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        max_h = h * (cfg.get('max_height', 40) / 100)
        idle, space = int(cfg.get('idle_height', 5)), int(cfg.get('spacing', 3))
        color_mode = cfg.get('color_mode', 'gradient')
        
        tot_w = w * current_wp
        bar_w = max(1, int((tot_w - (space * (n - 1))) / n))
        start_x = int(w * current_px) - int(tot_w / 2)
        base_y = int(h * current_py)
        
        # 🔥 1. BALOK PUTUS-PUTUS DIBUAT LEBIH KECIL 🔥
        seg_h = 3     # Tinggi per balok (sebelumnya 6 atau 4)
        seg_gap = 2   # Jarak antar balok
        max_segs = max(1, int(max_h / (seg_h + seg_gap)))
        
        if self.bar_h is None or len(self.bar_h) != n: self.bar_h = np.zeros(n)
        
        for i in range(n):
            target = bars[i] * cfg.get('reactivity', 0.66)
            self.bar_h[i] = (self.bar_h[i] * 0.85) + (target * 0.15)
            height = max(10, int(max(idle, self.bar_h[i] * max_h)))
            
            num_segs = int(height / (seg_h + seg_gap))
            if num_segs < 1: num_segs = 1
            
            x1 = start_x + i * (bar_w + space)
            x2 = x1 + bar_w
            x1_s, x2_s = max(0, min(w, x1)), max(0, min(w, x2))
            
            # 🔥 2. GRADASI WARNA KE SAMPING (Per Bar 'i'), BUKAN KE ATAS 🔥
            color = self.get_color(i, n, color_mode)
            
            for s in range(num_segs):
                y2 = base_y - (s * (seg_h + seg_gap))
                y1 = y2 - seg_h
                
                y1_s, y2_s = max(0, min(h, y1)), max(0, min(h, y2))
                
                if x2_s > x1_s and y2_s > y1_s:
                    cv2.rectangle(glow_canvas, (x1_s, y1_s), (x2_s, y2_s), color, -1)
                    cv2.rectangle(frame, (x1_s, y1_s), (x2_s, y2_s), color, -1)

    # ========== SPECTRUM BAR DI SEMUA SISI LAYAR (ATAS/BAWAH/KIRI/KANAN) ==========
    def draw_spectrum_edge(self, frame, glow_canvas, bars, cfg):
        import cv2, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        if n == 0:
            return

        # Batasi maksimal max_height ke 45% agar tidak mengambil seluruh ruang layar 
        # (jika mencapai 50%, margin tengah akan habis dan error).
        pct = min(45, cfg.get('max_height', 40)) / 100
        max_h = int(h * pct)   # panjang maksimal bar atas & bawah
        max_w = int(w * pct)   # panjang maksimal bar kiri & kanan
        idle = int(cfg.get('idle_height', 5))
        space = int(cfg.get('spacing', 3))
        color_mode = cfg.get('color_mode', 'gradient')
        reactivity = cfg.get('reactivity', 0.66)

        # 1. PERBAIKAN: Mencegah tabrakan antar sisi dengan memberikan margin di sudut.
        margin_x = max_w  # Ruang kosong di kiri & kanan untuk bar horizontal
        margin_y = max_h  # Ruang kosong di atas & bawah untuk bar vertikal
        avail_w = w - (2 * margin_x)
        avail_h = h - (2 * margin_y)

        # 2. PERBAIKAN: Mengurutkan bars agar High Beat (frekuensi rendah/index awal) 
        #    berada di TENGAH, dan sisa frekuensinya menyebar ke pinggir.
        centered_bars = np.zeros(n)
        mid = n // 2
        for i in range(n):
            if i % 2 == 0:
                centered_bars[mid + (i // 2)] = bars[i]
            else:
                centered_bars[mid - (i // 2) - 1] = bars[i]

        if getattr(self, 'bar_h_edge_v', None) is None or len(self.bar_h_edge_v) != n:
            self.bar_h_edge_v = np.zeros(n)
        if getattr(self, 'bar_h_edge_h', None) is None or len(self.bar_h_edge_h) != n:
            self.bar_h_edge_h = np.zeros(n)

        # ---- ATAS & BAWAH (bar tersebar di area tengah layar secara horizontal) ----
        bar_w = max(1, int((avail_w - (space * (n - 1))) / n))
        for i in range(n):
            target = centered_bars[i] * reactivity  # Menggunakan centered_bars
            self.bar_h_edge_v[i] = (self.bar_h_edge_v[i] * 0.85) + (target * 0.15)
            height = max(2, int(max(idle, self.bar_h_edge_v[i] * max_h)))

            # Start X sekarang ditambahkan dengan margin_x
            x1 = margin_x + i * (bar_w + space)
            x2 = x1 + bar_w
            x1s, x2s = max(0, min(w, x1)), max(0, min(w, x2))
            if x2s <= x1s:
                continue

            # BAWAH: dari tepi bawah nongol ke atas
            y1b, y2b = max(0, h - height), h
            for y in range(y1b, y2b):
                color = self.get_color(y2b - y, max_h, color_mode)
                cv2.line(glow_canvas, (x1s, y), (x2s - 1, y), color, 1)
            core_w = max(1, int((x2s - x1s) * 0.5))
            off_x = ((x2s - x1s) - core_w) // 2
            cv2.rectangle(frame, (x1s + off_x, y1b), (x1s + off_x + core_w, y2b), (255, 255, 255), -1)

            # ATAS: dari tepi atas nongol ke bawah
            y1t, y2t = 0, min(h, height)
            for y in range(y1t, y2t):
                color = self.get_color(y - y1t, max_h, color_mode)
                cv2.line(glow_canvas, (x1s, y), (x2s - 1, y), color, 1)
            cv2.rectangle(frame, (x1s + off_x, y1t), (x1s + off_x + core_w, y2t), (255, 255, 255), -1)

        # ---- KIRI & KANAN (bar tersebar di area tengah layar secara vertikal) ----
        bar_h_seg = max(1, int((avail_h - (space * (n - 1))) / n))
        for i in range(n):
            target = centered_bars[i] * reactivity  # Menggunakan centered_bars
            self.bar_h_edge_h[i] = (self.bar_h_edge_h[i] * 0.85) + (target * 0.15)
            length = max(2, int(max(idle, self.bar_h_edge_h[i] * max_w)))

            # Start Y sekarang ditambahkan dengan margin_y
            y1 = margin_y + i * (bar_h_seg + space)
            y2 = y1 + bar_h_seg
            y1s, y2s = max(0, min(h, y1)), max(0, min(h, y2))
            if y2s <= y1s:
                continue

            # KIRI: dari tepi kiri nongol ke kanan
            x1l, x2l = 0, min(w, length)
            for x in range(x1l, x2l):
                color = self.get_color(x - x1l, max_w, color_mode)
                cv2.line(glow_canvas, (x, y1s), (x, y2s - 1), color, 1)
            core_h = max(1, int((y2s - y1s) * 0.5))
            off_y = ((y2s - y1s) - core_h) // 2
            cv2.rectangle(frame, (x1l, y1s + off_y), (x2l, y1s + off_y + core_h), (255, 255, 255), -1)

            # KANAN: dari tepi kanan nongol ke kiri
            x1r, x2r = max(0, w - length), w
            for x in range(x1r, x2r):
                color = self.get_color(x2r - x, max_w, color_mode)
                cv2.line(glow_canvas, (x, y1s), (x, y2s - 1), color, 1)
            cv2.rectangle(frame, (x1r, y1s + off_y), (x2r, y1s + off_y + core_h), (255, 255, 255), -1)

    # ========== MAIN PROCESS ENGINE ==========
    def process(self, frame, vol, is_hit, bars, cfg):
        import numpy as np
        import math
        import cv2
        import urllib.request
        import os
        
        def safe_num(val, default):
            try: return float(val) if val != "" and val is not None else default
            except: return default

        self.frame_count += 1
        rot_speed = safe_num(cfg.get('rotation_speed', 2), 2) * 0.01
        rotation_offset = self.frame_count * rot_speed

        # 🔥 FITUR BARU: BACKGROUND ZOOM BASS (JEDAG-JEDUG BERDASARKAN SLIDER) 🔥
        enable_bg_pulse = cfg.get('enable_bg_pulse', True)
        bg_zoom_pct = float(cfg.get('bg_zoom', 5.0)) # <-- Mengambil nilai slider dari web
        
        if enable_bg_pulse and vol > 0 and bg_zoom_pct > 0:
            bg_scale = 1.0 + (min(vol, 3.0) * (bg_zoom_pct / 100.0))
            if bg_scale > 1.001:
                h, w = frame.shape[:2]
                new_w, new_h = int(w * bg_scale), int(h * bg_scale)
                resized_bg = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                # Crop kembali ke ukuran asli (tengah)
                y_offset = (new_h - h) // 2
                x_offset = (new_w - w) // 2
                frame = resized_bg[y_offset:y_offset+h, x_offset:x_offset+w]

        bar_style = cfg.get('bar_style', 'bottom')
        n = len(bars)
        self._init_rainbow(n)
        
        # ACAKAN GOLDEN RATIO AMAN (TIDAK DISENTUH)
        scattered_bars = np.zeros(n)
        if bar_style in ['bottom', 'center', 'circle', 'radial', 'horizontal', 'frequency_bands', 'led_eq']:
            for i in range(n):
                fraction = (i * 0.618033988749895) % 1.0
                source_idx = int(fraction * n)
                source_idx = max(0, min(n - 1, source_idx))
                scattered_bars[i] = bars[source_idx]
        else:
            scattered_bars = np.copy(bars)

        glow_canvas = np.zeros_like(frame)

        is_double = cfg.get('vis_instance', 'single') == 'double'
        instances = 2 if is_double else 1

        for inst in range(instances):
            px = safe_num(cfg.get('pos_x' if inst == 0 else 'pos_x2', 25 if inst == 0 else 75), 50)/100
            py = safe_num(cfg.get('pos_y' if inst == 0 else 'pos_y2', 85), 85)/100
            wp = (cfg.get('width_pct', 60) * 0.45 / 100) if is_double else (cfg.get('width_pct', 60)/100)

            if bar_style == 'horizontal':
                self.draw_horizontal_bars(frame, glow_canvas, scattered_bars, cfg, px, py, wp)
            elif bar_style == 'led_eq':
                self.draw_led_eq(frame, glow_canvas, scattered_bars, cfg, px, py, wp)
            elif bar_style == 'circle':
                self.draw_circle_bars(frame, glow_canvas, scattered_bars, cfg, rotation_offset, px, py, wp, vol)
            elif bar_style == 'spectrum_edge':
                pass  # digambar sekali di luar loop instance (posisi fix di pinggir layar)
            else:
                self.draw_vertical_bars(frame, glow_canvas, scattered_bars, cfg, px, py, wp)

        if bar_style == 'spectrum_edge':
            self.draw_spectrum_edge(frame, glow_canvas, scattered_bars, cfg)

        if cfg.get('enable_glow', False):
            g_int = int(safe_num(cfg.get('glow_intensity', 15), 15))
            if g_int % 2 == 0: g_int = max(1, g_int - 1)
            glow_canvas = cv2.GaussianBlur(glow_canvas, (g_int, g_int), 0)
            frame = cv2.addWeighted(frame, 1.0, glow_canvas, 0.9, 0)
        
        # Style berbasis lingkaran (cuma 'circle' sekarang) pakai mask lingkaran paksa
        if bar_style in ['circle']:
            if getattr(self, 'logo', None) is None and not getattr(self, 'logo_checked', False):
                self.logo_checked = True
                logo_url = cfg.get('logo_url', '')
                img_data = None
                if logo_url and logo_url.startswith('http'):
                    try:
                        req = urllib.request.urlopen(logo_url)
                        arr = np.asarray(bytearray(req.read()), dtype=np.uint8)
                        img_data = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
                    except: pass
                if img_data is None and os.path.exists('static/logo.png'):
                    img_data = cv2.imread('static/logo.png', cv2.IMREAD_UNCHANGED)
                if img_data is not None:
                    if len(img_data.shape) == 3 and img_data.shape[2] == 3:
                        img_data = cv2.cvtColor(img_data, cv2.COLOR_BGR2BGRA)
                    h_img, w_img = img_data.shape[:2]
                    mask = np.zeros((h_img, w_img), dtype=np.uint8)
                    cv2.circle(mask, (w_img//2, h_img//2), min(w_img, h_img)//2, 255, -1)
                    img_data[:, :, 3] = mask
                    self.logo = img_data

            if getattr(self, 'logo', None) is not None:
                h, w = frame.shape[:2]
                for inst in range(instances):
                    px = safe_num(cfg.get('pos_x' if inst == 0 else 'pos_x2', 25 if inst == 0 else 75), 50)/100
                    py = safe_num(cfg.get('pos_y' if inst == 0 else 'pos_y2', 85), 85)/100
                    center_x, center_y = int(w * px), int(h * py)
                    max_h = h * (cfg.get('max_height', 40)/100)
                    wp = (cfg.get('width_pct', 60) * 0.45 / 100) if is_double else (cfg.get('width_pct', 60) / 100)
                    
                    # PENTING: pakai `vol` asli (parameter process()), sama seperti ring & style 'circle'
                    beat_scale = 1.0 + (min(vol, 3.0) * 0.15) if cfg.get('enable_logo_pulse', True) else 1.0
                    dynamic_radius = min(w * wp, max_h) * 0.35 * beat_scale
                    logo_size = int(dynamic_radius * 2) 
                    
                    if logo_size > 0:
                        logo_resized = cv2.resize(self.logo, (logo_size, logo_size))
                        y1_l, x1_l = int(center_y - logo_size // 2), int(center_x - logo_size // 2)
                        y2_l, x2_l = y1_l + logo_size, x1_l + logo_size
                        if y1_l >= 0 and y2_l <= h and x1_l >= 0 and x2_l <= w:
                            alpha_s = logo_resized[:, :, 3] / 255.0
                            alpha_l = 1.0 - alpha_s
                            for c in range(3):
                                frame[y1_l:y2_l, x1_l:x2_l, c] = (alpha_s * logo_resized[:, :, c] + alpha_l * frame[y1_l:y2_l, x1_l:x2_l, c])
                                
        part_amount = int(safe_num(cfg.get('part_amount', 3), 3))
        if part_amount > 0:
            h, w = frame.shape[:2]
            part_size_mult = safe_num(cfg.get('part_size', 1.0), 1.0)
            part_color_mode = cfg.get('part_color_mode', 'solid')
            part_effect = cfg.get('part_effect', 'circle')
            part_opacity = max(0.0, min(1.0, safe_num(cfg.get('part_opacity', 100), 100) / 100.0))

            if is_hit and vol > 1.5:
                for _ in range(part_amount):
                    if part_color_mode == 'rgb':
                        hue = np.random.randint(0, 180)
                        hsv = np.uint8([[[hue, 255, 255]]])
                        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
                        p_color = (int(bgr[0]), int(bgr[1]), int(bgr[2]))
                    else:
                        p_color = self.col_part
                    size0 = np.random.uniform(2, 6) * part_size_mult
                    self.particles.append([
                        np.random.randint(0, w), np.random.randint(0, h),
                        np.random.uniform(-3, 3), np.random.uniform(-3, 3),
                        size0, p_color
                    ])

            alive = []
            spd = 1.0 + (vol * 0.1 * safe_num(cfg.get('part_speed', 1.0), 1.0))
            overlay = frame.copy()
            any_alive = False
            for p in self.particles:
                p[0] += p[2] * spd; p[1] += p[3] * spd; p[4] -= 0.1
                if p[4] > 0:
                    any_alive = True
                    x, y, size, color = int(p[0]), int(p[1]), max(1, int(p[4])), p[5]
                    if part_effect == 'spark':
                        pts = np.array([
                            [x, y - size], [x + size, y], [x, y + size], [x - size, y]
                        ], dtype=np.int32)
                        cv2.fillConvexPoly(overlay, pts, color)
                    elif part_effect == 'glow':
                        cv2.circle(overlay, (x, y), max(1, int(size * 1.8)), tuple(int(c * 0.35) for c in color), -1)
                        cv2.circle(overlay, (x, y), max(1, int(size * 1.1)), tuple(int(c * 0.7) for c in color), -1)
                        cv2.circle(overlay, (x, y), size, color, -1)
                    else:
                        cv2.circle(overlay, (x, y), size, color, -1)
                    alive.append(p)
            self.particles = alive
            if any_alive and part_opacity > 0:
                frame[:] = cv2.addWeighted(frame, 1 - part_opacity, overlay, part_opacity, 0)
            
        return frame

def hex_to_rgb(h): return tuple(int(str(h).lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

def render_video_core(task_id, audio_path, bg_paths, output_path, duration, cfg):
    # 🔥 1. RESOLUSI FHD 1080p (Lebih Tajam & Jernih) 🔥
    w, h = 1920, 1080; fps = 30; total_f = int(duration * fps)
    
    c_bot = hex_to_rgb(cfg.get('color_bot', '#10b981'))
    c_top = hex_to_rgb(cfg.get('color_top', '#0ea5e9'))
    c_part = hex_to_rgb(cfg.get('color_part', '#ffffff'))
    bar_c = int(cfg.get('bar_count', 64))
    
    vis = VisualEngine(c_bot, c_top, c_part)
    bg = BackgroundManager(bg_paths, w, h)
    audio = AudioBrain(); audio.load(audio_path)
    
    with db_lock:
        for d in active_tasks:
            if d['id'] == task_id: d['status'] = "Rendering Visual & Background... ⚡"
    save_tasks_db()

    # 🔥 2. FILTER ANTI-BURIK (CRF 18 + veryfast) 🔥
    cmd = [
        get_ffmpeg_path(), '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(fps), 
        '-i', '-', 
        '-i', audio_path, 
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-threads', '0', '-pix_fmt', 'yuv420p', output_path
    ]
    
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 🔥 PERINTAH 3: RESOLUSI KEMBALI KE 720p 🔥
    w, h = 1280, 720; fps = 30; total_f = int(duration * fps)
    
    c_bot = hex_to_rgb(cfg.get('color_bot', '#10b981'))
    c_top = hex_to_rgb(cfg.get('color_top', '#0ea5e9'))
    c_part = hex_to_rgb(cfg.get('color_part', '#ffffff'))
    bar_c = int(cfg.get('bar_count', 64))
    
    vis = VisualEngine(c_bot, c_top, c_part)
    bg = BackgroundManager(bg_paths, w, h)
    audio = AudioBrain(); audio.load(audio_path)
    
    with db_lock:
        for d in active_tasks:
            if d['id'] == task_id: d['status'] = "Rendering 720p... ⚡"
    save_tasks_db()

    cmd = [
        get_ffmpeg_path(), '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(fps), 
        '-i', '-', 
        '-i', audio_path, 
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-threads', '0', '-pix_fmt', 'yuv420p', output_path
    ]
    
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        for f in range(total_f):
            if stop_flags.get(task_id):
                raise Exception("Dibatalkan")
                
            v, is_hit, bars = audio.get_data(f/fps, bar_c)
            frame = vis.process(bg.get_frame(), v, is_hit, bars, cfg)
            proc.stdin.write(frame.tobytes())
            
    except Exception as e:
        proc.stdin.close()
        proc.terminate()
        bg.close()
        raise e
        
    proc.stdin.close(); proc.wait(); bg.close()

# ==========================================
# 🚀 BACKGROUND WORKER (SUDAH DI-TUNE UP NGEBUT)
# ==========================================
def background_worker():
    global channel_cooldowns
    
    while True:
        task = render_queue.get()
        task_id = task['id']
        yt_id = task['yt_id']
        
        # 🔥 SMART COOLDOWN SYSTEM
        if yt_id in channel_cooldowns:
            if time.time() < channel_cooldowns[yt_id]:
                sisa_menit = max(1, int((channel_cooldowns[yt_id] - time.time()) / 60))
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['status'] = f"Antrean Ditunda (Cooldown YT {sisa_menit} mnt) ⏳"
                save_tasks_db()
                render_queue.put(task)
                render_queue.task_done()
                time.sleep(5)
                continue
            else:
                del channel_cooldowns[yt_id]
                
        temp_files = [
            os.path.join(BASE_UPLOAD, f"temp_a_{task_id}.mp3"),
            os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt"),
            os.path.join(BASE_UPLOAD, f"temp_v_{task_id}.mp4"),
            os.path.join(BASE_UPLOAD, f"loop_{task_id}.txt"),
            os.path.join(BASE_DIR, f"static/final_{task_id}.mp4"),
        ]
        try:
            if not wait_for_resources(task_id): 
                raise Exception("Dibatalkan")
                
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id: d['status'] = "Meracik Aset Gallery... ⚙️"
            save_tasks_db()

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
                
                source_video = random.choice(video_paths)
                bg_paths = [source_video] # Kunci video ini sebagai background utama
                
                # Ekstrak audio dari video ke temp_a.mp3
                try:
                    subprocess.run([get_ffmpeg_path(), '-y', '-i', source_video, '-q:a', '0', '-map', 'a', base_audio], check=True, capture_output=True)
                except subprocess.CalledProcessError:
                    raise Exception(f"Gagal ekstrak audio! Video {os.path.basename(source_video)} mungkin tidak bersuara.")

                probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', base_audio], capture_output=True, text=True, check=True)
                try: base_duration_sec = float(probe.stdout.strip())
                except: raise Exception("Gagal membaca durasi audio dari video utama!")

                title_vid = os.path.splitext(os.path.basename(source_video))[0]
                track_schedule.append({'title': title_vid, 'path': source_video, 'start': 0.0, 'end': base_duration_sec, 'duration': base_duration_sec})

            else:
                # --- JALUR LAMA: MODE KLASIK (MIX MP3) ---
                audio_paths = get_all_audios(yt_id)
                if not audio_paths: raise Exception("Gallery Audio Kosong!")
                
                mp3_req = int(task.get('mp3_per_video', 5))
                mp3_count = min(mp3_req, len(audio_paths))
                selected_audios = audio_paths[:mp3_count]

                current_sec = 0.0
                c_txt = os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt")
                
                with open(c_txt, 'w', encoding='utf-8') as f:
                    for ap in selected_audios:
                        safe_path = os.path.abspath(ap).replace('\\', '/').replace("'", "'\\''")
                        f.write(f"file '{safe_path}'\n")
                        probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', ap], capture_output=True, text=True)
                        try: dur = float(probe.stdout.strip())
                        except: dur = 0.0
                        
                        title = os.path.splitext(os.path.basename(ap))[0]
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
            # ============== AKHIR BLOK SUMBER KONTEN (UPDATE) ==============raise Exception("Gallery Background Kosong!")

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
            preset['track_schedule'] = track_schedule
            preset['channel_name'] = ch_name

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
                # 🔥 REM DILEPAS: Looping Video Final
                subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', loop_txt, '-c', 'copy', '-t', str(target_sec), final_video], check=True)
            else:
                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                subprocess.run([get_ffmpeg_path(), '-y', '-i', base_video, '-c', 'copy', '-t', str(target_sec), final_video], check=True)

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
                            'snippet': {'title': task['title'], 'description': task.get('description', ''), 'tags': tags_list, 'categoryId': '10'},
                            'status': {'privacyStatus': task.get('privacy', 'public')}
                        }
                        if sch_obj > datetime.now():
                            wib = ZoneInfo("Asia/Jakarta")
                            sch_aware = sch_obj.replace(tzinfo=wib)
                            sch_utc = sch_aware.astimezone(timezone.utc)
                            body['status']['publishAt'] = sch_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                            body['status']['privacyStatus'] = 'private'
                            
                        media = MediaFileUpload(final_video, chunksize=1024*1024*5, resumable=True)
                        req = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
                        resp = None
                        
                        max_retries = 5
                        retry_count = 0
                        
                        while resp is None:
                            if stop_flags.get(task_id): raise Exception("Dibatalkan")
                            try:
                                status, resp = req.next_chunk()
                                if status:
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['status'] = f"Mengunggah (Key {index_kunci+1})... {int(status.progress()*100)}% 🚀"
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
                                try: os.remove(thumb_path)
                                except: pass
                            except: pass
                                
                        try:
                            if task.get('playlist_id'):
                                youtube.playlistItems().insert(part='snippet', body={'snippet': {'playlistId': task['playlist_id'], 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()
                        except: pass
                        move_to_history(task_id, f"Tayang! ✅ <a href='https://youtu.be/{video_id}' target='_blank'>[Lihat]</a>")
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
                            break
                        elif "rateLimitExceeded" in reason:
                            pesan_error = "Rate Limit (Terlalu Cepat) - Auto Cooldown 30 Menit"
                            channel_cooldowns[yt_id] = time.time() + 1800
                            break
                        else:
                            pesan_error = f"Ditolak YT: {reason}"
                            break
                    except Exception as e:
                        err_str = str(e).lower()
                        if "invalid_grant" in err_str or "expired" in err_str or "revoked" in err_str:
                            pesan_error = "Sesi Kedaluwarsa (Tautkan Ulang!)"
                            channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                        elif "timeout" in err_str or "connection" in err_str or "broken" in err_str:
                            pesan_error = "Koneksi VPS Putus/Timeout"
                        else:
                            pesan_error = f"Error: {str(e)[:40]}"
                        break
                        
                if not upload_berhasil:
                    if "API Habis" in pesan_error:
                        channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                    raise Exception(pesan_error)
            else:
                move_to_history(task_id, f"Render Selesai ✅ <a href='/static/final_{task_id}.mp4' target='_blank'>[Download]</a>")
        
        except Exception as e:
            err_msg = str(e)
            if "Limit" in err_msg or "Cooldown" in err_msg or "Habis" in err_msg:
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['status'] = f"Gagal Upload, Antre Ulang ({err_msg}) ⏳"
                save_tasks_db()
                render_queue.put(task)
            else:
                move_to_history(task_id, f"Gagal ❌ ({err_msg})")
        finally:
            for path in temp_files:
                try: os.remove(path)
                except: pass
            stop_flags.pop(task_id, None)
            render_queue.task_done()

threading.Thread(target=background_worker, daemon=True).start()

# ==========================================
# 📊 API ENDPOINTS (LANJUTAN)
# ==========================================
@app.route('/')
def index(): return render_template('index.html')

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

@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    global history_tasks
    with db_lock: history_tasks.clear()
    save_tasks_db()
    return jsonify({"status": "success", "message": "Riwayat dibersihkan!"})

@app.route('/api/get_channels')
def get_channels():
    safe_c = [{"id": c["id"], "name": c["name"], "yt_id": c["yt_id"], "thumbnail": c["thumbnail"], "status": c["status"], "title_bank": c.get("title_bank", [])} for c in database_channel]
    return jsonify(safe_c)

@app.route('/api/delete_channel', methods=['POST'])
def delete_channel():
    yt_id = request.form.get('yt_id')
    global database_channel
    database_channel = [c for c in database_channel if c['yt_id'] != yt_id]
    save_channels(database_channel)
    return jsonify({"status": "success", "message": "Channel dihapus!"})

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
    if not yt_id: return jsonify({"audios": 0, "backgrounds": 0, "thumbnails": 0, "base_videos": 0})
    def count_files(sub):
        path = get_channel_folder(yt_id, sub)
        return len([f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))])
    return jsonify({
        "audios": count_files("audios"), 
        "backgrounds": count_files("backgrounds"), 
        "thumbnails": count_files("thumbnails"),
        "base_videos": count_files("base_videos") # <--- TAMBAHAN
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
                    res.append({"name": f, "size": f"{size_mb} MB"})
        return res
    return jsonify({
        "audio":      get_files_data("audios"),
        "background": get_files_data("backgrounds"),
        "thumbnails": get_files_data("thumbnails"),
        "base_video": get_files_data("base_videos") # <--- TAMBAHAN
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
        if not lines:
            return jsonify({"status": "error", "message": "File .txt kosong atau tidak ada baris valid."}), 400

        global database_channel
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

@app.route('/api/stop_task/<int:task_id>', methods=['POST'])
def stop_task(task_id):
    stop_flags[task_id] = True
    return jsonify({"status": "success", "message": "Dihentikan!"})

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
        t_id = int(time.time()) + i
        
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
            "mp3_per_video": data.get('mp3_per_video', 5), 
            "bg_count": data.get('bg_count', 1), 
            "target_duration_hours": vid_duration,
            "vis_mode": data.get('vis_mode'), "vis_preset": data.get('vis_preset'),
            "vis_presets_allowed": data.get('vis_presets_allowed', []), 
            
            # 🔥 UBAH BARIS DESKRIPSI INI 🔥
            "description": descs[i] if i < len(descs) else data.get('description', ''), 
            
            "tags": data.get('tags', ''), "privacy": data.get('privacy', 'public'), "playlist_id": data.get('playlist_id', ''),
            "use_floating_card": data.get('use_floating_card', False)
        }
        with db_lock:
            # 🔥 KITA SIMPAN BLUEPRINT KE DALAM DATABASE AGAR TIDAK HILANG SAAT RESTART 🔥
            active_tasks.append({
                "id": t_id, 
                "title": blueprint['title'], 
                "time": blueprint['publish_date'], 
                "status": "In Factory Queue ⚙️", 
                "type": "📺 VOD",
                "blueprint": blueprint 
            })
        render_queue.put(blueprint)
        
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
threading.Thread(target=telegram_reminder_worker, daemon=True).start()

# --- Endpoint Telegram Settings ---
@app.route('/api/save_telegram', methods=['POST'])
def save_telegram():
    data = request.json
    bot_config = load_bot_config()
    bot_config['tg_token'] = data.get('token', '')
    bot_config['tg_chat_id'] = data.get('chat_id', '')
    
    with open(CONFIG_FILE, 'w') as f: 
        json.dump(bot_config, f, indent=4)
    
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
        "token": bot_config.get('tg_token', ''), 
        "chat_id": bot_config.get('tg_chat_id', '')
    })

if __name__ == '__main__':
    for t in active_tasks:
        # Deteksi semua task yang nyangkut (termasuk yang kena cooldown atau lagi upload)
        if "In Factory Queue" in t['status'] or "Rendering" in t['status'] or "Antrean" in t['status'] or "Mengunggah" in t['status']:
            
            # 🔥 JIKA ADA BLUEPRINT-NYA, KEMBALIKAN KE MESIN RENDER 🔥
            if "blueprint" in t:
                t['status'] = "In Factory Queue ⚙️ (Dilanjutkan)"
                render_queue.put(t["blueprint"])
            else:
                # Jaga-jaga untuk task versi lama yang belum punya blueprint
                t['status'] = "Dibatalkan (Data Hilang) ⚠️"
                history_tasks.insert(0, t)
                
    # Bersihkan task yang dibatalkan saja, biarkan yang dilanjutkan
    active_tasks = [t for t in active_tasks if "Dibatalkan" not in t['status']]
    save_tasks_db()
    
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
