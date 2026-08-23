from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from dualmaker.avsync import AVTimelineDecision, VideoMatchSample
from dualmaker.errors import ProcessingError
from dualmaker.fpssync import (
    _adaptive_anchor_hypothesis,
    _audio_duration_speed_candidates,
    _comparison_deinterlace_prefix,
    _Hypothesis,
    analyze_fps_timing,
    evaluate_fps_pair,
    validate_fps_timeline,
)
from dualmaker.models import DualMakerConfig, FPSMatchSample, FrameRate, MediaAsset


def asset(name: str, duration: float, rate: FrameRate | None) -> MediaAsset:
    return MediaAsset(Path(name), duration, [], frame_rate=rate)


class FPSDecisionTests(unittest.TestCase):
    def test_interlaced_sources_use_timeline_preserving_deinterlacing(self) -> None:
        self.assertEqual(
            _comparison_deinterlace_prefix(True),
            "bwdif=mode=send_frame:parity=auto:deint=interlaced,",
        )
        self.assertEqual(_comparison_deinterlace_prefix(False), "")

    def test_common_audio_durations_nominate_pal_speed_not_container_rate(self) -> None:
        candidates, evidence = _audio_duration_speed_candidates(
            1388.971,
            1446.570,
            DualMakerConfig(),
        )

        self.assertIn(("audio_duration_24_25", 0.96), candidates)
        self.assertAlmostEqual(evidence["observed_ratio"], 0.96019, places=4)  # type: ignore[arg-type]
        self.assertNotIn(("fps_ratio", 0.8), candidates)

    def test_nonstandard_duration_difference_does_not_invent_a_speed(self) -> None:
        candidates, evidence = _audio_duration_speed_candidates(
            1200.0,
            1446.0,
            DualMakerConfig(),
        )

        self.assertEqual(candidates, [])
        self.assertIn("not close", str(evidence["reason"]))

    def test_equal_exact_rationals_use_supported_path(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 3600, FrameRate(24000, 1001)),
            asset("dual.mkv", 3600, FrameRate(24000, 1001)),
            DualMakerConfig(),
        )
        self.assertFalse(decision.required)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.proposed_speed_factor, 1.0)

    def test_25_to_23976_reports_large_nominal_drift_and_requires_approval(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 5400, FrameRate(24000, 1001)),
            asset("dual.mkv", 5180, FrameRate(25, 1)),
            DualMakerConfig(),
        )
        self.assertTrue(decision.required)
        self.assertTrue(decision.compatible)
        self.assertFalse(decision.approved)
        self.assertAlmostEqual(decision.proposed_speed_factor, 0.959040959, places=6)
        self.assertGreater(decision.expected_drift_seconds, 200)

    def test_configured_approval_allows_compatible_pair(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 1800, FrameRate(24, 1)),
            asset("dual.mkv", 1440, FrameRate(30, 1)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        self.assertTrue(decision.required)
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.proposed_speed_factor, 0.8)

    def test_telecine_tvrip_can_defer_to_acoustic_and_segment_validation(self) -> None:
        config = DualMakerConfig(allow_experimental_fps_sync=True)
        decision = evaluate_fps_pair(
            asset("master.mkv", 1000, FrameRate(24000, 1001)),
            asset("dual.mkv", 999, FrameRate(30000, 1001)),
            config,
        )
        weak_samples = [
            FPSMatchSample(position, 100 + position * 300, 100 + position * 300, 0.3)
            for position in (0.08, 0.5, 0.92)
        ]
        with (
            patch(
                "dualmaker.fpssync._analyze_hypothesis",
                side_effect=lambda *args, speed_factor, **kwargs: _Hypothesis(
                    speed_factor=speed_factor, samples=list(weak_samples)
                ),
            ),
            patch(
                "dualmaker.fpssync._extract_anchor_descriptors",
                return_value=(np.ones((3, 2), dtype=np.float32), np.ones(3, dtype=np.float32)),
            ),
            patch(
                "dualmaker.fpssync._adaptive_anchor_hypothesis",
                side_effect=lambda *args, speed_factor, **kwargs: _Hypothesis(
                    speed_factor=speed_factor, samples=[]
                ),
            ),
        ):
            result = analyze_fps_timing(
                Path("dual.mkv"),
                Path("master.mkv"),
                duration=999,
                source_duration=999,
                decision=decision,
                config=config,
                work_dir=Path("."),
                runner=object(),  # type: ignore[arg-type]
                allow_segmented_mapping=True,
                source_original_duration=999,
                target_original_duration=1000,
            )

        self.assertFalse(result.apply_speed_correction)
        self.assertTrue(result.validation["segmented_anchor_mapping"])
        self.assertTrue(result.validation["telecine_acoustic_preflight"]["enabled"])  # type: ignore[index]

    def test_selected_real_time_telecine_hypothesis_uses_segmented_acoustic_path(self) -> None:
        """One usable visual anchor must not re-enable the sparse-pair tempo gate."""

        config = DualMakerConfig(allow_experimental_fps_sync=True)
        decision = evaluate_fps_pair(
            asset("master.mkv", 1000, FrameRate(24000, 1001)),
            asset("dual.mkv", 999, FrameRate(30000, 1001)),
            config,
        )
        samples = [FPSMatchSample(0.5, 500, 500, 0.8)]

        def hypothesis(*_args: object, speed_factor: float, **_kwargs: object) -> _Hypothesis:
            return _Hypothesis(
                speed_factor=speed_factor,
                confidence=0.8,
                detected_speed_factor=speed_factor,
                residual_drift=0.0,
                reliable=abs(speed_factor - 1.0) < 0.000_001,
                samples=samples,
            )

        with patch("dualmaker.fpssync._analyze_hypothesis", side_effect=hypothesis):
            result = analyze_fps_timing(
                Path("dual.mkv"),
                Path("master.mkv"),
                duration=999,
                source_duration=999,
                decision=decision,
                config=config,
                work_dir=Path("."),
                runner=object(),  # type: ignore[arg-type]
                allow_segmented_mapping=True,
                source_original_duration=999,
                target_original_duration=1000,
            )

        self.assertFalse(result.apply_speed_correction)
        self.assertEqual(result.validation["selected_hypothesis"]["name"], "real_time")  # type: ignore[index]
        self.assertTrue(result.validation["telecine_acoustic_preflight"]["enabled"])  # type: ignore[index]

    def test_telecine_acoustic_fallback_defers_final_video_proof_to_tvrip_segments(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 1000, FrameRate(24000, 1001)),
            asset("dual.mkv", 999, FrameRate(30000, 1001)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        decision.validation.update(
            {
                "segmented_anchor_mapping": True,
                "telecine_acoustic_preflight": {"enabled": True},
                "spectral_tempo_probe": {"fallback_accepted": True},
                "spectral_post_sync_validation": {"fallback_accepted": True},
            }
        )
        result = validate_fps_timeline(
            decision,
            AVTimelineDecision(reliable=False, samples=[]),
            shift_points=[],
            manual_delay=0.0,
            timeline_adjustment_ms=0,
            maximum_drift=0.5,
            audio_sync_coverage=0.9,
            minimum_audio_coverage=0.85,
        )

        self.assertTrue(result["validated"])
        self.assertTrue(result["deferred_to_tvrip_segments"])
        self.assertEqual(result["mode"], "telecine-acoustic-tvrip-deferred")

    def test_speed_corrected_telecine_defers_to_local_segments_when_render_clock_matches(self) -> None:
        """The raw source-clock slope must be normalized by the rendered tempo."""

        decision = evaluate_fps_pair(
            asset("master.mkv", 1446, FrameRate(24000, 1001)),
            asset("dual.mkv", 1414, FrameRate(30000, 1001)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        decision.validation.update(
            {
                "segmented_anchor_mapping": True,
                "telecine_acoustic_preflight": {"enabled": True},
                "spectral_tempo_probe": {"reliable": True, "speed_factor": 0.978032028},
                "spectral_post_sync_validation": {
                    "reliable": True,
                    "speed_factor": 0.978032028,
                    "relative_speed_factor": 1.0,
                },
            }
        )
        result = validate_fps_timeline(
            decision,
            AVTimelineDecision(reliable=False, samples=[]),
            shift_points=[],
            manual_delay=0.0,
            timeline_adjustment_ms=0,
            maximum_drift=0.5,
            audio_sync_coverage=0.9,
            minimum_audio_coverage=0.85,
        )

        self.assertTrue(result["validated"])
        self.assertTrue(result["deferred_to_tvrip_segments"])
        self.assertAlmostEqual(float(result["post_relative_speed_factor"]), 1.0)

    def test_completed_map_can_upgrade_a_deferred_telecine_preflight(self) -> None:
        """A corrected render is valid even when its sparse preflight deferred."""

        decision = evaluate_fps_pair(
            asset("master.mkv", 1446, FrameRate(24000, 1001)),
            asset("dual.mkv", 1389, FrameRate(30000, 1001)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        decision.validation.update(
            {
                "segmented_anchor_mapping": True,
                "telecine_acoustic_preflight": {"enabled": True},
                "spectral_tempo_probe": {"fallback_accepted": True},
                "spectral_post_sync_validation": {
                    "reliable": True,
                    "relative_speed_factor": 1.0003,
                },
            }
        )

        result = validate_fps_timeline(
            decision,
            AVTimelineDecision(reliable=False, samples=[]),
            shift_points=[],
            manual_delay=0.0,
            timeline_adjustment_ms=0,
            maximum_drift=0.5,
            audio_sync_coverage=0.9,
            minimum_audio_coverage=0.85,
        )

        self.assertTrue(result["validated"])
        self.assertTrue(result["deferred_to_tvrip_segments"])

    def test_speed_corrected_telecine_with_post_residual_uses_local_segment_fallback(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 1446, FrameRate(24000, 1001)),
            asset("dual.mkv", 1414, FrameRate(30000, 1001)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        decision.validation.update(
            {
                "segmented_anchor_mapping": True,
                "telecine_acoustic_preflight": {"enabled": True},
                "spectral_tempo_probe": {"reliable": True, "speed_factor": 0.978},
                "spectral_post_sync_validation": {
                    "reliable": True,
                    "fallback_accepted": True,
                    "relative_speed_factor": 0.981,
                },
            }
        )
        result = validate_fps_timeline(
            decision,
            AVTimelineDecision(reliable=False, samples=[]),
            shift_points=[],
            manual_delay=0.0,
            timeline_adjustment_ms=0,
            maximum_drift=0.5,
            audio_sync_coverage=0.9,
            minimum_audio_coverage=0.85,
        )

        self.assertTrue(result["validated"])
        self.assertTrue(result["deferred_to_tvrip_segments"])

    def test_post_map_validation_checks_beginning_middle_and_end(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 100, FrameRate(24, 1)),
            asset("dual.mkv", 96, FrameRate(25, 1)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        timeline = AVTimelineDecision(
            reliable=True,
            applied=True,
            adjustment_ms=-100,
            samples=[
                VideoMatchSample(10, 9, 1.0, 0.8),
                VideoMatchSample(50, 49, 1.0, 0.8),
                VideoMatchSample(90, 89, 1.0, 0.8),
            ],
        )
        result = validate_fps_timeline(
            decision,
            timeline,
            shift_points=[(0.0, 0.0, 1.1)],
            manual_delay=0.0,
            timeline_adjustment_ms=-100,
            maximum_drift=0.05,
        )
        self.assertTrue(result["validated"])

    def test_post_map_validation_rejects_progressive_error(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 100, FrameRate(24, 1)),
            asset("dual.mkv", 96, FrameRate(25, 1)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        timeline = AVTimelineDecision(
            reliable=False,
            samples=[
                VideoMatchSample(10, 10, 0.0, 0.8),
                VideoMatchSample(50, 49, 1.0, 0.8),
                VideoMatchSample(90, 88, 2.0, 0.8),
            ],
        )
        with self.assertRaisesRegex(ProcessingError, "validation failed"):
            validate_fps_timeline(
                decision,
                timeline,
                shift_points=[(0.0, 0.0, 0.0)],
                manual_delay=0.0,
                timeline_adjustment_ms=0,
                maximum_drift=0.5,
            )

    def test_segmented_post_map_accepts_two_wide_audio_verified_video_matches(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 1446, FrameRate(24000, 1001)),
            asset("dual.mkv", 1389, FrameRate(30000, 1001)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        decision.validation["segmented_anchor_mapping"] = True
        decision.samples = [
            FPSMatchSample(position / 5, position * 200, position * 190, 0.45)
            for position in range(1, 6)
        ]
        timeline = AVTimelineDecision(
            reliable=False,
            samples=[
                VideoMatchSample(100, 99, 1.0, 0.8),
                VideoMatchSample(700, 698, 2.0, 0.8),
            ],
        )
        result = validate_fps_timeline(
            decision,
            timeline,
            shift_points=[(0.0, 0.0, 0.9), (0.0, 500.0, 1.9)],
            manual_delay=0.0,
            timeline_adjustment_ms=0,
            maximum_drift=0.5,
            segmented_min_samples=2,
            segmented_min_span_seconds=120,
            audio_sync_coverage=0.9,
        )
        self.assertTrue(result["validated"])
        self.assertEqual(result["mode"], "segmented-audio-map")
        self.assertEqual(result["minimum_required_samples"], 2)
        self.assertAlmostEqual(result["target_span_seconds"], 600)

    def test_strong_pre_and_post_spectral_proof_accepts_one_exact_video_confirmation(self) -> None:
        decision = evaluate_fps_pair(
            asset("master.mkv", 1446, FrameRate(24000, 1001)),
            asset("dual.mkv", 1389, FrameRate(30000, 1001)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        decision.validation.update(
            {
                "segmented_anchor_mapping": True,
                "spectral_tempo_probe": {"reliable": True, "inlier_pairs": 2558},
                "spectral_post_sync_validation": {
                    "reliable": True,
                    "speed_factor": 1.0003,
                    "inlier_pairs": 12,
                },
            }
        )
        decision.samples = [
            FPSMatchSample(position / 5, position * 200, position * 190, 0.45)
            for position in range(1, 6)
        ]
        timeline = AVTimelineDecision(
            reliable=False,
            samples=[VideoMatchSample(700, 698, 2.0, 0.8)],
        )

        result = validate_fps_timeline(
            decision,
            timeline,
            shift_points=[(0.0, 0.0, 2.0)],
            manual_delay=0.0,
            timeline_adjustment_ms=0,
            maximum_drift=0.5,
            spectral_min_samples=1,
            audio_sync_coverage=0.98,
        )

        self.assertTrue(result["validated"])
        self.assertTrue(result["spectrally_verified"])
        self.assertEqual(result["mode"], "segmented-spectral-audio-map")
        self.assertEqual(result["minimum_required_samples"], 1)

    def test_spectral_proof_does_not_require_unreliable_initial_visual_anchors(self) -> None:
        """Telecined remasters can have no useful pre-sync frame correspondence."""

        decision = evaluate_fps_pair(
            asset("master.mkv", 1446, FrameRate(24000, 1001)),
            asset("dual.mkv", 1389, FrameRate(30000, 1001)),
            DualMakerConfig(allow_experimental_fps_sync=True),
        )
        decision.validation.update(
            {
                "segmented_anchor_mapping": True,
                "spectral_tempo_probe": {"reliable": True, "inlier_pairs": 2558},
                "spectral_post_sync_validation": {
                    "reliable": True,
                    "speed_factor": 1.0003,
                    "inlier_pairs": 12,
                },
            }
        )
        # The local image comparison found nothing trustworthy before Milksync;
        # the independent spectral proof and one exact final video check do.
        decision.samples = []
        result = validate_fps_timeline(
            decision,
            AVTimelineDecision(
                reliable=False,
                samples=[VideoMatchSample(700, 698, 2.0, 0.8)],
            ),
            shift_points=[(0.0, 0.0, 2.0)],
            manual_delay=0.0,
            timeline_adjustment_ms=0,
            maximum_drift=0.5,
            spectral_min_samples=1,
            audio_sync_coverage=0.98,
        )

        self.assertTrue(result["validated"])
        self.assertTrue(result["spectrally_verified"])
        self.assertEqual(result["initial_content_anchor_count"], 0)

    def test_adaptive_anchors_accept_a_global_mapping(self) -> None:
        generator = np.random.default_rng(42)
        target = generator.normal(size=(600, 64)).astype(np.float32)
        target /= np.linalg.norm(target, axis=1, keepdims=True)
        source = generator.normal(size=(640, 64)).astype(np.float32)
        source /= np.linalg.norm(source, axis=1, keepdims=True)
        source[20:620] = target
        config = DualMakerConfig(
            fps_anchor_sample_count=15,
            fps_anchor_candidate_count=3,
            fps_anchor_window_seconds=6,
            fps_anchor_min_separation_seconds=20,
        )
        result = _adaptive_anchor_hypothesis(
            source,
            target,
            target_indices=list(range(30, 570, 38)),
            target_duration=600,
            speed_factor=1.0,
            config=config,
        )
        self.assertTrue(result.reliable)
        self.assertFalse(result.segmented)
        self.assertAlmostEqual(result.detected_speed_factor or 0, 1.0, places=3)
        self.assertGreaterEqual(result.accepted_anchor_count, 3)

    def test_adaptive_anchors_classify_inserted_material_as_segmented(self) -> None:
        generator = np.random.default_rng(7)
        target = generator.normal(size=(600, 64)).astype(np.float32)
        target /= np.linalg.norm(target, axis=1, keepdims=True)
        source = generator.normal(size=(760, 64)).astype(np.float32)
        source /= np.linalg.norm(source, axis=1, keepdims=True)
        source[20:200] = target[:180]
        source[280:460] = target[180:360]
        source[540:720] = target[360:540]
        config = DualMakerConfig(
            fps_anchor_sample_count=15,
            fps_anchor_candidate_count=3,
            fps_anchor_window_seconds=6,
            fps_anchor_min_separation_seconds=20,
            fps_anchor_global_coverage=0.55,
        )
        result = _adaptive_anchor_hypothesis(
            source,
            target,
            target_indices=list(range(30, 540, 36)),
            target_duration=600,
            speed_factor=1.0,
            config=config,
        )
        self.assertFalse(result.reliable)
        self.assertTrue(result.segmented)
        self.assertGreaterEqual(result.accepted_anchor_count, 3)


if __name__ == "__main__":
    unittest.main()
