import os
import subprocess
import time
from core.media import executable, validate_video
from core.job_state import JobCancelled
import re
from datetime import datetime, timedelta

def get_ffmpeg_path():
    return executable('ffmpeg')

# =========================================================
# 🔥 SMART CHOPPER (VERSI TAHAN BANTING) 🔥
# =========================================================
def split_srt_to_words(input_srt, output_srt, max_words=1):
    def parse_time(time_str):
        # Format aman manual (tidak pakai strptime yang sensitif)
        time_str = time_str.strip().replace('.', ',')
        h, m, s_ms = time_str.split(':')
        s, ms = s_ms.split(',')
        return timedelta(hours=int(h), minutes=int(m), seconds=int(s), milliseconds=int(ms))
        
    def format_time(td):
        total_seconds = int(td.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        # Dapatkan sisa microsecond ke millisecond
        millis = int(td.microseconds / 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    try:
        with open(input_srt, 'r', encoding='utf-8') as f:
            content = f.read()

        blocks = re.split(r'\n\s*\n', content.strip())
        new_blocks = []
        counter = 1

        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                time_line = lines[1]
                # Gabungkan semua baris teks jadi 1 baris panjang
                text_lines = " ".join(lines[2:]).strip()
                
                if "-->" in time_line:
                    start_str, end_str = time_line.split("-->")
                    try:
                        start_td = parse_time(start_str)
                        end_td = parse_time(end_str)
                    except: continue
                    
                    # Pecah teks berdasarkan spasi (kata)
                    words = text_lines.split()
                    if not words: continue
                    
                    total_dur = (end_td - start_td).total_seconds()
                    dur_per_word = total_dur / max(len(words), 1)
                    
                    for i in range(0, len(words), max_words):
                        chunk_words = words[i:i+max_words]
                        chunk_text = " ".join(chunk_words)
                        chunk_start = start_td + timedelta(seconds=(i * dur_per_word))
                        chunk_end = start_td + timedelta(seconds=((i + len(chunk_words)) * dur_per_word))
                        
                        new_blocks.append(f"{counter}\n{format_time(chunk_start)} --> {format_time(chunk_end)}\n{chunk_text}")
                        counter += 1

        with open(output_srt, 'w', encoding='utf-8') as f:
            f.write("\n\n".join(new_blocks) + "\n\n")
        return True
    except Exception as e:
        print(f"SRT Split Error: {e}")
        return False

# =========================================================
# ⚙️ MESIN RENDER UTAMA
# =========================================================
def process_clip_pro(source_path, output_path, start_time, duration, crop_mode, lyric_style, srt_path, use_visualizer, bgm_path=None, audio_mode="bgm", custom_font="", vis_params=None, cancel_callback=None):
    filter_complex = []
    vis_params = vis_params or {}
    has_bgm = bgm_path and os.path.exists(bgm_path)
    
    if has_bgm:
        if audio_mode == "replace":
            filter_complex.append("[1:a]volume=1.0[a_mixed]")
        else:
            filter_complex.append("[0:a]volume=1.0[a0];[1:a]volume=0.15[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a_mixed]")
    else:
        filter_complex.append("[0:a]volume=1.0[a_mixed]")

    last_a_node = "[a_mixed]"

    is_vis = str(use_visualizer).strip().lower() in ['true', '1', 'yes', 't']
    
    vis_audio_node = None
    if is_vis:
        filter_complex.append(f"{last_a_node}asplit=2[a_vis][a_out]")
        vis_audio_node = "[a_vis]"
        last_a_node = "[a_out]"

    if crop_mode == "center":
        filter_complex.append("[0:v]crop=ih*9/16:ih:iw/2-ih*9/32:0,scale=1080:1920[v_cropped]")
    elif crop_mode == "split":
        filter_complex.append("[0:v]crop=iw/2:ih:iw/2:0[top];[0:v]crop=iw/2:ih:0:0[bottom];[top][bottom]vstack,scale=1080:1920[v_cropped]")
    else:
        filter_complex.append("[0:v]null[v_cropped]")

    last_v_node = "[v_cropped]"

    if is_vis and vis_audio_node:
        def number(name, default, low, high):
            return max(low, min(high, int(vis_params.get(name, default))))
        colors = []
        for field, default in [('color1', '#00e5ff'), ('color2', '#00e5ff')]:
            value = str(vis_params.get(field, default)).lstrip('#')
            if not re.fullmatch(r'[0-9a-fA-F]{6}', value):
                raise ValueError('Warna visualizer tidak valid.')
            colors.append('0x' + value)
        width = number('size', 100, 10, 100) * 1080 // 100
        height = number('height_scale', 50, 1, 100) * 6
        bars = number('num_bars', 48, 10, 128)
        position = number('pos_y', 90, 0, 100)
        thickness = number('thickness', 5, 1, 100)
        cell = max(2, width // bars)
        gap = max(1, cell - min(thickness, cell - 1))
        filter_complex.append(f"{vis_audio_node}showfreqs=s={bars}x{height}:mode=bar:ascale=log:colors={'|'.join(colors)},scale={width}:{height}:flags=neighbor,drawgrid=w={cell}:h={height}:t={gap}:c=black,format=yuv420p[wave]")
        filter_complex.append(f"{last_v_node}[wave]overlay=(W-w)/2:(H-h)*{position}/100[v_vis]")
        last_v_node = "[v_vis]"

    if lyric_style != "none" and srt_path and os.path.exists(srt_path) and os.path.getsize(srt_path) > 5:
        
        # 🔥 AKTIVASI SMART CHOPPER 🔥
        word_limit = 1 if lyric_style == "karaoke" else (2 if lyric_style == "mrbeast" else 4)
        processed_srt = srt_path.replace('.srt', f'_{lyric_style}.srt')
        
        if split_srt_to_words(srt_path, processed_srt, word_limit):
            active_srt = processed_srt
        else:
            active_srt = srt_path
            
        safe_srt = os.path.abspath(active_srt).replace('\\', '/').replace(':', '\\\\:')
        font_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'fonts')
        safe_font_dir = font_dir.replace('\\', '/').replace(':', '\\\\:')
        
        pos_y = 150 if is_vis else 35
        
        f_name = "Arial"
        if custom_font:
            f_name = custom_font.replace('_', ' ').replace('.ttf', '').replace('.otf', '')
        elif lyric_style == "mrbeast":
            f_name = "Impact"
            
        style = ""
        if lyric_style == "mrbeast":
            style = f"Fontname={f_name},Fontsize=26,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=2,MarginV={pos_y},Alignment=2"
        elif lyric_style == "neon":
            style = f"Fontname={f_name},Fontsize=22,PrimaryColour=&H00FFFF00,OutlineColour=&H00FF0000,BorderStyle=3,Outline=2,Shadow=0,MarginV={pos_y},Alignment=2"
        elif lyric_style == "karaoke":
            style = f"Fontname={f_name},Fontsize=28,PrimaryColour=&H0000FF00,OutlineColour=&H00000000,BorderStyle=1,Outline=2,MarginV={pos_y},Alignment=2"
            
        filter_complex.append(f"{last_v_node}subtitles='{safe_srt}':fontsdir='{safe_font_dir}':force_style='{style}'[v_out]")
        last_v_node = "[v_out]"

    command = [
        get_ffmpeg_path(), '-y',
        '-ss', str(start_time),
        '-t', str(duration),
        '-i', source_path
    ]
    
    if has_bgm:
        command.extend(['-stream_loop', '-1', '-i', bgm_path])

    command.extend([
        '-filter_complex', ";".join(filter_complex),
        '-map', last_v_node,
        '-map', last_a_node,
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-movflags', '+faststart',
        '-t', str(duration),
        '-threads', '1',
        output_path
    ])

    partial_output = output_path + '.rendering.mp4'
    command[-1] = partial_output
    import tempfile
    try:
        # Drain stderr to a temporary file, so FFmpeg cannot block on a full pipe.
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                while proc.poll() is None:
                    if cancel_callback and cancel_callback():
                        raise JobCancelled('Dibatalkan')
                    time.sleep(0.25)
                if proc.returncode:
                    raise RuntimeError('FFmpeg gagal merender clip; periksa media dan pengaturan filter.')
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
        validate_video(partial_output)
        os.replace(partial_output, output_path)
        return output_path
    finally:
        if os.path.exists(partial_output):
            os.remove(partial_output)
