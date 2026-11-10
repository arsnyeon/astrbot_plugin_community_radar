"""与平台无关的通用契约。

能力必须显式声明，不存在的能力不会被假设为可用。
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    search: bool = False
    feed: bool = False
    detail: bool = False
    comments: bool = False
    video: bool = False
    images: bool = False
    write: bool = False


@dataclass(frozen=True)
class Media:
    kind: str  # cover, frame, post_image, video
    source: str
    timestamp: float | None = None


@dataclass
class Content:
    platform: str
    native_id: str
    url: str
    title: str = ''
    author: str = ''
    text: str = ''
    media: list[Media] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def content_id(self):
        if not self.platform or not self.native_id:
            raise ValueError('platform and native_id required')
        return self.platform + ':' + self.native_id


class ReadAdapter(Protocol):
    name: str
    capabilities: Capabilities

    async def search(self, keyword: str, limit: int = 5) -> list[Content]: ...
    async def detail(self, native_id: str) -> Content: ...


class AdapterRegistry:
    def __init__(self):
        self._adapters = {}

    def register(self, adapter: ReadAdapter):
        if not adapter.name or adapter.name in self._adapters:
            raise ValueError('empty or duplicate platform')
        self._adapters[adapter.name] = adapter

    def get(self, name, capability=None):
        adapter = self._adapters.get(name)
        if adapter is None:
            raise LookupError('platform unavailable')
        if capability is not None:
            if capability not in Capabilities.__dataclass_fields__:
                raise ValueError('unknown capability')
            if not getattr(adapter.capabilities, capability):
                raise NotImplementedError('platform capability unavailable')
        return adapter
