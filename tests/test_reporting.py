from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dualmaker.models import ContentIdentity, JobPlan, JobResult, MediaAsset, Track
from dualmaker.reporting import archive_processed_inputs, build_report_summary, write_report


def _plan(root: Path) -> JobPlan:
    dual = MediaAsset(root / "show.DUAL.mkv", 10.0, [])
    normal = MediaAsset(root / "show.master.mkv", 10.0, [])
    original = Track(1, "audio", 0, language="en", language_ietf="en")
    dub = Track(2, "audio", 0, language="pt", language_ietf="pt")
    return JobPlan(
        normal=normal,
        dual=dual,
        identity=ContentIdentity("movie", "show"),
        output=root / "output.mkv",
        normal_original=original,
        dual_original=original,
        dub_tracks=[dub],
        normal_subtitles=[],
        dual_subtitles=[],
    )


class ReportingTests(unittest.TestCase):
    def test_summary_is_written_first_and_reports_problems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            result = JobResult(
                status="success",
                message="Created",
                plan=_plan(root),
                validation={
                    "experimental_fps_validation": {
                        "validated": False,
                        "reason": "FPS evidence was inconclusive",
                    }
                },
            )
            write_report(
                report,
                {"summary": build_report_summary([result], [], None), "version": "0.9.5"},
            )
            raw = report.read_text(encoding="utf-8")
            self.assertTrue(raw.lstrip().startswith('{\n  "summary"'))
            self.assertEqual(json.loads(raw)["summary"]["problem_count"], 1)

    def test_successful_inputs_move_to_processed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.dual.path.write_bytes(b"dual")
            plan.normal.path.write_bytes(b"normal")
            result = JobResult(status="success", plan=plan)

            archived = archive_processed_inputs([result], root)

            self.assertEqual({item["status"] for item in archived}, {"moved"})
            self.assertFalse((root / "show.DUAL.mkv").exists())
            self.assertFalse((root / "show.master.mkv").exists())
            self.assertTrue((root / "processed" / "show.DUAL.mkv").is_file())
            self.assertTrue((root / "processed" / "show.master.mkv").is_file())

    def test_summary_flags_high_risk_fps_and_low_sync_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _plan(root)
            plan.fps.validation["best_effort_fps_fallback"] = {
                "selected_hypothesis": "fps_ratio",
                "selected_speed_factor": 0.8,
            }
            result = JobResult(
                status="success",
                plan=plan,
                validation={"synchronization": {"sync_coverage": 0.67, "delete_buckets": [(0, 40)]}},
            )

            summary = build_report_summary([result], [], None)

            messages = [problem["message"] for problem in summary["problems"]]
            self.assertTrue(any("High-risk fallback" in message for message in messages))
            self.assertTrue(any("67.0%" in message for message in messages))
            self.assertTrue(any("40.0s" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
