"""Shared state rules; display text remains compatible with existing jobs."""


def channel_id(job):
    options = job.get('options') or {}
    return (job.get('yt_id') or job.get('blueprint', {}).get('yt_id')
            or job.get('channelId') or options.get('channelId')
            or options.get('yt_id') or options.get('targetChannelId'))


def cancelled(job):
    return 'Dibatalkan' in str(job.get('status', '')) or job.get('status') == 'FORCE_STOP'


def terminal(job):
    status = str(job.get('status', ''))
    if 'Antre Ulang' in status:
        return False
    return (status.lower() in ('done', 'error', 'finished', 'stopped')
            or any(word in status for word in ('Selesai', 'Sukses', 'Gagal', 'Dibatalkan', 'Tayang!', 'Siap! ✅')))


class JobCancelled(Exception):
    pass
