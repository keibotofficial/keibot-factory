import os
import requests

# Ambil key dari environment atau biarkan kosong jika belum disetting
REPLIZ_ACCESS_KEY = os.environ.get('REPLIZ_ACCESS_KEY')
REPLIZ_SECRET_KEY = os.environ.get('REPLIZ_SECRET_KEY')
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')

def auto_post_repliz(job_id, account_id, caption="", schedule_at=None):
    """
    Mengirim perintah ke Repliz untuk memposting video hasil clip.
    """
    if not REPLIZ_ACCESS_KEY or not REPLIZ_SECRET_KEY:
        raise Exception("REPLIZ_NOT_CONFIGURED: API Key Repliz belum diisi di konfigurasi.")

    url = "https://api.repliz.com/v1/post"
    
    headers = {
        "Authorization": f"Bearer {REPLIZ_ACCESS_KEY}",
        "X-Secret-Key": REPLIZ_SECRET_KEY,
        "Content-Type": "application/json"
    }
    
    # Repliz membutuhkan URL publik agar server mereka bisa menarik file video dari VPS Keibot
    video_url = f"{BASE_URL}/api/clip/download/{job_id}"
    
    payload = {
        "accountId": account_id,
        "videoUrl": video_url,
        "caption": caption
    }
    
    # Tambahkan jadwal jika ingin diposting nanti
    if schedule_at:
        payload["scheduleAt"] = schedule_at

    try:
        response = requests.post(url, json=payload, headers=headers)
        
        if response.status_code not in [200, 201, 202]:
            raise Exception(f"Gagal memposting ({response.status_code}): {response.text}")
            
        return response.json()
    except Exception as e:
        raise Exception(f"Repliz API Error: {str(e)}")