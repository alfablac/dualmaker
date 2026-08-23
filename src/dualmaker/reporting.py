from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
