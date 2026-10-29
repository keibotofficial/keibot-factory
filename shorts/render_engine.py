"""
shorts/render_engine.py

Mesin render headless 9:16 (720x1280) untuk modul Shorts —
Sudah menggunakan Visualizer Engine v2.0 yang 100% kompatibel dengan Factory & Shorts Studio.
"""

import os
import shutil
import random
import subprocess
from core.media import validate_video
from core.job_state import JobCancelled
import requests
import numpy as np
import cv2
import librosa

TARGET_SIZE = (720, 1280)  # (width, height) — Vertikal 9:16 HD
FPS = 30
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
        if os.path.exists(p):
            return p
    return "ffmpeg"


def get_gradient_color(c1, c2, ratio):
    ratio = float(np.clip(ratio, 0.0, 1.0))
    return (
        int(c2[0] + (c1[0] - c2[0]) * ratio),
        int(c2[1] + (c1[1] - c2[1]) * ratio),
        int(c2[2] + (c1[2] - c2[2]) * ratio)
    )


class ShortsAudioBrain:
    """Menganalisis frekuensi Sub-Bass, Kick Drum, dan Spektrum Bar (identik dengan Factory v2.0)."""
    def __init__(self, path, start_offset=0.0, duration=30.0):
        self.y = None
        self.sr = 22050
        self.has_audio = False
        self.fast_bass = 0.0
        self.slow_bass = 0.0
        try:
            self.y, self.sr = librosa.load(path, sr=22050, mono=True, offset=start_offset, duration=duration + 1.0)
            self.has_audio = True
        except Exception as e:
            print(f"Shorts AudioBrain Error: {e}")

    def get_data(self, t, n_bars=48):
        n_bars = int(n_bars)
        if not self.has_audio or self.y is None:
            return 0.0, np.zeros(n_bars)
        idx = int(t * self.sr)
        if idx >= len(self.y):
            return 0.0, np.zeros(n_bars)

        bass_val = 0.0
        final_bars = np.zeros(n_bars)
        try:
            n_fft = 2048
            fft_data = self.y[idx:idx + n_fft]
            if len(fft_data) == n_fft:
                spec = np.abs(np.fft.rfft(fft_data * np.hanning(n_fft)))
                raw_bass = float(np.max(spec[1:8]) * 0.65 + np.mean(spec[1:8]) * 0.35) / 25.0
                bass_val = float(np.clip(raw_bass, 0.0, 1.5))

                self.fast_bass = (self.fast_bass * 0.2 + bass_val * 0.8) if bass_val > self.fast_bass else (self.fast_bass * 0.75 + bass_val * 0.25)
                self.slow_bass = (self.slow_bass * 0.92) + (bass_val * 0.08)

                onset = max(0.0, self.fast_bass - (self.slow_bass * 0.85))
                bass_val = float(np.clip((onset * 2.8) + ((bass_val ** 2.5) * 0.5), 0.0, 1.0))

                usable = spec[2:200]
                ls = len(usable)
                if ls > 0:
                    half_n = max(1, n_bars // 2)
                    raw_bars = np.zeros(half_n)
                    for i in range(half_n):
                        s = int((i / half_n) * ls)
                        e = max(s + 1, min(ls, int(((i + 1) / half_n) * ls)))
                        raw_bars[i] = np.mean(usable[s:e]) / 15.0
                    smooth_half = np.convolve(raw_bars, np.ones(3) / 3, mode='same')
                    final_bars = np.concatenate((smooth_half[::-1], smooth_half))[:n_bars]
        except Exception:
            pass
        return bass_val, final_bars


class ShortsVisualEngine:
    """Mesin Visualizer & Efek 9:16 (720x1280) — Sinkron dengan Live Preview Shorts Studio."""
    def __init__(self):
        self.frame_count = 0
        self.beat_intensity = 0.0
        self.layer_states = {}
        self.rainbow_v = []
        for d in range(1000):
            hue = int((d / 999.0) * 179)
            bgr = cv2.cvtColor(np.uint8([[[hue, 255, 255]]]), cv2.COLOR_HSV2BGR)[0][0]
            self.rainbow_v.append((int(bgr[0]), int(bgr[1]), int(bgr[2])))

    def process(self, frame, vol, bars, cfg):
        self.frame_count += 1
        t = self.frame_count / float(FPS)

        target_beat = float(np.clip(vol, 0.0, 1.0))
        if target_beat > self.beat_intensity:
            self.beat_intensity = (self.beat_intensity * 0.30) + (target_beat * 0.70)
        else:
            self.beat_intensity = (self.beat_intensity * 0.80) + (target_beat * 0.20)

        h, w = frame.shape[:2]
        current_spectrum = np.clip(bars * 255, 0, 255)
        layers = cfg.get('layers', [])
        scale_res = w / 360.0  # Skala terhadap kanvas preview 360x640

        for idx, layer in enumerate(layers):
            l_name = layer.get('name', '')
            l_id = layer.get('id', f"{l_name}_{idx}")
            if l_id not in self.layer_states:
                self.layer_states[l_id] = {'particles': [], 'last_redirect_t': -999.0}
            state = self.layer_states[l_id]

            val = float(layer.get('intensity', 50)) / 100.0
            c1_hex = layer.get('color1', '#ffb703').lstrip('#')
            c2_hex = layer.get('color2', '#00d4ff').lstrip('#')
            c1 = tuple(int(c1_hex[i:i+2], 16) for i in (4, 2, 0)) if len(c1_hex) == 6 else (3, 183, 255)
            c2 = tuple(int(c2_hex[i:i+2], 16) for i in (4, 2, 0)) if len(c2_hex) == 6 else (255, 212, 0)
            c_mode = layer.get('color_mode', 'Solid / Gradien')
            size_factor = float(layer.get('size', 75)) / 100.0
            cx = int(w * (float(layer.get('pos_x', 50)) / 100.0))
            cy = int(h * (float(layer.get('pos_y', 82)) / 100.0))

            # 1. EFEK BACKGROUND (COLOR BOOST, GLITCH, SMOOTH ZOOM JEDAG-JEDUG)
            if "Color Boost" in l_name:
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
                hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (1.0 + val), 0, 255)
                frame = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_BGR2HSV if False else cv2.COLOR_HSV2BGR)

            elif "Glitch Beat" in l_name and self.beat_intensity > 0.05:
                shift = int(val * 24 * self.beat_intensity * scale_res)
                if 0 < shift < w:
                    fc = frame.copy()
                    frame[:, shift:, 2] = fc[:, :-shift, 2]
                    frame[:, :-shift, 0] = fc[:, shift:, 0]

            elif "Zoom Jedag-Jedug" in l_name and self.beat_intensity > 0.005:
                zoom = 1.0 + (val * 0.38 * self.beat_intensity)
                M = np.float32([[zoom, 0, (1.0 - zoom) * (w / 2.0)], [0, zoom, (1.0 - zoom) * (h / 2.0)]])
                frame = cv2.warpAffine(frame, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)

            # 2. VISUALIZER BAR / RADIAL / WAVEFORM
            elif "Visualizer" in l_name and "Particle" not in l_name:
                num_bars = max(10, int(layer.get('num_bars', 48)))
                max_h = h * 0.25 * (float(layer.get('height_scale', 45)) / 50.0)
                half_idx = len(current_spectrum) // 2
                straight_spec = current_spectrum[half_idx:]
                cutoff = max(1, len(straight_spec))

                s_key = f"bars_{l_id}"
                if s_key not in self.layer_states or len(self.layer_states[s_key]) != num_bars:
                    self.layer_states[s_key] = np.zeros(num_bars)
                bar_h_arr = self.layer_states[s_key]

                for i in range(num_bars):
                    pr = abs(np.sin((i + 1) * 12.9898) * 43758.5453) % 1.0
                    src_i = max(0, min(cutoff - 1, int(pr * cutoff)))
                    raw_n = (float(straight_spec[src_i]) / 255.0) * (0.5 + ((abs(np.cos((i + 1) * 78.233) * 43758.5453) % 1.0) * 0.5))
                    target = (min(1.0, raw_n) ** 2.3) * 0.85
                    bar_h_arr[i] = (bar_h_arr[i] * 0.25 + target * 0.75) if target > bar_h_arr[i] else (bar_h_arr[i] * 0.65 + target * 0.35)

                if "Bar Bawah" in l_name:
                    tot_w = float(w * size_factor)
                    start_x = cx - (tot_w / 2.0)
                    slot_w = tot_w / float(num_bars)
                    space = min(float(layer.get('bar_gap', 3)) * scale_res, max(0.0, slot_w - 1.0))
                    bar_w = max(1, int(round((slot_w - space) * (float(layer.get('thickness', 35)) / 100.0))))
                    offset = (slot_w - bar_w) / 2.0

                    for i in range(num_bars):
                        ratio = i / max(1, num_bars - 1)
                        bh = max(2, int(bar_h_arr[i] * max_h * (1.0 + self.beat_intensity * val * 0.35)))
                        x1 = max(0, min(w, int(round(start_x + slot_w * i + offset))))
                        x2 = max(0, min(w, x1 + bar_w))
                        y1, y2 = max(0, min(h, cy - bh)), max(0, min(h, cy))
                        if x2 > x1 and y2 > y1:
                            col = self.rainbow_v[int(((ratio + t * 0.2) % 1.0) * 999)] if c_mode == "Dynamic RGB Shift" else get_gradient_color(c2, c1, 0.6)
                            cv2.rectangle(frame, (x1, y1), (x2, y2), col, -1)

                elif "Radial Bar" in l_name:
                    base_r = min(w, h) * 0.28 * size_factor
                    pulse_r = base_r + (self.beat_intensity * 10.0 * val * scale_res)
                    slot_arc = (2.0 * np.pi * pulse_r) / float(num_bars)
                    space = min(float(layer.get('bar_gap', 3)) * scale_res, max(0.0, slot_arc - 1.0))
                    bar_w = max(1, int(round((slot_arc - space) * (float(layer.get('thickness', 35)) / 100.0))))

                    for i in range(num_bars):
                        ratio = i / max(1, num_bars - 1)
                        bh = max(2, int(bar_h_arr[i] * (max_h * 0.65) * (1.0 + self.beat_intensity * val * 0.35)))
                        ang = (i / float(num_bars)) * 2.0 * np.pi - (np.pi / 2.0)
                        pt1 = (int(cx + np.cos(ang) * pulse_r), int(cy + np.sin(ang) * pulse_r))
                        pt2 = (int(cx + np.cos(ang) * (pulse_r + bh)), int(cy + np.sin(ang) * (pulse_r + bh)))
                        col = self.rainbow_v[int(((ratio + t * 0.2) % 1.0) * 999)] if c_mode == "Dynamic RGB Shift" else c2
                        cv2.line(frame, pt1, pt2, col, bar_w, cv2.LINE_AA)

                    inner_r = max(4, int(pulse_r - 3 * scale_res))
                    if layer.get('use_logo', True):
                        if not hasattr(self, 'cached_logo'):
                            self.cached_logo = None
                            thumb_url = cfg.get('channel_thumbnail', '')
                            if thumb_url:
                                try:
                                    r_img = requests.get(thumb_url, timeout=5)
                                    if r_img.status_code == 200:
                                        self.cached_logo = cv2.imdecode(np.frombuffer(r_img.content, np.uint8), cv2.IMREAD_COLOR)
                                except Exception:
                                    pass
                            if self.cached_logo is None:
                                fb = os.path.join(BASE_DIR, 'static', 'logo.png')
                                if os.path.exists(fb):
                                    self.cached_logo = cv2.imread(fb, cv2.IMREAD_COLOR)
                        if self.cached_logo is not None:
                            diam = inner_r * 2
                            x1_l, y1_l = cx - inner_r, cy - inner_r
                            if x1_l >= 0 and y1_l >= 0 and x1_l + diam <= w and y1_l + diam <= h and diam > 4:
                                l_res = cv2.resize(self.cached_logo, (diam, diam), interpolation=cv2.INTER_AREA)
                                mask = np.zeros((diam, diam), dtype=np.uint8)
                                cv2.circle(mask, (inner_r, inner_r), inner_r, 255, -1, cv2.LINE_AA)
                                roi = frame[y1_l:y1_l + diam, x1_l:x1_l + diam]
                                roi[mask > 0] = l_res[mask > 0]
                    cv2.circle(frame, (cx, cy), inner_r, c1, max(1, int(2.5 * scale_res)), cv2.LINE_AA)

                elif "Waveform" in l_name:
                    tot_w = float(w * size_factor)
                    start_x = cx - (tot_w / 2.0)
                    slot_w = tot_w / float(max(1, num_bars - 1))
                    line_w = max(2, int(round((float(layer.get('thickness', 35)) / 100.0) * 8.0 * scale_res)))
                    pts = [(int(round(start_x + slot_w * i)), int(cy - max(2, int(bar_h_arr[i] * max_h * (1.0 + self.beat_intensity * val * 0.35))))) for i in range(num_bars)]
                    for i in range(len(pts) - 1):
                        cv2.line(frame, pts[i], pts[i + 1], c2, line_w, cv2.LINE_AA)

            # 3. PARTIKEL MELAYANG + BELOK ARAH ACAK SAAT BEAT (TANPA NGEBUT)
            elif "Particle" in l_name:
                target_count = max(10, int(layer.get('part_count', 55)))
                size_scale = (float(layer.get('size', 50)) / 50.0) * scale_res
                flow_speed = max(0.15, float(layer.get('flow_speed', 40)) / 40.0)

                while len(state['particles']) < target_count:
                    ia = random.uniform(0, 2 * np.pi)
                    state['particles'].append({
                        'x': random.uniform(0, w), 'y': random.uniform(0, h),
                        'angle': ia, 'target_angle': ia,
                        'base_speed': random.uniform(0.4, 1.2) * scale_res,
                        'wobble_freq': random.uniform(0.5, 1.2), 'wobble_amp': random.uniform(0.25, 0.6),
                        'phase': random.uniform(0, 2 * np.pi), 'base_size': random.uniform(1.8, 3.2) * size_scale,
                        'ratio': random.random()
                    })
                if len(state['particles']) > target_count:
                    state['particles'] = state['particles'][:target_count]

                if self.beat_intensity > 0.38 and (t - state['last_redirect_t']) > 0.18:
                    state['last_redirect_t'] = t
                    for p in state['particles']:
                        if random.random() <= (0.3 + val * 0.7):
                            p['target_angle'] += (random.random() - 0.5) * (0.4 + val * 0.8) * 2.0 * np.pi

                for p in state['particles']:
                    p['angle'] += (p['target_angle'] - p['angle']) * 0.12
                    d_ang = p['angle'] + np.sin(t * p['wobble_freq'] + p['phase']) * p['wobble_amp']
                    p['x'] = (p['x'] + np.cos(d_ang) * p['base_speed'] * flow_speed) % w
                    p['y'] = (p['y'] + np.sin(d_ang) * p['base_speed'] * flow_speed) % h
                    r_sz = max(1, int(p['base_size'] * (1.0 + self.beat_intensity * 0.25)))
                    cv2.circle(frame, (int(p['x']), int(p['y'])), r_sz, c2, -1, cv2.LINE_AA)

        # 4. GLASSY FLOATING CARD DI ATAS LAYAR SHORTS (9:16)
        if cfg.get('use_floating_card', True):
            card_mode = cfg.get('floating_card_mode', 'intro_5s')
            song_title = cfg.get('song_title', 'Shorts Track')[:28]
            card_alpha = 1.0
            if card_mode == 'intro_5s':
                if t > 5.5:
                    card_alpha = 0.0
                elif t < 0.7:
                    card_alpha = t / 0.7
                elif t > 4.5:
                    card_alpha = max(0.0, 1.0 - (t - 4.5))

            if card_alpha > 0.02:
                cx1, cy1 = 32, 48
                cx2, cy2 = w - 32, 128
                rw, rh = cx2 - cx1, cy2 - cy1
                roi = frame[cy1:cy2, cx1:cx2].copy()
                blurred = cv2.GaussianBlur(roi, (25, 25), 0)
                tint = np.full_like(roi, (30, 18, 12))
                frosted = cv2.addWeighted(blurred, 0.55, tint, 0.45, 0)

                for b in range(3):
                    bh = max(6, min(42, int(10 + self.beat_intensity * 24 + np.sin(t * 8.0 + b) * 4)))
                    bx = 24 + b * 11
                    cv2.rectangle(frosted, (bx, rh - 18 - bh), (bx + 6, rh - 18), (3, 183, 255), -1)

                font = cv2.FONT_HERSHEY_SIMPLEX
                cv2.putText(frosted, "NOW PLAYING", (72, 32), font, 0.45, (3, 183, 255), 1, cv2.LINE_AA)
                cv2.putText(frosted, song_title, (72, 62), font, 0.72, (255, 255, 255), 2, cv2.LINE_AA)

                blended = cv2.addWeighted(roi, 1.0 - (card_alpha * 0.88), frosted, card_alpha * 0.88, 0)
                frame[cy1:cy2, cx1:cx2] = blended

        return frame


def _crop_center_9_16(frame, target_size):
    """Memotong bagian tengah gambar/video (baik 16:9 maupun ukuran lain) menjadi tepat 9:16 (720x1280)."""
    h, w = frame.shape[:2]
    target_w, target_h = target_size
    target_ratio = target_w / float(target_h)
    current_ratio = w / float(h)
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x = (w - new_w) // 2
        frame = frame[:, x:x + new_w]
    else:
        new_h = int(w / target_ratio)
        y = (h - new_h) // 2
        frame = frame[y:y + new_h, :]
    return cv2.resize(frame, target_size, interpolation=cv2.INTER_LINEAR)


def render_job(bg_path, audio_path, output_path, effects_config=None, vis_cfg=None,
               duration_range=(30, 30), progress_callback=None, cancel_callback=None):
    """Render 1 video Shorts vertikal 9:16 (720x1280) lengkap dengan Visualizer v2.0 & muxing FFmpeg."""
    def _progress(stage, pct):
        if progress_callback:
            progress_callback(stage, pct)

    _progress("Menganalisis frekuensi audio...", 10)
    try:
        audio_duration = librosa.get_duration(path=audio_path)
    except Exception:
        y_tmp, sr_tmp = librosa.load(audio_path, sr=22050)
        audio_duration = len(y_tmp) / float(sr_tmp)

    req_dur = duration_range[0] if isinstance(duration_range, (list, tuple)) else int(duration_range)
    target_duration = min(float(req_dur), float(audio_duration))
    max_start = max(0.0, audio_duration - target_duration)
    start_offset = random.uniform(0, max_start) if max_start > 2.0 else 0.0

    brain = ShortsAudioBrain(audio_path, start_offset=start_offset, duration=target_duration)
    vis = ShortsVisualEngine()

    if not vis_cfg:
        vis_cfg = {"use_floating_card": True, "floating_card_mode": "intro_5s", "layers": effects_config or []}

    _progress("Menyiapkan background 9:16...", 20)
    ext = os.path.splitext(bg_path)[1].lower()
    is_video = ext in ('.mp4', '.mov', '.avi', '.mkv', '.webm')
    cap = None
    static_bg = None

    if is_video:
        cap = cv2.VideoCapture(bg_path)
    else:
        img = cv2.imread(bg_path)
        if img is None:
            raise RuntimeError(f"Gagal membaca gambar background: {bg_path}")
        static_bg = _crop_center_9_16(img, TARGET_SIZE)

    total_frames = int(target_duration * FPS)
    w, h = TARGET_SIZE
    ffmpeg_bin = get_ffmpeg_path()

    cmd = [
        ffmpeg_bin, '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(FPS),
        '-i', '-',
        '-ss', str(start_offset), '-t', str(target_duration), '-i', audio_path,
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k',
        '-map', '0:v:0', '-map', '1:a:0',
        '-movflags', '+faststart', '-shortest',
        output_path
    ]

    partial_output = output_path + '.rendering.mp4'
    cmd[-1] = partial_output
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for i in range(total_frames):
            if i % 15 == 0 and cancel_callback and cancel_callback():
                raise JobCancelled('Dibatalkan')
            if cap is not None:
                ret, raw_f = cap.read()
                if not ret:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, raw_f = cap.read()
                frame = _crop_center_9_16(raw_f, TARGET_SIZE) if (ret and raw_f is not None) else np.zeros((h, w, 3), dtype=np.uint8)
            else:
                frame = static_bg.copy()

            vol, bars = brain.get_data(i / float(FPS), 64)
            out_frame = vis.process(frame, vol, bars, vis_cfg)
            proc.stdin.write(out_frame.tobytes())

            if i % 30 == 0:
                pct = 25 + int((i / max(1, total_frames)) * 65)
                _progress(f"Rendering Shorts 9:16 ({i}/{total_frames} frame)...", pct)
    except BaseException:
        proc.kill()
        raise
    finally:
        if cap is not None:
            cap.release()
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.wait()
        if os.path.exists(partial_output) and (proc.returncode or (cancel_callback and cancel_callback())):
            os.remove(partial_output)

    if proc.returncode != 0:
        raise RuntimeError('FFmpeg gagal merender Shorts.')
    validate_video(partial_output)
    os.replace(partial_output, output_path)
    _progress("Selesai", 100)
    return output_path


def render_preview(bg_path, audio_path, effects_config, preview_seconds=3):
    tmp_output = bg_path + f".preview_{random.randint(1000,9999)}.mp4"
    render_job(bg_path=bg_path, audio_path=audio_path, output_path=tmp_output,
               effects_config=effects_config, duration_range=(preview_seconds, preview_seconds))
    return tmp_output