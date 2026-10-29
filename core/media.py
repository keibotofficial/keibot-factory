"""Media validation and executable discovery without installing anything."""
import json
import shutil
import subprocess


def executable(name):
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f'{name} tidak ditemukan di PATH server.')
    return found


def validate_video(path):
    result = subprocess.run(
        [executable('ffprobe'), '-v', 'error', '-show_entries',
         'format=duration:stream=codec_type', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=30, check=True)
    info = json.loads(result.stdout)
    kinds = {stream.get('codec_type') for stream in info.get('streams', [])}
    if float(info.get('format', {}).get('duration', 0)) <= 0 or not {'video', 'audio'} <= kinds:
        raise RuntimeError('Output video/audio tidak lengkap atau durasi tidak valid.')
    return info
