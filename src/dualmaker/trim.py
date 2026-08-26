from __future__ import annotations

from pathlib import Path

from .runner import ToolRunner


def remux_avi_to_mkv(
    source: Path,
    destination: Path,
    *,
    runner: ToolRunner | None = None,
) -> Path:
    """Losslessly stage an AVI as Matroska for the MKV-only processing tools."""

    if source.suffix.lower() != ".avi":
        return source
    runner = runner or ToolRunner()
    runner.run(
        (
            "ffmpeg",
            "-y",
            "-fflags",
            "+genpts",
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
