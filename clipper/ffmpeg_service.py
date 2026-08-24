import os
import subprocess
import re
from datetime import datetime, timedelta

def get_ffmpeg_path():
    return "/usr/bin/ffmpeg"

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
def process_clip_pro(source_path, output_path, start_time, duration, crop_mode, lyric_style, srt_path, use_visualizer, bgm_path=None, audio_mode="bgm", custom_font=""):
    filter_complex = []
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
        filter_complex.append(f"{vis_audio_node}showwaves=s=1080x300:colors=0x00e5ff:mode=cline,format=yuv420p[wave]")
        filter_complex.append(f"{last_v_node}[wave]overlay=0:H-h[v_vis]")
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

    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return output_path
    except subprocess.CalledProcessError as e:
        if os.path.exists(output_path):
            try: os.remove(output_path)
            except: pass
        raise Exception(f"FFmpeg Error: {e.stderr.decode('utf-8')}")