from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dualmaker.metadata import MediaInspector
from dualmaker.models import ContentIdentity, DualMakerConfig, JobPlan, SidecarSubtitle
from dualmaker.mux import _trim_end, mux_output
from dualmaker.runner import CommandResult, ToolRunner
from dualmaker.sync import SyncResult

TOOLS = ("ffmpeg", "ffprobe", "mediainfo", "mkvmerge", "mkvextract")


def run(*args: str | Path) -> None:
    subprocess.run(tuple(str(arg) for arg in args), check=True, capture_output=True)


class WarningMuxRunner(ToolRunner):
    """Turn the completed final mux into mkvmerge's warning exit status."""

    warning_injected = False

    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = super().run(*args, **kwargs)
        if (
            Path(result.args[0]).name == "mkvmerge"
            and "-o" in result.args
            and ".partial-" in result.args[result.args.index("-o") + 1]
        ):
            self.warning_injected = True
            return CommandResult(result.args, 1, result.stdout, "simulated warning")
        return result


class EndTrimUnitTests(unittest.TestCase):
    def test_unsupported_end_trim_keeps_the_completed_mux(self) -> None:
        """FLAC cannot be split by mkvmerge, but that must not fail the job."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "completed.mkv"
            destination = root / "trimmed.mkv"
            source.touch()

            class UnsupportedSplitRunner:
                def run(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                    return CommandResult(("mkvmerge",), 2, "", "FLAC cannot be split")

            trimmed, reason = _trim_end(
                source,
                destination,
                120.0,
                UnsupportedSplitRunner(),  # type: ignore[arg-type]
            )

            self.assertIsNone(trimmed)
            self.assertIn("not supported", reason or "")
            self.assertTrue(source.is_file())
            self.assertFalse(destination.exists())


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "media tools are required")
class MuxIntegrationTests(unittest.TestCase):
    def test_mux_order_flags_and_attachment_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pt_forced = root / "pt-forced.srt"
            en_sub = root / "en.srt"
            pt_regular = root / "pt-regular.srt"
            spanish_sidecar = root / "spanish-sidecar.srt"
            attachment = root / "font.txt"
            chapters = root / "chapters.xml"
            pt_forced.write_text("1\n00:00:00,100 --> 00:00:00,800\nForçado\n", encoding="utf-8")
            en_sub.write_text("1\n00:00:00,100 --> 00:00:00,800\nEnglish\n", encoding="utf-8")
            pt_regular.write_text("1\n00:00:00,100 --> 00:00:00,800\nPortuguês\n", encoding="utf-8")
            spanish_sidecar.write_text(
                "1\n00:00:00,100 --> 00:00:00,800\nEspañol\n", encoding="utf-8"
            )
            attachment.write_text("same attachment", encoding="utf-8")
            chapters.write_text(
                """<?xml version="1.0"?>
<Chapters><EditionEntry><ChapterAtom><ChapterTimeStart>00:00:00.000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Beginning</ChapterString><ChapterLanguage>eng</ChapterLanguage>
</ChapterDisplay></ChapterAtom></EditionEntry></Chapters>
""",
                encoding="utf-8",
            )

            normal_base = root / "normal-base.mkv"
            normal = root / "Movie.2026.1080p.MA.WEB-DL-GROUP.mkv"
            run(
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
                "-i",
                pt_forced,
                "-i",
                en_sub,
                "-t",
                "2",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:s",
                "-map",
                "3:s",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-c:s",
                "srt",
                "-metadata:s:a:0",
                "language=eng",
                "-metadata:s:s:0",
                "language=por",
                "-metadata:s:s:0",
                "title=Forced",
                "-disposition:s:0",
                "forced",
                "-metadata:s:s:1",
                "language=eng",
                normal_base,
            )
            run(
                "mkvmerge",
                "-q",
                "-o",
                normal,
                normal_base,
                "--chapters",
                chapters,
                "--attachment-mime-type",
                "text/plain",
                "--attachment-name",
                "font.txt",
                "--attach-file",
                attachment,
            )

            dual_base = root / "dual-base.mkv"
            dual = root / "Movie.2026.720p.AMZN.WEB-DL.DUAL-C76.mkv"
            run(
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
                "-i",
                pt_regular,
                "-i",
                en_sub,
                "-t",
                "2",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:a",
                "-map",
                "3:s",
                "-map",
                "4:s",
                "-c:v",
                "libx264",
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
                "-metadata:s:s:1",
                "language=eng",
                dual_base,
            )
            run(
                "mkvmerge",
                "-q",
                "-o",
                dual,
                dual_base,
                "--attachment-mime-type",
                "text/plain",
                "--attachment-name",
                "font-copy.txt",
                "--attach-file",
                attachment,
            )

            stage = root / "synchronized.mkv"
            run(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                normal,
                "-i",
                dual,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "0:a:0",
                "-map",
                "1:s:0",
                "-map",
                "1:s:1",
                "-c",
                "copy",
                stage,
            )

            warning_runner = WarningMuxRunner()
            inspector = MediaInspector(warning_runner)
            normal_asset = inspector.inspect(normal)
            dual_asset = inspector.inspect(dual)
            plan = JobPlan(
                normal=normal_asset,
                dual=dual_asset,
                identity=ContentIdentity("movie", "movie", year=2026),
                output=root / "dualmaker-output" / "Movie.2026.1080p.MA.WEB-DL.DUAL-alfaHD.mkv",
                normal_original=normal_asset.audio_tracks[0],
                dual_original=dual_asset.audio_tracks[1],
                dub_tracks=[dual_asset.audio_tracks[0]],
                normal_subtitles=normal_asset.subtitle_tracks,
                dual_subtitles=dual_asset.subtitle_tracks,
            )
            sync = SyncResult(
                path=stage,
                report_path=root / "sync.json",
                text_subtitles=dual_asset.subtitle_tracks,
                sidecar_subtitles=[SidecarSubtitle(spanish_sidecar, "dual", "es")],
                shift_points=[(0.0, 0.0, 0.0)],
                output_audio_mapping=[(0, 0), (1, 0)],
                stage_original_index=1,
            )
            phases: list[str] = []
            output, subtitle_count, validation = mux_output(
                plan,
                sync,
                normal_path=normal,
                dual_path=dual,
                work_dir=root,
                config=DualMakerConfig(end_trim=False, trim_recap=False),
                runner=warning_runner,
                inspector=inspector,
                on_phase=phases.append,
            )
            result = inspector.inspect(output)
            self.assertTrue(validation["ok"])
            self.assertTrue(warning_runner.warning_injected)
            self.assertEqual(subtitle_count, 4)
            self.assertEqual([item.language_ietf for item in result.audio_tracks], ["pt", "en"])
            self.assertTrue(result.audio_tracks[0].default)
            self.assertFalse(result.audio_tracks[1].default)
            self.assertEqual(
                [item.language_ietf for item in result.subtitle_tracks], ["pt", "pt", "en", "es"]
            )
            self.assertTrue(result.subtitle_tracks[0].forced)
            self.assertTrue(result.subtitle_tracks[0].default)
            self.assertEqual(len(result.attachments), 1)
            self.assertEqual(len(result.chapters), 1)
            self.assertEqual(
                phases,
                [
                    "Inspecting synchronized tracks",
                    "Deduplicating subtitles",
                    "Collecting attachments",
                    "Writing final MKV",
                    "Validating final MKV",
                    "Publishing output",
                ],
            )

    def test_validated_end_trim_uses_shortest_selected_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_audio = root / "original.wav"
            dub_audio = root / "dub.wav"
            normal = root / "Short.Movie.2026.1080p.MA.WEB-DL-GROUP.mkv"
            dual = root / "Short.Movie.2026.720p.AMZN.WEB-DL.DUAL-C76.mkv"
            stage = root / "stage.mkv"
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                original_audio,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=660:sample_rate=48000:duration=2",
                dub_audio,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=64x64:rate=24:duration=3",
                "-i",
                original_audio,
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-force_key_frames",
                "2",
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
                "color=size=48x48:rate=24:duration=3",
                "-i",
                dub_audio,
                "-i",
                original_audio,
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
                dual,
            )
            run(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                normal,
                "-i",
                dub_audio,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-metadata:s:a:0",
                "language=por",
                stage,
            )
            inspector = MediaInspector()
            normal_asset = inspector.inspect(normal)
            dual_asset = inspector.inspect(dual)
            plan = JobPlan(
                normal=normal_asset,
                dual=dual_asset,
                identity=ContentIdentity("movie", "short movie", year=2026),
                output=root / "output" / "Short.Movie.2026.DUAL-alfaHD.mkv",
                normal_original=normal_asset.audio_tracks[0],
                dual_original=dual_asset.audio_tracks[1],
                dub_tracks=[dual_asset.audio_tracks[0]],
                normal_subtitles=[],
                dual_subtitles=[],
            )
            output, _, _ = mux_output(
                plan,
                SyncResult(
                    stage,
                    root / "sync.json",
                    shift_points=[(0.0, 0.0, 0.0)],
                    output_audio_mapping=[(0, 0), (1, 0)],
                    stage_original_index=1,
                ),
                normal_path=normal,
                dual_path=dual,
                work_dir=root,
                config=DualMakerConfig(end_trim=True, end_tolerance_ms=500, trim_recap=False),
                inspector=inspector,
            )
            result = inspector.inspect(output)
            self.assertGreater(result.duration, 1.8)
            self.assertLess(result.duration, 2.4)


if __name__ == "__main__":
    unittest.main()
