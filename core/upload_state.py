"""Persist resumable sessions before sending bytes; never replace an uncertain session."""
import hashlib
import json
import os
from pathlib import Path

from core.json_store import read_json, write_json


def state_path(task_id, channel_id):
    key = hashlib.sha256(f'{channel_id}:{task_id}'.encode()).hexdigest()
    return Path(__file__).resolve().parents[1] / '.upload_state' / f'{key}.json'


def has_pending_upload(task_id, channel_id):
    path = state_path(task_id, channel_id)
    try:
        state = read_json(path, {})
    except (OSError, ValueError):
        return True
    return bool(state) and not state.get('video_id')


def _fingerprint(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return {'size': os.path.getsize(path), 'sha256': digest.hexdigest()}


class DurableUpload:
    def __init__(self, request, task_id, channel_id, video_path, credential_index=0):
        self.request = request
        self.path = state_path(task_id, channel_id)
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self.state = read_json(self.path, {})
        if self.state.get('video_id'):
            return
        fingerprint = _fingerprint(video_path)
        if self.state and self.state.get('media') != fingerprint:
            raise RuntimeError('Sesi upload tersimpan memakai file berbeda; perlu rekonsiliasi sebelum upload ulang.')
        self.state.setdefault('media', fingerprint)
        if self.state.get('uri') and self.state.get('credential_index') != credential_index:
            raise RuntimeError('Sesi upload harus dilanjutkan dengan kredensial asal.')
        self.state['credential_index'] = credential_index
        write_json(self.path, self.state)
        if self.state.get('uri'):
            request.resumable_uri = self.state['uri']
            # Query the server's offset/completion, rather than trusting a local byte count.
            request._in_error_state = True

    def next_chunk(self):
        if self.state.get('video_id'):
            return None, {'id': self.state['video_id']}
        owner = self
        transport = self.request.http

        class RecordingTransport:
            def request(self, uri, method='GET', body=None, headers=None, **kwargs):
                response, content = transport.request(uri, method=method, body=body, headers=headers, **kwargs)
                location = response.get('location')
                if location and response.status in (200, 201, 308):
                    owner.state['uri'] = location
                    write_json(owner.path, owner.state)
                if uri == owner.state.get('uri') and method == 'PUT' and response.status in (200, 201):
                    result = json.loads(content.decode() if isinstance(content, bytes) else content)
                    if result.get('id'):
                        owner.state['video_id'] = result['id']
                        write_json(owner.path, owner.state)
                return response, content

        try:
            return self.request.next_chunk(http=RecordingTransport())
        except Exception as error:
            status = getattr(getattr(error, 'resp', None), 'status', None)
            if self.state.get('uri') and status in (404, 410):
                raise RuntimeError('Sesi upload tidak dapat dilanjutkan; periksa hasil di YouTube sebelum mengulang.') from None
            raise
        finally:
            if self.request.resumable_uri:
                self.state['uri'] = self.request.resumable_uri
                write_json(self.path, self.state)
