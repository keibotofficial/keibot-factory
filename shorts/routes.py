"""
shorts/routes.py

Blueprint API untuk modul Shorts — Sinkron dengan Visualizer v2.0,
Template Metadata YouTube (Title Mode, Deskripsi, Tags, Privasi),
Jadwal Publish (Auto-Scheduler), Custom Thumbnail Shorts (9:16), & Global Task Monitor.
"""

import os
import time
import uuid
import json
import random
from datetime import datetime, timedelta
from core.job_state import terminal
from flask import Blueprint, request, jsonify, Response, send_from_directory

from .queue_worker import read_db, write_db, update_db, OUTPUT_DIR

try:
    from .queue_worker import _pick_random_asset
except ImportError:
    _pick_random_asset = None

try:
    from .render_engine import render_preview
except ImportError:
    render_preview = None

shorts_bp = Blueprint('shorts', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_DB_FILE = os.path.join(BASE_DIR, 'channels_db.json')

SHORTS_EFFECT_NAMES = [
    "Visualizer (Bar Bawah)",
    "Visualizer (Radial Bar Biasa)",
    "Visualizer (Waveform Garis)",
    "Zoom Jedag-Jedug",
    "Glitch Beat",
    "Color Boost (CapCut Vibe)",
    "Visualizer (Particle Burst)"
]


def _get_channel_info_and_titles(yt_id):
    """Mengambil nama channel dan daftar Title Bank dari channels_db.json."""
    if os.path.exists(CHANNELS_DB_FILE):
        try:
            with open(CHANNELS_DB_FILE, 'r', encoding='utf-8') as f:
                channels = json.load(f)
            for c in channels:
                if c.get('yt_id') == yt_id:
                    return c.get('name', 'Shorts Channel'), c.get('title_bank', [])
        except Exception:
            pass
    return 'Shorts Channel', []


@shorts_bp.route('/effects', methods=['GET'])
def list_effects():
    """Daftar efek untuk dropdown Manajer Efek di shorts.html."""
    return jsonify({"success": True, "data": SHORTS_EFFECT_NAMES})


@shorts_bp.route('/start', methods=['POST'])
def start_batch():
    data = request.json or {}
    channel_id = data.get('channelId') or data.get('yt_id')
    platforms = data.get('platforms', [])
    effects = data.get('effects', [])
    vis_preset = data.get('vis_preset', {})
    batch_count = max(1, int(data.get('batchCount', 1)))
    duration_sec = int(data.get('durationSec', 30))
    bg_source = data.get('bgSource', 'shorts_bg')
    audio_source = data.get('audioSource', 'non_cid')
    bg_path = data.get('bgPath')
    audio_path = data.get('audioPath')

    # 🔥 TAMBAHAN BARU UNTUK TIKTOK & FB REELS:
    fb_page_id = data.get('fb_page_id')
    
    # 🔥 TANGKAP METADATA YOUTUBE & JADWAL PUBLISH DARI SHORTS.HTML
    title_mode = data.get('title_mode', 'title_bank')
    generated_titles = data.get('generated_titles', [])
    raw_description = data.get('description', '🎧 Selamat mendengarkan!\n\n#Shorts #Music #YouTubeShorts')
    generated_descriptions = data.get('generated_descriptions', [])
    tags = data.get('tags', 'shorts, youtube shorts, music shorts, visualizer')
    privacy = data.get('privacy', 'public')
    start_date_str = data.get('start_date', '')
    interval_hours = float(data.get('interval_hours', 0))
    generated_publish_dates = data.get('generated_publish_dates', [])

    if not channel_id:
        return jsonify({"success": False, "error": {"message": "Channel/Brand wajib dipilih"}}), 400
    if any(platform not in ('youtube', 'facebook') for platform in platforms):
        return jsonify({'success': False, 'error': {'message': 'Platform tidak didukung.'}}), 400
    if not platforms:
        return jsonify({"success": False, "error": {"message": "Pilih minimal 1 platform upload"}}), 400

    ch_name, title_bank = _get_channel_info_and_titles(channel_id)

    base_job_id = uuid.uuid4().hex
    jobs = []

    # Hitung base_date jika start_date dikirim
    try:
        base_dt = datetime.strptime(start_date_str[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        base_dt = datetime.now()

    if not vis_preset or not isinstance(vis_preset, dict):
        vis_preset = {
            "use_floating_card": True,
            "floating_card_mode": "intro_5s",
            "layers": effects
        }
    elif "layers" not in vis_preset:
        vis_preset["layers"] = effects

    for i in range(batch_count):
        job_id = f"{base_job_id}_{i + 1}"

        # 1. Tentukan Judul per Shorts
        if i < len(generated_titles) and str(generated_titles[i]).strip():
            job_title = str(generated_titles[i]).strip()
        elif title_bank:
            raw_title = random.choice(title_bank).strip()
            job_title = raw_title if "#shorts" in raw_title.lower() else f"{raw_title} #Shorts"
        else:
            job_title = f"Shorts #{i + 1} - {ch_name} #Shorts"

        if "#shorts" not in job_title.lower():
            job_title = f"{job_title} #Shorts"

        # 2. Tentukan Deskripsi per Shorts
        if i < len(generated_descriptions) and str(generated_descriptions[i]).strip():
            job_desc = str(generated_descriptions[i]).strip()
        else:
            job_desc = raw_description

        # 3. Tentukan Jadwal Publish per Shorts
        if i < len(generated_publish_dates) and str(generated_publish_dates[i]).strip():
            pub_date_str = str(generated_publish_dates[i]).strip().replace('T', ' ')
        else:
            item_dt = base_dt + timedelta(hours=i * interval_hours)
            pub_date_str = item_dt.strftime("%Y-%m-%d %H:%M")

        jobs.append({
            "id": job_id,
            "yt_id": channel_id,
            "channel_name": ch_name,
            "title": job_title,
            "publish_date": pub_date_str,
            "status": "Menunggu Giliran (Queue) ⏳",
            "progress": 0,
            "stage": f"Dijadwalkan: {pub_date_str}",
            "batch_index": i + 1,
            "total_batch": batch_count,
            "options": {
                "channelId": channel_id,
                "title": job_title,
                "title_mode": title_mode,
                "description": job_desc,
                "tags": tags,
                "privacy": privacy,
                "publish_date": pub_date_str,
                "platforms": platforms,
                "fb_page_id": fb_page_id,          # <-- TAMBAHKAN INI
                "effects": effects,
                "vis_preset": vis_preset,
                "durationSec": duration_sec,
                "bgSource": bg_source,
                "audioSource": audio_source,
                "bgPath": bg_path,
                "audioPath": audio_path,
                "useShortsThumb": True,
            },
        })

    update_db(lambda current: current.extend(jobs))
    return jsonify({
        "success": True,
        "data": {
            "baseJobId": base_job_id,
            "message": f"{batch_count} video Shorts ({duration_sec} detik) berhasil dijadwalkan & masuk antrean!"
        },
    })


@shorts_bp.route('/status/<job_id>', methods=['GET'])
def stream_status(job_id):
    def event_stream():
        last_status, last_progress = None, None
        while True:
            jobs = read_db()
            job = next((j for j in jobs if j.get('id') == job_id), None)
            if not job:
                yield f"data: {json.dumps({'error': 'Job tidak ditemukan'})}\n\n"
                break
            if job.get('status') != last_status or job.get('progress') != last_progress:
                yield f"data: {json.dumps(job)}\n\n"
                last_status, last_progress = job.get('status'), job.get('progress')
            st_str = str(job.get('status', '')).lower()
            if terminal(job):
                break
            time.sleep(1.5)

    return Response(event_stream(), mimetype="text/event-stream")


@shorts_bp.route('/jobs', methods=['GET'])
def get_all_jobs():
    jobs = read_db()
    return jsonify(jobs[-30:])


@shorts_bp.route('/jobs/clear', methods=['DELETE'])
def clear_jobs():
    update_db(lambda jobs: [job for job in jobs if not terminal(job)])
    return jsonify({"success": True, "message": "Riwayat antrean Shorts dibersihkan."})


@shorts_bp.route('/download/<job_id>', methods=['GET'])
def download_result(job_id):
    jobs = read_db()
    job = next((j for j in jobs if j.get('id') == job_id), None)
    if not job or not job.get('output_file'):
        return jsonify({"success": False, "error": "File belum siap"}), 404
    return send_from_directory(OUTPUT_DIR, job.get('output_file'), as_attachment=True)


@shorts_bp.route('/preview', methods=['POST'])
def preview_effect():
    if not render_preview or not _pick_random_asset:
        return jsonify({"success": False, "error": {"message": "Gunakan Real-Time Live Preview langsung di browser."}}), 400

    data = request.json or {}
    channel_id = data.get('channelId')
    effects = data.get('effects', [])

    if not channel_id:
        return jsonify({"success": False, "error": {"message": "channelId wajib diisi"}}), 400

    bg_path = data.get('bgPath') or _pick_random_asset(channel_id, 'shorts_bg') or _pick_random_asset(channel_id, 'backgrounds')
    audio_path = data.get('audioPath') or _pick_random_asset(channel_id, 'audio_non_cid') or _pick_random_asset(channel_id, 'audio_cid')

    if not bg_path or not audio_path:
        return jsonify({"success": False, "error": {"message": "Background/audio kosong di Asset Gallery channel ini."}}), 400

    try:
        preview_path = render_preview(bg_path, audio_path, effects, preview_seconds=3)
        filename = os.path.basename(preview_path)
        preview_dir = os.path.dirname(preview_path)
        final_preview_path = os.path.join(OUTPUT_DIR, filename)
        if preview_dir != OUTPUT_DIR:
            os.replace(preview_path, final_preview_path)
        return jsonify({"success": True, "data": {"url": f"/static/shorts_output/{filename}"}})
    except Exception as e:
        return jsonify({"success": False, "error": {"message": str(e)}}), 500