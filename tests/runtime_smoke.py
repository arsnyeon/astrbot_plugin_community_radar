"""Run in the AstrBot container staging folder; never starts production background jobs."""
import asyncio
import importlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, "/AstrBot")
ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("radar_runtime_check")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
main = importlib.import_module("radar_runtime_check.main")
video = importlib.import_module("radar_runtime_check.media.video_watch")
core = importlib.import_module("radar_runtime_check.core.radar_core")


async def run():
    checks = {"real_astrbot_import": True}
    import inspect
    owned = []
    for name, function in inspect.getmembers(main.CommunityRadarPlugin, inspect.isfunction):
        if function.__module__ == 'radar_runtime_check.main':
            owned.append(name)
    for name in ('_on_recent_share', '_on_community_link', 'radar_cmd', '_t_radar_share', 'radar_recall_tool'):
        assert name in owned, ('handler not owned by entry module', name)
    checks['entry_owned_methods'] = len(owned)
    with tempfile.TemporaryDirectory(prefix="radar_smoke_") as folder:
        path = Path(folder)
        sample = path / "sample.mp4"
        generated = await video.run_media([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc=size=320x240:rate=10", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=16000", "-t", "6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(sample),
        ], 20)
        assert generated and sample.exists(), "synthetic video generation failed"
        watcher = video.VideoWatch(types.SimpleNamespace())
        frames = await watcher._extract(str(sample), 6, 3)
        assert len(frames) == 3, ("frames", len(frames))
        assert frames[-1][0] > 5.5
        checks["ffmpeg_frames"] = {"count": len(frames), "timestamps": [t for t, _ in frames]}
        from PIL import Image
        for _, file in frames:
            with Image.open(file) as image:
                assert image.width == 640
            Path(file).unlink()
        store = core.RadarStore(path)
        cache_module = importlib.import_module('radar_runtime_check.media.media_cache')
        from astrbot.core.message.components import Image as MessageImage
        from unittest.mock import AsyncMock
        watcher.plugin = types.SimpleNamespace(radar_store=store)
        watcher._describe = AsyncMock(return_value='实际测试画面')
        frames = await watcher._extract(str(sample), 6, 3)
        cfg = {}
        await watcher._caption([p for _, p in frames], cfg, [t for t, _ in frames])
        assert all(not Path(p).exists() for _, p in frames)
        observed = cfg.get('_observed_media', [])
        assert len(observed) >= 2
        cache = cache_module.MediaCache(path)
        for media in observed:
            data = cache.read(media['media_id'])
            assert data
            component = MessageImage.fromBytes(data)
            assert component.file.startswith('base64://')
        checks['observed_media_survives_frame_cleanup'] = len(observed)
        store.close()
    checks["media_temp_cleanup"] = True
    print("RADAR_SMOKE=" + json.dumps(checks, ensure_ascii=False))


asyncio.run(run())
