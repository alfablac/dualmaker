from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dualmaker.models import (
    AudioTrackSelection,
    ContentIdentity,
    DualMakerConfig,
    FPSDecision,
    JobPlan,
    MediaAsset,
    SidecarSubtitle,
    Track,
)
from dualmaker.sync.adapter import (
    MilksyncAdapter,
    SyncResult,
    estimate_spectral_tempo,
    next_spectral_speed_factor,
    post_sync_relative_speed,
)


class FakeRunner:
    def __init__(self) -> None:
        self.binaries = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}
        self.environment: dict[str, str] = {}
        self.command: tuple[str, ...] = ()

    def which(self, _name: str) -> str:
        return "/bin/true"

    def run_live(self, args: list[object]) -> SimpleNamespace:
        self.command = tuple(str(item) for item in args)
        output = Path(self.command[self.command.index("--output") + 1])
        report = Path(self.command[self.command.index("--sync-report") + 1])
        output.touch()
        report.write_text(
            json.dumps(
                {
                    "0": {
                        "audio_shift_points": [[0, 0, 0]],
                        "sync_buckets": [[0, 100, 0]],
                        "delete_buckets": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class SparseSpectralRunner(FakeRunner):
    """Return an intentionally sparse map for both tempo probe and render."""

    def run(
        self,
        args: list[object],
        *,
        check: bool = True,
    ) -> SimpleNamespace:
        command = tuple(str(item) for item in args)
        report = Path(command[command.index("--sync-report") + 1])
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps({"0": {"audio_shift_points": [[0, 0, 0]]}}),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class CrossLanguageTempoRunner(FakeRunner):
    """Provide an event-only raw map with a PAL-speed content clock."""

    def __init__(self, factor: float) -> None:
        super().__init__()
        self.factor = factor
        self.preflight_command: tuple[str, ...] = ()
        self.render_commands: list[tuple[str, ...]] = []

    def run(
        self,
        args: list[object],
        *,
        check: bool = True,
    ) -> SimpleNamespace:
        del check
        self.preflight_command = tuple(str(item) for item in args)
        report = Path(self.preflight_command[self.preflight_command.index("--sync-report") + 1])
        report.parent.mkdir(parents=True, exist_ok=True)
        points = [
            [source / self.factor, source, source / self.factor - source]
            for source in range(0, 201, 20)
        ]
        report.write_text(
            json.dumps({"0": {"audio_shift_points": points}}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def run_live(self, args: list[object]) -> SimpleNamespace:
        self.command = tuple(str(item) for item in args)
        self.render_commands.append(self.command)
        output = Path(self.command[self.command.index("--output") + 1])
        report = Path(self.command[self.command.index("--sync-report") + 1])
        output.touch()
        # The rendered event map is now stationary, so its post-map tempo is
        # one and no extra speed correction should be requested.
        points = [[source, source, 0.0] for source in range(0, 201, 20)]
        report.write_text(
            json.dumps(
                {
                    "0": {
                        "audio_shift_points": points,
                        "sync_buckets": [[0, 1_000_000, 0]],
                        "delete_buckets": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class SparseCrossLanguageEventRunner(CrossLanguageTempoRunner):
    """Return just one bounded post-render event pair."""

    def run_live(self, args: list[object]) -> SimpleNamespace:
        self.command = tuple(str(item) for item in args)
        self.render_commands.append(self.command)
        output = Path(self.command[self.command.index("--output") + 1])
        report = Path(self.command[self.command.index("--sync-report") + 1])
        output.touch()
        report.write_text(
            json.dumps(
                {
                    "0": {
                        "audio_shift_points": [[0, 0, 0], [20, 20, 0]],
                        "sync_buckets": [[0, 1_000_000, 0]],
                        "delete_buckets": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class SpectralSlopeRunner(FakeRunner):
    """Report the residual slope of Milksync's rendered waveform."""

    def __init__(self, factor: float) -> None:
        super().__init__()
        self.factor = factor

    def run_live(self, args: list[object]) -> SimpleNamespace:
        self.command = tuple(str(item) for item in args)
        output = Path(self.command[self.command.index("--output") + 1])
        report = Path(self.command[self.command.index("--sync-report") + 1])
        output.touch()
        reported_factor = (
            1.0 if "--framerate-speed-factor" in self.command else self.factor
        )
        points = [
            [source / reported_factor, source, source / reported_factor - source]
            for source in range(0, 201, 20)
        ]
        report.write_text(
            json.dumps(
                {
                    "0": {
                        "audio_shift_points": points,
                        "sync_buckets": [[0, 1_000_000, 0]],
                        "delete_buckets": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class TelecineLinearDriftRunner(FakeRunner):
    """Expose a stable source clock after a real-time TVRip first pass."""

    def __init__(self, factor: float) -> None:
        super().__init__()
        self.factor = factor
        self.commands: list[tuple[str, ...]] = []

    def run_live(self, args: list[object]) -> SimpleNamespace:
        self.command = tuple(str(item) for item in args)
        self.commands.append(self.command)
        output = Path(self.command[self.command.index("--output") + 1])
        report = Path(self.command[self.command.index("--sync-report") + 1])
        output.touch()
        # The first pass exposes the source clock. Once atempo is requested,
        # Milksync measures the already-rendered waveform and reports a unit
        # residual.
        reported_factor = (
            1.0 if "--framerate-speed-factor" in self.command else self.factor
        )
        points = [
            [source / reported_factor, source, source / reported_factor - source]
            for source in range(0, 1201, 20)
        ]
        report.write_text(
            json.dumps(
                {
                    "0": {
                        "audio_shift_points": points,
                        "sync_buckets": [[0, 1_000_000, 0]],
                        "delete_buckets": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class AdapterMappingTests(unittest.TestCase):
    def test_cross_language_events_relax_tempo_evidence_for_pal_speed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_original = Track(1, "audio", 0, language_ietf="en")
            dual_dub = Track(1, "audio", 0, language_ietf="pt-BR")
            master = MediaAsset(root / "master.mkv", 210, [master_original])
            dual = MediaAsset(root / "dub.mkv", 200, [dual_dub])
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
                output=root / "output.mkv",
                normal_original=master_original,
                dual_original=dual_dub,
                dub_tracks=[dual_dub],
                normal_subtitles=[],
                dual_subtitles=[],
                alignment_mode="cross-language-events",
                fps=FPSDecision(required=True, approved=True, proposed_speed_factor=1.0),
            )
            runner = CrossLanguageTempoRunner(24000 / 25025)
            with patch("dualmaker.sync.adapter.first_packet_pts", return_value=0.0):
                MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(),
                )

            self.assertIn("--event-anchors", runner.preflight_command)
            self.assertAlmostEqual(plan.fps.proposed_speed_factor, 24000 / 25025)
            probe = plan.fps.validation["spectral_tempo_probe"]
            self.assertEqual(probe["alignment_mode"], "cross-language-events")  # type: ignore[index]
            self.assertEqual(probe["relaxed_event_evidence"]["minimum_pairs"], 4)  # type: ignore[index]
            self.assertTrue(plan.fps.validation["cross_language_event_post_map"]["reliable"])  # type: ignore[index]
            self.assertEqual(len(runner.render_commands), 1)

    def test_cross_language_dub_uses_short_high_energy_event_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_original = Track(1, "audio", 0, language_ietf="en")
            dual_dub = Track(1, "audio", 0, language_ietf="pt-BR")
            master = MediaAsset(root / "master.mkv", 100, [master_original])
            dual = MediaAsset(root / "dub.avi", 100, [dual_dub])
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
                output=root / "output.mkv",
                normal_original=master_original,
                # This is an alignment reference only. It remains the output
                # Portuguese dub, never an original-language output track.
                dual_original=dual_dub,
                dub_tracks=[dual_dub],
                normal_subtitles=[],
                dual_subtitles=[],
                alignment_mode="cross-language-events",
            )
            runner = FakeRunner()
            with patch("dualmaker.sync.adapter.first_packet_pts", return_value=0.0):
                MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(fps_spectral_tempo_probe=False),
                )

            self.assertIn("--event-anchors", runner.command)
            window = runner.command.index("--acoustic-anchor-window-size")
            self.assertEqual(runner.command[window + 1], "96")

    def test_sparse_cross_language_post_map_does_not_use_common_audio_failure_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_original = Track(1, "audio", 0, language_ietf="en")
            dual_dub = Track(1, "audio", 0, language_ietf="pt-BR")
            master = MediaAsset(root / "master.mkv", 210, [master_original])
            dual = MediaAsset(root / "dub.mkv", 200, [dual_dub])
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
                output=root / "output.mkv",
                normal_original=master_original,
                dual_original=dual_dub,
                dub_tracks=[dual_dub],
                normal_subtitles=[],
                dual_subtitles=[],
                alignment_mode="cross-language-events",
                fps=FPSDecision(required=True, approved=True, proposed_speed_factor=1.0),
            )
            runner = SparseCrossLanguageEventRunner(24000 / 25025)
            with patch("dualmaker.sync.adapter.first_packet_pts", return_value=0.0):
                result = MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(),
                )

            self.assertTrue(result.path.is_file())
            event_post = plan.fps.validation["cross_language_event_post_map"]
            self.assertFalse(event_post["reliable"])  # type: ignore[index]
            self.assertIn("1 bounded acoustic pairs", event_post["reason"])  # type: ignore[index]
            self.assertNotIn("spectral_post_sync_validation", plan.fps.validation)

    def test_sparse_spectral_probe_and_post_map_warn_but_render(self) -> None:
        """Sparse fingerprints must not reject an explicitly approved FPS run."""

        project_temp = Path.cwd() / ".test-work"
        project_temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=project_temp) as directory:
            root = Path(directory)
            master_original = Track(1, "audio", 0, language_ietf="en")
            dual_original = Track(2, "audio", 0, language_ietf="en")
            master = MediaAsset(root / "master.mkv", 1446, [master_original])
            dual = MediaAsset(root / "dual.mkv", 1414, [dual_original])
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
                output=root / "output.mkv",
                normal_original=master_original,
                dual_original=dual_original,
                dub_tracks=[],
                normal_subtitles=[],
                dual_subtitles=[],
                fps=FPSDecision(
                    required=True,
                    approved=True,
                    apply_speed_correction=True,
                    proposed_speed_factor=0.96,
                ),
            )
            runner = SparseSpectralRunner()
            with patch("dualmaker.sync.adapter.first_packet_pts", return_value=0.0):
                result = MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(),
                )

            self.assertTrue(result.path.is_file())
            probe = plan.fps.validation["spectral_tempo_probe"]
            post = plan.fps.validation["spectral_post_sync_validation"]
            self.assertTrue(probe["fallback_accepted"])  # type: ignore[index]
            self.assertTrue(post["fallback_accepted"])  # type: ignore[index]
            self.assertIn("--framerate-speed-factor", runner.command)
            self.assertIn("--acoustic-anchor-window-size", runner.command)
            self.assertEqual(
                runner.command[
                    runner.command.index("--acoustic-anchor-window-size") + 1
                ],
                "96",
            )
            self.assertEqual(runner.environment["OPENBLAS_NUM_THREADS"], "2")
            self.assertEqual(runner.environment["NUMBA_NUM_THREADS"], "2")
            self.assertEqual(
                runner.command[runner.command.index("--chroma-workers") + 1],
                "1",
            )
            self.assertEqual(
                runner.command[
                    runner.command.index("--max-cost-matrix-size") + 1
                ],
                "25000000",
            )

    def test_post_sync_speed_is_measured_on_the_rendered_timeline(self) -> None:
        # Milksync measures chroma after atempo, so a corrected render has a
        # unit post-render residual regardless of the tempo used to make it.
        self.assertAlmostEqual(
            post_sync_relative_speed(1.0, 0.9780320280189394) or 0,
            1.0,
        )
        self.assertAlmostEqual(
            post_sync_relative_speed(0.96, 0.9780320280189394) or 0,
            0.96,
            places=10,
        )

    def test_post_sync_unit_slope_after_rendered_tempo_does_not_abort(self) -> None:
        project_temp = Path.cwd() / ".test-work"
        project_temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=project_temp) as directory:
            root = Path(directory)
            master_original = Track(1, "audio", 0, language_ietf="en")
            dual_original = Track(2, "audio", 0, language_ietf="en")
            master = MediaAsset(root / "master.mkv", 1446, [master_original])
            dual = MediaAsset(root / "dual.mkv", 1414, [dual_original])
            factor = 0.9780320280189394
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
                output=root / "output.mkv",
                normal_original=master_original,
                dual_original=dual_original,
                dub_tracks=[],
                normal_subtitles=[],
                dual_subtitles=[],
                fps=FPSDecision(
                    required=True,
                    approved=True,
                    apply_speed_correction=True,
                    proposed_speed_factor=factor,
                ),
            )
            runner = SpectralSlopeRunner(factor)
            with (
                patch.object(MilksyncAdapter, "_refine_experimental_speed"),
                patch("dualmaker.sync.adapter.first_packet_pts", return_value=0.0),
            ):
                result = MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(),
                )

            post = plan.fps.validation["spectral_post_sync_validation"]
            self.assertAlmostEqual(float(post["relative_speed_factor"]), 1.0)  # type: ignore[index]
            self.assertAlmostEqual(result.speed_correction_factor, factor)

    def test_real_time_telecine_tvrip_skips_sparse_linear_tempo_preflight(self) -> None:
        """The actual map, not a second sparse analysis pass, proves this path."""

        root = Path.cwd() / ".test-work" / "telecine-preflight"
        master_original = Track(1, "audio", 0, language_ietf="en")
        dual_original = Track(2, "audio", 0, language_ietf="en")
        master = MediaAsset(root / "master.mkv", 100, [master_original])
        dual = MediaAsset(root / "dual.mkv", 100, [dual_original])
        plan = JobPlan(
            normal=master,
            dual=dual,
            identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
            output=root / "output.mkv",
            normal_original=master_original,
            dual_original=dual_original,
            dub_tracks=[],
            normal_subtitles=[],
            dual_subtitles=[],
            source_kind="tvrip",
            fps=FPSDecision(
                required=True,
                approved=True,
                proposed_speed_factor=1.0,
                validation={"telecine_acoustic_preflight": {"enabled": True}},
            ),
        )

        MilksyncAdapter(FakeRunner())._refine_experimental_speed(  # type: ignore[arg-type]
            plan,
            normal_path=master.path,
            dual_path=dual.path,
            temp_dir=root,
            config=DualMakerConfig(),
        )

        probe = plan.fps.validation["spectral_tempo_probe"]
        self.assertTrue(probe["skipped"])  # type: ignore[index]
        self.assertTrue(probe["fallback_accepted"])  # type: ignore[index]

    def test_completed_telecine_map_rerenders_when_it_proves_linear_drift(self) -> None:
        """A dense final map must not be rendered as hundreds of silence gaps."""

        project_temp = Path.cwd() / ".test-work"
        project_temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=project_temp) as directory:
            root = Path(directory)
            master_original = Track(1, "audio", 0, language_ietf="en")
            dual_original = Track(2, "audio", 0, language_ietf="en")
            master = MediaAsset(root / "master.mkv", 1446, [master_original])
            dual = MediaAsset(root / "dual.mkv", 1414, [dual_original])
            factor = 0.9780320280189394
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
                output=root / "output.mkv",
                normal_original=master_original,
                dual_original=dual_original,
                dub_tracks=[],
                normal_subtitles=[],
                dual_subtitles=[],
                source_kind="tvrip",
                fps=FPSDecision(
                    required=True,
                    approved=True,
                    proposed_speed_factor=1.0,
                    validation={"telecine_acoustic_preflight": {"enabled": True}},
                ),
            )
            runner = TelecineLinearDriftRunner(factor)
            with (
                patch.object(MilksyncAdapter, "_refine_experimental_speed"),
                patch("dualmaker.sync.adapter.first_packet_pts", return_value=0.0),
            ):
                result = MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(),
                )

            self.assertEqual(len(runner.commands), 2)
            self.assertNotIn("--framerate-speed-factor", runner.commands[0])
            self.assertIn("--framerate-speed-factor", runner.commands[1])
            rendered_factor = runner.commands[1][
                runner.commands[1].index("--framerate-speed-factor") + 1
            ]
            self.assertAlmostEqual(float(rendered_factor), factor, places=9)
            self.assertAlmostEqual(result.speed_correction_factor, factor, places=9)
            post = plan.fps.validation["spectral_post_sync_validation"]
            self.assertAlmostEqual(float(post["relative_speed_factor"]), 1.0)  # type: ignore[index]
            history = plan.fps.validation["spectral_speed_refinements"]
            self.assertEqual(history[0]["correction_method"], "post-map-linear-drift-rescue")  # type: ignore[index]

    def test_spectral_refinement_is_damped_until_measurements_bracket_zero(self) -> None:
        first, method = next_spectral_speed_factor(
            0.978032028,
            1.000330907,
            [],
            damping=0.5,
        )
        self.assertEqual(method, "damped-residual")
        self.assertAlmostEqual(first, 0.97819385, places=7)

        # A subsequent opposite-sign residual brackets the physical clock.
        second, method = next_spectral_speed_factor(
            0.978355665,
            0.99956925,
            [(0.978032028, 0.000330907)],
            damping=0.5,
        )
        self.assertEqual(method, "bracketed-secant")
        self.assertGreater(second, 0.978032028)
        self.assertLess(second, 0.978355665)
        self.assertAlmostEqual(second, 0.9781726, places=6)

    def test_spectral_tempo_separates_linear_drift_from_edit_steps(self) -> None:
        # Target runs 2.04% longer per source second. Two editorial steps alter
        # only the intercept and must not be interpreted as a different tempo.
        points = []
        for source in range(0, 1201, 20):
            edit_offset = 0.0 if source < 400 else (12.0 if source < 800 else 4.0)
            target = source / 0.98 + edit_offset
            points.append((target, float(source), target - source))

        result = estimate_spectral_tempo(points, DualMakerConfig())

        self.assertTrue(result["reliable"])
        self.assertAlmostEqual(float(result["speed_factor"]), 0.98, places=4)
        self.assertGreaterEqual(int(result["inlier_pairs"]), 12)

    def test_sparse_post_sync_points_find_stationary_delay_cluster(self) -> None:
        points = [
            (1.672, 0.0, 1.672),
            (46.161, 43.143, 3.019),
            (97.524, 94.645, 2.879),
            (167.509, 164.490, 3.019),
            (184.227, 181.348, 2.879),
            (227.556, 224.583, 2.972),
            (369.847, 366.968, 2.879),
            (415.684, 412.711, 2.972),
            (579.338, 576.505, 2.833),
            (749.076, 744.200, 4.876),
            (915.563, 910.594, 4.969),
            (1255.410, 1250.534, 4.876),
            (1429.513, 1408.848, 20.666),
        ]

        result = estimate_spectral_tempo(points, DualMakerConfig())

        self.assertTrue(result["reliable"])
        self.assertAlmostEqual(float(result["speed_factor"]), 1.0, delta=0.003)

    def test_source_aware_output_mapping_syncs_dual_original_and_keeps_master_dub(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_english = Track(1, "audio", 0, language_ietf="en")
            master_dub = Track(2, "audio", 1, language_ietf="pt")
            dual_dub = Track(1, "audio", 0, language_ietf="pt")
            dual_english = Track(2, "audio", 1, language_ietf="en")
            master = MediaAsset(root / "master.mkv", 100, [master_english, master_dub])
            dual = MediaAsset(root / "dual.mkv", 100, [dual_dub, dual_english])
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("movie", "movie", 2026),
                output=root / "output.mkv",
                normal_original=master_english,
                dual_original=dual_english,
                dub_tracks=[master_dub, dual_dub],
                normal_subtitles=[],
                dual_subtitles=[],
                dub_selections=[
                    AudioTrackSelection("master", master_dub),
                    AudioTrackSelection("dual", dual_dub),
                ],
                output_original=AudioTrackSelection("dual", dual_english),
                fps=FPSDecision(
                    required=True,
                    approved=True,
                    apply_speed_correction=True,
                    proposed_speed_factor=0.96,
                ),
            )
            runner = FakeRunner()
            with patch("dualmaker.sync.adapter.first_packet_pts", return_value=0.0):
                result = MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(fps_spectral_tempo_probe=False),
                )
            mapping = runner.command[runner.command.index("--output-audio-mapping") + 1]
            self.assertEqual(mapping, "1:1,0:0,0:1")
            self.assertEqual(result.output_audio_mapping, [(1, 1), (0, 0), (0, 1)])
            self.assertEqual(result.stage_original_index, 2)
            self.assertIn("--framerate-speed-factor", runner.command)
            self.assertEqual(result.speed_correction_factor, 0.96)

    def test_reference_audio_pts_difference_is_diagnostic_not_a_dub_delay(self) -> None:
        """A master AAC priming PTS must not shift every rendered DUAL dub."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_original = Track(1, "audio", 0, language_ietf="en")
            dual_dub = Track(1, "audio", 0, language_ietf="pt-BR")
            dual_original = Track(2, "audio", 1, language_ietf="en")
            master = MediaAsset(root / "master.mkv", 100, [master_original])
            dual = MediaAsset(root / "dual.mkv", 100, [dual_dub, dual_original])
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("episode", "show", season=1, episodes=(1,)),
                output=root / "output.mkv",
                normal_original=master_original,
                dual_original=dual_original,
                dub_tracks=[dual_dub],
                normal_subtitles=[],
                dual_subtitles=[],
            )
            runner = FakeRunner()
            # DUAL reference begins at zero, while the master reference has a
            # negative packet PTS typical of codec priming. The dub itself is
            # on the DUAL video timeline at zero.
            with patch(
                "dualmaker.sync.adapter.first_packet_pts",
                side_effect=[0.0, -0.4, 0.0],
            ):
                result = MilksyncAdapter(runner).synchronize(  # type: ignore[arg-type]
                    plan,
                    normal_path=master.path,
                    dual_path=dual.path,
                    temp_dir=root / "work",
                    config=DualMakerConfig(fps_spectral_tempo_probe=False),
                )

            self.assertNotIn("--adjust-delay", runner.command)
            self.assertAlmostEqual(result.observed_reference_pts_offset or 0, -0.4)
            self.assertEqual(result.container_delay_adjustment, 0.0)
            self.assertEqual(result.effective_delay_adjustment, 0.0)

    def test_dual_sidecar_uses_the_existing_milksync_shift_map(self) -> None:
        """External text subtitles must follow the DUAL source timeline too."""
        project_temp = Path.cwd() / ".test-work"
        project_temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=project_temp) as directory:
            root = Path(directory)
            sidecar = root / "dual.pt-BR.srt"
            legacy_windows_1252 = b"1\n00:00:02,000 --> 00:00:04,000\nOl\xe1\n"
            sidecar.write_bytes(legacy_windows_1252)
            master_english = Track(1, "audio", 0, language_ietf="en")
            dual_english = Track(1, "audio", 0, language_ietf="en")
            master = MediaAsset(root / "master.mkv", 10, [master_english])
            dual = MediaAsset(root / "dual.mkv", 10, [dual_english])
            plan = JobPlan(
                normal=master,
                dual=dual,
                identity=ContentIdentity("movie", "movie", 2026),
                output=root / "output.mkv",
                normal_original=master_english,
                dual_original=dual_english,
                dub_tracks=[],
                normal_subtitles=[],
                dual_subtitles=[],
                sidecar_subtitles=[SidecarSubtitle(sidecar, "dual", "pt-BR")],
            )
            sync = SyncResult(
                path=root / "sync.mkv",
                report_path=root / "sync.json",
                sync_buckets=[(0.0, 10.0, 0.5)],
            )

            MilksyncAdapter().synchronize_sidecars(
                plan,
                sync,
                normal_path=master.path,
                dual_path=dual.path,
                temp_dir=root / "work",
                config=DualMakerConfig(only_delta=True),
            )

            self.assertEqual(len(sync.sidecar_subtitles), 1)
            self.assertEqual(sidecar.read_bytes(), legacy_windows_1252)
            synced = sync.sidecar_subtitles[0].path
            self.assertTrue(synced.is_file())
            self.assertTrue(synced.read_bytes().startswith(b"\xef\xbb\xbf"))
            synchronized_text = synced.read_text(encoding="utf-8-sig")
            self.assertIn("00:00:02,500", synchronized_text)
            self.assertIn("Olá", synchronized_text)


if __name__ == "__main__":
    unittest.main()
