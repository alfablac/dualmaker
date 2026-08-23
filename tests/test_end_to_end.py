from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from dualmaker.metadata import MediaInspector

TOOLS = ("ffmpeg", "ffprobe", "mediainfo", "mkvmerge", "mkvextract", "mkvpropedit")
DEPENDENCIES = ("annoy", "cv2", "librosa", "pymediainfo", "scipy", "skimage")


def slow_test_available() -> bool:
    if os.environ.get("DUALMAKER_RUN_SLOW") != "1":
        return False
    if not all(shutil.which(tool) for tool in TOOLS):
        return False
    try:
        for module in DEPENDENCIES:
            __import__(module)
    except ImportError:
        return False
    return True


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args: str | Path) -> None:
    subprocess.run(tuple(str(arg) for arg in args), check=True, capture_output=True)


def dominant_frequency(path: Path, audio_index: int, *, start: float = 4.0) -> float:
    completed = subprocess.run(
        (
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-map",
            f"0:a:{audio_index}",
            "-t",
            "0.5",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "-",
        ),
        check=True,
        capture_output=True,
    )
    samples = np.frombuffer(completed.stdout, dtype=np.int16).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.argmax(spectrum) * 8000 / len(samples))


@unittest.skipUnless(slow_test_available(), "set DUALMAKER_RUN_SLOW=1 with sync dependencies")
class EndToEndTests(unittest.TestCase):
    def test_explicit_pair_is_synchronized_and_sources_are_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_audio = root / "original.wav"
            dub_audio = root / "dub.wav"
            video_source = root / "video-source.mkv"
            normal = root / "Synthetic.Movie.2026.1080p.MA.WEB-DL-GROUP.mkv"
            dual = root / "Synthetic.Movie.2026.720p.AMZN.WEB-DL.DUAL-C76.mkv"
            normal_subtitle = root / "normal-en.srt"
            dual_subtitle = root / "dual-pt.srt"
            normal_subtitle.write_text(
                "1\n00:00:02,000 --> 00:00:03,000\nEnglish\n", encoding="utf-8"
            )
            dual_subtitle.write_text(
                "1\n00:00:02,000 --> 00:00:03,000\nPortuguês\n", encoding="utf-8"
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*(220+18*t)*t):s=48000:d=20",
                original_audio,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*(330+11*t)*t):s=48000:d=20",
                dub_audio,
            )
            # Both releases must contain the same pictures. Generating
            # testsrc2 independently at two sizes changes the pattern itself,
            # which is not equivalent to two encodes of one release.
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=96x54:rate=24:duration=22",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                video_source,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                video_source,
                "-itsoffset",
                "2",
                "-i",
                original_audio,
                "-i",
                normal_subtitle,
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:s",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-c:s",
                "srt",
                "-metadata:s:a:0",
                "language=eng",
                "-metadata:s:s:0",
                "language=eng",
                normal,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                video_source,
                "-i",
                dub_audio,
                "-i",
                original_audio,
                "-i",
                dual_subtitle,
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:a",
                "-map",
                "3:s",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-vf",
                "scale=64:36",
                "-c:a",
                "aac",
                "-c:s",
                "srt",
                "-metadata:s:a:0",
                "language=por",
                "-metadata:s:a:1",
                "language=eng",
                "-metadata:s:s:0",
                "language=por",
                dual,
            )
            before = {normal: digest(normal), dual: digest(dual)}
            report = root / "report.json"
            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "dualmaker",
                    "--dual",
                    str(dual),
                    "--normal",
                    str(normal),
                    "--no-trim-recap",
                    "--no-end-trim",
                    "--temp-dir",
                    str(root / "temp"),
                    "--report",
                    str(report),
                ),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            output = (
                root / "dualmaker-output" / "Synthetic.Movie.2026.1080p.MA.WEB-DL.DUAL-alfaHD.mkv"
            )
            self.assertTrue(output.is_file())
            self.assertTrue(report.is_file())
            report_data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(len(report_data["assets"]), 2)
            self.assertEqual(report_data["results"][0]["status"], "success")
            self.assertTrue(report_data["results"][0]["sync_points"])
            synchronization = report_data["results"][0]["validation"]["synchronization"]
            av_timeline = report_data["results"][0]["validation"]["av_timeline"]
            self.assertAlmostEqual(synchronization["observed_reference_pts_offset"], 2.0, delta=0.05)
            self.assertEqual(synchronization["container_delay_adjustment"], 0.0)
            self.assertAlmostEqual(report_data["results"][0]["sync_points"][0][2], 0.0, delta=0.05)
            self.assertEqual(len(synchronization["source_dub_starts"]), 1)
            self.assertEqual(synchronization["output_audio_mapping"], [[0, 0], [1, 0]])
            self.assertEqual(synchronization["stage_original_index"], 1)
            self.assertTrue(av_timeline["reliable"])
            self.assertFalse(av_timeline["applied"])
            self.assertAlmostEqual(av_timeline["video_delay"], 0.0, delta=0.15)
            self.assertEqual(av_timeline["adjustment_ms"], 0)
            self.assertEqual(
                synchronization["timeline_adjustment_ms"], av_timeline["adjustment_ms"]
            )
            self.assertEqual(before, {normal: digest(normal), dual: digest(dual)})
            inspected = MediaInspector().inspect(output)
            self.assertEqual(
                [track.language_ietf for track in inspected.audio_tracks], ["pt", "en"]
            )
            self.assertTrue(inspected.audio_tracks[0].default)
            self.assertFalse(inspected.audio_tracks[1].default)
            # Regression guard: milksync used to emit target audio before
            # synchronized source audio even when --output-audio-mapping asked
            # for the opposite order. Metadata then disguised the swap.
            self.assertGreater(
                dominant_frequency(output, 0),
                dominant_frequency(output, 1) + 40,
            )
            self.assertEqual(
                [track.language_ietf for track in inspected.subtitle_tracks], ["pt", "en"]
            )
            self.assertFalse(any(track.default for track in inspected.subtitle_tracks))
            self.assertEqual(inspected.video_tracks[0].properties.get("pixel_dimensions"), "96x54")

    def test_experimental_24_to_25_fps_speed_is_measured_applied_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_source = root / "video-source.mkv"
            original_audio = root / "original.wav"
            dub_audio = root / "dub.wav"
            normal = root / "Beta.Movie.2026.1080p.MA.WEB-DL-GROUP.mkv"
            dual = root / "Beta.Movie.2026.720p.AMZN.WEB-DL.DUAL-C76.mkv"
            output = root / "result.mkv"
            report = root / "report.json"
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=96x54:rate=24:duration=60",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "24",
                video_source,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*(220+3*t)*t):s=48000:d=60",
                original_audio,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*(330+2*t)*t):s=48000:d=60",
                dub_audio,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                video_source,
                "-i",
                original_audio,
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "copy",
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
                "-i",
                video_source,
                "-i",
                dub_audio,
                "-i",
                original_audio,
                "-filter_complex",
                (
                    "[0:v]setpts=0.96*PTS,fps=25[v];"
                    "[1:a]atempo=25/24[d];[2:a]atempo=25/24[o]"
                ),
                "-map",
                "[v]",
                "-map",
                "[d]",
                "-map",
                "[o]",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "25",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=por",
                "-metadata:s:a:1",
                "language=eng",
                dual,
            )
            before = {normal: digest(normal), dual: digest(dual)}
            rejected_report = root / "rejected-report.json"
            rejected = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "dualmaker",
                    "--dual",
                    str(dual),
                    "--normal",
                    str(normal),
                    "--output",
                    str(root / "must-not-exist.mkv"),
                    "--temp-dir",
                    str(root / "work-rejected"),
                    "--report",
                    str(rejected_report),
                    "--dry-run",
                    "--json",
                ),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
            rejected_payload = json.loads(rejected.stdout)
            self.assertIn("requires explicit approval", rejected_payload["skipped"][0])
            self.assertFalse((root / "must-not-exist.mkv").exists())
            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "dualmaker",
                    "--dual",
                    str(dual),
                    "--normal",
                    str(normal),
                    "--output",
                    str(output),
                    "--temp-dir",
                    str(root / "work"),
                    "--report",
                    str(report),
                    "--allow-experimental-fps-sync",
                    "--fps-search-radius-seconds",
                    "3",
                    "--fps-validation-position",
                    "0.1",
                    "--fps-validation-position",
                    "0.5",
                    "--fps-validation-position",
                    "0.9",
                    "--fps-min-match-confidence",
                    "0.2",
                    "--fps-speed-ratio-tolerance",
                    "0.005",
                    "--fps-max-drift-seconds",
                    "0.5",
                    "--no-trim-recap",
                    "--no-end-trim",
                ),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            data = json.loads(report.read_text(encoding="utf-8"))
            validation = data["results"][0]["validation"]
            fps = validation["experimental_fps"]
            self.assertTrue(fps["approved"])
            self.assertTrue(fps["apply_speed_correction"])
            self.assertAlmostEqual(fps["proposed_speed_factor"], 0.96, places=6)
            self.assertAlmostEqual(fps["detected_speed_factor"], 0.96, delta=0.005)
            self.assertGreater(fps["confidence"], 0.8)
            self.assertTrue(validation["experimental_fps_validation"]["validated"])
            self.assertEqual(len(validation["experimental_fps_validation"]["samples"]), 3)
            self.assertEqual(before, {normal: digest(normal), dual: digest(dual)})
            inspected = MediaInspector().inspect(output)
            self.assertEqual(inspected.frame_rate.rational, "24/1")  # type: ignore[union-attr]
            self.assertEqual([track.language_ietf for track in inspected.audio_tracks], ["pt", "en"])

    def test_tvrip_commercial_is_removed_once_and_post_cut_audio_remains_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "Show.S01E01.1080p.WEB-DL-MASTER.mkv"
            dub = root / "dub.wav"
            commercial = root / "commercial.mkv"
            tvrip = root / "Show.S01E01.HDTV.DUAL-TVRIP.mkv"
            output = root / "result.mkv"
            report = root / "report.json"
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=96x54:rate=24:duration=70",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*(180+2*t)*t)+0.3*sin(2*PI*(410+7*t)*t):s=48000:d=70",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "24",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                master,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*(260+3*t)*t)+0.2*sin(2*PI*(510+4*t)*t):s=48000:d=70",
                dub,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "smptebars=size=96x54:rate=24:duration=10",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:amplitude=0.1:s=48000:d=10",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "24",
                "-c:a",
                "aac",
                commercial,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                master,
                "-i",
                dub,
                "-i",
                commercial,
                "-filter_complex",
                (
                    "[0:v]split=2[mv0][mv1];"
                    "[mv0]trim=start=0:end=30,setpts=PTS-STARTPTS[v0];"
                    "[mv1]trim=start=30:end=70,setpts=PTS-STARTPTS[v1];"
                    "[0:a]asplit=2[me0][me1];"
                    "[me0]atrim=start=0:end=30,asetpts=PTS-STARTPTS[e0];"
                    "[me1]atrim=start=30:end=70,asetpts=PTS-STARTPTS[e1];"
                    "[1:a]asplit=2[md0][md1];"
                    "[md0]atrim=start=0:end=30,asetpts=PTS-STARTPTS[d0];"
                    "[md1]atrim=start=30:end=70,asetpts=PTS-STARTPTS[d1];"
                    "[2:v]setpts=PTS-STARTPTS[cv];"
                    "[2:a]asplit=2[ce][cd];"
                    "[ce]asetpts=PTS-STARTPTS[ce0];"
                    "[cd]asetpts=PTS-STARTPTS[cd0];"
                    "[v0][e0][d0][cv][ce0][cd0][v1][e1][d1]"
                    "concat=n=3:v=1:a=2[outv][outen][outpt]"
                ),
                "-map",
                "[outv]",
                "-map",
                "[outpt]",
                "-map",
                "[outen]",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "24",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=por",
                "-metadata:s:a:1",
                "language=eng",
                tvrip,
            )
            before = {master: digest(master), tvrip: digest(tvrip)}
            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "dualmaker",
                    "--tvrip",
                    str(tvrip),
                    "--normal",
                    str(master),
                    "--output",
                    str(output),
                    "--allow-tvrip-segment-sync",
                    "--tvrip-fallback",
                    "silence",
                    "--tvrip-max-segment-seconds",
                    "20",
                    "--tvrip-min-source-confidence",
                    "0.30",
                    "--tvrip-min-segment-confidence",
                    "0.30",
                    "--tvrip-min-coverage",
                    "0.80",
                    "--tvrip-max-residual-seconds",
                    "0.5",
                    "--tvrip-validation-search-seconds",
                    "2",
                    "--no-end-trim",
                    "--report",
                    str(report),
                ),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            data = json.loads(report.read_text(encoding="utf-8"))
            validation = data["results"][0]["validation"]
            tvrip_report = validation["experimental_tvrip"]
            self.assertEqual(tvrip_report["result"], "accepted")
            self.assertGreater(tvrip_report["coverage"], 0.98)
            self.assertGreaterEqual(tvrip_report["accepted_segments"], 4)
            source_only = sum(
                interval["end"] - interval["start"]
                for interval in tvrip_report["tvrip_only"]
            )
            self.assertAlmostEqual(source_only, 10.4, delta=0.8)
            self.assertEqual(
                "Portuguese (Brazil)",
                validation["audio_selection"]["dubs"][0]["track"]["title"],
            )
            # At 35 seconds the output must contain the original dub's 35-second
            # content, not its 45-second content. This catches double-removal of
            # the ten-second commercial at the piecewise cut.
            self.assertAlmostEqual(
                dominant_frequency(output, 0, start=35),
                dominant_frequency(dub, 0, start=35),
                delta=8,
            )
            self.assertEqual(before, {master: digest(master), tvrip: digest(tvrip)})


if __name__ == "__main__":
    unittest.main()
