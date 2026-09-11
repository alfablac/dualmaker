"""Optional OCR helpers for image-based subtitle tracks."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import DependencyError, ProcessingError
from .languages import normalize_language
from .models import Track
from .runner import ToolRunner

_PORTUGUESE_OCR_REPLACEMENTS = (
    (re.compile(r"(?<!\w)S6(?!\w)", re.IGNORECASE), "Só"),
    (re.compile(r"(?<!\w)agiientariam(?!\w)", re.IGNORECASE), "aguentariam"),
)


def correct_portuguese_ocr(text: str) -> str:
    """Apply conservative, whole-word corrections to Portuguese OCR output."""
    for pattern, replacement in _PORTUGUESE_OCR_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


def ocr_vobsub(
    source: Path,
    track: Track,
    destination: Path,
    *,
    work_dir: Path,
    runner: ToolRunner,
) -> Path:
    """Extract and OCR one embedded VobSub track into an SRT file.

    ``vobsub2srt`` consumes the extracted ``.idx/.sub`` basename and uses
    Tesseract locally. The source MKV is never modified.
    """
    executable = runner.which("vobsub2srt")
    if not executable:
        raise DependencyError(
            "VobSub OCR requires vobsub2srt and Tesseract; install vobsub2srt "
            "and make it available on PATH"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    basename = work_dir / f"ocr-vobsub-{track.id}"
    idx_path = basename.with_suffix(".idx")
    srt_path = basename.with_suffix(".srt")
    runner.run(("mkvextract", "tracks", source, f"{track.id}:{idx_path}"))
    if not idx_path.is_file() or not idx_path.with_suffix(".sub").is_file():
        raise ProcessingError(f"mkvextract did not create the VobSub pair for track {track.id}")
    language = normalize_language(track.effective_language)
    language = language.split("-", 1)[0]
    runner.run((executable, "--lang", language, str(basename)))
    if not srt_path.is_file():
        raise ProcessingError(f"vobsub2srt did not create an SRT for track {track.id}")
    srt_path.write_text(
        correct_portuguese_ocr(srt_path.read_text(encoding="utf-8-sig")),
        encoding="utf-8",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    srt_path.replace(destination)
    return destination
