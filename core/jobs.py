"""Transactional lifecycle updates used by API routes and background workers."""
from core.json_store import read_json, update_json
from core.job_state import JobCancelled, cancelled, terminal, channel_id


def check_job(path, job_id):
    job = next((j for j in read_json(path, []) if str(j.get('id')) == str(job_id)), None)
    if job is None or cancelled(job):
        raise JobCancelled('Dibatalkan')
    return job


def update_job_in(path, job_id, fields):
    def mutate(jobs):
        job = next((j for j in jobs if str(j.get('id')) == str(job_id)), None)
        if job is None or cancelled(job):
            raise JobCancelled('Dibatalkan')
        job.update(fields)
    return update_json(path, [], mutate)


def cancel_jobs_in(path, job_id=None, yt_id=None):
    def mutate(jobs):
        for job in jobs:
            matches = (job_id is not None and str(job.get('id')) == str(job_id))
            matches = matches or (yt_id is not None and channel_id(job) == yt_id)
            if matches and not terminal(job):
                job['status'] = 'Dibatalkan 🛑'
    return update_json(path, [], mutate)


def recover_jobs(path):
    def mutate(jobs):
        for job in jobs:
            if terminal(job):
                continue
            stage = str(job.get('stage', ''))
            status = str(job.get('status', ''))
            if 'Mengunggah' in status or 'Upload ke' in stage or 'Proses Upload' in stage:
                job['status'] = 'Gagal ❌ (Upload terputus; periksa hasil di platform sebelum mengulang)'
            else:
                job['status'] = 'queued'
                job['stage'] = 'Dilanjutkan setelah restart'
    return update_json(path, [], mutate)
