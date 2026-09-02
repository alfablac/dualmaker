from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import JobResult


def default_report_path(output_dir: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    first = output_dir / f"dualmaker-report-{timestamp}.json"
    if not first.exists():
        return first
    for number in range(2, 100_000):
        candidate = output_dir / f"dualmaker-report-{timestamp}.{number}.json"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not allocate a report name in {output_dir}")


def write_report(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        # Keep the human-facing summary at the top of the report. The remaining
        # fields intentionally retain their construction order as well.
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def archive_processed_inputs(
    results: list[JobResult],
    root: Path,
) -> list[dict[str, str]]:
    """Move successfully processed source pairs below ``root/processed``."""

    successful = [result for result in results if result.status == "success" and result.plan]
    if not successful:
        return []
    destination_root = root.expanduser().resolve() / "processed"
    destination_root.mkdir(parents=True, exist_ok=True)
    archived: list[dict[str, str]] = []
    for result in successful:
        assert result.plan is not None
        for side in ("dual", "normal"):
            asset = getattr(result.plan, side)
            source = asset.path.expanduser().resolve()
            if not source.is_file():
                archived.append({"side": side, "source": str(source), "status": "missing"})
                continue
            destination = destination_root / source.name
            if destination.exists():
                stem, suffix = source.stem, source.suffix
                for number in range(2, 100_000):
                    candidate = destination_root / f"{stem}.{number}{suffix}"
                    if not candidate.exists():
                        destination = candidate
                        break
                else:
                    archived.append(
                        {"side": side, "source": str(source), "status": "conflict"}
                    )
                    continue
            try:
                shutil.move(str(source), str(destination))
            except OSError as exc:
                archived.append(
                    {
                        "side": side,
                        "source": str(source),
                        "status": "error",
                        "message": str(exc),
                    }
                )
                continue
            asset.path = destination
            archived.append(
                {"side": side, "source": str(source), "destination": str(destination), "status": "moved"}
            )
    return archived


def build_report_summary(
    results: list[JobResult],
    skipped: list[str],
    cancelled: str | None,
) -> dict[str, Any]:
    """Build a compact, human-first summary for batch and single-job reports."""

    counts = {status: sum(result.status == status for result in results) for status in (
        "planned", "success", "skipped", "failed"
    )}
    problems: list[dict[str, Any]] = []
    for message in skipped:
        problems.append({"kind": "skipped", "message": message})
    if cancelled:
        problems.append({"kind": "cancelled", "message": cancelled})
    for result in results:
        label = result.plan.identity.title if result.plan else "job"
        if result.status in {"failed", "skipped"}:
            problems.append({"kind": result.status, "job": label, "message": result.message})
        if result.plan:
            fps = result.plan.fps
            fallback = fps.validation.get("best_effort_fps_fallback", {})
            if isinstance(fallback, dict) and fallback.get("selected_hypothesis") == "fps_ratio":
                selected_factor = fallback.get("selected_speed_factor")
                problems.append({
                    "kind": "fps-risk",
                    "job": label,
                    "message": (
                        "High-risk fallback selected the raw container FPS ratio "
                        f"({float(selected_factor):.3f}x); audio may be badly retimed "
                        "unless content-clock evidence validates it."
                        if isinstance(selected_factor, (int, float))
                        else "High-risk fallback selected the raw container FPS ratio; audio retiming requires validation."
                    ),
                })
        validation = result.validation
        fps_validation = validation.get("experimental_fps_validation")
        if isinstance(fps_validation, dict) and fps_validation.get("validated") is False:
            problems.append({
                "kind": "fps",
                "job": label,
                "message": str(fps_validation.get("reason", "FPS validation was inconclusive")),
            })
        dub_resync = validation.get("experimental_dub_resync")
        if isinstance(dub_resync, dict) and dub_resync.get("trusted") is False:
            problems.append({
                "kind": "audio-sync",
                "job": label,
                "message": str(dub_resync.get("reason", "Dubbed-audio synchronization confidence is low")),
            })
        synchronization = validation.get("synchronization")
        if isinstance(synchronization, dict):
            coverage = synchronization.get("sync_coverage")
            if isinstance(coverage, (int, float)) and coverage < 0.90:
                problems.append({
                    "kind": "sync-risk",
                    "job": label,
                    "message": f"Only {coverage:.1%} of the source audio was mapped; drift or out-of-sync sections are likely.",
                })
            delete_buckets = synchronization.get("delete_buckets")
            if isinstance(delete_buckets, list):
                deleted_seconds = sum(
                    max(float(item[1]) - float(item[0]), 0.0)
                    for item in delete_buckets
                    if isinstance(item, (list, tuple)) and len(item) >= 2
                )
                if deleted_seconds >= 30.0:
                    problems.append({
                        "kind": "sync-risk",
                        "job": label,
                        "message": f"The sync map deletes approximately {deleted_seconds:.1f}s of source audio.",
                    })
        archival = validation.get("archival")
        if isinstance(archival, list):
            for item in archival:
                if isinstance(item, dict) and item.get("status") not in {"moved"}:
                    problems.append({
                        "kind": "archival",
                        "job": label,
                        "message": f"Could not archive {item.get('side', 'input')}: {item.get('status')}",
                    })
    if not problems:
        status = "ok"
    elif counts["failed"]:
        status = "failed"
    elif counts["skipped"] or skipped:
        status = "partial"
    else:
        status = "warning"
    return {
        "status": status,
        "jobs": counts,
        "problem_count": len(problems),
        "problems": problems[:20],
    }
