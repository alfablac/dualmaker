from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from dualmaker.models import (
    AudioTrackSelection,
    ContentIdentity,
    FPSDecision,
    FrameRate,
    MediaAsset,
    PairCandidate,
    SidecarSubtitleCandidate,
    Track,
    TVRipSegment,
    TVRipSyncReport,
)
from dualmaker.tui import (
    AudioTrackPickerApp,
    ExperimentalFPSApp,
    LanguagePickerApp,
    PairPickerApp,
    SidecarLanguagePickerApp,
    TVRipSegmentReviewApp,
)


def _candidate(title: str = "minions and monsters") -> PairCandidate:
    identity = ContentIdentity("movie", title, 2026)
    normal = MediaAsset(Path("/media/normal.mkv"), 100.0, [], identity=identity)
    dual = MediaAsset(Path("/media/dual.mkv"), 100.0, [], identity=identity)
    return PairCandidate(normal, dual, identity, 0.99, ("eng", "jpn"), ["test"])


class TextualWorkflowTests(unittest.TestCase):
    def test_pair_review_back_and_confirm(self) -> None:
        async def exercise() -> None:
            app = PairPickerApp([_candidate()], {0})
            async with app.run_test(size=(120, 40)) as pilot:
                details = str(app.query_one("#pair-details").render())
                self.assertIn("/media/normal.mkv", details)
                self.assertIn("/media/dual.mkv", details)
                await pilot.click("#review")
                await pilot.pause()
                self.assertTrue(app.query_one("#review-pane").display)
                await pilot.click("#back")
                await pilot.pause()
                self.assertTrue(app.query_one("#body").display)
                await pilot.click("#review")
                await pilot.pause()
                await pilot.click("#confirm")
                await pilot.pause()
            self.assertEqual(app.return_value, [0])

        asyncio.run(exercise())

    def test_pair_cancel_returns_no_selection(self) -> None:
        async def exercise() -> None:
            app = PairPickerApp([_candidate()], {0})
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.click("#cancel")
                await pilot.pause()
            self.assertIsNone(app.return_value)

        asyncio.run(exercise())

    def test_pair_picker_selects_and_deselects_all(self) -> None:
        async def exercise() -> None:
            app = PairPickerApp([_candidate(), _candidate("another movie")], {0})
            async with app.run_test(size=(120, 40)) as pilot:
                pairs = app.query_one("#pairs")
                self.assertEqual(set(pairs.selected), {0})
                await pilot.click("#deselect-all")
                await pilot.pause()
                self.assertEqual(set(pairs.selected), set())
                await pilot.click("#select-all")
                await pilot.pause()
                self.assertEqual(set(pairs.selected), {0, 1})

        asyncio.run(exercise())

    def test_sidecar_language_uses_radio_controls(self) -> None:
        async def exercise() -> None:
            sidecar = SidecarSubtitleCandidate(Path("/media/episode.DUAL.srt"), "dual")
            app = SidecarLanguagePickerApp(sidecar)
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.click("#sidecar-confirm")
                await pilot.pause()
            self.assertEqual(app.return_value, "pt-BR")

        asyncio.run(exercise())

    def test_language_choice_uses_radio_controls(self) -> None:
        async def exercise() -> None:
            app = LanguagePickerApp(_candidate())
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.click("#language-confirm")
                await pilot.pause()
            self.assertEqual(app.return_value, "eng")

        asyncio.run(exercise())

    def test_audio_choice_uses_metadata_radio_controls(self) -> None:
        async def exercise() -> None:
            choices = [
                AudioTrackSelection(
                    "master",
                    Track(1, "audio", 0, codec_id="A_EAC3", channels=6),
                    10.0,
                ),
                AudioTrackSelection(
                    "dual",
                    Track(2, "audio", 1, codec_id="A_TRUEHD", channels=8),
                    10.0,
                ),
            ]
            app = AudioTrackPickerApp("original", choices)
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.click("#audio-confirm")
                await pilot.pause()
            self.assertEqual(app.return_value, 0)

        asyncio.run(exercise())

    def test_experimental_fps_requires_confirmation(self) -> None:
        async def exercise() -> None:
            decision = FPSDecision(
                required=True,
                compatible=True,
                master_rate=FrameRate(24000, 1001),
                dual_rate=FrameRate(25, 1),
                expected_drift_seconds=220.0,
            )
            app = ExperimentalFPSApp(_candidate(), decision)
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.click("#fps-confirm")
                await pilot.pause()
            self.assertTrue(app.return_value)

        asyncio.run(exercise())

    def test_tvrip_review_exposes_segment_checklist_and_fallback_controls(self) -> None:
        async def exercise() -> None:
            report = TVRipSyncReport(
                coverage=0.95,
                source_match_confidence=0.9,
                accepted_segments=1,
                segments=[
                    TVRipSegment(
                        1,
                        10,
                        100,
                        0,
                        90,
                        -10,
                        confidence=0.9,
                        residual_seconds=0.02,
                        status="accepted",
                    )
                ],
            )
            app = TVRipSegmentReviewApp(report, "ask")
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.click("#tvrip-confirm")
                await pilot.pause()
            self.assertEqual(app.return_value, ({1}, "original"))

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
