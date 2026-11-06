"""Discovery tests do not launch browsers or touch user profiles."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('browser_runtime_test', Path(__file__).resolve().parents[1] / 'browser/browser_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class BrowserRuntimeTests(unittest.TestCase):
    def test_explicit_path_must_be_file(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(runtime.find_browser(root), '')
            p = Path(root) / 'browser.exe'
            p.touch()
            self.assertEqual(runtime.find_browser(str(p)), str(p))

    def test_edge_preferred_over_chrome(self):
        with tempfile.TemporaryDirectory() as root:
            for suffix in ('Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'):
                p = Path(root) / suffix
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            with patch.dict(runtime.os.environ, {'PROGRAMFILES': root}, clear=True):
                self.assertTrue(runtime.find_browser(windows=True).endswith('msedge.exe'))

    def test_linux_path_lookup(self):
        with patch.object(runtime.shutil, 'which', side_effect=lambda n: '/usr/bin/chromium' if n == 'chromium' else None):
            self.assertEqual(runtime.find_browser(windows=False), '/usr/bin/chromium')

    def test_explicit_invalid_does_not_fallback(self):
        with patch.object(runtime.shutil, 'which', return_value='/bin/chromium'):
            self.assertEqual(runtime.find_browser('/nonexistent/radar/browser'), '')


if __name__ == '__main__':
    unittest.main()
