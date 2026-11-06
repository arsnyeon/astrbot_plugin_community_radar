"""Validate model selections without network calls or AstrBot installation."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
import json

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'radar_media_selection_tests'
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = pkg
# Only the JSON parser dependency is stubbed; actual cache/selection modules run.
core = types.ModuleType(PACKAGE + '.core.radar_core')
core.parse_json = lambda text: json.loads(text)
sys.modules[core.__name__] = core
import importlib
selection = importlib.import_module(PACKAGE + '.media.media_selection')
cache_module = importlib.import_module(PACKAGE + '.media.media_cache')


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = cache_module.MediaCache(self.temp.name)
        self.media = [self.cache.put(b'\xff\xd8\xff' + bytes([i]), timestamp=i) for i in range(3)]
        self.ask = AsyncMock(return_value='{"indices":[3,1]}')
        self.plugin = types.SimpleNamespace(config={}, radar_store=types.SimpleNamespace(directory=Path(self.temp.name)),
                                            radar_ai=types.SimpleNamespace(ask=self.ask))
        self.items = [{'_evidence': {'observed_media': self.media}}]

    async def test_multiple_real_images_in_requested_order(self):
        result = await selection.select_images(self.plugin, self.items)
        self.assertEqual(result, [self.media[2], self.media[0]])
        self.assertEqual(len(self.ask.call_args.kwargs['image_urls']), 3)

    async def test_untrusted_indices_and_cap(self):
        self.ask.return_value = '{"indices":[true,0,99,1,1,2,3,"2"]}'
        result = await selection.select_images(self.plugin, self.items, limit=2)
        self.assertEqual(result, self.media[:2])

    async def test_model_can_choose_none(self):
        self.ask.return_value = '{"indices":[]}'
        self.assertEqual(await selection.select_images(self.plugin, self.items), [])

    async def test_zero_disables_call(self):
        self.assertEqual(await selection.select_images(self.plugin, self.items, limit=0), [])
        self.ask.assert_not_awaited()

    async def test_missing_file_not_sent_to_model(self):
        # Cache TTL expiration simulates unavailable media without any arbitrary file access.
        self.items[0]['_evidence']['observed_media'] = [{'media_id':'0'*64}]
        self.assertEqual(await selection.select_images(self.plugin, self.items), [])
        self.ask.assert_not_awaited()

    async def test_model_failure_keeps_no_selection(self):
        self.ask.side_effect = RuntimeError('provider unavailable')
        self.assertEqual(await selection.select_images(self.plugin, self.items), [])
