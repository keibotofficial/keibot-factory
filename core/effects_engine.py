"""
core/effects_engine.py

Library efek visualizer headless — porting dari "KeiBot Shorts Studio v10.0"
(desktop app, CustomTkinter) ke modul murni Python/OpenCV yang bisa dipanggil
dari server (Flask, headless, tanpa GUI).

STATUS: Ini mem-port 10 efek dari script Shorts. VisualEngine milik Factory
(app.py) BELUM digabung ke sini — itu langkah terpisah, sengaja tidak
disentuh dulu karena sudah production dan berjalan (lihat catatan di bagian
bawah file).

Cara pakai per-frame (dipanggil dari render loop, baik render final maupun
render preview singkat):

    from core.effects_engine import EffectsStack

    stack = EffectsStack(fps=30)
    stack.add_effect("Visualizer (Trap Wings - Solid Stacked)")
    stack.add_overlay(logo_img_rgba, is_logo=True)

    for t in frame_times:
        frame = stack.apply(frame, t, spectrum_norm, beat_times)

Semua efek stateful (partikel, dsb) disimpan di dalam objek EffectsStack,
BUKAN variabel global — jadi aman dipanggil paralel untuk banyak job render
sekaligus (beda job = beda instance EffectsStack).
"""

import random
import numpy as np
import cv2


# ============================================================
# HELPER WARNA (identik dengan versi desktop)
# ============================================================
def hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))


def get_gradient_color(c1, c2, ratio):
    ratio = np.clip(ratio, 0.0, 1.0)
    return (
        int(c2[0] + (c1[0] - c2[0]) * ratio),
        int(c2[1] + (c1[1] - c2[1]) * ratio),
        int(c2[2] + (c1[2] - c2[2]) * ratio),
    )


def get_rainbow_color(ratio):
    ratio = np.clip(ratio, 0.0, 1.0)
    hue = int(ratio * 179)
    hsv_pixel = np.uint8([[[hue, 255, 255]]])
    rgb_pixel = cv2.cvtColor(hsv_pixel, cv2.COLOR_HSV2RGB)[0][0]
    return (int(rgb_pixel[0]), int(rgb_pixel[1]), int(rgb_pixel[2]))


# ============================================================
# HELPER GAMBAR (glow / gradient polygon) — identik dengan versi desktop
# ============================================================
def draw_gradient_polygon_vertical(frame, pts, color1_rgb, color2_rgb, color_mode="Solid / Gradien", flash=0.0):
    pts = np.array(pts, dtype=np.int32)
    if len(pts) < 3:
        return
    x_min, y_min = np.min(pts, axis=0)
    x_max, y_max = np.max(pts, axis=0)
    h_frame, w_frame = frame.shape[:2]
    x_min, y_min = max(0, int(x_min)), max(0, int(y_min))
    x_max, y_max = min(w_frame, int(x_max)), min(h_frame, int(y_max))
    patch_h, patch_w = y_max - y_min, x_max - x_min
    if patch_h <= 0 or patch_w <= 0:
        return

    y_coords = np.arange(patch_h, dtype=np.float32)
    ratio = 1.0 - (y_coords / max(1.0, float(patch_h - 1)))
    gradient_patch = np.zeros((patch_h, patch_w, 3), dtype=np.uint8)

    if color_mode == "Dynamic RGB Shift":
        hues = (ratio * 179).astype(np.uint8)
        sats = np.full((patch_h,), 255, dtype=np.uint8)
        vals = np.full((patch_h,), 255, dtype=np.uint8)
        hsv_col = np.stack([hues, sats, vals], axis=1).reshape((patch_h, 1, 3))
        gradient_patch[:, :] = cv2.cvtColor(hsv_col, cv2.COLOR_HSV2RGB)
    elif color_mode in ("Solid / Gradien", "Dual-Color Flash"):
        c1 = np.array(color1_rgb, dtype=np.float32)
        c2 = np.array(color2_rgb, dtype=np.float32)
        if color_mode == "Dual-Color Flash":
            c1 = c1 * (1.0 + flash * 0.8)
            c2 = c2 * (1.0 + flash * 0.8)
        colors_col = c2 + np.outer(ratio, (c1 - c2))
        colors_col = np.clip(colors_col, 0, 255).astype(np.uint8)
        gradient_patch[:, :] = colors_col[:, np.newaxis, :]
    else:
        gradient_patch[:, :] = color1_rgb

    mask = np.zeros((patch_h, patch_w), dtype=np.uint8)
    pts_shifted = pts - np.array([x_min, y_min])
    cv2.fillPoly(mask, [pts_shifted], 255)
    frame_crop = frame[y_min:y_max, x_min:x_max]
    mask_3ch = mask[:, :, np.newaxis] > 0
    np.copyto(frame_crop, gradient_patch, where=mask_3ch)


def draw_glow_polygon(frame, pts, color, blur_ksize=31, intensity=1.0):
    pts = np.array(pts, dtype=np.int32)
    if len(pts) < 3:
        return
    h_frame, w_frame = frame.shape[:2]
    pad = int(blur_ksize)
    x_min = max(0, int(np.min(pts[:, 0])) - pad)
    y_min = max(0, int(np.min(pts[:, 1])) - pad)
    x_max = min(w_frame, int(np.max(pts[:, 0])) + pad)
    y_max = min(h_frame, int(np.max(pts[:, 1])) + pad)
    pw, ph = x_max - x_min, y_max - y_min
    if pw <= 0 or ph <= 0:
        return

    layer = np.zeros((ph, pw, 3), dtype=np.uint8)
    pts_shifted = pts - np.array([x_min, y_min])
    cv2.fillPoly(layer, [pts_shifted], color)

    k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
    k = max(3, k)
    layer = cv2.GaussianBlur(layer, (k, k), 0)

    roi = frame[y_min:y_max, x_min:x_max]
    blended = cv2.add(roi, (layer.astype(np.float32) * intensity).clip(0, 255).astype(np.uint8))
    frame[y_min:y_max, x_min:x_max] = blended


def draw_rainbow_glow_polygon(frame, pts, cx, cy, hue_offset=0.0, blur_ksize=31, intensity=1.0):
    pts = np.array(pts, dtype=np.int32)
    if len(pts) < 3:
        return
    h_frame, w_frame = frame.shape[:2]
    pad = int(blur_ksize)
    x_min = max(0, int(np.min(pts[:, 0])) - pad)
    y_min = max(0, int(np.min(pts[:, 1])) - pad)
    x_max = min(w_frame, int(np.max(pts[:, 0])) + pad)
    y_max = min(h_frame, int(np.max(pts[:, 1])) + pad)
    pw, ph = x_max - x_min, y_max - y_min
    if pw <= 0 or ph <= 0:
        return

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
    rainbow_rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)

    layer = np.zeros((ph, pw, 3), dtype=np.uint8)
    mask_bool = mask > 0
    layer[mask_bool] = rainbow_rgb[mask_bool]

    k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
    k = max(3, k)
    layer = cv2.GaussianBlur(layer, (k, k), 0)

    roi = frame[y_min:y_max, x_min:x_max]
    blended = cv2.add(roi, (layer.astype(np.float32) * intensity).clip(0, 255).astype(np.uint8))
    frame[y_min:y_max, x_min:x_max] = blended


def draw_dualcolor_glow_polygon(frame, pts, color1, color2, blur_ksize=31, intensity=1.0, flash=0.0):
    pts = np.array(pts, dtype=np.int32)
    if len(pts) < 3:
        return
    h_frame, w_frame = frame.shape[:2]
    pad = int(blur_ksize)
    x_min = max(0, int(np.min(pts[:, 0])) - pad)
    y_min = max(0, int(np.min(pts[:, 1])) - pad)
    x_max = min(w_frame, int(np.max(pts[:, 0])) + pad)
    y_max = min(h_frame, int(np.max(pts[:, 1])) + pad)
    pw, ph = x_max - x_min, y_max - y_min
    if pw <= 0 or ph <= 0:
        return

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


def make_circle_rgba(img_rgba):
    h, w = img_rgba.shape[:2]
    r = min(h, w) // 2
    cy, cx = h // 2, w // 2
    img_sq = img_rgba[cy - r:cy + r, cx - r:cx + r].copy()
    mask = np.zeros((r * 2, r * 2), dtype=np.uint8)
    cv2.circle(mask, (r, r), r, 255, -1)
    img_sq[:, :, 3] = cv2.bitwise_and(img_sq[:, :, 3], mask)
    return img_sq


# Daftar nama efek yang valid — dipakai juga oleh shorts/routes.py untuk
# validasi input dari frontend (dropdown "Manajer Efek" di shorts.html)
EFFECT_NAMES = [
    "Color Boost (CapCut Vibe)",
    "Zoom Jedag-Jedug",
    "Glitch Beat",
    "Lightning Burst",
    "Smoke / Fog",
    "Visualizer (Bar Bawah)",
    "Visualizer (Radial Bar Biasa)",
    "Visualizer (Trap Wings - Solid Stacked)",
    "Visualizer (Waveform Garis)",
    "Visualizer (Particle Burst)",
]


def new_effect_state(name, **overrides):
    """Buat 1 entry efek dengan default value — setara `add_effect()` di desktop app."""
    state = {
        'name': name,
        'intensity': 50.0,
        'thickness': 30.0,
        'size': 50.0,
        'pos_x': 50.0,
        'pos_y': 50.0,
        'color_mode': 'Solid / Gradien',
        'color1': '#FFFFFF',
        'color2': '#FF0055',
        'particles': [],
        'last_t': None,
        'last_spawn_beat': None,
    }
    state.update(overrides)
    return state


class EffectsStack:
    """
    Kumpulan efek + overlay yang dipakai pada satu render job.
    Satu instance = satu video yang sedang dirender (state efek/partikel
    tidak dibagi antar job, aman dipanggil paralel).
    """

    def __init__(self, fps=30):
        self.fps = fps
        self.effects = []   # list of dict, lihat new_effect_state()
        self.overlays = []  # list of dict: {img_raw, img_circle, x, y, scale, is_logo}

    # ---------- Manajemen efek ----------
    def add_effect(self, name, **overrides):
        if name not in EFFECT_NAMES:
            raise ValueError(f"Efek tidak dikenal: {name}")
        eff = new_effect_state(name, **overrides)
        self.effects.append(eff)
        return eff

    def remove_effect(self, index):
        self.effects.pop(index)

    def add_overlay(self, img_rgba, is_logo=False, x=50.0, y=50.0, scale=50.0):
        self.overlays.append({
            'img_raw': img_rgba,
            'img_circle': make_circle_rgba(img_rgba) if is_logo else None,
            'x': x, 'y': y, 'scale': scale, 'is_logo': is_logo,
        })

    def _apply_alpha_overlay(self, bg, overlay_data, beat_pulse_factor=1.0):
        is_logo = overlay_data['is_logo']
        overlay_img = overlay_data['img_circle'] if is_logo else overlay_data['img_raw']

        scale_pct = overlay_data['scale']
        center_x_pct = 50.0 if is_logo else overlay_data['x']
        center_y_pct = 50.0 if is_logo else overlay_data['y']

        bh, bw = bg.shape[:2]
        oh, ow = overlay_img.shape[:2]
        base_scale = (bw * (scale_pct / 100.0)) / ow
        final_scale = base_scale * beat_pulse_factor
        new_w, new_h = int(ow * final_scale), int(oh * final_scale)
        if new_w <= 0 or new_h <= 0:
            return bg

        overlay_resized = cv2.resize(overlay_img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        oh, ow = overlay_resized.shape[:2]
        cx = int(bw * (center_x_pct / 100.0))
        cy = int(bh * (center_y_pct / 100.0))
        x1, y1 = max(0, cx - ow // 2), max(0, cy - oh // 2)
        x2, y2 = min(bw, x1 + ow), min(bh, y1 + oh)
        ovl_x1 = 0 if cx - ow // 2 >= 0 else abs(cx - ow // 2)
        ovl_y1 = 0 if cy - oh // 2 >= 0 else abs(cy - oh // 2)
        ovl_x2, ovl_y2 = ovl_x1 + (x2 - x1), ovl_y1 + (y2 - y1)
        if x1 >= bw or y1 >= bh or x2 <= 0 or y2 <= 0:
            return bg

        overlay_crop = overlay_resized[ovl_y1:ovl_y2, ovl_x1:ovl_x2]
        alpha = overlay_crop[:, :, 3] / 255.0
        for c in range(3):
            bg[y1:y2, x1:x2, c] = (alpha * overlay_crop[:, :, c] + (1 - alpha) * bg[y1:y2, x1:x2, c])
        return bg

    # ---------- Render 1 frame ----------
    def apply(self, frame, t, spectrum_norm, beat_times):
        """
        frame: array BGR/RGB (720x1280 disarankan, tapi generik)
        t: waktu (detik) di dalam klip
        spectrum_norm: array (freq_bins, n_frames) dari librosa STFT — sama
                       persis format yang dipakai desktop app
        beat_times: array waktu-waktu beat (detik), dari librosa.beat.beat_track
        """
        h, w = frame.shape[:2]
        if not self.effects and not self.overlays:
            return frame

        frame_idx = min(int(t * self.fps), spectrum_norm.shape[1] - 1) if spectrum_norm is not None else 0
        current_spectrum = spectrum_norm[:, frame_idx] if spectrum_norm is not None else np.zeros(1024)

        beat_intensity = 0.0
        beat_times_arr = np.asarray(beat_times) if beat_times is not None else np.array([])
        past_beats = beat_times_arr[beat_times_arr <= t] if beat_times_arr.size else beat_times_arr
        if len(past_beats) > 0:
            time_since_beat = t - past_beats[-1]
            if time_since_beat < 0.2:
                beat_intensity = (1.0 - (time_since_beat / 0.2)) ** 2

        for eff in self.effects:
            self._apply_single_effect(frame, eff, t, w, h, current_spectrum, beat_intensity, beat_times_arr)

        pulse_factor = 1.0 + (0.2 * beat_intensity)
        for ovl in self.overlays:
            frame = self._apply_alpha_overlay(frame, ovl, pulse_factor if ovl['is_logo'] else 1.0)

        return frame

    def _apply_single_effect(self, frame, eff, t, w, h, current_spectrum, beat_intensity, beat_times_arr):
        val = eff['intensity'] / 100.0
        name = eff['name']

        if "Visualizer" in name:
            c1 = hex_to_rgb(eff['color1'])
            c2 = hex_to_rgb(eff['color2'])
            c_mode = eff['color_mode']
            cx = int(w * (eff['pos_x'] / 100.0))
            cy = int(h * (eff['pos_y'] / 100.0))
            size_factor = eff['size'] / 50.0
            thick_val = max(1, int(eff['thickness'] / 5.0))

            if name == "Visualizer (Trap Wings - Solid Stacked)":
                self._fx_trap_wings(frame, eff, cx, cy, size_factor, val, c1, c2, c_mode,
                                     current_spectrum, beat_intensity, t)
            elif name == "Visualizer (Bar Bawah)":
                self._fx_bar_bawah(frame, cx, cy, size_factor, val, thick_val, c1, c2, c_mode,
                                    current_spectrum, beat_intensity)
            elif name == "Visualizer (Radial Bar Biasa)":
                self._fx_radial_bar(frame, cx, cy, size_factor, val, thick_val, c1, c2, c_mode,
                                     current_spectrum, beat_intensity, h)
            elif name == "Visualizer (Waveform Garis)":
                self._fx_waveform(frame, cx, cy, size_factor, val, thick_val, c1, c2, c_mode, current_spectrum)
            elif name == "Visualizer (Particle Burst)":
                self._fx_particle_burst(frame, eff, w, h, val, thick_val, size_factor, c1, c2, c_mode,
                                         t, beat_times_arr)
        else:
            if name == "Color Boost (CapCut Vibe)":
                hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV).astype(np.float32)
                hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (1.0 + val), 0, 255)
                frame[:] = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
            elif name == "Glitch Beat" and beat_intensity > 0.05:
                shift = int(val * 25 * beat_intensity)
                if shift > 0:
                    frame[:, :-shift, 0] = frame[:, shift:, 0]
                    frame[:, shift:, 2] = frame[:, :-shift, 2]
            elif name == "Zoom Jedag-Jedug" and beat_intensity > 0.01:
                punch = beat_intensity ** 0.5
                zoom = 1.0 + (val * 0.45 * punch)
                new_h, new_w = int(h / zoom), int(w / zoom)
                y1, x1 = (h - new_h) // 2, (w - new_w) // 2
                frame[:] = cv2.resize(frame[y1:y1 + new_h, x1:x1 + new_w], (w, h))
            elif name == "Lightning Burst" and beat_intensity > 0.6:
                num_lines = int(val * 3) + 1
                for _ in range(num_lines):
                    start_x = random.randint(0, w)
                    pts = [(start_x, 0)]
                    curr_x, curr_y = start_x, 0
                    while curr_y < h // 2:
                        curr_x += random.randint(-30, 30)
                        curr_y += random.randint(20, 50)
                        pts.append((curr_x, curr_y))
                    cv2.polylines(frame, [np.array(pts)], False, (255, 255, 255),
                                  max(1, int(val * 4 * beat_intensity)))
            elif name == "Smoke / Fog":
                noise = np.random.randint(0, 100, (h // 4, w // 4, 3), dtype=np.uint8)
                noise = cv2.resize(noise, (w, h), interpolation=cv2.INTER_LINEAR)
                noise = cv2.GaussianBlur(noise, (51, 51), 0)
                alpha = val * 0.3
                frame[:] = cv2.addWeighted(frame, 1 - alpha, noise, alpha, 0)

    # ---------- Implementasi tiap efek visualizer ----------
    def _fx_trap_wings(self, frame, eff, cx, cy, size_factor, val, c1, c2, c_mode,
                        current_spectrum, beat_intensity, t):
        base_radius = int(frame.shape[1] * 0.22 * size_factor)
        num_bands, high_res = 12, 240
        spec_len = current_spectrum.shape[0]
        cutoff = max(num_bands, int(spec_len * 0.5))
        idx = np.linspace(0, cutoff - 1, num_bands).astype(int)
        band_data = current_spectrum[idx].astype(np.float32)

        idle_wiggle = 6 * np.sin(t * 1.8 + np.arange(num_bands) * 1.1)
        band_data = band_data + idle_wiggle
        b_min, b_max = float(band_data.min()), float(band_data.max())
        b_range = max(1e-4, b_max - b_min)
        band_norm = (band_data - b_min) / b_range
        band_sharp = band_norm ** 1.4

        soft_kernel = np.array([0.1, 0.2, 0.4, 0.2, 0.1])
        padded_soft = np.concatenate([band_sharp[-2:], band_sharp, band_sharp[:2]])
        band_soft = np.convolve(padded_soft, soft_kernel, mode='valid')
        overall_energy = np.clip(np.mean(band_data) / 180.0, 0.15, 1.0)

        pulse_r = base_radius + (beat_intensity * 18 * val)
        max_amp = (140 * val + 10) * (0.5 + 0.5 * overall_energy)

        angles_ctrl = np.linspace(-np.pi / 2, -np.pi / 2 + 2 * np.pi, num_bands, endpoint=False)
        angles_ctrl_ext = np.concatenate([angles_ctrl, [angles_ctrl[0] + 2 * np.pi]])
        angles_hi = np.linspace(-np.pi / 2, -np.pi / 2 + 2 * np.pi, high_res, endpoint=False)

        def _interp_radii(band_arr, extra_amp_mult, extra_offset):
            arr_ext = np.concatenate([band_arr, [band_arr[0]]])
            hi = np.interp(angles_hi, angles_ctrl_ext, arr_ext)
            return pulse_r + extra_offset + (hi * max_amp * extra_amp_mult)

        radii_crown = _interp_radii(band_sharp, 1.0, 0.0)
        radii_glow_rim = _interp_radii(band_sharp, 1.05, 4.0)
        radii_glow_mid = _interp_radii(band_sharp, 1.35, 14.0)
        radii_glow_outer = _interp_radii(band_soft, 1.7, 26.0)

        def _pts(radii):
            return [(cx + r * np.cos(a), cy + r * np.sin(a)) for a, r in zip(angles_hi, radii)]

        crown_pts, glow_rim_pts = _pts(radii_crown), _pts(radii_glow_rim)
        glow_mid_pts, glow_outer_pts = _pts(radii_glow_mid), _pts(radii_glow_outer)

        if c_mode == "Dynamic RGB Shift":
            hue_offset = (t * 25) % 179
            draw_rainbow_glow_polygon(frame, glow_outer_pts, cx, cy, hue_offset, 71, 0.5)
            draw_rainbow_glow_polygon(frame, glow_mid_pts, cx, cy, hue_offset, 31, 0.85)
            draw_rainbow_glow_polygon(frame, glow_rim_pts, cx, cy, hue_offset, 9, 1.0)
        elif c_mode == "Dual-Color Flash":
            draw_dualcolor_glow_polygon(frame, glow_outer_pts, c1, c2, 71, 0.5, beat_intensity)
            draw_dualcolor_glow_polygon(frame, glow_mid_pts, c1, c2, 31, 0.85, beat_intensity)
            draw_dualcolor_glow_polygon(frame, glow_rim_pts, c1, c2, 9, 1.0, beat_intensity)
        else:
            draw_glow_polygon(frame, glow_outer_pts, c2, 71, 0.5)
            draw_glow_polygon(frame, glow_mid_pts, c2, 31, 0.85)
            draw_glow_polygon(frame, glow_rim_pts, c2, 9, 1.0)

        crown_arr = np.array(crown_pts, dtype=np.int32)
        cv2.fillPoly(frame, [crown_arr], (255, 255, 255))
        cv2.polylines(frame, [crown_arr], True, (255, 255, 255), max(1, 30 // 3))
        cv2.circle(frame, (cx, cy), int(pulse_r * 0.95), c1, -1)

    def _fx_bar_bawah(self, frame, cx, cy, size_factor, val, thick_val, c1, c2, c_mode,
                       current_spectrum, beat_intensity):
        w = frame.shape[1]
        num_bars = 40
        total_width = int(w * size_factor)
        bar_w = total_width // num_bars
        max_h = int(frame.shape[0] * 0.2)
        start_x = cx - (total_width // 2)
        sampled = current_spectrum[:num_bars // 2]
        sym_spec = np.concatenate((sampled[::-1], sampled))
        for i, amp in enumerate(sym_spec):
            bar_h = int((amp / 255.0) * max_h * val * 1.5)
            px = start_x + (i * bar_w)
            py_top = cy - bar_h
            bar_pts = [(px, cy), (px + max(1, bar_w - (10 - thick_val)), cy),
                       (px + max(1, bar_w - (10 - thick_val)), py_top), (px, py_top)]
            draw_gradient_polygon_vertical(frame, bar_pts, c1, c2, c_mode, flash=beat_intensity)

    def _fx_radial_bar(self, frame, cx, cy, size_factor, val, thick_val, c1, c2, c_mode,
                        current_spectrum, beat_intensity, h):
        w = frame.shape[1]
        base_radius = int(w * 0.2 * size_factor)
        pulse_r = base_radius + (beat_intensity * 10 * val)
        num_bars = 40
        sampled = current_spectrum[:num_bars]
        max_amp = 90 * val
        for i in range(num_bars):
            angle = i * (2 * np.pi / num_bars)
            amp_scaled = (sampled[i] / 255.0) * max_amp
            x1 = int(cx + pulse_r * np.cos(angle))
            y1 = int(cy + pulse_r * np.sin(angle))
            x2 = int(cx + (pulse_r + amp_scaled) * np.cos(angle))
            y2 = int(cy + (pulse_r + amp_scaled) * np.sin(angle))
            if c_mode == "Dynamic RGB Shift":
                color = get_rainbow_color(1.0 - (y2 / float(h)))
            else:
                color = get_gradient_color(c1, c2, i / num_bars)
            cv2.line(frame, (x1, y1), (x2, y2), color, thick_val)

    def _fx_waveform(self, frame, cx, cy, size_factor, val, thick_val, c1, c2, c_mode, current_spectrum):
        w = frame.shape[1]
        num_pts = 60
        sampled = current_spectrum[:num_pts]
        sym_spec = np.concatenate((sampled[::-1], sampled))
        total_width = int(w * size_factor)
        step = total_width / len(sym_spec)
        start_x = cx - (total_width // 2)
        for i in range(len(sym_spec) - 1):
            amp1 = (sym_spec[i] / 255.0) * (130 * val)
            amp2 = (sym_spec[i + 1] / 255.0) * (130 * val)
            pt1 = (int(start_x + i * step), int(cy - amp1))
            pt2 = (int(start_x + (i + 1) * step), int(cy - amp2))
            color = get_rainbow_color(i / len(sym_spec)) if c_mode == "Dynamic RGB Shift" \
                else get_gradient_color(c1, c2, i / len(sym_spec))
            cv2.line(frame, pt1, pt2, color, thick_val)

    def _fx_particle_burst(self, frame, eff, w, h, val, thick_val, size_factor, c1, c2, c_mode,
                            t, beat_times_arr):
        last_t = eff.get('last_t')
        dt = 0.033 if last_t is None else max(0.0, min(0.5, t - last_t))
        eff['last_t'] = t

        past_beats_local = beat_times_arr[beat_times_arr <= t] if beat_times_arr.size else beat_times_arr
        current_beat_time = past_beats_local[-1] if len(past_beats_local) > 0 else None
        is_new_beat = (
            current_beat_time is not None
            and current_beat_time != eff.get('last_spawn_beat')
            and (t - current_beat_time) < 0.08
        )

        if is_new_beat:
            eff['last_spawn_beat'] = current_beat_time
            spawn_count = int(6 + val * 18)
            for _ in range(spawn_count):
                depth = random.random()
                eff['particles'].append({
                    'x': random.uniform(0, w), 'y': random.uniform(0, h),
                    'drift_vx': random.uniform(-1, 1) * (6 + depth * 10) * size_factor,
                    'drift_vy': -random.uniform(5, 14) * (0.4 + depth * 0.8) * size_factor,
                    'wobble_amp': random.uniform(4, 14) * (0.5 + depth),
                    'wobble_freq': random.uniform(0.4, 1.2),
                    'wobble_phase': random.uniform(0, 2 * np.pi),
                    'spawn_t': t, 'life': 1.0, 'decay': random.uniform(0.12, 0.28),
                    'radius': 3 + depth * (16 + thick_val), 'depth': depth,
                    'color_seed': random.random(),
                })

        alive_particles = []
        for p in eff['particles']:
            age = t - p['spawn_t']
            p['x'] += p['drift_vx'] * dt
            p['y'] += p['drift_vy'] * dt
            p['life'] -= p['decay'] * dt
            if p['life'] <= 0:
                continue

            wobble_x = p['wobble_amp'] * np.sin(age * p['wobble_freq'] * 2 * np.pi + p['wobble_phase'])
            draw_x, draw_y = p['x'] + wobble_x, p['y']
            if not (-80 <= draw_x <= w + 80 and -80 <= draw_y <= h + 80):
                continue

            alive_particles.append(p)
            fade_in = min(1.0, age / 0.4)
            fade = float(np.clip(p['life'], 0.0, 1.0)) * fade_in
            depth = p['depth']

            if c_mode == "Dynamic RGB Shift":
                color = get_rainbow_color((p['color_seed'] + t * 0.05) % 1.0)
            else:
                color = get_gradient_color(c1, c2, p['color_seed'])

            r = max(1, int(p['radius'] * (0.6 + 0.4 * fade)))
            if depth > 0.55:
                blur_k = int(9 + depth * 22)
                square_pts = [(draw_x - r, draw_y - r), (draw_x + r, draw_y - r),
                              (draw_x + r, draw_y + r), (draw_x - r, draw_y + r)]
                draw_glow_polygon(frame, square_pts, color, blur_ksize=blur_k, intensity=fade)
            else:
                faded_color = tuple(int(ch * fade) for ch in color)
                cv2.circle(frame, (int(draw_x), int(draw_y)), r, faded_color, -1)

        eff['particles'] = alive_particles


# ============================================================
# CATATAN MIGRASI — dibaca sebelum lanjut tahap berikutnya
# ============================================================
# 1. VisualEngine Factory (app.py, class VisualEngine baris ~704) BELUM
#    dipindah ke sini. Style Factory: bottom/center/circle/horizontal/
#    frequency_bands/led_eq/spectrum_edge — semuanya masih hidup di app.py,
#    dipanggil apa adanya, TIDAK diubah. Migrasi itu langkah terpisah,
#    supaya render Factory yang sudah production tidak ikut kena resiko.
#
# 2. Fungsi analisis audio (librosa: beat_track, STFT -> spectrum_norm)
#    BELUM ada di sini — itu tetap dipanggil dari luar (mirip
#    analyze_audio_full() di desktop script), hasilnya (beat_times,
#    spectrum_norm) baru dioper ke EffectsStack.apply(). Alasan: analisis
#    audio itu 1x per file (bisa di-cache), sementara EffectsStack.apply()
#    dipanggil per-frame — dipisah biar gak re-analisis audio tiap frame.
#
# 3. Overlay/logo (add_overlay) menerima array RGBA (numpy), bukan path
#    file — proses baca file (cv2.imread + convert ke RGBA) tetap jadi
#    tanggung jawab kode pemanggil (shorts/routes.py nanti).