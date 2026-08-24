import threading
import time

# Lock global untuk memastikan hanya ada 1 task berat di seluruh sistem
_heavy_task_lock = threading.Lock()

def check_ram_ok(max_ram_pct=85.0):
    """Mengecek apakah RAM masih di bawah batas aman."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return mem.percent < max_ram_pct
    except ImportError:
        return True # Abaikan jika psutil tidak ada

def acquire_resource(worker_name="Worker"):
    """
    Mengantre untuk mendapatkan akses CPU/RAM.
    Jika sedang dipakai modul lain, modul ini akan tertahan di sini.
    """
    print(f"⏳ [{worker_name}] Meminta akses resource...")
    _heavy_task_lock.acquire()
    
    # Setelah dapat giliran, pastikan RAM tidak sedang ngos-ngosan
    while not check_ram_ok():
        print(f"⚠️ [{worker_name}] RAM penuh (>85%). Menunggu lega...")
        time.sleep(10)
        
    print(f"🟢 [{worker_name}] Resource diamankan. Mulai eksekusi!")

def release_resource(worker_name="Worker"):
    """Melepas akses agar antrean modul lain bisa berjalan."""
    try:
        _heavy_task_lock.release()
        print(f"🔓 [{worker_name}] Resource dilepas untuk antrean berikutnya.")
    except RuntimeError:
        pass # Abaikan jika lock sudah terlepas