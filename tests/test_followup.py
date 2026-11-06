import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("rs", Path(__file__).resolve().parents[1] / "radar_session.py")
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)


class FakeStore:
    def __init__(self):
        self.data = {}
    def get(self, key, default=None):
        return self.data.get(key, default)
    def set(self, key, value):
        self.data[key] = value


class SessionShareTests(unittest.TestCase):
    def test_remember_and_recent(self):
        store = FakeStore()
        rs.remember(store, "g1", [{"content_id": "bili:BV1", "title": "t", "url": "u", "digest": "d"}], now=1000)
        self.assertIsNotNone(rs.recent(store, "g1", 45, now=2000))
        self.assertEqual(rs.ids(store, "g1", 45, now=2000), ["bili:BV1"])
        self.assertIsNone(rs.recent(store, "g2", 45, now=2000))
        self.assertIsNone(rs.recent(store, "g1", 45, now=1000 + 46 * 60))

    def test_prunes_old_targets(self):
        store = FakeStore()
        rs.remember(store, "old", [{"content_id": "bili:1"}], now=1000)
        rs.remember(store, "new", [{"content_id": "bili:2"}], now=1000 + 8 * 86400)
        self.assertNotIn("old", store.get(rs.KEY, {}))
        self.assertIn("new", store.get(rs.KEY, {}))


if __name__ == "__main__":
    unittest.main()
