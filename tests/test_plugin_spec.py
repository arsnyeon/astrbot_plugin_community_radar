"""校验插件元数据与配置是否符合 AstrBot 官方规范。

规则实现位于 ``scripts/check_plugin_spec.py``，这里只把它接进回归测试，
避免后续改动无意中破坏发布所需的字段格式。
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "scripts" / "check_plugin_spec.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("radar_spec_check", SPEC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PluginSpecTests(unittest.TestCase):
    def test_plugin_matches_official_spec(self):
        checker = load_checker()
        self.assertEqual(
            checker.failures, [],
            "不符合 AstrBot 官方规范：" + "；".join(checker.failures),
        )

    def test_version_is_bare_semver(self):
        import yaml

        meta = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertFalse(meta["version"].startswith("v"),
                         "市场规范要求 version 不带 v 前缀")
        self.assertRegex(meta["version"], r"^\d+\.\d+\.\d+")

    def test_secrets_are_masked(self):
        import json

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("bili_cookie", "heybox_cookie"):
            field = schema[key]
            self.assertEqual(field["type"], "string", "secret 仅对 string 生效")
            self.assertTrue(field.get("secret"), f"{key} 应默认遮蔽显示")


if __name__ == "__main__":
    unittest.main()
