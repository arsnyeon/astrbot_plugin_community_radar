"""主人账号解析的测试：插件配置优先，留空回退 AstrBot 全局管理员。"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_owner_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

owners = __import__(pkg.__name__ + ".core.owners", fromlist=["x"])
NS = types.SimpleNamespace


def plugin(config=None, admins=None, platform="渡可", raise_on_config=False):
    """构造一个最小的插件替身。admins 为 None 表示读取全局配置会失败。"""

    def get_config(umo=None):
        if raise_on_config:
            raise RuntimeError("config unavailable")
        return {"admins_id": admins if admins is not None else []}

    return NS(
        config=config or {},
        context=NS(get_config=get_config),
        _platform_name=lambda: platform,
    )


class AdminIdsTests(unittest.TestCase):
    def test_plugin_config_wins(self):
        self.assertEqual(owners.admin_ids(plugin({"admin_uin": "123"})), ["123"])

    def test_multiple_admins_separated(self):
        value = "123, 456；789 000"
        self.assertEqual(owners.admin_ids(plugin({"admin_uin": value})), ["123", "456", "789", "000"])

    def test_duplicates_removed(self):
        self.assertEqual(owners.admin_ids(plugin({"admin_uin": "123,123"})), ["123"])

    def test_falls_back_to_astrbot_admins(self):
        self.assertEqual(owners.admin_ids(plugin({}, admins=["2659055656"])), ["2659055656"])

    def test_blank_plugin_config_uses_global(self):
        self.assertEqual(owners.admin_ids(plugin({"admin_uin": "   "}, admins=["42"])), ["42"])

    def test_both_missing_yields_empty(self):
        self.assertEqual(owners.admin_ids(plugin({}, admins=[])), [])

    def test_unreadable_global_config_is_safe(self):
        self.assertEqual(owners.admin_ids(plugin({}, raise_on_config=True)), [])

    def test_non_list_admins_ignored(self):
        self.assertEqual(owners.admin_ids(plugin({}, admins="123")), [])


class OwnerUmoTests(unittest.TestCase):
    def test_builds_friend_umo_from_plugin_config(self):
        self.assertEqual(
            owners.owner_umo(plugin({"admin_uin": "123"})), "渡可:FriendMessage:123")

    def test_builds_friend_umo_from_global_admin(self):
        self.assertEqual(
            owners.owner_umo(plugin({}, admins=["2659055656"])),
            "渡可:FriendMessage:2659055656",
        )

    def test_empty_when_no_admin_anywhere(self):
        self.assertEqual(owners.owner_umo(plugin({}, admins=[])), "")

    def test_platform_failure_falls_back_to_default(self):
        target = plugin({"admin_uin": "123"})
        target._platform_name = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual(owners.owner_umo(target), "default:FriendMessage:123")


if __name__ == "__main__":
    unittest.main()
