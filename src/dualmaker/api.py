from __future__ import annotations

from pathlib import Path

from .matching import inspect_directory
from .metadata import MediaInspector
from .models import DualMakerConfig, JobPlan, JobResult, MediaAsset
from .pipeline import plan_explicit, process_job


def scan_directory(
    path: str | Path = ".",
    *,
    recursive: bool = False,
    inspector: MediaInspector | None = None,
) -> list[MediaAsset]:
    """Inspect MKVs eligible for matching below *path*."""

    return inspect_directory(Path(path), recursive=recursive, inspector=inspector)


def plan_pair(
    dual: str | Path,
    normal: str | Path,
    config: DualMakerConfig | None = None,
    *,
    inspector: MediaInspector | None = None,
    tvrip: bool = False,
) -> JobPlan:
    """Validate an explicit pair and return its deterministic job plan."""

    config = config or DualMakerConfig()
    return plan_explicit(
        Path(dual),
        Path(normal),
        config,
        inspector=inspector,
        tvrip=tvrip,
    )


def make_dual(
    plan: JobPlan,
    config: DualMakerConfig | None = None,
    *,
    inspector: MediaInspector | None = None,
) -> JobResult:
    """Execute a planned job without modifying either source file."""

    return process_job(plan, config or DualMakerConfig(), inspector=inspector)
