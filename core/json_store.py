"""Atomic JSON transactions shared by routes and workers in this process."""
import copy
import json
import os
import tempfile
import threading

_locks = {}
_registry_lock = threading.Lock()


def lock_for(path):
    with _registry_lock:
        return _locks.setdefault(os.path.realpath(path), threading.RLock())


def read_json(path, default):
    with lock_for(path):
        try:
            with open(path, encoding='utf-8') as source:
                return json.load(source)
        except FileNotFoundError:
            return copy.deepcopy(default)


def write_json(path, data):
    with lock_for(path):
        fd, temporary = tempfile.mkstemp(prefix='.' + os.path.basename(path), suffix='.tmp',
                                         dir=os.path.dirname(os.path.abspath(path)))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as target:
                json.dump(data, target, indent=4, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def update_json(path, default, mutate):
    with lock_for(path):
        data = read_json(path, default)
        replacement = mutate(data)
        if replacement is not None:
            data = replacement
        write_json(path, data)
        return data


def merge_snapshot(path, before, after):
    """Merge worker changes without resurrecting deleted jobs or overwriting route edits."""
    old = {str(item['id']): item for item in before}
    new = {str(item['id']): item for item in after}

    def merge(current):
        for item in current:
            key = str(item['id'])
            if key not in old or key not in new:
                continue
            # A route changed the lifecycle while the worker was doing network I/O.
            if item.get('status') != old[key].get('status'):
                # Preserve resources created while a Stop request was arriving, so
                # the next worker pass can close the corresponding broadcast.
                if item.get('status') == 'FORCE_STOP':
                    for field in ('broadcast_id', 'stream_key'):
                        if field in new[key] and item.get(field) == old[key].get(field):
                            item[field] = new[key][field]
                continue
            for field, value in new[key].items():
                if value != old[key].get(field) and item.get(field) == old[key].get(field):
                    item[field] = value
    return update_json(path, [], merge)
