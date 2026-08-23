from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dualmaker.metadata import MediaInspector
from dualmaker.preprocess import choose_recap_trim

TOOLS = ("ffmpeg", "ffprobe", "mediainfo", "mkvmerge")


def run(*args: str | Path) -> None:
    subprocess.run(tuple(str(arg) for arg in args), check=True, capture_output=True)


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "media tools are required")
class RecapIntegrationTests(unittest.TestCase):
    def test_one_sided_black_bounded_opening_is_trimmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = root / "common.wav"
            delayed = root / "delayed.wav"
            normal = root / "normal.mkv"
            dual = root / "dual.mkv"
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*(220+18*t)*t):s=48000:d=20",
                common,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                common,
                "-af",
                "adelay=3000:all=1",
                delayed,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=96x54:rate=24:color=black:duration=3",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=96x54:rate=24:duration=20",
                "-i",
                delayed,
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map",
                "[v]",
                "-map",
                "2:a",
                "-c:v",
                "libx264",
                "-force_key_frames",
                "3",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                normal,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=64x36:rate=24:duration=20",
                "-i",
                common,
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                dual,
            )
            inspector = MediaInspector()
            normal_asset = inspector.inspect(normal)
            dual_asset = inspector.inspect(dual)
            decision = choose_recap_trim(
                normal,
                dual,
                normal_asset.audio_tracks[0],
                dual_asset.audio_tracks[0],
                window=10,
            )
            self.assertTrue(decision.applied, decision)
            self.assertAlmostEqual(decision.normal_trim, 3.0, delta=0.1)
            self.assertEqual(decision.dual_trim, 0.0)
            self.assertGreater(decision.selected_score or 0, decision.baseline_score or 0)


if __name__ == "__main__":
    unittest.main()
