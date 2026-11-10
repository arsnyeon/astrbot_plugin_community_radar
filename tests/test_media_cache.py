import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('media_cache_test', Path(__file__).resolve().parents[1] / 'media/media_cache.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = m.MediaCache(self.tmp.name, max_bytes=20)

    def test_content_addressing_and_roundtrip(self):
        a = self.cache.put(b'abc', timestamp=4)
        b = self.cache.put(b'abc', timestamp=8)
        self.assertEqual(a['media_id'], b['media_id'])
        self.assertEqual(self.cache.read(a['media_id']), b'abc')
        self.assertEqual(len(list(self.cache.root.glob('*.img'))), 1)

    def test_limits_and_traversal(self):
        self.assertIsNone(self.cache.put(b'x' * 21))
        self.assertIsNone(self.cache.read('../../secret'))
        first = self.cache.put(b'a' * 12)
        b = self.cache.put(b'b' * 12)
        self.assertLessEqual(sum(p.stat().st_size for p in self.cache.root.glob('*.img')), 20)
        self.assertIsNotNone(self.cache.read(b['media_id']))
        # 超出总量上限后，最早的一批应当已被清理。
        self.assertIn(self.cache.read(first['media_id']), (None, first))

    def test_expired_not_returned(self):
        a = self.cache.put(b'abc')
        self.cache.ttl = -1
        self.assertIsNone(self.cache.read(a['media_id']))
        self.cache.prune()
        self.assertFalse(list(self.cache.root.glob('*.img')))
