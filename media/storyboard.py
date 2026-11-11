"""B 站官方视频快照（storyboard）读取与裁切。

B 站为每个视频提供了预先生成的缩略图拼版：一张大图按网格排列了覆盖全片的
若干帧，并附带每帧对应的时间点。相比下载视频再用 ffmpeg 抽帧，这种方式
不需要外部程序、不需要传输完整视频流，且天然覆盖整段时长。

本模块只负责「取得画面」。对画面的理解、以及它是否足以支撑结论，由
``video_watch`` 决定；这里不调用模型。

思路：官方 videoshot 接口已给出覆盖全片的缩略图网格与时间轴，直接裁切即可，无需下载视频。
"""

from __future__ import annotations

import asyncio
import io
import struct
from dataclasses import dataclass
from typing import Any, Iterable

from astrbot.api import logger

VIDEOSHOT_ENDPOINT = "https://api.bilibili.com/x/player/videoshot"

#: 拼版默认网格尺寸，接口未给出时使用。
DEFAULT_GRID = (10, 10)
#: 单帧默认像素尺寸，接口未给出时使用。
DEFAULT_FRAME_SIZE = (160, 90)
#: 同时下载的拼版图数量上限，避免瞬时打出过多请求。
MAX_CONCURRENT_SHEETS = 3
#: 单张拼版图的体积上限，超出视为异常响应。
MAX_SHEET_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Storyboard:
    """一个视频的快照拼版及其时间轴。

    Attributes:
        image_urls: 拼版图地址，按顺序拼接成完整时间轴。
        timestamps: 与 ``image_urls`` 中每一帧一一对应的秒数（已去掉首项偏移）。
        grid: ``(列数, 行数)``，单张拼版图内的帧排布。
        frame_size: 单帧的 ``(宽, 高)`` 像素。
        duration: 视频总时长（秒），未知时为 0。
    """

    image_urls: tuple[str, ...]
    timestamps: tuple[int, ...]
    grid: tuple[int, int] = DEFAULT_GRID
    frame_size: tuple[int, int] = DEFAULT_FRAME_SIZE
    duration: int = 0

    @property
    def frames_per_sheet(self) -> int:
        return max(1, self.grid[0] * self.grid[1])

    @property
    def frame_count(self) -> int:
        return len(self.timestamps)

    @property
    def covered_until(self) -> int:
        """最后一帧覆盖到的秒数。"""
        return int(self.timestamps[-1]) if self.timestamps else 0

    @property
    def usable(self) -> bool:
        return bool(self.image_urls) and bool(self.timestamps)


def _normalize_url(url: str) -> str:
    url = str(url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def normalize_index(raw: Any, duration: int = 0) -> list[int]:
    """把接口返回的 ``index`` 规范成以秒为单位的整数列表。

    B 站历史上在该字段使用过三种形态：纯秒数数组、以毫秒为单位的数组、
    以及元素为 ``{"timestamp": ...}`` 的对象数组。这里统一处理，并在数值
    明显超出视频时长时按毫秒换算。
    """
    if not isinstance(raw, list) or len(raw) <= 1:
        return []
    values: list[float] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("timestamp")
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            continue
    if not values:
        return []
    if duration > 0 and max(values) > duration * 1.5:
        values = [value / 1000.0 for value in values]
    return [max(0, int(round(value))) for value in values]


def parse_pvdata(raw: bytes) -> list[int]:
    """解析 ``pvdata`` 二进制时间轴（大端 uint16 秒数序列）。"""
    if not raw or len(raw) % 2:
        return []
    count = len(raw) // 2
    if count > 20000:
        return []
    return [int(value) for value in struct.unpack(f">{count}H", raw)]


def _timestamps_from(index: list[int], sheets: int, per_sheet: int) -> list[int]:
    """由接口时间轴推出逐帧秒数。

    ``index`` 的首项与第一帧对齐（通常为 0），逐帧时间取其后各项。接口只为
    实际排出的帧给出时间点，因此这里不做任何推测性补齐：凭空延长时间轴会让
    后续的「覆盖到第几秒」变成假话。
    """
    if not index:
        return []
    stamped = list(index[1:])
    if not stamped:
        return []
    total = sheets * per_sheet
    return stamped[:total]

async def fetch(client: Any, bvid: str, cid: str = "", duration: int = 0) -> Storyboard | None:
    """读取一个视频的快照拼版。

    Args:
        client: ``clients.bili.BiliClient`` 实例，复用其连接与请求头。
        bvid: 视频 BV 号。
        cid: 分 P 的 cid；已知时传入可避免取到其他分 P 的快照。
        duration: 视频时长（秒），用于校正毫秒制时间轴并推断末段步长。

    Returns:
        可用的 :class:`Storyboard`；接口没有提供快照或响应异常时返回 ``None``。
        本函数不抛出业务异常，调用方据此回退到其他取帧方式。
    """
    bvid = str(bvid or "").strip()
    if not bvid:
        return None
    params: dict[str, str] = {"bvid": bvid, "index": "1"}
    if cid:
        params["cid"] = str(cid)

    data: dict = {}
    for attempt in range(2):
        try:
            data = await client._get(VIDEOSHOT_ENDPOINT, params) or {}
        except Exception as exc:  # noqa: BLE001 - 网络与业务错误统一降级
            logger.debug("[CommunityRadar] 快照接口不可用（%s）", type(exc).__name__)
            data = {}
        if data.get("image"):
            break
        if attempt == 0:
            await asyncio.sleep(1.0)

    if not isinstance(data, dict) or not data.get("image"):
        return None

    sheets = tuple(
        url for url in (_normalize_url(item) for item in (data.get("image") or [])) if url
    )
    if not sheets:
        return None

    grid = (int(data.get("img_x_len") or DEFAULT_GRID[0]), int(data.get("img_y_len") or DEFAULT_GRID[1]))
    frame_size = (
        int(data.get("img_x_size") or DEFAULT_FRAME_SIZE[0]),
        int(data.get("img_y_size") or DEFAULT_FRAME_SIZE[1]),
    )
    per_sheet = max(1, grid[0] * grid[1])

    index = normalize_index(data.get("index") or [], duration)
    if len(index) <= 1 and data.get("pvdata"):
        try:
            response = await client._client.get(_normalize_url(str(data["pvdata"])), headers=client._headers())
            index = parse_pvdata(response.content)
        except Exception:  # noqa: BLE001 - pvdata 只是兜底来源
            index = []

    timestamps = _timestamps_from(index, len(sheets), per_sheet)
    if not timestamps:
        return None

    board = Storyboard(
        image_urls=sheets,
        timestamps=tuple(timestamps),
        grid=grid,
        frame_size=frame_size,
        duration=int(duration or 0),
    )
    return board if board.usable else None


def _candidate_frames(board: Storyboard, start: float = 0, end: float | None = None) -> list[tuple[int, int]]:
    """筛出落在 ``[start, end]`` 区间内的帧，返回 ``(帧序号, 秒数)``。"""
    low = max(0.0, float(start or 0))
    high = float(end) if end is not None else None
    frames = []
    for order, timestamp in enumerate(board.timestamps):
        if timestamp < low:
            continue
        if high is not None and timestamp > high:
            continue
        frames.append((order, timestamp))
    return frames


def _aligned_targets(cues: Iterable[dict], low: float, high: float) -> list[float]:
    """取落在区间内的字幕时间中点，作为「这一段有话」的位置参考。"""
    targets = []
    for cue in cues or []:
        try:
            begin = max(0.0, float(cue.get("start") or 0))
            finish = max(begin, float(cue.get("end") or begin))
        except (TypeError, ValueError):
            continue
        middle = (begin + finish) / 2
        if low <= middle <= high:
            targets.append(middle)
    return targets


def select_frames(
    board: Storyboard,
    count: int,
    *,
    start: float = 0,
    end: float | None = None,
    cues: Iterable[dict] | None = None,
) -> list[tuple[int, int]]:
    """在要求的时间范围内挑选若干帧。

    采用分桶策略：把区间等分成 ``count`` 段，每段优先挑一个靠近字幕中点的帧，
    段内没有字幕时退回该段中点最近的一帧。这样即使字幕集中在开头，结尾仍然
    会被覆盖，不会只看到前几十秒。

    Args:
        board: 快照拼版。
        count: 期望帧数，超出可用帧数时按可用帧数返回。
        start: 区间起点（秒）。
        end: 区间终点（秒），``None`` 表示到结尾。
        cues: 字幕片段，提供时用于对齐重点画面。

    Returns:
        按时间升序的 ``(帧序号, 秒数)`` 列表；区间内无可用帧时返回空列表。
    """
    frames = _candidate_frames(board, start, end)
    if not frames:
        return []
    limit = max(1, min(int(count or 1), len(frames)))
    if limit >= len(frames):
        return frames
    if limit == 1:
        return [frames[len(frames) // 2]]

    low = frames[0][1]
    high = frames[-1][1]
    span = max(1.0, high - low)
    targets = _aligned_targets(cues or [], low, high)

    picked: list[tuple[int, int]] = []
    used: set[int] = set()
    for slot in range(limit):
        left = low + span * slot / limit
        right = low + span * (slot + 1) / limit
        in_slot = [frame for frame in frames if left <= frame[1] <= right and frame[0] not in used]
        if not in_slot:
            in_slot = [frame for frame in frames if frame[0] not in used]
        cue_targets = [value for value in targets if left <= value <= right]
        if cue_targets:
            target = min(cue_targets, key=lambda value: abs(value - (left + right) / 2))
        else:
            target = (left + right) / 2
        # 首尾两段直接贴边：分桶居中会让开头和结尾各留一个半桶的空档，
        # 导致「看完了」却既没看到开头也没看到结尾。
        if slot == 0:
            target = low
        elif slot == limit - 1:
            target = high
        chosen = min(in_slot, key=lambda frame: abs(frame[1] - target), default=None)
        if chosen is None:
            break
        used.add(chosen[0])
        picked.append(chosen)
    picked.sort(key=lambda frame: frame[1])
    return picked


def _frame_signature(image: Any) -> tuple[int, ...]:
    """生成用于近似重复判定的 64 位指纹。"""
    tiny = image.convert("L").resize((8, 8))
    pixels = list(tiny.tobytes())
    average = sum(pixels) / max(1, len(pixels))
    return tuple(1 if value >= average else 0 for value in pixels)


def _signature_distance(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    return sum(1 for a, b in zip(left, right) if a != b)


def _is_blank(image: Any) -> bool:
    """判断是否为接近纯黑的过渡帧。"""
    from PIL import ImageStat

    stat = ImageStat.Stat(image.convert("L"))
    return stat.mean[0] < 12 and stat.stddev[0] < 8


async def crop(
    client: Any,
    board: Storyboard,
    picks: Iterable[tuple[int, int]],
    *,
    width: int = 0,
    avoid_blank: bool = True,
    avoid_similar: bool = True,
) -> list[tuple[int, bytes]]:
    """从拼版图中裁出选定的帧。

    选中帧若是纯黑过渡画面，或与上一帧近乎相同，会在邻近位置换一帧；
    换不到时保留原帧，不静默丢弃图片。

    Args:
        client: ``BiliClient`` 实例。
        board: 快照拼版。
        picks: :func:`select_frames` 返回的 ``(帧序号, 秒数)`` 列表。
        width: 输出宽度，``0`` 表示保持原始尺寸。
        avoid_blank: 是否避开纯黑帧。
        avoid_similar: 是否避开与前一帧近似的画面。

    Returns:
        ``(秒数, JPEG 字节)`` 列表；下载失败的帧会被跳过。
    """
    from PIL import Image

    picks = list(picks)
    if not picks:
        return []
    per_sheet = board.frames_per_sheet
    total = board.frame_count
    needed = sorted({order // per_sheet for order, _ in picks if 0 <= order // per_sheet < len(board.image_urls)})
    if not needed:
        return []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SHEETS)
    cache: dict[int, bytes] = {}

    async def load(sheet_index: int) -> bytes | None:
        if sheet_index in cache:
            return cache[sheet_index]
        if not 0 <= sheet_index < len(board.image_urls):
            return None
        async with semaphore:
            try:
                response = await client._client.get(board.image_urls[sheet_index], headers=client._headers())
                if response.status_code != 200:
                    return None
                payload = response.content
            except Exception:  # noqa: BLE001 - 单张失败不影响其他拼版
                return None
        if not payload or len(payload) > MAX_SHEET_BYTES:
            return None
        cache[sheet_index] = payload
        return payload

    for sheet_index in needed:
        await load(sheet_index)

    def cut(order: int) -> Any | None:
        sheet_index = order // per_sheet
        payload = cache.get(sheet_index)
        if payload is None:
            return None
        position = order % per_sheet
        row, column = divmod(position, board.grid[0])
        try:
            sheet = Image.open(io.BytesIO(payload)).convert("RGB")
        except Exception:  # noqa: BLE001 - 损坏的图片按不可用处理
            return None
        cell_width, cell_height = board.frame_size
        box = (
            column * cell_width,
            row * cell_height,
            (column + 1) * cell_width,
            (row + 1) * cell_height,
        )
        if box[2] > sheet.width or box[3] > sheet.height:
            return None
        frame = sheet.crop(box)
        if width and frame.width != width:
            ratio = width / frame.width
            frame = frame.resize((width, max(1, round(frame.height * ratio))), Image.LANCZOS)
        return frame

    output: list[tuple[int, bytes]] = []
    used: set[int] = set()
    previous: tuple[int, ...] | None = None

    for order, timestamp in picks:
        neighbours = [order] + [order + delta for delta in (1, -1, 2, -2, 3, -3)]
        chosen: tuple[int, Any] | None = None
        fallback: tuple[int, Any] | None = None
        for candidate in neighbours:
            if not 0 <= candidate < total or candidate in used:
                continue
            frame = cut(candidate)
            if frame is None:
                continue
            if fallback is None:
                fallback = (candidate, frame)
            if avoid_blank and _is_blank(frame):
                continue
            signature = _frame_signature(frame)
            if avoid_similar and previous is not None and _signature_distance(previous, signature) < 3:
                continue
            chosen = (candidate, frame)
            break
        if chosen is None:
            chosen = fallback
        if chosen is None:
            continue
        candidate, frame = chosen
        used.add(candidate)
        previous = _frame_signature(frame)
        seconds = board.timestamps[candidate] if candidate < total else timestamp
        buffer = io.BytesIO()
        try:
            frame.save(buffer, format="JPEG", quality=88)
        except Exception:  # noqa: BLE001
            continue
        output.append((int(seconds), buffer.getvalue()))
    return output
