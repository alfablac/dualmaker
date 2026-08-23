from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from dualmaker.errors import AmbiguousPairError, ConfigurationError
from dualmaker.languages import base_language, languages_match, normalize_language
from dualmaker.matching import discover_mkvs, find_pair_candidates
from dualmaker.models import DualMakerConfig, FrameRate, MediaAsset, PairCandidate, Track
from dualmaker.mux import (
    TrackRef,
    _bitmap_packet_delay_ms,
    _dedupe_subtitles,
    _dedupe_subtitles_by_presentation,
    _has_master_subtitle_replacement,
    _retime_pgs_bytes,
)
from dualmaker.naming import choose_conflict_path, make_output_basename, parse_identity
from dualmaker.ordering import order_subtitles, preferred_portuguese_forced
from dualmaker.planning import create_job_plan
from dualmaker.preprocess import envelope_similarity
from dualmaker.sidecars import (
    discover_pair_sidecars,
    resolve_sidecar_subtitles,
    sidecar_languages_from_overrides,
)


def track(
    track_id: int,
    kind: str,
    index: int,
    language: str = "und",
    *,
    title: str = "",
    default: bool = False,
    forced: bool = False,
    hearing_impaired: bool = False,
    commentary: bool = False,
    channels: int | None = None,
    bitrate: int | None = None,
    sample_rate: int | None = None,
    codec_id: str = "",
    duration: float | None = None,
) -> Track:
    return Track(
        id=track_id,
        kind=kind,  # type: ignore[arg-type]
        type_index=index,
        language=language,
        language_ietf=language,
        title=title,
        default=default,
        forced=forced,
        hearing_impaired=hearing_impaired,
        commentary=commentary,
        channels=channels,
        bitrate=bitrate,
        sample_rate=sample_rate,
        codec_id=codec_id,
        duration=duration,
    )


def asset(path: str, duration: float, tracks: list[Track]) -> MediaAsset:
    item = MediaAsset(Path(path), duration, tracks, frame_rate=FrameRate(24, 1))
    item.identity = parse_identity(path)
    return item


class NamingTests(unittest.TestCase):
    def test_movie_examples_match_across_release_tokens(self) -> None:
        dual = parse_identity(
            "Minions.and.Monsters.2026.1080p.iT.WEB-DL.DDP5.1.Atmos.H.264.DUAL-C76.mkv"
        )
        normal = parse_identity(
            "Minions.and.Monsters.2026.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264-BYNDR.mkv"
        )
        self.assertEqual(dual.key, normal.key)
        self.assertEqual(dual.title, "minions and monsters")

    def test_episode_resolution_does_not_change_identity(self) -> None:
        left = parse_identity("Name.S01E01.1080p.AMZN.WEB-DL-GROUP.mkv")
        right = parse_identity("Name.S01E01.2160p.MA.WEB-DL-DUAL.mkv")
        self.assertEqual(left.key, right.key)
        self.assertEqual(left.episodes, (1,))

    def test_episode_title_and_provider_do_not_change_identity(self) -> None:
        master = parse_identity(
            "Furious.S01E01.The.Gorgon.2160p.HULU.WEB-DL.DDP5.1.H.265-FLUX.mkv"
        )
        dual = parse_identity(
            "Furious.S01E01.The.Gorgon.1080p.DSNP.WEB-DL.DDP5.1.H.264.DUAL-RiPER.mkv"
        )
        self.assertEqual(master.key, dual.key)
        self.assertEqual(master.title, "furious")

    def test_library_episode_year_and_release_episode_identity_match(self) -> None:
        dual = parse_identity(
            "Warehouse 13 (2009) - S03E01 - The New Guy "
            "(1080p BluRay x265 Panda) DUAL-JK.mkv"
        )
        master = parse_identity("warehouse.13.s03e01.1080p.bluray.x264-shortbrehd.mkv")
        self.assertEqual(dual.key, master.key)
        self.assertEqual(dual.title, "warehouse 13")

    def test_multi_episode_identity(self) -> None:
        identity = parse_identity("Name.S02E03-E04.1080p.WEB-DL-GROUP.mkv")
        self.assertEqual(identity.season, 2)
        self.assertEqual(identity.episodes, (3, 4))

    def test_output_name_replaces_group(self) -> None:
        value = make_output_basename(
            "Minions.and.Monsters.2026.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264-BYNDR.mkv"
        )
        self.assertEqual(
            value,
            "Minions.and.Monsters.2026.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264.DUAL-alfaHD.mkv",
        )

    def test_conflict_increments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "Movie.DUAL-alfaHD.mkv"
            first.touch()
            second = choose_conflict_path(first)
            self.assertEqual(second.name, "Movie.DUAL-alfaHD.2.mkv")

    def test_source_group_matching_output_tag_is_not_filtered_as_generated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Furious.S01E01.1080p.DSNP.WEB-DL.DUAL-RiPER.mkv"
            master = root / "Furious.S01E01.2160p.HULU.WEB-DL-FLUX.mkv"
            generated_directory = root / "dualmaker-output"
            generated_directory.mkdir()
            generated = generated_directory / "Furious.S01E01.DUAL-RiPER.mkv"
            source.touch()
            master.touch()
            generated.touch()

            discovered = discover_mkvs(
                root,
                recursive=True,
                tag="RiPER",
                ignored_dir_names=(),
                ignored_paths=(generated_directory,),
            )

            self.assertEqual(discovered, [source, master])


class LanguageTests(unittest.TestCase):
    def test_common_aliases(self) -> None:
        self.assertEqual(normalize_language("pob"), "pt-BR")
        self.assertEqual(base_language("pt_BR"), "pt")
        self.assertTrue(languages_match("eng", "en-US"))

    def test_iso_639_2_aliases_are_stable_after_mkvtoolnix_adds_ietf_tags(self) -> None:
        self.assertEqual(normalize_language("chi"), "zh")
        self.assertEqual(normalize_language("dan"), "da")
        self.assertEqual(normalize_language("spa"), "es")
        self.assertEqual(normalize_language("fre"), "fr")
        self.assertEqual(normalize_language("kor"), "ko")
        self.assertEqual(normalize_language("nor"), "no")
        self.assertEqual(normalize_language("swe"), "sv")
        self.assertEqual(normalize_language("chi-Hant"), "zh-Hant")


class MatchingAndPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        video = track(0, "video", 0)
        self.normal = asset(
            "/shows/Name.S01E01.2160p.MA.WEB-DL-GROUP.mkv",
            1800,
            [video, track(1, "audio", 0, "en", default=True, channels=6, bitrate=640000)],
        )
        self.dual = asset(
            "/shows/Name.S01E01.1080p.AMZN.WEB-DL.DUAL-C76.mkv",
            1801,
            [
                video,
                track(1, "audio", 0, "pt-BR", default=True, channels=6, bitrate=640000),
                track(2, "audio", 1, "pt", channels=2, bitrate=192000),
                track(3, "audio", 2, "pt-BR", title="Director Commentary", commentary=True),
                track(4, "audio", 3, "en", channels=6, bitrate=640000),
            ],
        )

    def test_roles_are_metadata_driven(self) -> None:
        candidates, skipped = find_pair_candidates([self.dual, self.normal])
        self.assertFalse(skipped)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].normal.path, self.normal.path)
        self.assertEqual(candidates[0].shared_original_languages, ("en",))

    def test_episode_pairs_use_natural_numeric_order(self) -> None:
        """A season must display/process E2 before E10 even with unpadded names."""

        assets: list[MediaAsset] = []
        for episode in (1, 10, 11, 2, 20, 21):
            assets.extend(
                (
                    asset(
                        f"/shows/Name.S01E{episode}.2160p.MA.WEB-DL-GROUP.mkv",
                        1800,
                        [track(0, "video", 0), track(1, "audio", 0, "en")],
                    ),
                    asset(
                        f"/shows/Name.S01E{episode}.1080p.AMZN.WEB-DL.DUAL-GROUP.mkv",
                        1800,
                        [
                            track(0, "video", 0),
                            track(1, "audio", 0, "pt-BR"),
                            track(2, "audio", 1, "en"),
                        ],
                    ),
                )
            )

        candidates, skipped = find_pair_candidates(assets)

        self.assertFalse(skipped)
        self.assertEqual(
            [candidate.identity.episodes for candidate in candidates],
            [(1,), (2,), (10,), (11,), (20,), (21,)],
        )

    def test_plan_keeps_all_non_commentary_dubs(self) -> None:
        candidate = find_pair_candidates([self.dual, self.normal])[0][0]
        config = DualMakerConfig(output_dir=Path("/output"), trim_recap=False)
        plan = create_job_plan(candidate, config)
        self.assertEqual([item.id for item in plan.dub_tracks], [1, 2])
        self.assertEqual(plan.normal_original.id, 1)
        self.assertEqual(plan.dual_original.id, 4)
        self.assertEqual(plan.output.parent, Path("/output"))

    def test_higher_quality_dual_original_can_beat_master_preference(self) -> None:
        self.normal.audio_tracks[0].codec_id = "A_EAC3"
        self.normal.audio_tracks[0].sample_rate = 48000
        self.normal.audio_tracks[0].duration = 1800
        self.dual.audio_tracks[-1].codec_id = "A_TRUEHD"
        self.dual.audio_tracks[-1].sample_rate = 48000
        self.dual.audio_tracks[-1].bitrate = 4_000_000
        self.dual.audio_tracks[-1].duration = 1801
        candidate = find_pair_candidates([self.dual, self.normal])[0][0]
        plan = create_job_plan(candidate, DualMakerConfig(output_dir=Path("/output")))
        self.assertEqual(plan.resolved_original.source, "dual")
        self.assertEqual(plan.resolved_original.track.id, 4)
        self.assertIn("codec A_TRUEHD", plan.resolved_original.reasons)

    def test_equivalent_original_tracks_require_a_choice_without_source_priority(self) -> None:
        for item in (self.normal.audio_tracks[0], self.dual.audio_tracks[-1]):
            item.codec_id = "A_EAC3"
            item.channels = 6
            item.bitrate = 640000
            item.sample_rate = 48000
            item.duration = 1800
            item.default = False
        candidate = find_pair_candidates([self.dual, self.normal])[0][0]
        config = DualMakerConfig(
            output_dir=Path("/output"),
            preferred_original_source="quality",
        )
        with self.assertRaisesRegex(AmbiguousPairError, "Equivalent original tracks"):
            create_job_plan(candidate, config)

    def test_source_aware_override_selects_dual_original(self) -> None:
        candidate = find_pair_candidates([self.dual, self.normal])[0][0]
        config = DualMakerConfig(
            output_dir=Path("/output"),
            original_track_selector="dual:4",
            dub_track_selectors=("dual:2", "dual:1"),
        )
        plan = create_job_plan(candidate, config)
        self.assertEqual(plan.resolved_original.label, "dual:4")
        self.assertEqual([item.label for item in plan.resolved_dubs], ["dual:2", "dual:1"])

    def test_dubs_from_both_selected_sources_are_retained(self) -> None:
        self.normal.tracks.append(
            track(5, "audio", 1, "pt-BR", channels=8, bitrate=1_000_000, codec_id="A_EAC3")
        )
        candidate = find_pair_candidates([self.dual, self.normal])[0][0]
        plan = create_job_plan(candidate, DualMakerConfig(output_dir=Path("/output")))
        self.assertEqual(
            {item.label for item in plan.resolved_dubs},
            {"master:5", "dual:1", "dual:2"},
        )


class OrderingTests(unittest.TestCase):
    def test_subtitle_policy(self) -> None:
        tracks = [
            track(10, "subtitles", 0, "fr", title="French"),
            track(11, "subtitles", 1, "en", forced=True, title="English Forced"),
            track(12, "subtitles", 2, "pt-BR", hearing_impaired=True, title="SDH"),
            track(13, "subtitles", 3, "pt-BR", forced=True, title="Forced"),
            track(14, "subtitles", 4, "pt", forced=True, title="Forced PT"),
        ]
        ordered = order_subtitles(tracks)
        self.assertEqual([item.id for item in ordered], [13, 14, 12, 11, 10])
        self.assertEqual(preferred_portuguese_forced(tracks).id, 13)

    def test_three_letter_source_tags_keep_the_same_order_after_remux(self) -> None:
        source_tracks = [
            track(1, "subtitles", 0, "chi"),
            track(2, "subtitles", 1, "dan"),
            track(3, "subtitles", 2, "spa"),
            track(4, "subtitles", 3, "fre"),
            track(5, "subtitles", 4, "kor"),
            track(6, "subtitles", 5, "nor"),
            track(7, "subtitles", 6, "swe"),
        ]
        ordered_source = order_subtitles(source_tracks)
        remuxed_tags = {
            "chi": "zh",
            "dan": "da",
            "spa": "es",
            "fre": "fr",
            "kor": "ko",
            "nor": "no",
            "swe": "sv",
        }
        remuxed = [
            track(
                item.id,
                "subtitles",
                index,
                remuxed_tags[item.effective_language],
            )
            for index, item in enumerate(ordered_source)
        ]
        self.assertEqual(
            [item.id for item in remuxed],
            [item.id for item in order_subtitles(remuxed)],
        )

    def test_missing_portuguese_categories_do_not_make_ordering_invalid(self) -> None:
        tracks = [
            track(1, "subtitles", 0, "fr"),
            track(2, "subtitles", 1, "en"),
        ]
        self.assertEqual([item.id for item in order_subtitles(tracks)], [2, 1])


class SubtitleExtractionTests(unittest.TestCase):
    def test_deduplication_extracts_all_tracks_from_each_source_in_one_pass(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def run(self, args: tuple[object, ...], *, check: bool = True) -> SimpleNamespace:
                command = tuple(str(item) for item in args)
                self.calls.append(command)
                source = Path(command[1])
                for specification in command[3:]:
                    track_id, destination = specification.split(":", 1)
                    content = b"duplicate" if track_id in {"1", "7"} else source.name.encode()
                    Path(destination).write_bytes(content)
                return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normal = root / "normal.mkv"
            synchronized = root / "synchronized.mkv"
            normal_track = track(1, "subtitles", 0, "en")
            duplicate_track = track(7, "subtitles", 0, "en")
            unique_track = track(8, "subtitles", 1, "pt")
            references = [
                TrackRef(0, normal, normal_track, normal_track, "normal"),
                TrackRef(1, synchronized, duplicate_track, duplicate_track, "dual-synced"),
                TrackRef(1, synchronized, unique_track, unique_track, "dual-synced"),
            ]
            runner = FakeRunner()
            kept = _dedupe_subtitles(  # type: ignore[arg-type]
                references, root, runner, policy="exact-union"
            )
            self.assertEqual([reference.actual.id for reference in kept], [1, 8])
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual([len(call) - 3 for call in runner.calls], [1, 2])

    def test_master_preferred_slots_remove_overlapping_dual_subtitles(self) -> None:
        master = Path("/media/master.mkv")
        dual = Path("/media/dual-synchronized.mkv")
        master_regular = track(1, "subtitles", 0, "en")
        master_sdh = track(2, "subtitles", 1, "en", title="SDH", hearing_impaired=True)
        master_pt = track(3, "subtitles", 2, "pt-BR", title="Brazilian")
        dual_regular = track(4, "subtitles", 0, "en")
        dual_sdh = track(5, "subtitles", 1, "en", title="SDH", hearing_impaired=True)
        dual_pt = track(6, "subtitles", 2, "pt-BR", title="Brazil")
        dual_forced = track(7, "subtitles", 3, "pt-BR", title="Brazil (Forced)", forced=True)
        dual_portugal = track(8, "subtitles", 4, "pt-PT")
        references = [
            TrackRef(0, master, item, item, "normal")
            for item in (master_regular, master_sdh, master_pt)
        ] + [
            TrackRef(1, dual, item, item, "dual-synced")
            for item in (dual_regular, dual_sdh, dual_pt, dual_forced, dual_portugal)
        ]

        kept = _dedupe_subtitles_by_presentation(references)

        self.assertEqual([reference.actual.id for reference in kept], [1, 2, 3, 7, 8])


class SidecarTests(unittest.TestCase):
    def test_dual_sidecar_is_associated_and_defaults_to_portuguese_brazil(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dual_path = root / "Warehouse 13 (2009) - S03E01 DUAL-JK.mkv"
            normal_path = root / "warehouse.13.s03e01.1080p.bluray-shortbrehd.mkv"
            sidecar = dual_path.with_suffix(".srt")
            for path in (dual_path, normal_path, sidecar):
                path.touch()
            identity = parse_identity(dual_path)
            candidate = PairCandidate(
                MediaAsset(normal_path, 10, [], identity=identity),
                MediaAsset(dual_path, 10, [], identity=identity),
                identity,
                1.0,
                ("en",),
            )

            found = discover_pair_sidecars(candidate)
            self.assertEqual([(item.path, item.source) for item in found], [(sidecar, "dual")])
            with self.assertRaisesRegex(ConfigurationError, "language is required"):
                resolve_sidecar_subtitles(found, {})

            defaulted = resolve_sidecar_subtitles(found, {}, default_dual_language="pt-BR")
            self.assertEqual(defaulted[0].language, "pt-BR")

            languages = sidecar_languages_from_overrides(found, [f"{sidecar.name}=pt-BR"])
            resolved = resolve_sidecar_subtitles(found, languages)
            self.assertEqual(resolved[0].language, "pt-BR")
            self.assertEqual(resolved[0].source, "dual")


class BitmapSubtitleTests(unittest.TestCase):
    @staticmethod
    def _pgs_record(pts: int, segment_type: int = 0x80, payload: bytes = b"") -> bytes:
        import struct

        return b"PG" + struct.pack(">IIBH", pts, 0, segment_type, len(payload)) + payload

    def test_pgs_display_sets_follow_speed_and_piecewise_offsets(self) -> None:
        import struct

        source = self._pgs_record(90_000) + self._pgs_record(900_000)
        retimed, statistics = _retime_pgs_bytes(
            source,
            source_time_scale=0.96,
            sync_buckets=[(0.0, 5.0, 2.0), (5.0, 20.0, 3.0)],
            delete_buckets=[],
            timeline_adjustment_ms=125,
            source_cutoff=20.0,
        )
        first_pts = struct.unpack_from(">I", retimed, 2)[0]
        second_offset = 13
        second_pts = struct.unpack_from(">I", retimed, second_offset + 2)[0]

        self.assertAlmostEqual(first_pts / 90_000, 1 / 0.96 + 2.125, places=4)
        self.assertAlmostEqual(second_pts / 90_000, 10 / 0.96 + 3.125, places=4)
        self.assertEqual(statistics, {"kept_display_sets": 2, "dropped_display_sets": 0})

    def test_pgs_drops_deleted_and_post_video_display_sets(self) -> None:
        import struct

        source = (
            self._pgs_record(90_000)
            + self._pgs_record(540_000)
            + self._pgs_record(1_800_000)
        )
        retimed, statistics = _retime_pgs_bytes(
            source,
            source_time_scale=1.0,
            sync_buckets=[(0.0, 30.0, 0.0)],
            delete_buckets=[(5.0, 7.0)],
            timeline_adjustment_ms=0,
            source_cutoff=15.0,
        )

        self.assertEqual(struct.unpack_from(">I", retimed, 2)[0], 90_000)
        self.assertEqual(len(retimed), 13)
        self.assertEqual(statistics, {"kept_display_sets": 1, "dropped_display_sets": 2})

    def test_late_movie_edit_does_not_reject_a_bitmap_track_that_ends_before_it(self) -> None:
        # Warehouse 13 S04E12 has a 93 ms edit at 42:57, while its PGS track
        # ends at 42:37. Every subtitle packet therefore needs the initial
        # -20 ms delay despite the movie-level map containing later edits.
        delay, issue = _bitmap_packet_delay_ms(
            [1.961, 2557.055],
            [
                (0.0, 2577.600725623583, -0.02),
                (2577.6936054421767, 2579.1332426303857, -0.113),
                (2579.1332426303857, 1_000_000.0, -0.02),
            ],
            [(2577.600605442177, 2577.6936054421767)],
        )

        self.assertEqual(delay, -20)
        self.assertIsNone(issue)

    def test_bitmap_track_with_multiple_active_delays_requires_a_master_replacement(self) -> None:
        delay, issue = _bitmap_packet_delay_ms(
            [1.0, 2578.0],
            [(0.0, 2577.6, -0.02), (2577.7, 1_000_000.0, -0.113)],
            [(2577.6, 2577.7)],
        )
        bitmap = track(3, "subtitles", 0, "eng", codec_id="S_HDMV/PGS")
        master_srt = track(2, "subtitles", 0, "eng", codec_id="S_TEXT/UTF8")

        self.assertIsNone(delay)
        self.assertIn("multiple delays", issue or "")
        self.assertTrue(_has_master_subtitle_replacement(bitmap, [master_srt]))


class PreprocessTests(unittest.TestCase):
    def test_envelope_similarity_finds_small_lag(self) -> None:
        rng = np.random.default_rng(7)
        source = rng.normal(size=400).astype(np.float32)
        shifted = np.concatenate((np.zeros(8, dtype=np.float32), source[:-8]))
        self.assertGreater(envelope_similarity(source, shifted, max_lag=20), 0.9)


if __name__ == "__main__":
    unittest.main()
