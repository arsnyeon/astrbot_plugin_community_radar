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
video = importlib.import_module("radar_runtime_check.video_watch")
core = importlib.import_module("radar_runtime_check.radar_core")


async def run():
    checks = {"real_astrbot_import": True}
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
        store.close()
    checks["media_temp_cleanup"] = True
    print("RADAR_SMOKE=" + json.dumps(checks, ensure_ascii=False))


asyncio.run(run())
