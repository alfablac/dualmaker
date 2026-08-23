from __future__ import annotations

from pathlib import Path

from .runner import ToolRunner


def trim_start_copy(
    source: Path,
    destination: Path,
    seconds: float,
    *,
    runner: ToolRunner | None = None,
) -> Path:
    if seconds <= 0:
        return source
    runner = runner or ToolRunner()
    runner.run(
        (
            "ffmpeg",
            "-y",
            "-ss",
            f"{seconds:.6f}",
            "-i",
            source,
            "-map",
            "0",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            destination,
        )
    )
    return destination
