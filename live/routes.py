"""
live/routes.py

Blueprint API untuk modul Live Streaming 24/7 (Multi-Channel & VIP Global Queue).
"""

import os
import json
import time
import threading
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from core.json_store import lock_for, read_json, write_json, update_json
from flask import Blueprint, request, jsonify

live_bp = Blueprint('live', __name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'live_db.json')
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, 'output', 'live_assets')
os.makedirs(OUTPUT_DIR, exist_ok=True)

_live_db_lock = lock_for(DB_PATH)

def read_db():
    data = read_json(DB_PATH, [])
    if isinstance(data, dict):
        if data.get('yt_id'):
            data.setdefault('id', int(time.time()))
            return [data]
        return []
    if not isinstance(data, list):
        raise ValueError('Format database Live tidak valid.')
    return data

def write_db(data):
    write_json(DB_PATH, data)

def update_live_item(live_id, updates: dict):
    """Fungsi bantuan untuk mengupdate 1 jadwal spesifik berdasarkan ID."""
    with _live_db_lock:
        items = read_db()
        changed = False
        for item in items:
            if str(item.get('id')) == str(live_id):
                item.update(updates)
                changed = True
                break
        if changed:
            write_db(items)
        return changed

@live_bp.route('/schedule', methods=['POST'])
def schedule_live():
    data = request.json or {}
    yt_id = data.get('yt_id')
    title = data.get('title') or '🔴 Live Streaming 24/7'
    start_time = data.get('start_time')
    try:
        duration_hours = float(data.get('duration_hours', 4))
        datetime.strptime(str(start_time)[:16], '%Y-%m-%dT%H:%M')
        if not 0 < duration_hours <= 168:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'Waktu mulai atau durasi Live tidak valid.'}), 400
    
    audios = data.get('audios', [])
    bgs = data.get('bgs', [])
    if not bgs and data.get('bg'):
        bgs = [data.get('bg')]
        
    preset = data.get('preset', '')
    thumb = data.get('thumb', '')
    desc = data.get('desc', '')
    tags = data.get('tags', '')
    privacy = data.get('privacy', 'public')
    
    if not yt_id or not start_time or not audios or not bgs:
        return jsonify({
            "success": False, 
            "message": "Data tidak lengkap! Pastikan Channel, Waktu Mulai, Audio, dan Background sudah dipilih."
        }), 400

    live_id = secrets.randbits(52)
    base_video_path = os.path.join(OUTPUT_DIR, f"base_live_{live_id}.mp4")

    new_item = {
        "id": live_id,
        "status": "QUEUED_RENDER",  # QUEUED_RENDER -> RENDERING_BASE -> READY_TO_LOCK -> WAITING_TO_START -> LIVE -> FINISHED
        "yt_id": yt_id,
        "title": title,
        "desc": desc,
        "tags": tags,
        "privacy": privacy,
        "start_time": start_time,
        "duration_hours": duration_hours,
        "audios": audios,
        "bgs": bgs,
        "preset": preset,
        "thumb": thumb,
        "base_video_path": base_video_path,
        "message": "Masuk antrean VIP Task Global untuk merakit Base Video...",
        "viewers": 0,
        "likes": 0,
        "stream_key": "",
        "broadcast_id": "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    with _live_db_lock:
        items = read_db()
        items.insert(0, new_item)
        write_db(items)

    # 🔥 LEMPAR KE ANTREAN TASK GLOBAL (MESIN FACTORY) DENGAN PRIORITAS VIP 🔥
    try:
        import __main__ as main_app
        if not hasattr(main_app, 'active_tasks'):
            import app as main_app

        blueprint = {
            "id": live_id,
            "is_live_base": True,          # Bendera khusus VIP Live Base Video
            "live_id": live_id,
            "yt_id": yt_id,
            "title": f"[LIVE BASE] {title}",
            "publish_date": start_time.replace('T', ' '),
            "selected_audios": audios,     # Daftar lagu spesifik pilihan user
            "selected_bgs": bgs,           # Daftar BG spesifik pilihan user
            "vis_mode": "preset" if preset else "random",
            "vis_preset": preset,
            "vis_presets_allowed": [preset] if preset else [],
            "output_override": base_video_path,
            "use_floating_card": True,
            "floating_card_mode": "intro_5s",
            "source_mode": "live_custom"
        }

        with main_app.db_lock:
            main_app.active_tasks.insert(0, {
                "id": live_id,
                "yt_id": yt_id,
                "title": f"🔴 [VIP Base Live] {title}",
                "time": start_time.replace('T', ' '),
                "status": "Menunggu Giliran VIP (Queue) ⚡",
                "type": "🔴 LIVE BASE",
                "blueprint": blueprint
            })
            main_app.save_tasks_db()
        main_app.queue_put(blueprint)
    except Exception:
        update_live_item(live_id, {'status': 'ERROR', 'message': 'Gagal memasukkan Base Video ke antrean Factory.'})
        return jsonify({'success': False, 'message': 'Base Video gagal masuk antrean Factory.'}), 500

    return jsonify({
        "success": True, 
        "message": "Jadwal disimpan & pembuatan Base Video masuk antrean VIP Task Global!", 
        "id": live_id
    })

@live_bp.route('/status', methods=['GET'])
def get_status():
    items = read_db()
    active_count = len([i for i in items if i.get('status') in ('LIVE', 'WAITING_TO_START', 'PRE_RENDERING', 'RENDERING_BASE', 'QUEUED_RENDER', 'READY_TO_LOCK')])
    live_now_count = len([i for i in items if i.get('status') == 'LIVE'])
    total_viewers = sum(int(i.get('viewers', 0)) for i in items if i.get('status') == 'LIVE')
    total_likes = sum(int(i.get('likes', 0)) for i in items if i.get('status') == 'LIVE')

    return jsonify({
        "items": items,
        "summary": {
            "active_schedules": active_count,
            "live_now": live_now_count,
            "total_viewers": total_viewers,
            "total_likes": total_likes
        }
    })

@live_bp.route('/stop', methods=['POST'])
def stop_live():
    data = request.json or {}
    target_id = data.get('id')
    
    with _live_db_lock:
        items = read_db()
        for item in items:
            if target_id is None or str(item.get('id')) == str(target_id):
                if item.get('status') not in ('FINISHED', 'STOPPED', 'IDLE'):
                    item['status'] = 'FORCE_STOP'
                    item['message'] = 'Sinyal berhenti/batal dikirim...'
                    
                    # Batalkan juga jika masih di antrean Task Global Factory
                    try:
                        import __main__ as main_app
                        if not hasattr(main_app, 'stop_flags'):
                            import app as main_app
                        main_app.stop_flags[item['id']] = True
                    except Exception:
                        pass
        write_db(items)
        
    return jsonify({"success": True, "message": "Perintah penghentian dikirim."})

@live_bp.route('/delete', methods=['POST'])
def delete_live():
    """Menghapus jadwal yang sudah selesai/dibatalkan dari tabel."""
    data = request.json or {}
    target_id = data.get('id')
    
    with _live_db_lock:
        items = read_db()
        new_items = []
        for item in items:
            if str(item.get('id')) == str(target_id):
                if item.get('status') not in ('FINISHED', 'STOPPED', 'ERROR', 'IDLE'):
                    return jsonify({'success': False, 'message': 'Hentikan jadwal sebelum menghapusnya.'}), 409
                # Hapus file Base Video jika ada agar hemat disk VPS
                bpath = item.get('base_video_path', '')
                if bpath and os.path.commonpath([os.path.realpath(OUTPUT_DIR), os.path.realpath(bpath)]) == os.path.realpath(OUTPUT_DIR) and os.path.isfile(bpath):
                    try: os.remove(bpath)
                    except Exception: pass
                continue
            new_items.append(item)
        write_db(new_items)
        
    return jsonify({"success": True})