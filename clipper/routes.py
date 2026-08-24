import os
import uuid
import json
import time
import glob
import shutil
import subprocess
from flask import Blueprint, request, jsonify, Response, send_from_directory
from werkzeug.utils import secure_filename
from .ytdlp_service import get_metadata
from .queue_worker import read_db, write_db

# Inisialisasi Blueprint
clipper_bp = Blueprint('clipper', __name__)

@clipper_bp.route('/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"success": False, "error": {"message": "URL wajib diisi"}}), 400
    result = get_metadata(url)
    if not result.get('success'):
        return jsonify(result), 400
    return jsonify(result)

@clipper_bp.route('/start', methods=['POST'])
def start_clip():
    data = request.json
    url = data.get('url')
    
    batch_count = int(data.get('batchCount', 1))
    ai_provider = data.get('aiProvider', 'huggingface')
    api_keys = data.get('apiKeys', [])
    publish_date = data.get('publishDate', '')
    
    options = {
        "cropMode": data.get('cropMode', 'center'),
        "lyricStyle": data.get('lyricStyle', 'none'),
        "customFont": data.get('customFont', ''),
        "useVisualizer": data.get('useVisualizer', False),
        "bgmFile": data.get('bgmFile', 'none'),
        "audioMode": data.get('audioMode', 'bgm'),
        "outputDest": data.get('outputDest', 'local'),
        "assetChannelId": data.get('assetChannelId', ''),
        "targetChannelId": data.get('targetChannelId', '')
    }

    if not url:
        return jsonify({"success": False, "error": {"message": "URL tidak boleh kosong."}})

    base_job_id = f"{int(time.time())}"
    jobs = read_db()
    
    for i in range(batch_count):
        job_id = f"{base_job_id}_{i+1}"
        job_data = {
            "id": job_id,
            "url": url,
            "status": "queued",
            "progress": 0,
            "stage": "Menunggu antrean...",
            "batch_index": i + 1,
            "total_batch": batch_count,
            "ai_provider": ai_provider,
            "api_keys": api_keys,
            "publish_date": publish_date,
            "options": options
        }
        jobs.append(job_data)
        
    write_db(jobs)

    return jsonify({
        "success": True, 
        "data": {
            "baseJobId": base_job_id,
            "message": f"{batch_count} Clip dimasukkan ke antrean."
        }
    })

@clipper_bp.route('/status/<job_id>', methods=['GET'])
def stream_status(job_id):
    def event_stream():
        last_status = None
        last_progress = None
        while True:
            jobs = read_db()
            job = next((j for j in jobs if j.get('id') == job_id), None)
            
            if not job:
                yield f"data: {json.dumps({'error': 'Job tidak ditemukan'})}\n\n"
                break
            
            if job['status'] != last_status or job['progress'] != last_progress:
                yield f"data: {json.dumps(job)}\n\n"
                last_status = job['status']
                last_progress = job['progress']
            
            if job['status'] in ['done', 'error']:
                break
            time.sleep(1.5)
            
    return Response(event_stream(), mimetype="text/event-stream")

@clipper_bp.route('/download/<job_id>', methods=['GET'])
def download_clip(job_id):
    jobs = read_db()
    job = next((j for j in jobs if j.get('id') == job_id), None)
    if not job or job.get('status') != 'done':
        return jsonify({"success": False, "error": "File belum siap"}), 404
    output_file = job.get('output_file')
    # queue_worker menyimpan hasil render ke folder static.
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
    return send_from_directory(output_dir, output_file, as_attachment=True)

@clipper_bp.route('/jobs', methods=['GET'])
def get_all_jobs():
    jobs = read_db()
    return jsonify(jobs[-10:])

@clipper_bp.route('/jobs/clear', methods=['DELETE'])
def clear_jobs_history():
    write_db([]) 
    return jsonify({"success": True, "message": "Riwayat antrean berhasil dibersihkan."})

@clipper_bp.route('/gallery', methods=['GET'])
def get_gallery():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
    search_pattern = os.path.join(static_dir, 'clip_*.mp4')
    files = glob.glob(search_pattern)
    
    clips = []
    for f in files:
        filename = os.path.basename(f)
        size_mb = os.path.getsize(f) / (1024 * 1024)
        clips.append({
            "filename": filename,
            "url": f"/static/{filename}",
            "size": f"{size_mb:.2f} MB"
        })
    clips.sort(key=lambda x: x['filename'], reverse=True)
    return jsonify({"success": True, "data": clips})

@clipper_bp.route('/gallery/<filename>', methods=['DELETE'])
def delete_single_clip(filename):
    if not filename.startswith('clip_') or not filename.endswith('.mp4'):
        return jsonify({"success": False, "error": "Akses ditolak."}), 403
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        return jsonify({"success": True, "message": f"{filename} dihapus."})
    return jsonify({"success": False, "error": "File tidak ditemukan."}), 404

@clipper_bp.route('/gallery/all', methods=['DELETE'])
def delete_all_clips():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
    search_pattern = os.path.join(static_dir, 'clip_*.mp4')
    files = glob.glob(search_pattern)
    count = 0
    for f in files:
        try:
            os.remove(f)
            count += 1
        except: pass
    return jsonify({"success": True, "message": f"{count} video dibersihkan!"})

@clipper_bp.route('/assets/<channel_id>', methods=['GET'])
def get_channel_assets(channel_id):
    target_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads', channel_id)
    if not os.path.exists(target_dir):
        return jsonify({"success": True, "data": []})
    mp3_files = glob.glob(os.path.join(target_dir, '**', '*.mp3'), recursive=True)
    data = []
    for f in mp3_files:
        data.append({"filename": os.path.basename(f), "value": os.path.relpath(f, target_dir).replace('\\', '/')})
    return jsonify({"success": True, "data": data})

# --- ENDPOINT MANAJER FONT ---
# --- MANAJER COOKIE YT-DLP ---
COOKIE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cookies.txt')

@clipper_bp.route('/cookies/status', methods=['GET'])
def cookie_status():
    exists = os.path.isfile(COOKIE_PATH) and os.path.getsize(COOKIE_PATH) > 0
    return jsonify({
        "success": True,
        "data": {"configured": exists}
    })

@clipper_bp.route('/cookies/upload', methods=['POST'])
def upload_cookie():
    # Mode utama: menerima teks hasil tombol Copy dari ekstensi cookie.
    cookie_text = request.form.get('cookies', '')

    # Tetap dukung upload file sebagai fallback.
    if not cookie_text and 'file' in request.files:
        cookie_file = request.files['file']
        cookie_text = cookie_file.read().decode('utf-8', errors='replace')

    cookie_text = cookie_text.strip()
    if not cookie_text:
        return jsonify({"success": False, "error": "Tempel teks cookies terlebih dahulu."}), 400

    if '# Netscape HTTP Cookie File' not in cookie_text and not any(
        line.count('\t') >= 6 for line in cookie_text.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ):
        return jsonify({
            "success": False,
            "error": "Format cookie tidak valid. Pilih Netscape lalu klik Copy."
        }), 400

    with open(COOKIE_PATH, 'w', encoding='utf-8', newline='\n') as output:
        output.write(cookie_text + '\n')
    try:
        os.chmod(COOKIE_PATH, 0o600)
    except OSError:
        pass

    return jsonify({
        "success": True,
        "message": "Cookies berhasil disimpan dan akan dipakai otomatis oleh yt-dlp."
    })

@clipper_bp.route('/cookies', methods=['DELETE'])
def delete_cookie():
    if os.path.isfile(COOKIE_PATH):
        os.remove(COOKIE_PATH)
    return jsonify({"success": True, "message": "Cookies dihapus."})

@clipper_bp.route('/fonts', methods=['GET'])
def get_fonts():
    font_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'fonts')
    if not os.path.exists(font_dir):
        os.makedirs(font_dir, exist_ok=True)
        return jsonify({"success": True, "data": []})
        
    font_files = glob.glob(os.path.join(font_dir, '*.ttf')) + glob.glob(os.path.join(font_dir, '*.otf'))
    data = []
    for f in font_files:
        font_name = os.path.splitext(os.path.basename(f))[0]
        data.append({"filename": os.path.basename(f), "name": font_name})
    return jsonify({"success": True, "data": data})

@clipper_bp.route('/fonts/upload', methods=['POST'])
def upload_font():
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "Tidak ada file."}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "Pilih file font."}), 400
        
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if ext not in ['ttf', 'otf']:
        return jsonify({"success": False, "error": "Hanya format .ttf atau .otf yang diizinkan."}), 400
        
    # 1. Simpan ke folder web agar muncul di dropdown
    font_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'fonts')
    os.makedirs(font_dir, exist_ok=True)
    filename = secure_filename(file.filename)
    web_font_path = os.path.join(font_dir, filename)
    file.save(web_font_path)

    # 2. HACK FFMPEG: Install otomatis ke sistem Ubuntu agar libass tidak buta
    try:
        user_font_dir = os.path.expanduser('~/.fonts')
        os.makedirs(user_font_dir, exist_ok=True)
        sys_font_path = os.path.join(user_font_dir, filename)
        shutil.copy(web_font_path, sys_font_path)
        
        # Paksa Ubuntu me-refresh ingatan font-nya (Fontconfig cache)
        subprocess.run(['fc-cache', '-f', user_font_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        pass # Lanjut saja jika VPS tidak mengizinkan

    return jsonify({"success": True, "filename": filename})

@clipper_bp.route('/fonts/<filename>', methods=['DELETE'])
def delete_font(filename):
    font_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'fonts')
    file_path = os.path.join(font_dir, secure_filename(filename))
    
    if os.path.exists(file_path):
        os.remove(file_path)
        
        # Bersihkan juga dari sistem Ubuntu
        try:
            user_font_dir = os.path.expanduser('~/.fonts')
            sys_font_path = os.path.join(user_font_dir, secure_filename(filename))
            if os.path.exists(sys_font_path):
                os.remove(sys_font_path)
            # Refresh ulang cache Ubuntu
            subprocess.run(['fc-cache', '-f', user_font_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass
            
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Font tidak ditemukan."}), 404