"""百度贴吧只读客户端。

贴吧对匿名访问有明显区分：

- 网页版（``tieba.baidu.com/f``、``/p/xxx``）会返回 403 安全页，必须登录才可读；
- 移动端 JSON 接口（``c.tieba.baidu.com/c/f/...``）匿名可用，但需要进行签名；
- 帖子楼层接口使用 protobuf 传输，因此本模块内置了一个最小的 protobuf 编解码器，
  避免为此引入 protobuf、aiohttp 等额外依赖。

本模块只做读取与结构归一，不处理登录、不发帖、不回复。
签名算法与字段编号参考 aiotieba（Unlicense）与 MediaCrawler 的公开实现。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx

#: 移动端接口签名盐值。
MOBILE_SALT = "tiebaclient!!!"
#: 网页端签名盐值。
PC_SALT = "36770b1f34c9bbf2e7d1a99d2b82fa9e"
#: 帖子楼层接口的命令号。
CMD_POST_PAGE = 302001
#: 移动端接口使用的版本号。
CLIENT_VERSION = "12.64.1.1"

MOBILE_HOST = "http://c.tieba.baidu.com"
WEB_HOST = "https://tieba.baidu.com"
#: protobuf 接口使用的域名，与 JSON 接口不同。
APP_HOST = "https://tiebac.baidu.com"

UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class TiebaError(RuntimeError):
    """贴吧接口返回业务错误或响应无法解析。"""


class TiebaLoginRequired(TiebaError):
    """该接口在匿名状态下不可用，需要登录态才能读取。

    典型场景是帖子楼层：服务端对未登录请求统一返回「帖子可能已被删除」。
    """


# ── 签名 ──────────────────────────────────────────────────────────


def mobile_sign(params: dict[str, Any]) -> str:
    """计算移动端接口签名。

    规则：按键名排序后以 ``k=v`` 直接拼接（无分隔符），追加盐值后取 MD5 大写。

    Args:
        params: 待签名的参数（不含 ``sign``）。

    Returns:
        32 位大写十六进制签名。
    """
    text = "".join(f"{key}={params[key]}" for key in sorted(params) if key not in ("sign", "sig"))
    return hashlib.md5((text + MOBILE_SALT).encode("utf-8")).hexdigest().upper()


def pc_sign(params: dict[str, Any]) -> str:
    """计算网页端接口签名。

    Args:
        params: 待签名的参数（不含 ``sign``）。

    Returns:
        32 位小写十六进制签名。
    """
    text = "".join(f"{key}={value}" for key, value in sorted(params.items())
                   if key not in ("sign", "sig"))
    return hashlib.md5((text + PC_SALT).encode("utf-8")).hexdigest()


# ── 最小 protobuf 编解码 ──────────────────────────────────────────
#
# 只覆盖贴吧接口实际用到的两种线格式：varint 与长度分隔的字节串。


def _encode_varint(value: int) -> bytes:
    """把非负整数编码为 protobuf varint。"""
    out = bytearray()
    value = int(value)
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_varint(number: int, value: int) -> bytes:
    """编码一个 varint 字段。"""
    return _encode_varint(number << 3) + _encode_varint(int(value))


def _field_bytes(number: int, payload: bytes) -> bytes:
    """编码一个长度分隔字段。"""
    return _encode_varint((number << 3) | 2) + _encode_varint(len(payload)) + payload


def _field_string(number: int, value: str) -> bytes:
    return _field_bytes(number, str(value).encode("utf-8"))


def decode_message(data: bytes) -> dict[int, list[Any]]:
    """把一段 protobuf 字节解析成 ``{字段号: [值, ...]}``。

    varint 字段解析为 ``int``，长度分隔字段解析为 ``bytes``，其余线格式跳过。
    未知字段会被保留，便于将来接口增加字段时不影响解析。

    Args:
        data: protobuf 序列化后的字节。

    Returns:
        字段号到值列表的映射；输入为空时返回空字典。
    """
    fields: dict[int, list[Any]] = {}
    index = 0
    size = len(data)
    while index < size:
        key, index = _read_varint(data, index)
        if key is None:
            break
        number, wire = key >> 3, key & 0x07
        if number <= 0:
            break
        if wire == 0:
            value, index = _read_varint(data, index)
            if value is None:
                break
            fields.setdefault(number, []).append(value)
        elif wire == 2:
            length, index = _read_varint(data, index)
            if length is None or index + length > size:
                break
            fields.setdefault(number, []).append(data[index:index + length])
            index += length
        elif wire == 1:
            index += 8
        elif wire == 5:
            index += 4
        else:
            break
    return fields


def _read_varint(data: bytes, index: int) -> tuple[int | None, int]:
    """从 ``index`` 起读取一个 varint，返回 ``(值, 新下标)``。"""
    result = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            break
    return None, index


def _get_int(fields: dict[int, list[Any]], number: int, default: int = 0) -> int:
    values = fields.get(number) or []
    for value in values:
        if isinstance(value, int):
            return value
    return default


def _get_str(fields: dict[int, list[Any]], number: int, default: str = "") -> str:
    values = fields.get(number) or []
    for value in values:
        if isinstance(value, bytes):
            return value.decode("utf-8", "ignore")
    return default


def _get_bytes(fields: dict[int, list[Any]], number: int) -> bytes:
    values = fields.get(number) or []
    return values[0] if values and isinstance(values[0], bytes) else b""


def _get_messages(fields: dict[int, list[Any]], number: int) -> list[dict[int, list[Any]]]:
    return [decode_message(value) for value in fields.get(number) or [] if isinstance(value, bytes)]


def _get_message(fields: dict[int, list[Any]], number: int) -> dict[int, list[Any]]:
    payload = _get_bytes(fields, number)
    return decode_message(payload) if payload else {}


def pack_post_page(tid: int, page: int, limit: int, *, comments: bool, comment_limit: int) -> bytes:
    """构造帖子楼层请求（``PbPageReqIdl``）。

    Args:
        tid: 帖子编号。
        page: 页码，从 1 开始。
        limit: 本页楼层数。
        comments: 是否同时请求楼中楼。
        comment_limit: 每层最多取几条楼中楼。

    Returns:
        可直接作为请求体发送的 protobuf 字节。
    """
    common = _field_varint(1, 2) + _field_string(2, CLIENT_VERSION)
    data = b"".join([
        _field_varint(4, int(tid)),      # kz：帖子编号
        _field_varint(5, 0),             # lz：只看楼主
        _field_varint(6, 0),             # r：排序
        _field_varint(13, max(1, int(limit))),   # rn：楼层数
        _field_varint(18, max(1, int(page))),    # pn：页码
        _field_bytes(25, common),        # common：公共请求头
    ])
    if comments:
        data += b"".join([
            _field_varint(8, 1),                          # with_floor：附带楼中楼
            _field_varint(9, max(1, int(comment_limit))),  # floor_rn
            _field_varint(74, 0),                         # floor_sort_type
        ])
    return _field_bytes(1, data)


# ── 结构归一 ──────────────────────────────────────────────────────


def _rich_text(value: Any) -> str:
    """把 ``abstract`` 之类的富文本结构压成纯文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    return ""


def _author_name(value: Any) -> str:
    """从贴吧的多层用户结构里取昵称。

    列表接口的 ``author`` 字段有两种形态：``{"show_icon_list": [...]}``（昵称藏在
    ``sub_type == "name_show"`` 的图标项里）与 ``{"user_show_info": {...}}``。
    """
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("name_show", "name", "user_name", "user_nickname"):
        name = str(value.get(key) or "").strip()
        if name:
            return name
    icons = value.get("show_icon_list")
    if isinstance(icons, list):
        for icon in icons:
            if isinstance(icon, dict) and str(icon.get("sub_type") or "") == "name_show":
                name = str(icon.get("text") or "").strip()
                if name:
                    return name
    for key in ("user_show_info", "user", "author"):
        nested = value.get(key)
        if isinstance(nested, dict):
            name = _author_name(nested)
            if name:
                return name
    return ""


def _pick_image(entry: Any) -> str:
    """从贴吧的多种图片结构里取一个可访问的地址。"""
    if isinstance(entry, str):
        url = entry.strip()
    elif isinstance(entry, dict):
        url = ""
        for key in ("big_pic", "cdn_src", "origin_src", "water_pic", "src",
                    "small_pic", "cdn_src_active", "big_cdn_src"):
            candidate = str(entry.get(key) or "").strip()
            if candidate:
                url = candidate
                break
    else:
        return ""
    if not url:
        return ""
    return "https:" + url if url.startswith("//") else url


def _split_contents(contents: Any) -> tuple[str, list[str]]:
    """把 ``first_post_content`` 之类的富内容拆成正文与图片地址。"""
    if isinstance(contents, str):
        return contents.strip(), []
    if not isinstance(contents, list):
        return "", []
    texts: list[str] = []
    images: list[str] = []
    for entry in contents:
        if isinstance(entry, str):
            texts.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        text = str(entry.get("text") or "").strip()
        if text:
            texts.append(text)
        if kind == 3 or entry.get("cdn_src") or entry.get("origin_src"):
            url = _pick_image(entry)
            if url:
                images.append(url)
    return "".join(texts).strip(), images


def _content_parts(fields: dict[int, list[Any]]) -> tuple[str, list[str]]:
    """把楼层的 ``PbContent`` 列表拆成正文与图片地址。"""
    texts: list[str] = []
    images: list[str] = []
    for content in _get_messages(fields, 5):
        kind = _get_int(content, 1)
        text = _get_str(content, 2)
        if kind == 0 and text:
            texts.append(text)
        elif kind == 3:
            if text:
                texts.append(text)
            src = _get_str(content, 25) or _get_str(content, 9) or _get_str(content, 8) or _get_str(content, 4)
            if src:
                images.append("https:" + src if src.startswith("//") else src)
        elif kind == 4:
            link = _get_str(content, 3)
            if link and link not in texts:
                texts.append(link)
    return "".join(texts).strip(), images


class TiebaClient:
    """百度贴吧只读客户端，覆盖吧内主题、搜索与帖子楼层。"""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    # ── 吧内主题列表 ────────────────────────────────────────────

    async def forum_threads(self, forum: str, page: int = 0, limit: int = 30) -> list[dict]:
        """取某个吧的主题列表。

        使用移动端 JSON 接口，匿名可读。返回的条目带标题、摘要、作者与互动数，
        适合作为「逛贴吧」的候选；正文需要另外取 :meth:`thread`。

        Args:
            forum: 吧名（不带「吧」字），例如 ``天堂鸡汤``。
            page: 页码，从 0 开始。
            limit: 最多返回条数，接口单页上限 30。

        Returns:
            归一后的主题列表，字段与 :meth:`search` 保持一致。
        """
        params: dict[str, Any] = {
            "_client_version": CLIENT_VERSION,
            "_client_type": "2",
            "_phone_imei": "000000000000000",
            "kw": str(forum or "").strip(),
            "pn": str(max(0, int(page))),
            "rn": str(max(1, min(int(limit or 30), 30))),
            "st_type": "tb_frslist",
        }
        payload = await self._mobile_json("/c/f/frs/page", params)
        threads = payload.get("thread_list") or []
        return [item for item in (self._normalize_thread(raw) for raw in threads) if item]

    @staticmethod
    def _normalize_thread(raw: dict) -> dict | None:
        """把列表接口的原始条目归一成通用结构。

        首帖正文优先取 ``first_post_content``（可能含图片），退回 ``abstract``。
        """
        tid = str(raw.get("id") or raw.get("tid") or "").strip()
        if not tid:
            return None
        text, images = _split_contents(raw.get("first_post_content"))
        if not text:
            text = _rich_text(raw.get("abstract"))
        for entry in raw.get("media") or []:
            url = _pick_image(entry)
            if url and url not in images:
                images.append(url)
        return {
            "tid": tid,
            "title": str(raw.get("title") or "").strip(),
            "author": _author_name(raw.get("author")),
            "reply_num": int(raw.get("reply_num") or 0),
            "view_num": int(raw.get("view_num") or 0),
            "text": text[:2000],
            "images": images[:6],
            "forum": str((raw.get("forum_info") or {}).get("forum_name") or "").strip(),
            "url": f"{WEB_HOST}/p/{tid}",
            "source": "forum",
        }

    # ── 搜索 ────────────────────────────────────────────────────

    async def search(self, keyword: str, page: int = 1, limit: int = 10) -> list[dict]:
        """按关键词搜索帖子。

        使用网页端搜索接口（免登录可用），返回结果自带正文片段与配图，
        因此即使拿不到楼层详情，也能作为内容证据使用。

        Args:
            keyword: 搜索词。
            page: 页码，从 1 开始。
            limit: 最多返回条数。

        Returns:
            归一后的帖子列表；接口无结果时返回空列表。
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        params: dict[str, Any] = {
            "word": keyword,
            "pn": str(max(1, int(page))),
            "rn": str(max(1, min(int(limit or 10), 20))),
            "st": "1",
            "tt": "1",
            "subapp_type": "pc",
            "_client_type": "20",
        }
        params["sign"] = pc_sign(params)
        response = await self._client.get(
            f"{WEB_HOST}/mo/q/search/thread",
            params=params,
            headers={"User-Agent": UA_WEB, "Referer": f"{WEB_HOST}/f/search/res"},
        )
        if response.status_code != 200:
            raise TiebaError(f"贴吧搜索失败（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TiebaError("贴吧搜索返回的不是 JSON") from exc
        if payload.get("no") not in (0, "0", None):
            raise TiebaError(f"贴吧搜索出错：{payload.get('error') or payload.get('no')}")
        items = (payload.get("data") or {}).get("post_list") or []
        results = []
        for raw in items:
            tid = str(raw.get("tid") or "").strip()
            if not tid:
                continue
            images = [url for url in (_pick_image(entry) for entry in raw.get("media") or []) if url]
            comments = []
            for entry in raw.get("comment_list") or []:
                if not isinstance(entry, dict):
                    continue
                text = str(entry.get("content") or "").strip()
                if not text:
                    continue
                comments.append({
                    "name": _author_name(entry.get("user")) or "匿名",
                    "text": text[:200],
                    "likes": 0,
                    "replies": 0,
                })
            results.append({
                "tid": tid,
                "title": str(raw.get("title") or "").strip(),
                "author": _author_name(raw.get("user")),
                "forum": str(raw.get("forum_name") or "").strip(),
                "text": str(raw.get("content") or "").strip()[:2000],
                "images": images[:6],
                "comments": comments[:6],
                "likes": int(raw.get("like_num") or 0),
                "url": f"{WEB_HOST}/p/{tid}",
                "keyword": keyword,
                "source": "search",
            })
        return results

    # ── 帖子楼层 ────────────────────────────────────────────────

    async def thread(self, tid: str, page: int = 1, limit: int = 20,
                     *, comments: bool = True, comment_limit: int = 5) -> dict:
        """取一个帖子的标题与楼层。

        走移动端 protobuf 接口。匿名可用，但贴吧对该接口的风控比 JSON 接口更敏感，
        失败时应当由调用方降级为「仅标题与摘要」。

        Args:
            tid: 帖子编号。
            page: 页码，从 1 开始。
            limit: 本页楼层数。
            comments: 是否同时取楼中楼。
            comment_limit: 每层最多取几条楼中楼。

        Returns:
            ``{"title", "author", "posts": [{"floor", "author", "text", "images", "comments"}]}``。
        """
        try:
            tid_value = int(str(tid).strip())
        except (TypeError, ValueError) as exc:
            raise TiebaError("帖子编号无效") from exc

        body = pack_post_page(tid_value, page, limit, comments=comments, comment_limit=comment_limit)
        response = await self._client.post(
            f"{APP_HOST}/c/f/pb/page?{urlencode({'cmd': CMD_POST_PAGE})}",
            content=body,
            headers={
                "User-Agent": UA_MOBILE,
                "Referer": f"{WEB_HOST}/p/{tid}",
                # 该请求头告诉服务端按 protobuf 解析请求体；缺失时会按 JSON 处理并报参数错误。
                "x_bd_data_type": "protobuf",
            },
        )
        if response.status_code != 200:
            raise TiebaError(f"贴吧帖子接口失败（HTTP {response.status_code}）")
        root = decode_message(response.content)
        error = _get_message(root, 1)
        code = _get_int(error, 1)
        if code:
            message = _get_str(error, 2) or ""
            if code == 350004:
                # 服务端对未登录的楼层请求统一回这条，实际含义是「匿名不可读」。
                raise TiebaLoginRequired(f"贴吧楼层需要登录态：{message or code}")
            raise TiebaError(f"贴吧返回错误 {code}：{message}".strip())

        data = _get_message(root, 2)
        thread = _get_message(data, 8)
        forum = _get_message(data, 2)
        posts = []
        for raw in _get_messages(data, 6):
            text, images = _content_parts(raw)
            replies = []
            for sub in _get_messages(_get_message(raw, 15), 2):
                sub_text, _ = _content_parts(sub)
                if sub_text:
                    replies.append({
                        "author": _author_name(_get_message(sub, 7)),
                        "text": sub_text[:200],
                    })
            posts.append({
                "floor": _get_int(raw, 3),
                "author": _author_name(_get_message(raw, 23)),
                "text": text,
                "images": images[:6],
                "likes": _get_int(_get_message(raw, 37), 1),
                "comments": replies[:max(0, int(comment_limit))],
            })
        return {
            "tid": str(tid),
            "title": _get_str(thread, 3),
            "forum": _get_str(forum, 1),
            "author": _author_name(_get_message(thread, 18)),
            "reply_num": _get_int(thread, 4),
            "view_num": _get_int(thread, 5),
            "url": f"{WEB_HOST}/p/{tid}",
            "posts": posts,
        }

    # ── 内部请求 ────────────────────────────────────────────────

    async def _mobile_json(self, path: str, params: dict[str, Any]) -> dict:
        """向移动端接口发起一次已签名的 GET，返回解析后的 JSON。"""
        payload: dict[str, Any] = dict(params)
        payload["sign"] = mobile_sign(payload)
        response = await self._client.get(
            f"{MOBILE_HOST}{path}",
            params=payload,
            headers={"User-Agent": UA_MOBILE, "Referer": f"{WEB_HOST}/"},
        )
        if response.status_code != 200:
            raise TiebaError(f"贴吧接口失败（HTTP {response.status_code}）")
        try:
            data = json.loads(response.text)
        except ValueError as exc:
            raise TiebaError("贴吧接口返回的不是 JSON") from exc
        if data.get("error_code"):
            raise TiebaError(f"贴吧返回错误 {data.get('error_code')}：{data.get('error_msg') or ''}".strip())
        return data


def build_evidence(item: dict) -> dict:
    """把一条贴吧候选转成与视频理解一致的内容证据结构。

    贴吧匿名状态只能拿到首帖正文与搜索片段，楼层回复需要登录。因此这里始终把
    「未读取楼层」写进局限，避免后续把摘要当成完整讨论。

    Args:
        item: :meth:`TiebaClient.forum_threads` 或 :meth:`TiebaClient.search`
            返回的条目。

    Returns:
        含 ``ok``/``status``/``digest``/``evidence``/``limitations`` 等字段的字典，
        可直接交给观感与分享环节。
    """
    tid = str(item.get("tid") or "")
    title = str(item.get("title") or "无标题")
    text = str(item.get("text") or "").strip()
    images = [url for url in item.get("images") or [] if url]
    comments = [entry for entry in item.get("comments") or [] if isinstance(entry, dict)]
    forum = str(item.get("forum") or "")
    author = str(item.get("author") or "匿名")

    parts: list[tuple[str, str]] = []
    if text:
        parts.append(("article", text[:1200]))
    if comments:
        parts.append(("comments", "\n".join(
            f"{entry.get('name') or '匿名'}：{entry.get('text') or ''}" for entry in comments[:6]
        )))
    evidence = [name for name, _ in parts]
    limitations = ["贴吧楼层回复需要登录，本次只有首帖内容与可见评论"]

    labels = {"article": "正文", "comments": "评论"}
    digest = f"《{title}》\n"
    if forum:
        digest += f"吧名：{forum}\n"
    digest += f"作者：{author}\n"
    if item.get("reply_num") is not None:
        digest += f"回复：{item.get('reply_num') or 0}  浏览：{item.get('view_num') or 0}\n"
    digest += "\n".join(f"{labels.get(name, name)}：{body}" for name, body in parts)
    if not parts:
        digest += "仅取得标题，尚未确认正文内容，不应声称看过。"
    if images:
        digest += f"\n配图：共 {len(images)} 张"
    digest += "\n局限：" + "；".join(limitations)

    return {
        "ok": bool(text or title),
        "status": "partial" if evidence else "metadata_only",
        "content_id": f"tieba:{tid}" if tid else "",
        "title": title,
        "author": author,
        "via": "、".join(labels.get(name, name) for name in evidence) or "标题",
        "evidence": evidence,
        "images": images,
        "duration_seconds": 0,
        "sampled_timestamps": [],
        "uncertainties": limitations,
        "digest": digest[:12000],
    }


def post_text(thread: dict, limit: int = 6) -> str:
    """把帖子楼层压成一段可供提示词使用的文本，楼层之间保留分隔。"""
    lines: list[str] = []
    for post in (thread.get("posts") or [])[:limit]:
        author = post.get("author") or "匿名"
        text = str(post.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{author}（{post.get('floor') or '?'}楼，{post.get('likes') or 0}赞）：{text}")
        for reply in (post.get("comments") or [])[:3]:
            lines.append(f"    └ {reply.get('author') or '匿名'}：{reply.get('text') or ''}")
    return "\n".join(lines)


def iter_images(thread: dict, limit: int = 6) -> Iterable[str]:
    """按楼层顺序取帖子里的图片地址。"""
    count = 0
    for post in thread.get("posts") or []:
        for url in post.get("images") or []:
            yield url
            count += 1
            if count >= limit:
                return
