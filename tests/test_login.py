"""扫码登录相关的离线测试：cookie 解析与二维码降级。"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_login_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

import logging  # noqa: E402

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_login")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

login = __import__(pkg.__name__ + ".clients.bili_login", fromlist=["x"])
qr = __import__(pkg.__name__ + ".media.qr", fromlist=["x"])


class CookieTests(unittest.TestCase):
    def test_keeps_only_known_fields(self):
        raw = "SESSDATA=abc; bili_jct=def; DedeUserID=123; foo=bar"
        self.assertEqual(login.cookie_header(raw),
                         "SESSDATA=abc; bili_jct=def; DedeUserID=123")

    def test_handles_newlines_and_spaces(self):
        raw = "SESSDATA=abc;\nbili_jct=def;\n  DedeUserID=123"
        self.assertEqual(login.cookie_header(raw),
                         "SESSDATA=abc; bili_jct=def; DedeUserID=123")

    def test_unknown_only_returns_empty(self):
        self.assertEqual(login.cookie_header("foo=bar"), "")

    def test_empty_input(self):
        self.assertEqual(login.cookie_header(""), "")
        self.assertEqual(login.cookie_header(None), "")

    def test_keeps_original_order(self):
        raw = "DedeUserID=1; SESSDATA=2"
        self.assertEqual(login.cookie_header(raw), "SESSDATA=2; DedeUserID=1")


class QrTests(unittest.TestCase):
    @staticmethod
    def _has_qrcode() -> bool:
        from importlib.util import find_spec

        return find_spec("qrcode") is not None

    @unittest.skipUnless(_has_qrcode(), "本机没装 qrcode，跳过真实渲染")
    def test_png_has_signature(self):
        data = qr.png_bytes("https://example.org/login")
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_empty_text_returns_empty(self):
        self.assertEqual(qr.png_bytes(""), b"")

    def test_missing_dependency_returns_empty(self):
        import builtins

        original = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "qrcode":
                raise ImportError("no qrcode")
            return original(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            self.assertEqual(qr.png_bytes("https://example.org"), b"")
        finally:
            builtins.__import__ = original


class StateTests(unittest.TestCase):
    def test_state_constants_are_distinct(self):
        states = {login.WAITING, login.SCANNED, login.SUCCESS, login.EXPIRED}
        self.assertEqual(len(states), 4)


if __name__ == "__main__":
    unittest.main()
