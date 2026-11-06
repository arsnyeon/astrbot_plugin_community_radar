"""Bounded plugin-owned cache for observed images, never arbitrary file reads."""
import hashlib
import time
from pathlib import Path


class MediaCache:
    def __init__(self, directory, max_bytes=32 * 1024 * 1024, ttl=86400):
        self.root = Path(directory) / 'observed_media'
        if self.root.is_symlink():
            raise ValueError('cache directory cannot be a symlink')
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.ttl = ttl

    def put(self, data: bytes, *, kind='frame', timestamp=None):
        if not data or len(data) > min(self.max_bytes, 2 * 1024 * 1024):
            return None
        key = hashlib.sha256(data).hexdigest()
        path = self.root / (key + '.img')
        if path.is_symlink():
            return None
        if not path.exists():
            path.write_bytes(data)
        path.touch()
        self.prune()
        if not path.exists():
            return None
        return {'media_id': key, 'kind': kind, 'timestamp': timestamp}

    def read(self, media_id):
        if not isinstance(media_id, str) or len(media_id) != 64 or any(c not in '0123456789abcdef' for c in media_id):
            return None
        path = self.root / (media_id + '.img')
        if path.is_symlink() or not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > self.ttl:
            return None
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        return path.read_bytes()

    def prune(self):
        now = time.time()
        total = 0
        for path in sorted(self.root.glob('*.img'), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_symlink():
                continue
            size = path.stat().st_size
            if now - path.stat().st_mtime > self.ttl or total + size > self.max_bytes:
                # All deletion targets originate from the fixed cache directory.
                if path.parent == self.root:
                    path.unlink(missing_ok=True)
            else:
                total += size
