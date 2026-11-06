import sys
import types
import importlib
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

pkg = types.ModuleType('radar_adapter_tests')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[pkg.__name__] = pkg
adapters = importlib.import_module(pkg.__name__ + '.clients.adapters')
contracts = importlib.import_module(pkg.__name__ + '.clients.platform_contracts')
api = importlib.import_module(pkg.__name__ + '.api.radar_explore')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalized_search_and_public_contract(self):
        plugin = types.SimpleNamespace(
            bili=types.SimpleNamespace(search=AsyncMock(return_value=[{'bvid':'BVtest','title':'video','pic':'https://example.org/cover.jpg'}])),
            heybox=types.SimpleNamespace(search=AsyncMock(return_value=[{'link_id':'123','title':'post','images':[]}]))
        )
        registry = adapters.read_registry(plugin)
        content = (await registry.get('bili','search').search('topic'))[0]
        self.assertEqual(content.content_id, 'bili:BVtest')
        self.assertEqual(content.media[0].kind, 'cover')
        result = await api.RadarExploreAPI(plugin).explore('topic')
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['items']), 2)
        self.assertTrue(all(x['limitations'] for x in result['items']))

    async def test_capabilities_fail_explicitly(self):
        registry = contracts.AdapterRegistry()
        adapter = adapters.BiliAdapter(None)
        registry.register(adapter)
        with self.assertRaises(ValueError): registry.register(adapter)
        with self.assertRaises(NotImplementedError): registry.get('bili','write')
        with self.assertRaises(LookupError): registry.get('xiaohongshu')
        with self.assertRaises(ValueError): registry.get('bili','unknown')

    async def test_partial_platform_failure(self):
        plugin = types.SimpleNamespace(
            bili=types.SimpleNamespace(search=AsyncMock(side_effect=RuntimeError('offline'))),
            heybox=types.SimpleNamespace(search=AsyncMock(return_value=[{'link_id':'123','title':'post'}]))
        )
        result = await api.RadarExploreAPI(plugin).explore('topic')
        self.assertTrue(result['ok'])
        self.assertEqual(result['errors'], ['bili:RuntimeError'])
