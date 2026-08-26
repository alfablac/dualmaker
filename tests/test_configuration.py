from __future__ import annotations

import grp
import os
import stat
import tempfile
import unittest
from pathlib import Path

from dualmaker.configuration import (
    CONFIG_SETTING_COMMENTS,
    _commented_default_config,
    _default_config_document,
    default_user_config_path,
    initialize_config_file,
    load_configuration,
    refresh_config_file,
    validate_configuration,
)
from dualmaker.errors import ConfigurationError
from dualmaker.models import DualMakerConfig
from dualmaker.mux import _set_output_group


class ConfigurationTests(unittest.TestCase):
    def test_yaml_config_is_loaded_and_environment_and_cli_still_win(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "settings.yml"
            config_file.write_text(
                """
dualmaker:
  tag: from-file
  recap_window: 90
paths:
  required_paths: []
interface:
  color: never
""".strip(),
                encoding="utf-8",
            )
            config = load_configuration(
                {"tag": "from-cli"},
                config_path=config_file,
                environment={"DUALMAKER_COLOR": "always"},
                cwd=root,
            )
            self.assertEqual(config.tag, "from-cli")
            self.assertEqual(config.recap_window, 90)
            self.assertEqual(config.color, "always")

    def test_default_user_config_path_can_be_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                default_user_config_path({"DUALMAKER_CONFIG_HOME": str(root)}),
                root / "config.yml",
            )

    def test_initialize_config_is_private_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user" / "config.yml"
            initialized, created = initialize_config_file(target)
            self.assertTrue(created)
            self.assertEqual(initialized, target)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertIn("output_group: null", target.read_text(encoding="utf-8"))
            self.assertIn(
                "allow_experimental_fps_sync: false",
                target.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "experimental_dub_resync: true",
                target.read_text(encoding="utf-8"),
            )
            self.assertIn("preferred_original_source: master", target.read_text(encoding="utf-8"))
            self.assertIn("subtitle_policy: prefer-master", target.read_text(encoding="utf-8"))
            self.assertIn("sidecar_dual_language: pt-BR", target.read_text(encoding="utf-8"))
            self.assertIn("sidecar_language_overrides: []", target.read_text(encoding="utf-8"))
            self.assertIn("allow_tvrip_segment_sync: false", target.read_text(encoding="utf-8"))
            self.assertIn("tvrip_fallback: ask", target.read_text(encoding="utf-8"))
            self.assertIn("dub_gap_fallback: original", target.read_text(encoding="utf-8"))
            self.assertIn(
                "# For verified master-only dub gaps:", target.read_text(encoding="utf-8")
            )
            self.assertIn(
                "# Optional per-run paths; uncomment only when you want these defaults:",
                target.read_text(encoding="utf-8"),
            )

            target.write_text("dualmaker:\n  tag: personal\n", encoding="utf-8")
            _, created_again = initialize_config_file(target)
            self.assertFalse(created_again)
            self.assertEqual(target.read_text(encoding="utf-8"), "dualmaker:\n  tag: personal\n")

    def test_generated_config_documents_every_persistent_setting(self) -> None:
        document = _default_config_document()
        expected = {
            f"{section}.{key}"
            for section, values in document.items()
            for key in values
        }
        self.assertTrue(expected <= set(CONFIG_SETTING_COMMENTS))
        rendered = _commented_default_config()
        for description in CONFIG_SETTING_COMMENTS.values():
            self.assertIn(description.split()[0], rendered)

    def test_refresh_config_preserves_values_and_creates_private_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.yml"
            original = "dualmaker:\n  tag: RiPER\nfeatures:\n  trim_recap: false\n"
            target.write_text(original, encoding="utf-8")
            refreshed, backup = refresh_config_file(target)
            self.assertEqual(refreshed, target)
            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            updated = target.read_text(encoding="utf-8")
            self.assertIn("tag: RiPER", updated)
            self.assertIn("trim_recap: false", updated)
            self.assertIn("dub_gap_fallback: original", updated)
            self.assertIn("# For verified master-only dub gaps:", updated)
            config = load_configuration({}, config_path=target, environment={}, cwd=target.parent)
            self.assertEqual(config.tag, "RiPER")
            self.assertFalse(config.trim_recap)

    def test_bootstrap_creates_and_loads_user_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_home = root / "config-home"
            config = load_configuration(
                {},
                environment={"DUALMAKER_CONFIG_HOME": str(config_home)},
                cwd=root,
                bootstrap_user_config=True,
            )
            self.assertEqual(config.config_file, config_home / "config.yml")
            self.assertTrue(config.config_file.is_file())
            self.assertEqual(config.config_sources["tag"], f"config:{config.config_file}")

    def test_precedence_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "settings.toml"
            config_file.write_text(
                """
[dualmaker]
tag = "from-file"
recap_window = 90

[paths]
path = "."
temp_dir = ".work"

[interface]
color = "never"
""".strip(),
                encoding="utf-8",
            )
            config = load_configuration(
                {"tag": "from-cli"},
                config_path=config_file,
                environment={"DUALMAKER_TAG": "from-env"},
                cwd=root,
            )
            self.assertEqual(config.tag, "from-cli")
            self.assertEqual(config.recap_window, 90)
            self.assertEqual(config.temp_dir, root / ".work")
            self.assertEqual(config.config_sources["tag"], "command-line")

    def test_environment_overrides_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "settings.toml"
            config_file.write_text("[dualmaker]\ntag = 'file'\n", encoding="utf-8")
            config = load_configuration(
                {},
                config_path=config_file,
                environment={
                    "DUALMAKER_TAG": "environment",
                    "DUALMAKER_RECONCILE_AV": "false",
                    "DUALMAKER_AV_TOLERANCE_MS": "125",
                    "DUALMAKER_ALLOW_EXPERIMENTAL_FPS_SYNC": "true",
                    "DUALMAKER_EXPERIMENTAL_DUB_RESYNC": "false",
                    "DUALMAKER_EXPERIMENTAL_DUB_RESYNC_MIN_CONFIDENCE": "0.9",
                    "DUALMAKER_MILKSYNC_MAX_THREADS": "3",
                    "DUALMAKER_MILKSYNC_CHROMA_WORKERS": "2",
                    "DUALMAKER_MILKSYNC_MAX_COST_MATRIX_CELLS": "8000000",
                    "DUALMAKER_FPS_VALIDATION_POSITIONS": "0.1,0.4,0.9",
                    "DUALMAKER_ORIGINAL_TRACK": "dual:4",
                    "DUALMAKER_SUBTITLE_POLICY": "exact-union",
                    "DUALMAKER_SIDECAR_DUAL_LANGUAGE": "pt-PT",
                    "DUALMAKER_SIDECAR_LANGUAGES": "episode.DUAL.srt=pt-BR",
                },
                cwd=root,
            )
            self.assertEqual(config.tag, "environment")
            self.assertIn("DUALMAKER_TAG", config.config_sources["tag"])
            self.assertFalse(config.reconcile_av)
            self.assertEqual(config.av_tolerance_ms, 125)
            self.assertTrue(config.allow_experimental_fps_sync)
            self.assertFalse(config.experimental_dub_resync)
            self.assertEqual(config.experimental_dub_resync_min_confidence, 0.9)
            self.assertEqual(config.milksync_max_threads, 3)
            self.assertEqual(config.milksync_chroma_workers, 2)
            self.assertEqual(config.milksync_max_cost_matrix_cells, 8_000_000)
            self.assertEqual(config.fps_validation_positions, (0.1, 0.4, 0.9))
            self.assertEqual(config.original_track_selector, "dual:4")
            self.assertEqual(config.subtitle_policy, "exact-union")
            self.assertEqual(config.sidecar_dual_language, "pt-PT")
            self.assertEqual(config.sidecar_language_overrides, ("episode.DUAL.srt=pt-BR",))

    def test_invalid_fps_policy_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_configuration(
                {
                    "path": root,
                    "compatible_fps_pairs": ("twenty-five",),
                    "fps_validation_positions": (0.5,),
                },
                environment={},
                cwd=root,
            )
            with self.assertRaisesRegex(ConfigurationError, "compatible_fps_pairs"):
                validate_configuration(config, validate_binaries=False)

    def test_tvrip_section_and_environment_are_centralized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "settings.yml"
            config_file.write_text(
                "tvrip:\n  tvrip_min_coverage: 0.9\n  tvrip_fallback: silence\n"
                "  tvrip_continue_on_validation_warnings: false\n",
                encoding="utf-8",
            )
            config = load_configuration(
                {},
                config_path=config_file,
                environment={
                    "DUALMAKER_ALLOW_TVRIP_SEGMENT_SYNC": "true",
                    "DUALMAKER_TVRIP_CONTINUE_ON_VALIDATION_WARNINGS": "true",
                },
                cwd=root,
            )
            self.assertTrue(config.allow_tvrip_segment_sync)
            self.assertEqual(config.tvrip_min_coverage, 0.9)
            self.assertEqual(config.tvrip_fallback, "silence")
            self.assertTrue(config.tvrip_continue_on_validation_warnings)
            validate_configuration(config, validate_binaries=False)

    def test_tvrip_local_acoustic_guard_is_configurable_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "settings.yml"
            config_file.write_text(
                """tvrip:
  tvrip_acoustic_segment_validation: false
  tvrip_acoustic_segment_window_seconds: 4
  tvrip_acoustic_segment_min_seconds: 2
  tvrip_acoustic_segment_max_gap_seconds: 45
  tvrip_acoustic_segment_rejection_padding_seconds: 7
  tvrip_acoustic_segment_min_similarity: 0.6
  tvrip_acoustic_segment_require_proof: false
""",
                encoding="utf-8",
            )
            config = load_configuration(
                {},
                config_path=config_file,
                environment={"DUALMAKER_TVRIP_ACOUSTIC_SEGMENT_VALIDATION": "true"},
                cwd=root,
            )
            self.assertTrue(config.tvrip_acoustic_segment_validation)
            self.assertEqual(config.tvrip_acoustic_segment_window_seconds, 4)
            self.assertEqual(config.tvrip_acoustic_segment_min_seconds, 2)
            self.assertEqual(config.tvrip_acoustic_segment_max_gap_seconds, 45)
            self.assertEqual(config.tvrip_acoustic_segment_rejection_padding_seconds, 7)
            self.assertEqual(config.tvrip_acoustic_segment_min_similarity, 0.6)
            self.assertFalse(config.tvrip_acoustic_segment_require_proof)
            validate_configuration(config, validate_binaries=False)

            invalid = DualMakerConfig(
                path=root,
                tvrip_acoustic_segment_window_seconds=1,
                tvrip_acoustic_segment_min_seconds=2,
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "tvrip_acoustic_segment_window_seconds must be at least",
            ):
                validate_configuration(invalid, validate_binaries=False)

    def test_dub_gap_policy_can_be_configured_by_file_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "settings.yml"
            config_file.write_text(
                "features:\n  dub_gap_fallback: silence\n  dub_gap_min_seconds: 2.5\n",
                encoding="utf-8",
            )
            config = load_configuration(
                {},
                config_path=config_file,
                environment={
                    "DUALMAKER_DUB_GAP_FALLBACK": "original",
                    "DUALMAKER_DUB_GAP_MIN_COVERAGE": "0.9",
                },
                cwd=root,
            )
            self.assertEqual(config.dub_gap_fallback, "original")
            self.assertEqual(config.dub_gap_min_seconds, 2.5)
            self.assertEqual(config.dub_gap_min_coverage, 0.9)
            validate_configuration(config, validate_binaries=False)

    def test_enforced_paths_reject_outside_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            config = load_configuration(
                {
                    "path": allowed,
                    "allowed_paths": (allowed,),
                    "enforce_paths": True,
                    "output_dir": outside,
                },
                environment={},
                cwd=root,
            )
            with self.assertRaisesRegex(ConfigurationError, "outside configured allowed_paths"):
                validate_configuration(config)

    def test_missing_configured_binary_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "not-ffmpeg"
            config = load_configuration(
                {"path": root, "binaries": {"ffmpeg": str(missing)}},
                environment={},
                cwd=root,
            )
            with self.assertRaisesRegex(ConfigurationError, "ffmpeg binary does not exist"):
                validate_configuration(config)

    def test_current_group_can_be_required(self) -> None:
        current_group = grp.getgrgid(os.getegid()).gr_name
        config = load_configuration(
            {
                "path": Path.cwd(),
                "required_group": current_group,
                "output_group": current_group,
            },
            environment={},
            cwd=Path.cwd(),
        )
        validate_configuration(config)

    def test_output_group_is_applied_to_staged_file(self) -> None:
        current_group = grp.getgrgid(os.getegid()).gr_name
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.mkv"
            output.write_bytes(b"staged")
            _set_output_group(output, current_group)
            self.assertEqual(output.stat().st_gid, os.getegid())


if __name__ == "__main__":
    unittest.main()
