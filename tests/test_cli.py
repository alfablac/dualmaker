from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from dualmaker.cli import main


class CliTests(unittest.TestCase):
    def test_help_documents_primary_forms(self) -> None:
        result = CliRunner().invoke(main, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("dualmaker /media/shows --recursive", result.output)
        self.assertIn("--dual FILE", result.output)
        self.assertIn("--check-deps", result.output)
        self.assertIn("--init-config", result.output)
        self.assertIn("--refresh-config", result.output)
        self.assertIn("--original-track", result.output)
        self.assertIn("--allow-experimental-fps-sync", result.output)
        self.assertIn("--fps-validation-position", result.output)
        self.assertIn("--tvrip FILE", result.output)
        self.assertIn("--allow-tvrip-segment-sync", result.output)
        self.assertIn("--tvrip-fallback", result.output)
        self.assertIn("--tvrip-continue-on-validation-warnings", result.output)
        self.assertIn("--dub-gap-fallback", result.output)

    def test_init_config_creates_yaml_without_dependency_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config" / "config.yml"
            result = CliRunner().invoke(
                main,
                ["--init-config", "--config", str(target), "--json"],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload, {"status": "created", "config": str(target.resolve())})
            self.assertTrue(target.is_file())

            second = CliRunner().invoke(
                main,
                ["--init-config", "--config", str(target), "--json"],
            )
            self.assertEqual(second.exit_code, 0, second.output)
            self.assertEqual(json.loads(second.output)["status"], "exists")

    def test_refresh_config_keeps_values_and_returns_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.yml"
            target.write_text("dualmaker:\n  tag: RiPER\n", encoding="utf-8")
            result = CliRunner().invoke(
                main,
                ["--refresh-config", "--config", str(target), "--json"],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "refreshed")
            self.assertEqual(payload["config"], str(target.resolve()))
            self.assertTrue(Path(payload["backup"]).is_file())
            self.assertIn("tag: RiPER", target.read_text(encoding="utf-8"))

    def test_explicit_inputs_must_be_supplied_together(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mkv") as dual:
            result = CliRunner().invoke(main, ["--dual", dual.name])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--dual and --normal must be supplied together", result.output)

    def test_empty_folder_dry_run_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            result = CliRunner().invoke(
                main,
                [str(root), "--dry-run", "--report", str(report)],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["assets"], [])
            self.assertEqual(payload["results"], [])
            self.assertEqual(payload["skipped"], [])

    def test_invalid_dub_language_is_a_clean_usage_error(self) -> None:
        result = CliRunner().invoke(main, ["--dub-language", "es", "--dry-run"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("dub_language must be Portuguese", result.output)

    def test_json_mode_emits_one_machine_readable_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            result = CliRunner().invoke(
                main,
                [str(root), "--dry-run", "--json", "--report", str(report)],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["report"], str(report.resolve()))
            self.assertEqual(payload["results"], [])

    def test_dependency_failure_is_json_in_json_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-ffmpeg"
            result = CliRunner().invoke(
                main,
                ["--check-deps", "--json", "--ffmpeg", str(missing)],
            )
            self.assertEqual(result.exit_code, 1, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "error")
            self.assertFalse(payload["dependencies"]["ffmpeg"]["ok"])

    def test_interactive_mode_requires_a_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = CliRunner().invoke(main, [directory, "--interactive", "--dry-run"])
            self.assertEqual(result.exit_code, 2)
            self.assertIn("requires an attached terminal", result.output)


if __name__ == "__main__":
    unittest.main()
