from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dualmaker.errors import ExperimentalTVRipRequiredError, TVRipValidationError
from dualmaker.matching import require_explicit_tvrip_pair
from dualmaker.models import (
    ContentIdentity,
    DualMakerConfig,
    FrameRate,
    JobPlan,
    MediaAsset,
    Track,
    TVRipInterval,
    TVRipSegment,
    TVRipSyncReport,
)
from dualmaker.pipeline import _effective_tvrip_policy
from dualmaker.planning import create_job_plan
from dualmaker.sync.adapter import SyncResult
from dualmaker.tvrip import (
    _partition,
    _recover_mapped_master_gaps,
    _validate_segment,
    approve_dub_gap_report,
    approve_tvrip_report,
    build_dub_gap_report,
    build_tvrip_sync_report,
    detected_master_only_intervals,
)


def _asset(path: Path, duration: float, *, portuguese: bool) -> MediaAsset:
    identity = ContentIdentity("episode", "show", season=1, episodes=(1,))
    tracks = [
        Track(0, "video", 0),
        Track(1, "audio", 0, language="en", language_ietf="en", duration=duration),
    ]
    if portuguese:
        tracks.insert(
            1,
            Track(2, "audio", 0, language="pt", language_ietf="pt-BR", duration=duration),
        )
        tracks[-1].type_index = 1
    return MediaAsset(
        path,
        duration,
        tracks,
        identity=identity,
        frame_rate=FrameRate(24, 1),
    )


def _plan(root: Path) -> JobPlan:
    master = _asset(root / "Show.S01E01.WEB-DL-GROUP.mkv", 1140, portuguese=False)
    tvrip = _asset(root / "Show.S01E01.HDTV.DUAL-GROUP.mkv", 1200, portuguese=True)
    candidate = require_explicit_tvrip_pair(master, tvrip)
    config = DualMakerConfig(
        path=root,
        output=root / "out.mkv",
        allow_tvrip_segment_sync=True,
        tvrip_fallback="silence",
    )
    return create_job_plan(candidate, config)


class TVRipPlanningTests(unittest.TestCase):
    def test_explicit_tvrip_requires_opt_in_when_unattended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _asset(root / "Show.S01E01.WEB-DL-GROUP.mkv", 1140, portuguese=False)
            tvrip = _asset(root / "Show.S01E01.HDTV.DUAL-GROUP.mkv", 1200, portuguese=True)
            candidate = require_explicit_tvrip_pair(master, tvrip)
            self.assertEqual(candidate.source_kind, "tvrip")
            with self.assertRaisesRegex(
                ExperimentalTVRipRequiredError, "allow-tvrip-segment-sync"
            ):
                create_job_plan(candidate, DualMakerConfig(path=root))

    def test_portuguese_only_tvrip_is_rejected_instead_of_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _asset(root / "Show.S01E01.WEB-DL-GROUP.mkv", 100, portuguese=False)
            tvrip = MediaAsset(
                root / "Show.S01E01.HDTV-GROUP.mkv",
                100,
                [
                    Track(0, "video", 0),
                    Track(1, "audio", 0, language="pt", language_ietf="pt-BR"),
                ],
                identity=master.identity,
            )
            with self.assertRaisesRegex(Exception, "Portuguese-only"):
                require_explicit_tvrip_pair(master, tvrip)


class TVRipSegmentReportTests(unittest.TestCase):
    def test_segment_validation_ignores_one_confident_but_timing_wrong_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = TVRipSegment(1, 0, 100, 0, 100, 0)
            config = DualMakerConfig(
                path=root,
                tvrip_min_segment_confidence=0.25,
                tvrip_max_residual_seconds=0.4,
            )
            # At 8 fps and a two-second search radius, frame 16 is the
            # audio-predicted location.  The middle result is visually stronger
            # but two seconds away, as can happen with repeated broadcast shots.
            with (
                patch("dualmaker.tvrip._extract_frames", return_value=[b"frame"]),
                patch(
                    "dualmaker.tvrip._match_window",
                    side_effect=[(16, 0.30), (0, 0.99), (16, 0.40)],
                ),
            ):
                _validate_segment(
                    segment,
                    source_path=root / "source.mkv",
                    master_path=root / "master.mkv",
                    source_time_scale=1.0,
                    timeline_adjustment=0.0,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                    minimum_confidence=0.25,
                )
            self.assertEqual(segment.status, "accepted")
            self.assertAlmostEqual(segment.confidence, 0.35)
            self.assertAlmostEqual(segment.residual_seconds, 0.0)

    def test_verified_spectral_timing_uses_its_separate_source_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            report = TVRipSyncReport(
                approved=True,
                source_analysis={"spectrally_verified_timing": True},
                segments=[
                    TVRipSegment(
                        1,
                        0,
                        1140,
                        0,
                        1140,
                        0,
                        confidence=0.30,
                        status="accepted",
                    )
                ],
                fallback="original",
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_min_source_match_confidence=0.55,
                tvrip_spectral_min_source_match_confidence=0.25,
            )
            approved = approve_tvrip_report(report, plan, config)
            self.assertEqual(approved.result, "accepted")
            self.assertAlmostEqual(approved.source_match_confidence, 0.30)

    def test_explicit_telecine_acoustic_map_accepts_visual_remaster_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.fps.validation.update(
                {
                    "telecine_acoustic_preflight": {"enabled": True},
                    "spectral_tempo_probe": {"fallback_accepted": True},
                    "spectral_post_sync_validation": {"fallback_accepted": True},
                }
            )
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                shift_points=[(0, 0, 0), (400, 400, 0), (800, 800, 0)],
                sync_buckets=[(0, 400, 0), (400, 800, 0), (800, 1_000_000, 0)],
                sync_coverage=0.99,
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="original",
                tvrip_min_coverage=0.85,
                tvrip_spectral_min_segment_confidence=0.25,
                tvrip_spectral_min_source_match_confidence=0.25,
            )
            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment"),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            self.assertTrue(report.source_analysis["telecine_acoustic_map_validation"]["enabled"])  # type: ignore[index]
            self.assertTrue(all(segment.status == "accepted" for segment in report.segments))
            self.assertGreaterEqual(report.coverage, 0.99)
            self.assertEqual(approve_tvrip_report(report, plan, config).result, "accepted")

    def test_telecine_local_acoustic_guard_replaces_unmatched_middle_scene(self) -> None:
        """A good global map must not bridge an HDTV-only scene between anchors."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            plan.fps.validation.update(
                {
                    "telecine_acoustic_preflight": {"enabled": True},
                    "spectral_tempo_probe": {"fallback_accepted": True},
                    "spectral_post_sync_validation": {"fallback_accepted": True},
                }
            )
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                shift_points=[(0, 0, 0), (400, 400, 0), (800, 800, 0)],
                sync_buckets=[(0, 300, 0), (300, 600, 0), (600, 1_000_000, 0)],
                sync_coverage=0.99,
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=False,
                tvrip_fallback="original",
                tvrip_min_coverage=0.10,
                tvrip_spectral_min_segment_confidence=0.25,
                tvrip_spectral_min_source_match_confidence=0.10,
                tvrip_acoustic_segment_min_similarity=0.50,
                # Use a large gap here so this focused test exercises the
                # fixed edge + broad probes without intermediate probes.
                tvrip_acoustic_segment_max_gap_seconds=1_000,
                tvrip_terminal_tail_validation=False,
            )
            # The first and last mapped buckets match. The second has no
            # common-original counterpart even though it lies between anchors.
            similarities = [0.90] * 5 + [0.10] * 5 + [0.90] * 10
            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment"),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch("dualmaker.tvrip.envelope_similarity", side_effect=similarities),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            rejected = next(
                segment for segment in report.segments if segment.source_start == 300
            )
            self.assertEqual(rejected.status, "rejected")
            self.assertIn("local common-original", rejected.operation)
            self.assertTrue(
                any(
                    interval.start == 300 and interval.end == 600
                    for interval in report.master_only
                )
            )
            evidence = report.source_analysis["acoustic_segment_validation"]
            self.assertEqual(evidence["action"], "replaced-with-fallback")  # type: ignore[index]
            self.assertEqual(evidence["rejected_segments"], 1)  # type: ignore[index]
            self.assertEqual(approve_tvrip_report(report, plan, config).result, "accepted")

    def test_authorized_telecine_map_keeps_dub_when_local_probe_is_weak(self) -> None:
        """A mix-only local mismatch must not create short English takeovers."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            plan.fps.validation.update(
                {
                    "telecine_acoustic_preflight": {"enabled": True},
                    "spectral_tempo_probe": {"fallback_accepted": True},
                    "spectral_post_sync_validation": {"fallback_accepted": True},
                }
            )
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                shift_points=[(0, 0, 0), (100, 100, 0), (200, 200, 0)],
                sync_buckets=[(0, 1_000_000, 0)],
                sync_coverage=0.99,
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                # This is the default authorized behaviour: local probes are
                # report diagnostics, not permission to replace a mapped dub.
                tvrip_continue_on_validation_warnings=True,
                tvrip_fallback="original",
                tvrip_min_coverage=0.10,
                tvrip_spectral_min_segment_confidence=0.25,
                tvrip_spectral_min_source_match_confidence=0.10,
                tvrip_acoustic_segment_min_similarity=0.50,
                tvrip_acoustic_segment_max_gap_seconds=1_000,
                tvrip_terminal_tail_validation=False,
            )
            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment") as validate_segment,
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch(
                    "dualmaker.tvrip.envelope_similarity",
                    side_effect=[0.90] * 5 + [0.10] + [0.90] * 14,
                ),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            validate_segment.assert_not_called()
            self.assertTrue(all(segment.status == "accepted" for segment in report.segments))
            self.assertFalse(report.master_only)
            acoustic = report.source_analysis["acoustic_segment_validation"]
            self.assertEqual(acoustic["action"], "retained-with-warnings")  # type: ignore[index]
            self.assertEqual(acoustic["suspected_segments"], 1)  # type: ignore[index]
            video = report.source_analysis["video_segment_validation"]
            self.assertEqual(video["action"], "skipped")  # type: ignore[index]

    def test_telecine_local_guard_replaces_only_the_failed_probe_with_padding(self) -> None:
        """One bad local scene must not leak, nor discard a whole good bucket."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            plan.fps.validation.update(
                {
                    "telecine_acoustic_preflight": {"enabled": True},
                    "spectral_tempo_probe": {"fallback_accepted": True},
                    "spectral_post_sync_validation": {"fallback_accepted": True},
                }
            )
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                shift_points=[(0, 0, 0), (100, 100, 0), (200, 200, 0)],
                sync_buckets=[(0, 300, 0)],
                sync_coverage=0.99,
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=False,
                tvrip_fallback="original",
                tvrip_min_coverage=0.10,
                tvrip_spectral_min_segment_confidence=0.25,
                tvrip_spectral_min_source_match_confidence=0.10,
                tvrip_acoustic_segment_min_similarity=0.50,
                tvrip_acoustic_segment_max_gap_seconds=1_000,
                tvrip_acoustic_segment_rejection_padding_seconds=5,
                tvrip_terminal_tail_validation=False,
            )
            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment"),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch(
                    "dualmaker.tvrip.envelope_similarity",
                    side_effect=[0.90, 0.90, 0.10, 0.90, 0.90],
                ),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            rejected = [segment for segment in report.segments if segment.status == "rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertAlmostEqual(rejected[0].master_start, 142.5)
            self.assertAlmostEqual(rejected[0].master_end, 157.5)
            self.assertEqual(
                report.source_analysis["acoustic_segment_validation"]["action"],  # type: ignore[index]
                "partially-replaced-with-fallback",
            )

    def test_telecine_local_guard_probes_the_bucket_edges(self) -> None:
        """A release-only scene immediately after an anchor cannot leak past it."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            plan.fps.validation.update(
                {
                    "telecine_acoustic_preflight": {"enabled": True},
                    "spectral_tempo_probe": {"fallback_accepted": True},
                    "spectral_post_sync_validation": {"fallback_accepted": True},
                }
            )
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                shift_points=[(0, 0, 0), (100, 100, 0), (200, 200, 0)],
                sync_buckets=[(0, 300, 0)],
                sync_coverage=0.99,
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=False,
                tvrip_fallback="original",
                tvrip_min_coverage=0.10,
                tvrip_spectral_min_segment_confidence=0.25,
                tvrip_spectral_min_source_match_confidence=0.10,
                tvrip_acoustic_segment_min_similarity=0.50,
                tvrip_acoustic_segment_max_gap_seconds=1_000,
                tvrip_acoustic_segment_rejection_padding_seconds=5,
                tvrip_terminal_tail_validation=False,
            )
            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment"),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch(
                    "dualmaker.tvrip.envelope_similarity",
                    side_effect=[0.10, 0.90, 0.90, 0.90, 0.90],
                ),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            rejected = [segment for segment in report.segments if segment.status == "rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertAlmostEqual(rejected[0].master_start, 0.0)
            self.assertAlmostEqual(rejected[0].master_end, 10.0)

    def test_telecine_local_guard_replaces_only_one_sided_original_audio(self) -> None:
        """A missing source reference is a local fallback, never a whole-bucket loss."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            plan.fps.validation.update(
                {
                    "telecine_acoustic_preflight": {"enabled": True},
                    "spectral_tempo_probe": {"fallback_accepted": True},
                    "spectral_post_sync_validation": {"fallback_accepted": True},
                }
            )
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                shift_points=[(0, 0, 0), (100, 100, 0), (200, 200, 0)],
                sync_buckets=[(0, 300, 0)],
                sync_coverage=0.99,
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=False,
                tvrip_fallback="original",
                tvrip_min_coverage=0.10,
                tvrip_spectral_min_segment_confidence=0.25,
                tvrip_spectral_min_source_match_confidence=0.10,
                tvrip_acoustic_segment_min_similarity=0.50,
                tvrip_acoustic_segment_max_gap_seconds=1_000,
                tvrip_acoustic_segment_rejection_padding_seconds=5,
                tvrip_terminal_tail_validation=False,
            )
            signal = [1] * 200
            # Five edge-aware probes. The centre has no source original while
            # the master has audible material, representing a source-only map
            # hypothesis; the surrounding verified Portuguese coverage stays.
            envelopes = [
                signal, signal,
                signal, signal,
                [], signal,
                signal, signal,
                signal, signal,
            ]
            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment"),
                patch("dualmaker.tvrip._binary_audio_envelope", side_effect=envelopes),
                patch("dualmaker.tvrip.envelope_similarity", return_value=0.90),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            rejected = [segment for segment in report.segments if segment.status == "rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertAlmostEqual(rejected[0].master_start, 142.5)
            self.assertAlmostEqual(rejected[0].master_end, 157.5)
            retained = [segment for segment in report.segments if segment.status == "accepted"]
            self.assertTrue(retained)
            evidence = report.source_analysis["acoustic_segment_validation"]
            self.assertEqual(evidence["segments"][0]["probe_states"]["source-only-master-audible"], 1)  # type: ignore[index]

    def test_telecine_local_guard_keeps_matching_silence(self) -> None:
        """A quiet mapped scene must not turn into an unnecessary English fallback."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            plan.fps.validation.update(
                {
                    "telecine_acoustic_preflight": {"enabled": True},
                    "spectral_tempo_probe": {"fallback_accepted": True},
                    "spectral_post_sync_validation": {"fallback_accepted": True},
                }
            )
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                shift_points=[(0, 0, 0), (100, 100, 0), (200, 200, 0)],
                sync_buckets=[(0, 300, 0)],
                sync_coverage=0.99,
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="original",
                tvrip_min_coverage=0.10,
                tvrip_spectral_min_segment_confidence=0.25,
                tvrip_spectral_min_source_match_confidence=0.10,
                tvrip_acoustic_segment_max_gap_seconds=1_000,
                tvrip_terminal_tail_validation=False,
            )
            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment"),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[]),
                patch(
                    "dualmaker.tvrip.envelope_similarity",
                    side_effect=AssertionError("matching silence must not be compared as spectra"),
                ),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            self.assertTrue(all(segment.status == "accepted" for segment in report.segments))
            evidence = report.source_analysis["acoustic_segment_validation"]
            self.assertEqual(evidence["segments"][0]["probe_states"]["both-silent"], 5)  # type: ignore[index]

    def test_piecewise_buckets_are_bounded_split_and_report_commercial_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                sync_buckets=[(0, 600, 0), (660, 1_000_000, -60)],
                delete_buckets=[(600, 660)],
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="silence",
                tvrip_max_segment_seconds=300,
                tvrip_min_coverage=0.9,
                tvrip_min_source_match_confidence=0.5,
            )

            def accept(segment: TVRipSegment, **_: object) -> None:
                segment.status = "accepted"
                segment.confidence = 0.95
                segment.residual_seconds = 0.02

            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment", side_effect=accept),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )
            self.assertEqual(len(report.segments), 4)
            self.assertAlmostEqual(report.coverage, 1.0)
            self.assertAlmostEqual(sum(item.duration for item in report.tvrip_only), 60.0)
            self.assertIn("commercial", report.tvrip_only[0].classification)
            approved = approve_tvrip_report(report, plan, config)
            self.assertEqual(approved.result, "accepted")

    def test_unmatched_open_ended_tvrip_tail_is_replaced_by_master_fallback(self) -> None:
        """A final sentinel bucket is a hypothesis, not evidence of matching credits."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                sync_buckets=[(0, 1100, 0), (1100, 1_000_000, 0)],
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=False,
                tvrip_fallback="original",
                tvrip_min_coverage=0.01,
                tvrip_min_source_match_confidence=0.01,
                tvrip_terminal_tail_min_similarity=0.50,
            )

            def accept(segment: TVRipSegment, **_: object) -> None:
                segment.status = "accepted"
                segment.confidence = 0.95

            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment", side_effect=accept),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch("dualmaker.tvrip.envelope_similarity", return_value=0.20),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            tail = max(report.segments, key=lambda segment: segment.master_end)
            self.assertEqual(tail.status, "rejected")
            terminal = report.source_analysis["terminal_tail_validation"]
            self.assertEqual(terminal["action"], "replaced-with-fallback")  # type: ignore[index]
            self.assertTrue(any(item.start == 1100 for item in report.master_only))

    def test_open_tail_is_proved_even_when_the_master_runs_longer(self) -> None:
        """An HDTV-only ending must not leak through an unanchored last bucket."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.normal.duration = 1260
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                sync_buckets=[(0, 900, 0), (900, 1_000_000, 0)],
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                # Continuation mode keeps uncertain *bounded* program scenes,
                # but it cannot authorize an unproven terminal source tail.
                tvrip_continue_on_validation_warnings=True,
                tvrip_fallback="original",
                tvrip_min_coverage=0.01,
                tvrip_min_source_match_confidence=0.01,
                tvrip_terminal_tail_min_similarity=0.50,
            )

            def accept(segment: TVRipSegment, **_: object) -> None:
                segment.status = "accepted"
                segment.confidence = 0.95

            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment", side_effect=accept),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch("dualmaker.tvrip.envelope_similarity", return_value=0.20),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            terminal = report.source_analysis["terminal_tail_validation"]
            self.assertEqual(terminal["action"], "replaced-with-fallback")  # type: ignore[index]
            self.assertTrue(
                any(
                    segment.status == "rejected" and segment.master_start == 900
                    for segment in report.segments
                )
            )
            self.assertTrue(any(item.start == 900 for item in report.master_only))

    def test_matching_open_ended_tvrip_tail_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                sync_buckets=[(0, 1100, 0), (1100, 1_000_000, 0)],
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="original",
                tvrip_min_coverage=0.01,
                tvrip_min_source_match_confidence=0.01,
                tvrip_terminal_tail_min_similarity=0.50,
            )

            def accept(segment: TVRipSegment, **_: object) -> None:
                segment.status = "accepted"
                segment.confidence = 0.95

            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment", side_effect=accept),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch("dualmaker.tvrip.envelope_similarity", return_value=0.90),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            terminal = report.source_analysis["terminal_tail_validation"]
            self.assertEqual(terminal["action"], "retained")  # type: ignore[index]
            self.assertTrue(all(segment.status == "accepted" for segment in report.segments))

    def test_terminal_tail_replaces_only_a_failed_edge_window(self) -> None:
        """A source-only end/preview boundary must not force fallback for all credits."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                sync_buckets=[(0, 1100, 0), (1100, 1_000_000, 0)],
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=False,
                tvrip_fallback="original",
                tvrip_min_coverage=0.01,
                tvrip_min_source_match_confidence=0.01,
                tvrip_terminal_tail_min_similarity=0.50,
            )

            def accept(segment: TVRipSegment, **_: object) -> None:
                segment.status = "accepted"
                segment.confidence = 0.95

            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment", side_effect=accept),
                patch("dualmaker.tvrip._telecine_acoustic_probe_positions", return_value=(0.0, 46.0, 92.0)),
                patch("dualmaker.tvrip._binary_audio_envelope", return_value=[1] * 200),
                patch("dualmaker.tvrip.envelope_similarity", side_effect=[0.10, 0.90, 0.90]),
            ):
                report = build_tvrip_sync_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )

            rejected = [segment for segment in report.segments if segment.status == "rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertAlmostEqual(rejected[0].master_start, 1100.0)
            self.assertAlmostEqual(rejected[0].master_end, 1113.0)
            terminal = report.source_analysis["terminal_tail_validation"]
            self.assertEqual(terminal["action"], "partially-replaced-with-fallback")  # type: ignore[index]

    def test_strict_unattended_ambiguous_segment_fails_with_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            report = TVRipSyncReport(
                approved=True,
                segments=[
                    TVRipSegment(
                        1,
                        0,
                        100,
                        0,
                        100,
                        0,
                        confidence=0.8,
                        status="ambiguous",
                    )
                ],
                coverage=0.9,
                source_match_confidence=0.8,
                fallback="silence",
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="silence",
                tvrip_min_coverage=0.5,
                tvrip_min_source_match_confidence=0.5,
                tvrip_continue_on_validation_warnings=False,
            )
            with self.assertRaises(TVRipValidationError) as raised:
                approve_tvrip_report(report, plan, config)
            self.assertIs(raised.exception.report, report)
            self.assertIn("interactive review", report.reason)

    def test_authorized_unattended_ambiguous_segment_continues_with_report(self) -> None:
        """Explicit TVRip opt-in defers review warnings instead of skipping output."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            report = TVRipSyncReport(
                approved=True,
                segments=[
                    TVRipSegment(
                        1,
                        0,
                        100,
                        0,
                        100,
                        0,
                        confidence=0.8,
                        status="ambiguous",
                    )
                ],
                fallback="original",
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="original",
            )

            approved = approve_tvrip_report(report, plan, config)

            self.assertEqual(approved.result, "accepted")
            self.assertEqual(
                approved.source_analysis["deferred_validation"]["action"],  # type: ignore[index]
                "continued",
            )
            self.assertTrue(any("segments remain ambiguous" in warning for warning in approved.warnings))

    def test_authorized_interactive_run_defers_large_segment_checklist(self) -> None:
        """Interactive pair selection must not force a 138-row pre-mux review."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            report = TVRipSyncReport(
                approved=True,
                segments=[
                    TVRipSegment(1, 0, 1140, 0, 1140, 0, confidence=0.8, status="accepted")
                ],
                fallback="original",
            )
            config = DualMakerConfig(
                path=root,
                interactive=True,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="original",
            )

            approved = approve_tvrip_report(report, plan, config)

            self.assertEqual(approved.result, "accepted")
            self.assertEqual(
                approved.source_analysis["interactive_segment_review"]["action"],  # type: ignore[index]
                "deferred",
            )

    def test_segment_count_above_diagnostic_threshold_still_assembles(self) -> None:
        """Cut-heavy broadcast maps are reviewed later, never skipped by count alone."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            report = TVRipSyncReport(
                approved=True,
                segments=[
                    TVRipSegment(1, 0, 570, 0, 570, 0, confidence=0.8, status="accepted"),
                    TVRipSegment(
                        2, 570, 1140, 570, 1140, 0, confidence=0.8, status="accepted"
                    ),
                ],
                fallback="original",
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_fallback="original",
                tvrip_max_segments=1,
                tvrip_min_coverage=0.5,
                tvrip_min_source_match_confidence=0.5,
            )

            approved = approve_tvrip_report(report, plan, config)

            self.assertEqual(approved.result, "accepted")
            self.assertTrue(
                any("segments exceed configured diagnostic threshold" in warning for warning in approved.warnings)
            )
            self.assertEqual(
                approved.source_analysis["segment_count_diagnostic"]["action"],  # type: ignore[index]
                "continued",
            )

    def test_track_selection_still_uses_tvrip_dub_and_master_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = _plan(Path(directory))
            self.assertEqual(
                [(item.source, item.track.effective_language) for item in plan.resolved_dubs],
                [("dual", "pt-BR")],
            )
            self.assertEqual(plan.resolved_original.source, "master")
            self.assertIs(plan.resolved_original.track, plan.normal_original)


class DubGapFallbackTests(unittest.TestCase):
    def _gap_plan(self, root: Path) -> JobPlan:
        master = _asset(root / "Show.S01E01.WEB-DL-GROUP.mkv", 105, portuguese=False)
        dual = _asset(root / "Show.S01E01.DUAL-GROUP.mkv", 100, portuguese=True)
        candidate = require_explicit_tvrip_pair(master, dual)
        candidate.source_kind = "dual"
        return create_job_plan(candidate, DualMakerConfig(path=root, output=root / "out.mkv"))

    def test_detects_only_a_master_timeline_hole_between_reference_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._gap_plan(root)
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                sync_buckets=[(0, 20, 0), (20, 100, 5)],
            )
            gaps = detected_master_only_intervals(plan, sync, minimum_seconds=1.0)
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0].kind, "master-only")
            self.assertAlmostEqual(gaps[0].start, 20.0)
            self.assertAlmostEqual(gaps[0].end, 25.0)

    def test_authorized_long_original_probe_recovers_false_map_hole(self) -> None:
        """A proven source/master match fills the dub instead of inserting English."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._gap_plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(root / "stage.mkv", root / "map.json")
            report = TVRipSyncReport(
                segments=[
                    TVRipSegment(1, 0, 20, 0, 20, 0, confidence=0.9, status="accepted"),
                    TVRipSegment(2, 20, 100, 25, 105, 5, confidence=0.9, status="accepted"),
                ],
                master_only=[TVRipInterval(20, 25, "master-only", "map hole")],
                fallback="original",
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=True,
                tvrip_acoustic_segment_window_seconds=12,
                tvrip_acoustic_segment_min_similarity=0.6,
            )
            with patch(
                "dualmaker.tvrip._common_original_probe",
                return_value={"similarity": 0.95, "state": "comparable"},
            ):
                evidence = _recover_mapped_master_gaps(
                    plan,
                    sync,
                    report,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    source_duration=100,
                    master_duration=105,
                    config=config,
                    runner=object(),  # type: ignore[arg-type]
                )

            self.assertEqual(evidence["action"], "recovered")
            self.assertEqual(evidence["recovered_count"], 1)
            bridge = report.source_analysis["raw_dub_gap_bridges"][0]
            self.assertEqual((bridge["master_start"], bridge["master_end"]), (20, 25))
            self.assertTrue(
                any("recovered false master-only" in segment.operation for segment in report.segments)
            )

    def test_subsecond_map_hole_keeps_raw_dub_when_long_proof_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._gap_plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(root / "stage.mkv", root / "map.json")
            report = TVRipSyncReport(
                segments=[
                    TVRipSegment(1, 0, 20, 0, 20, 0, confidence=0.9, status="accepted"),
                    TVRipSegment(2, 20, 100, 20.1, 100.1, 0.1, confidence=0.9, status="accepted"),
                ],
                master_only=[TVRipInterval(20, 20.1, "master-only", "map hole")],
                fallback="original",
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=True,
                tvrip_acoustic_segment_min_seconds=2.0,
                tvrip_acoustic_segment_min_similarity=0.6,
            )
            with patch(
                "dualmaker.tvrip._common_original_probe",
                return_value={"similarity": 0.1, "state": "comparable"},
            ):
                evidence = _recover_mapped_master_gaps(
                    plan,
                    sync,
                    report,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    source_duration=100,
                    master_duration=105,
                    config=config,
                    runner=object(),  # type: ignore[arg-type]
                )

            self.assertEqual(evidence["recovered_count"], 1)
            bridge = report.source_analysis["raw_dub_gap_bridges"][0]
            self.assertEqual((bridge["master_start"], bridge["master_end"]), (20, 20.1))
            self.assertEqual(evidence["gaps"][0]["action"], "bridged-micro-gap-with-raw-dub")

    def test_terminal_tail_rejection_cannot_be_recovered_by_a_broad_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._gap_plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(root / "stage.mkv", root / "map.json")
            report = TVRipSyncReport(
                segments=[
                    TVRipSegment(1, 0, 90, 0, 90, 0, confidence=0.9, status="accepted"),
                    TVRipSegment(2, 90, 100, 95, 105, 5, confidence=0.0, status="rejected"),
                ],
                master_only=[TVRipInterval(90, 105, "master-only", "map hole")],
                fallback="original",
                source_analysis={
                    "terminal_tail_validation": {
                        "action": "replaced-with-fallback",
                        "rejected_master_ranges": [{"start": 95, "end": 105}],
                    }
                },
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=True,
                tvrip_acoustic_segment_min_similarity=0.6,
            )
            with patch(
                "dualmaker.tvrip._common_original_probe",
                return_value={"similarity": 0.95, "state": "comparable"},
            ):
                evidence = _recover_mapped_master_gaps(
                    plan,
                    sync,
                    report,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    source_duration=100,
                    master_duration=105,
                    config=config,
                    runner=object(),  # type: ignore[arg-type]
                )

            self.assertNotIn("raw_dub_gap_bridges", report.source_analysis)
            self.assertTrue(
                any(item["action"] == "fallback-preserved-terminal-tail" for item in evidence["gaps"])
            )

    def test_recovery_never_requests_raw_dub_past_source_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._gap_plan(root)
            plan.dual.path.touch()
            plan.normal.path.touch()
            sync = SyncResult(root / "stage.mkv", root / "map.json")
            report = TVRipSyncReport(
                segments=[
                    TVRipSegment(1, 0, 90, 0, 90, 0, confidence=0.9, status="accepted"),
                ],
                master_only=[TVRipInterval(90, 105, "master-only", "map hole")],
                fallback="original",
            )
            config = DualMakerConfig(
                path=root,
                allow_tvrip_segment_sync=True,
                tvrip_continue_on_validation_warnings=True,
                tvrip_acoustic_segment_min_similarity=0.6,
            )
            with patch(
                "dualmaker.tvrip._common_original_probe",
                return_value={"similarity": 0.95, "state": "comparable"},
            ):
                evidence = _recover_mapped_master_gaps(
                    plan,
                    sync,
                    report,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    source_duration=100,
                    master_duration=105,
                    config=config,
                    runner=object(),  # type: ignore[arg-type]
                )

            self.assertNotIn("raw_dub_gap_bridges", report.source_analysis)
            self.assertEqual(evidence["gaps"][0]["action"], "fallback-retained-source-exhausted")

    def test_original_fallback_requires_every_mapped_section_to_validate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._gap_plan(root)
            sync = SyncResult(
                root / "stage.mkv",
                root / "map.json",
                sync_buckets=[(0, 20, 0), (20, 100, 5)],
            )
            config = DualMakerConfig(
                path=root,
                dub_gap_fallback="original",
                dub_gap_min_seconds=1.0,
                dub_gap_min_coverage=0.8,
            )

            def accept(segment: TVRipSegment, **_: object) -> None:
                segment.status = "accepted"
                segment.confidence = 0.95
                segment.residual_seconds = 0.02

            with (
                patch("dualmaker.tvrip.analyze_tvrip_sources", return_value={}),
                patch("dualmaker.tvrip._validate_segment", side_effect=accept),
            ):
                report = build_dub_gap_report(
                    plan,
                    sync,
                    source_path=plan.dual.path,
                    master_path=plan.normal.path,
                    config=config,
                    work_dir=root,
                    runner=object(),  # type: ignore[arg-type]
                )
            approved = approve_dub_gap_report(report, plan, config)
            self.assertEqual(approved.workflow, "dub-gap")
            self.assertEqual(approved.fallback, "original")
            self.assertEqual(approved.result, "accepted")
            self.assertEqual([(gap.start, gap.end) for gap in approved.master_only], [(20, 25)])
            self.assertEqual(
                approved.source_analysis["fallback_reference"]["track_id"],
                plan.normal_original.id,
            )
            self.assertEqual(
                _partition(approved, 105),
                [(0.0, 20.0, True), (20.0, 25.0, False), (25.0, 105.0, True)],
            )

    def test_unvalidated_reference_section_cannot_be_replaced_unattended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._gap_plan(root)
            report = TVRipSyncReport(
                workflow="dub-gap",
                fallback="original",
                segments=[
                    TVRipSegment(1, 0, 20, 0, 20, 0, status="accepted"),
                    TVRipSegment(2, 20, 100, 25, 105, 5, status="rejected"),
                ],
            )
            with self.assertRaisesRegex(TVRipValidationError, "withheld"):
                approve_dub_gap_report(report, plan, DualMakerConfig(path=root))

    def test_hdtv_workflow_inherits_universal_original_fallback(self) -> None:
        config = DualMakerConfig()
        effective = _effective_tvrip_policy(config)
        self.assertEqual(config.tvrip_fallback, "ask")
        self.assertEqual(effective.tvrip_fallback, "original")

        explicit = DualMakerConfig(tvrip_fallback="silence")
        self.assertIs(_effective_tvrip_policy(explicit), explicit)

        review_only = DualMakerConfig(dub_gap_fallback="off")
        self.assertIs(_effective_tvrip_policy(review_only), review_only)


if __name__ == "__main__":
    unittest.main()
