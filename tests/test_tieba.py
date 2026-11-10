"""贴吧客户端的离线测试：签名、protobuf 编解码与结构归一。

真实网络请求不在单元测试中执行；这里用构造的 protobuf 字节验证解析路径。
"""
import hashlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_tieba_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

tieba = __import__(pkg.__name__ + ".clients.tieba", fromlist=["x"])


def varint(value):
    return tieba._encode_varint(value)


def field_varint(num, value):
    return tieba._field_varint(num, value)


def field_bytes(num, payload):
    return tieba._field_bytes(num, payload)


def field_str(num, text):
    return tieba._field_string(num, text)


class SignatureTests(unittest.TestCase):
    def test_mobile_sign_is_uppercase_md5(self):
        params = {"kw": "测试", "pn": "0"}
        expected = hashlib.md5(
            ("kw=测试pn=0" + tieba.MOBILE_SALT).encode()).hexdigest().upper()
        self.assertEqual(tieba.mobile_sign(params), expected)

    def test_mobile_sign_sorts_keys(self):
        self.assertEqual(
            tieba.mobile_sign({"b": "2", "a": "1"}),
            tieba.mobile_sign({"a": "1", "b": "2"}),
        )

    def test_sign_excludes_existing_signature(self):
        base = {"kw": "x"}
        with_sign = dict(base, sign="deadbeef")
        self.assertEqual(tieba.mobile_sign(base), tieba.mobile_sign(with_sign))

    def test_pc_sign_is_lowercase(self):
        value = tieba.pc_sign({"word": "独立游戏", "pn": "1"})
        self.assertEqual(len(value), 32)
        self.assertEqual(value, value.lower())


class ProtobufCodecTests(unittest.TestCase):
    def test_varint_roundtrip(self):
        for value in (0, 1, 127, 128, 300, 65535, 2 ** 40):
            encoded = varint(value)
            decoded, index = tieba._read_varint(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(index, len(encoded))

    def test_field_types(self):
        payload = field_varint(1, 7) + field_str(2, "标题")
        fields = tieba.decode_message(payload)
        self.assertEqual(tieba._get_int(fields, 1), 7)
        self.assertEqual(tieba._get_str(fields, 2), "标题")

    def test_nested_message(self):
        inner = field_str(3, "昵称")
        outer = field_bytes(23, inner)
        fields = tieba.decode_message(outer)
        author = tieba._get_message(fields, 23)
        self.assertEqual(tieba._get_str(author, 3), "昵称")

    def test_missing_field_returns_default(self):
        fields = tieba.decode_message(b"")
        self.assertEqual(tieba._get_int(fields, 1, 42), 42)
        self.assertEqual(tieba._get_str(fields, 1, "无"), "无")

    def test_truncated_payload_does_not_raise(self):
        payload = field_str(2, "完整内容")
        fields = tieba.decode_message(payload[:4])
        self.assertIsInstance(fields, dict)

    def test_pack_post_page_contains_expected_fields(self):
        body = tieba.pack_post_page(123456, 2, 20, comments=True, comment_limit=5)
        outer = tieba.decode_message(body)
        data = tieba._get_message(outer, 1)
        self.assertEqual(tieba._get_int(data, 4), 123456)   # kz = tid
        self.assertEqual(tieba._get_int(data, 18), 2)       # pn
        self.assertEqual(tieba._get_int(data, 13), 20)      # rn
        self.assertEqual(tieba._get_int(data, 8), 1)        # with_floor
        common = tieba._get_message(data, 25)
        self.assertEqual(tieba._get_int(common, 1), 2)      # _client_type
        self.assertEqual(tieba._get_str(common, 2), tieba.CLIENT_VERSION)


class NormalizeTests(unittest.TestCase):
    def test_rich_text_from_list(self):
        self.assertEqual(tieba._rich_text([{"type": 0, "text": "你好"}, {"text": "世界"}]), "你好世界")

    def test_rich_text_from_string(self):
        self.assertEqual(tieba._rich_text("  纯文本 "), "纯文本")

    def test_author_name_nested(self):
        value = {"user_show_info": {"name_show": "某吧友"}}
        self.assertEqual(tieba._author_name(value), "某吧友")

    def test_normalize_thread_requires_tid(self):
        self.assertIsNone(tieba.TiebaClient._normalize_thread({"title": "无编号"}))

    def test_normalize_thread_fields(self):
        raw = {
            "id": 111,
            "title": "标题",
            "reply_num": 3,
            "view_num": 9,
            "first_post_content": [{"type": 0, "text": "首帖正文"},
                                   {"type": 3, "cdn_src": "//tiebapic.baidu.com/a.jpg"}],
            "abstract": [{"text": "摘要内容"}],
            "author": {"user_show_info": {"name_show": "楼主"}},
            "forum_info": {"forum_name": "天堂鸡汤"},
        }
        item = tieba.TiebaClient._normalize_thread(raw)
        self.assertEqual(item["tid"], "111")
        self.assertEqual(item["author"], "楼主")
        self.assertEqual(item["text"], "首帖正文")
        self.assertEqual(item["images"], ["https://tiebapic.baidu.com/a.jpg"])
        self.assertEqual(item["forum"], "天堂鸡汤")
        self.assertTrue(item["url"].endswith("/p/111"))

    def test_normalize_thread_falls_back_to_abstract(self):
        raw = {"id": 5, "title": "t", "abstract": [{"text": "只有摘要"}],
               "first_post_content": [], "media": None}
        item = tieba.TiebaClient._normalize_thread(raw)
        self.assertEqual(item["text"], "只有摘要")

    def test_author_name_from_show_icon_list(self):
        value = {"show_icon_list": [
            {"sub_type": "level", "text": "Lv.5"},
            {"sub_type": "name_show", "text": "某吧友"},
        ]}
        self.assertEqual(tieba._author_name(value), "某吧友")


class RenderingTests(unittest.TestCase):
    def thread(self):
        return {
            "title": "标题",
            "posts": [
                {"floor": 1, "author": "楼主", "text": "正文一", "likes": 2, "images": ["a.jpg"],
                 "comments": [{"author": "吧友", "text": "回复一"}]},
                {"floor": 2, "author": "二楼", "text": "", "images": [], "comments": []},
            ],
        }

    def test_post_text_skips_empty_and_includes_replies(self):
        text = tieba.post_text(self.thread())
        self.assertIn("正文一", text)
        self.assertIn("回复一", text)
        self.assertNotIn("二楼", text)

    def test_iter_images(self):
        self.assertEqual(list(tieba.iter_images(self.thread())), ["a.jpg"])


class EvidenceTests(unittest.TestCase):
    def item(self, **extra):
        base = {
            "tid": "123",
            "title": "帖子标题",
            "author": "楼主",
            "forum": "独立游戏",
            "text": "帖子正文内容",
            "images": ["https://example.org/a.jpg"],
            "reply_num": 12,
            "view_num": 340,
            "comments": [{"name": "吧友", "text": "有意思"}],
        }
        base.update(extra)
        return base

    def test_evidence_has_expected_shape(self):
        result = tieba.build_evidence(self.item())
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["content_id"], "tieba:123")
        self.assertIn("article", result["evidence"])
        self.assertIn("帖子正文内容", result["digest"])

    def test_evidence_records_floor_limitation(self):
        result = tieba.build_evidence(self.item())
        self.assertTrue(any("楼层" in note for note in result["uncertainties"]))

    def test_title_only_is_metadata_only(self):
        result = tieba.build_evidence(self.item(text="", comments=[], images=[]))
        self.assertEqual(result["status"], "metadata_only")
        self.assertIn("尚未确认正文", result["digest"])

    def test_search_comments_are_rendered(self):
        result = tieba.build_evidence(self.item())
        self.assertIn("有意思", result["digest"])


if __name__ == "__main__":
    unittest.main()
