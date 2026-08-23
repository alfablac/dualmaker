from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dualmaker.matching import find_pair_candidates
from dualmaker.metadata import MediaInspector, _duration_seconds, first_packet_pts, last_packet_end
from dualmaker.naming import parse_identity

TOOLS = ("ffmpeg", "ffprobe", "mediainfo", "mkvmerge")


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "media tools are required")
class MetadataIntegrationTests(unittest.TestCase):
    def test_matroska_clock_duration_is_parsed(self) -> None:
        self.assertAlmostEqual(_duration_seconds("00:23:08.921000000") or 0, 1388.921)

    def test_first_packet_pts_preserves_a_matroska_track_delay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            delayed = Path(directory) / "delayed.mkv"
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=24:duration=2",
                    "-itsoffset",
                    "0.75",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=1",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(delayed),
                ),
                check=True,
            )
            self.assertAlmostEqual(first_packet_pts(delayed, "audio", 0) or 0.0, 0.75, delta=0.05)

    def test_synthetic_files_are_inspected_and_paired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normal = root / "Tiny.Movie.2026.1080p.MA.WEB-DL-GROUP.mkv"
            dual = root / "Tiny.Movie.2026.720p.AMZN.WEB-DL.DUAL-C76.mkv"
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=64x64:rate=24:color=blue",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000",
                    "-t",
                    "1",
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
                    str(normal),
                ),
                check=True,
            )
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=48x48:rate=24:color=red",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=660:sample_rate=48000",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000",
                    "-t",
                    "1",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-map",
                    "2:a",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-metadata:s:a:0",
                    "language=por",
                    "-metadata:s:a:1",
                    "language=eng",
                    str(dual),
                ),
                check=True,
            )

            inspector = MediaInspector()
            normal_asset = inspector.inspect(normal)
            dual_asset = inspector.inspect(dual)
            self.assertIsNotNone(normal_asset.frame_rate)
            self.assertEqual(normal_asset.frame_rate.rational, "24/1")  # type: ignore[union-attr]
            self.assertEqual(dual_asset.frame_rate.rational, "24/1")  # type: ignore[union-attr]
            normal_asset.identity = parse_identity(normal)
            dual_asset.identity = parse_identity(dual)
            candidates, skipped = find_pair_candidates([normal_asset, dual_asset])
            self.assertFalse(skipped)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].shared_original_languages, ("en",))

    def test_subtitle_tail_does_not_extend_primary_av_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.mkv"
            subtitle = root / "late.srt"
            polluted = root / "polluted.mkv"
            subtitle.write_text(
                "1\n00:00:10,000 --> 00:00:11,000\nlate subtitle\n",
                encoding="utf-8",
            )
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=24:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(base),
                ),
                check=True,
            )
            subprocess.run(("mkvmerge", "-q", "-o", str(polluted), str(base), str(subtitle)), check=True)
            inspected = MediaInspector().inspect(polluted)
            self.assertLess(inspected.duration, 3.0)
            self.assertAlmostEqual(last_packet_end(polluted, "video", 0) or 0, 2.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
