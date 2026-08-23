from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dualmaker.sync.milksync import (
    Video,
    extract_and_sync_audio,
    turn_audio_shift_points_to_audio_segments,
)

TOOLS = ("ffmpeg", "ffprobe", "mediainfo")


class SyncBucketUnitTests(unittest.TestCase):
    def test_crossed_anchor_cannot_create_a_reverse_audio_bucket(self) -> None:
        points = [
            (0.0, 0.0, 0.0),
            (20.0, 20.0, 0.0),
            # A local DTW false match: source time cannot go backwards on a
            # rendered playback timeline.
            (25.0, 18.0, 7.0),
            (40.0, 40.0, 0.0),
        ]

        buckets, _deleted = turn_audio_shift_points_to_audio_segments(points)

        self.assertTrue(all(end > start for start, end, _delta in buckets))
        self.assertEqual(buckets[0], (0.0, 20.0, 0.0))
        self.assertEqual(buckets[1], (20.0, 40.0, 0.0))


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "media tools are required")
class SyncIntegrationTests(unittest.TestCase):
    def test_negative_delta_removes_audio_instead_of_only_logging_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
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
                    "ac3",
                    source,
                ),
                check=True,
            )
            output = extract_and_sync_audio(
                Video(source),
                0,
                2.0,
                [(0.0, 0.0, -0.5)],
                [(0.0, 1_000_000.0, -0.5)],
                [],
                root / "Don't-synchronize.unknown",
            )
            probe = subprocess.run(
                (
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_name",
                    "-of",
                    "json",
                    output,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            self.assertGreater(duration, 1.4)
            self.assertLess(duration, 1.6)

    def test_negative_input_start_pts_does_not_trim_the_synchronized_audio(self) -> None:
        """Codec priming PTS is metadata, not a -400 ms content correction."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
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
                    "ac3",
                    source,
                ),
                check=True,
            )
            video = Video(source)
            # Simulate the stream-level start timestamp reported by a release
            # with negative codec priming PTS while keeping deterministic test
            # media. The renderer must still retain the full audio timeline.
            video.probe["streams"][1]["start_time"] = "-0.400000"
            output = extract_and_sync_audio(
                video,
                0,
                2.0,
                [(0.0, 0.0, 0.0)],
                [(0.0, 1_000_000.0, 0.0)],
                [],
                root / "negative-start-pts.unknown",
            )
            probe = subprocess.run(
                (
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    output,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertAlmostEqual(float(json.loads(probe.stdout)["format"]["duration"]), 2.0, delta=0.08)

    def test_framerate_alignment_time_stretches_retained_source_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=25:duration=2",
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
                    "ac3",
                    source,
                ),
                check=True,
            )
            output = extract_and_sync_audio(
                Video(source),
                0,
                2.5,
                [(0.0, 0.0, 0.0)],
                [(0.0, 2.5, 0.0)],
                [],
                root / "stretched.unknown",
                framerate_align=(1.0, 0.8),
            )
            probe = subprocess.run(
                (
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_name",
                    "-of",
                    "json",
                    output,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            metadata = json.loads(probe.stdout)
            duration = float(metadata["format"]["duration"])
            self.assertAlmostEqual(duration, 2.5, delta=0.08)
            self.assertEqual(metadata["streams"][0]["codec_name"], "flac")

    def test_framerate_piecewise_aac_uses_one_delay_free_lossless_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-aac.mkv"
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=24:duration=4",
                    "-f",
                    "lavfi",
                    "-i",
                    "aevalsrc=sin(2*PI*(300+50*t)*t):s=48000:d=4",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    source,
                ),
                check=True,
            )
            buckets = [(index / 2, (index + 1) / 2, 0.2) for index in range(8)]
            output = extract_and_sync_audio(
                Video(source),
                0,
                4.0,
                [(0.0, 0.0, 0.0)],
                buckets,
                [],
                root / "piecewise-fps.unknown",
                framerate_align=(1.0, 1.0),
            )
            probe = subprocess.run(
                (
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_name",
                    "-of",
                    "json",
                    output,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            metadata = json.loads(probe.stdout)
            self.assertEqual(metadata["streams"][0]["codec_name"], "flac")
            self.assertAlmostEqual(float(metadata["format"]["duration"]), 4.2, delta=0.03)
            subprocess.run(
                ("ffmpeg", "-v", "error", "-i", output, "-f", "null", "-"),
                check=True,
            )

    def test_piecewise_aac_with_inserted_silence_uses_one_decodable_flac_timeline(self) -> None:
        """Do not stream-concat HE-AAC-like program packets with AAC silence.

        AAC streams with different AudioSpecificConfig values can both be
        labelled ``aac`` by ffprobe. Normalizing every segmented timeline
        prevents a corrupt output that only surfaces when a later policy pass
        tries to decode it.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-aac.mkv"
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=24:duration=4",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=4",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    source,
                ),
                check=True,
            )
            output = extract_and_sync_audio(
                Video(source),
                0,
                4.0,
                [(0.0, 0.0, 0.0)],
                [(0.0, 1.0, 0.0), (1.0, 3.0, 1.0)],
                [],
                root / "segmented-aac.unknown",
            )
            probe = subprocess.run(
                (
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_name",
                    "-of",
                    "json",
                    output,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            metadata = json.loads(probe.stdout)
            self.assertEqual(metadata["streams"][0]["codec_name"], "flac")
            self.assertAlmostEqual(float(metadata["format"]["duration"]), 4.0, delta=0.04)
            subprocess.run(
                ("ffmpeg", "-v", "error", "-i", output, "-f", "null", "-"),
                check=True,
            )

    def test_mid_file_source_gap_is_not_removed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-with-commercial.mkv"
            subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=24:duration=6",
                    "-f",
                    "lavfi",
                    "-i",
                    "aevalsrc=sin(2*PI*(300+20*t)*t):s=48000:d=6",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "ac3",
                    source,
                ),
                check=True,
            )
            output = extract_and_sync_audio(
                Video(source),
                0,
                4.0,
                [(0.0, 0.0, 0.0), (2.0, 4.0, -2.0)],
                [(0.0, 2.0, 0.0), (4.0, 6.0, -2.0)],
                [(2.0, 4.0)],
                root / "piecewise.unknown",
            )
            probe = subprocess.run(
                (
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    output,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            self.assertAlmostEqual(duration, 4.0, delta=0.08)


if __name__ == "__main__":
    unittest.main()
