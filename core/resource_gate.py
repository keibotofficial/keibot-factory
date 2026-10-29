"""
core/resource_gate.py

Satpam Resource CPU/RAM dengan fitur VIP Priority-Lock untuk Live Streaming.
"""

import threading
import time

# Lock global untuk memastikan hanya ada 1 task berat di seluruh sistem
_heavy_task_lock = threading.Lock()
_resource_owner = None

# Saklar VIP untuk Live Streaming
_live_active = False

def set_live_active(state: bool):
    """Mengaktifkan atau mematikan Priority-Lock untuk Live Streaming."""
    global _live_active
    if _live_active == state:
        return
    _live_active = state
    if state:
        print("🔴 [Priority-Lock] MODE LIVE DIAKTIFKAN! Semua antrean render ditahan...")
    else:
        print("🟢 [Priority-Lock] MODE LIVE DIMATIKAN! Antrean render kembali berjalan bebas.")

def is_live_active() -> bool:
    """Mengecek apakah Live Streaming sedang mengunci sistem."""
    return _live_active

def check_ram_ok(max_ram_pct=85.0):
    """Mengecek apakah RAM masih di bawah batas aman."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return mem.percent < max_ram_pct
    except ImportError:
        return True # Abaikan jika psutil tidak ada

def acquire_resource(worker_name="Worker", cancelled=None):
    """
    Mengantre untuk mendapatkan akses CPU/RAM.
    Jika Live Streaming aktif, modul lain HARUS MINGGIR dan menunggu.
    """
    global _resource_owner
    from core.job_state import JobCancelled
    cancelled = cancelled or (lambda: False)
    print(f"⏳ [{worker_name}] Meminta akses resource...")
    
    while True:
        if cancelled():
            raise JobCancelled('Dibatalkan')
        # 1. Tahan di luar gembok jika Live sedang berjalan (Jalur VIP)
        while _live_active:
            # print(f"🛑 [{worker_name}] Ditahan! Live Streaming mengambil alih (Priority-Lock).")
            if cancelled():
                raise JobCancelled("Dibatalkan")
            time.sleep(1)
            
        # 2. Coba amankan gembok CPU
        if not _heavy_task_lock.acquire(timeout=1):
            continue
        
        # 3. Cek lagi, siapa tahu pas mau ngunci, Live mendadak dinyalakan
        if _live_active:
            _heavy_task_lock.release()
            time.sleep(5)
            continue
            
        # 4. Pastikan RAM tidak ngos-ngosan
        if not check_ram_ok():
            print(f"⚠️ [{worker_name}] RAM penuh (>85%). Menunggu lega...")
            _heavy_task_lock.release()
            time.sleep(10)
            continue
            
        _resource_owner = threading.get_ident()
        # Jika semua aman (Live mati & RAM lega), eksekusi jalan!
        print(f"🟢 [{worker_name}] Resource diamankan. Mulai eksekusi!")
        break

def release_resource(worker_name="Worker"):
    global _resource_owner
    if _resource_owner != threading.get_ident():
        return
    _resource_owner = None
    _heavy_task_lock.release()
