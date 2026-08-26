"""Rich rendering for non-interactive terminal workflows."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from .models import DualMakerConfig, JobPlan, JobResult
from .ordering import subtitle_presentation_key


def _subtitle_summary(plan: JobPlan, policy: str) -> str:
    master_count = len(plan.normal_subtitles)
    dual_count = len(plan.dual_subtitles)
    if policy == "exact-union":
        return f"Subs: {master_count} master + {dual_count} DUAL (exact-union)"

    slots = {subtitle_presentation_key(track) for track in plan.normal_subtitles}
    imported = 0
    overlapping = 0
    for track in plan.dual_subtitles:
        language, forced, accessibility = subtitle_presentation_key(track)
        # Untagged tracks are retained; two unknown-language subtitle streams
        # cannot be confidently classified as alternative presentations.
        if language == "und":
            imported += 1
            continue
        slot = (language, forced, accessibility)
        if slot in slots:
            overlapping += 1
            continue
        slots.add(slot)
        imported += 1
    return (
        f"Subs: {master_count} master + {imported} imported"
        + (f" ({overlapping} overlapping DUAL slots omitted)" if overlapping else "")
    )


class TerminalUI:
    """Presentation-only terminal adapter; processing remains usable as a library."""

    def __init__(self, config: DualMakerConfig) -> None:
        force_terminal = True if config.color == "always" else None
        no_color = config.color == "never" or config.output_format == "plain"
        self.config = config
        self.enabled = not config.quiet and config.output_format != "json"
        self.console = Console(
            force_terminal=force_terminal,
            no_color=no_color,
            highlight=False,
            soft_wrap=False,
        )
        self.error_console = Console(
            stderr=True,
            force_terminal=force_terminal,
            no_color=no_color,
            highlight=False,
        )

    def heading(self, path: Path, *, interactive: bool) -> None:
        if not self.enabled:
            return
        mode = "Interactive" if interactive else "Unattended"
        body = Text()
        body.append("Universal Portuguese dual-audio maker\n", style="bold")
        body.append(f"{mode} mode  •  {path}", style="dim")
        self.console.print(Panel(body, title="[bold cyan]dualmaker[/]", border_style="cyan"))

    def dependency_table(self, checks: dict[str, dict[str, Any]]) -> None:
        if not self.enabled:
            return
        table = Table(title="External tools", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("Status", width=8)
        table.add_column("Tool", style="bold")
        table.add_column("Resolved path")
        table.add_column("Version", overflow="fold")
        for name, result in checks.items():
            ok = bool(result["ok"])
            marker = Text("● ready" if ok else "● error", style="green" if ok else "red")
            table.add_row(
                marker,
                name,
                str(result.get("path") or "not found"),
                str(result.get("version") or "—"),
            )
        self.console.print(table)

    def resolved_configuration(self, values: dict[str, Any]) -> None:
        if not self.enabled:
            return
        table = Table(title="Resolved configuration", box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Setting", style="bold")
        table.add_column("Value", overflow="fold")
        table.add_column("Source", style="dim")
        sources = values.get("config_sources", {})
        displayed = (
            "path",
            "output_dir",
            "temp_dir",
            "tag",
            "dub_language",
            "recursive",
            "trim_recap",
            "end_trim",
            "preferred_original_source",
            "preferred_dub_source",
            "audio_codec_preference",
            "audio_selection_margin",
            "dub_gap_fallback",
            "dub_gap_min_seconds",
            "dub_gap_min_coverage",
            "subtitle_policy",
            "allow_experimental_fps_sync",
            "compatible_fps_pairs",
            "fps_max_drift_seconds",
            "fps_min_match_confidence",
            "fps_validation_positions",
            "fps_search_radius_seconds",
            "fps_speed_ratio_tolerance",
            "allow_tvrip_segment_sync",
            "tvrip_min_source_match_confidence",
            "tvrip_min_segment_confidence",
            "tvrip_max_residual_seconds",
            "tvrip_min_coverage",
            "tvrip_max_segments",
            "tvrip_fallback",
            "tvrip_allow_speed_correction",
            "tvrip_max_speed_adjustment",
            "tvrip_allow_partial_tracks",
            "tvrip_validation_positions",
            "enforce_paths",
            "allowed_paths",
            "required_paths",
            "required_group",
            "output_group",
            "output_format",
        )
        for name in displayed:
            table.add_row(name, str(values.get(name)), str(sources.get(name, "default")))
        for name, executable in values.get("binaries", {}).items():
            source = sources.get(f"binary.{name}", sources.get("binaries", "default"))
            table.add_row(f"binary.{name}", str(executable), str(source))
        self.console.print(table)

    def scan_summary(self, asset_count: int, plan_count: int, skipped_count: int) -> None:
        if not self.enabled:
            return
        self.console.print(
            f"[bold]Scan complete[/]  [cyan]{asset_count}[/] MKVs inspected  "
            f"[green]{plan_count}[/] jobs ready  [yellow]{skipped_count}[/] notices"
        )

    def plan_table(self, plans: Iterable[JobPlan], *, dry_run: bool) -> None:
        if not self.enabled:
            return
        plans = list(plans)
        if not plans:
            self.console.print("[yellow]No eligible jobs were selected.[/]")
            return
        title = "Dry-run plan" if dry_run else "Processing plan"
        table = Table(title=title, box=box.ROUNDED, header_style="bold cyan", show_lines=True)
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Content", overflow="fold")
        table.add_column("DUAL source file", overflow="fold")
        table.add_column("Master video file", overflow="fold")
        table.add_column("Tracks", overflow="fold")
        table.add_column("Output", overflow="fold")
        for number, plan in enumerate(plans, 1):
            identity = plan.identity.title
            if plan.identity.kind == "episode":
                episodes = "/".join(f"E{episode:02d}" for episode in plan.identity.episodes)
                identity += f" S{plan.identity.season:02d}{episodes}"
            elif plan.identity.year:
                identity += f" ({plan.identity.year})"
            tracks = (
                "PT: "
                + ", ".join(
                    f"{item.source}#{item.track.id}" for item in plan.resolved_dubs
                )
                + "\n"
                + f"Original: {plan.resolved_original.source}#"
                + f"{plan.resolved_original.track.id} "
                + f"({plan.resolved_original.track.effective_language})\n"
                + _subtitle_summary(plan, self.config.subtitle_policy)
            )
            if plan.sidecar_subtitles:
                sidecars = ", ".join(
                    f"{item.source}:{item.path.name} ({item.language})"
                    for item in plan.sidecar_subtitles
                )
                tracks += f"\nSidecars: {sidecars}"
            if plan.fps.required:
                tracks += (
                    "\nFPS BETA: "
                    f"{plan.fps.dual_rate.rational if plan.fps.dual_rate else '?'} → "
                    f"{plan.fps.master_rate.rational if plan.fps.master_rate else '?'} "
                    f"({plan.fps.expected_drift_seconds:+.2f}s nominal drift)"
                )
            if plan.source_kind == "tvrip":
                tracks += "\nTVRIP BETA: per-segment validation required"
            elif self.config.dub_gap_fallback != "off":
                tracks += (
                    "\nDub-gap repair: "
                    f"{self.config.dub_gap_fallback} after validated comparison"
                )
            table.add_row(
                str(number),
                Text(identity),
                Text(f"{plan.dual.path.name}\n{plan.dual.path.parent}"),
                Text(f"{plan.normal.path.name}\n{plan.normal.path.parent}"),
                tracks,
                Text(str(plan.output)),
            )
        self.console.print(table)

    def progress(self, total: int) -> Progress:
        disable = not self.enabled or not self.config.progress
        return Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self.console,
            disable=disable,
        )

    def result(self, result: JobResult) -> None:
        if not self.enabled:
            return
        if result.status == "success":
            self.console.print(f"[green]✓ Created[/] {escape(str(result.output))}")
        elif result.status == "planned":
            self.console.print(f"[cyan]● Planned[/] {escape(str(result.output))}")
        elif result.status == "skipped":
            self.console.print(f"[yellow]↷ Skipped[/] {escape(result.message)}")
        else:
            self.error_console.print(f"[red]✗ Failed[/] {escape(result.message)}")

    def notices(self, skipped: Iterable[str]) -> None:
        if not self.enabled:
            return
        for reason in skipped:
            self.error_console.print(f"[yellow]↷ Skipped[/] {escape(reason)}")

    def report(self, path: Path) -> None:
        if self.enabled:
            self.console.print(f"[dim]JSON report:[/] {escape(str(path))}")

    def error(self, message: str, *, hint: str | None = None) -> None:
        if self.config.output_format == "json":
            payload = {"status": "error", "error": {"message": message}}
            if hint:
                payload["error"]["hint"] = hint
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return
        content = Text(message)
        if hint:
            content.append(f"\n\nHint: {hint}", style="cyan")
        self.error_console.print(
            Panel(content, title="[bold red]dualmaker error[/]", border_style="red")
        )

    @staticmethod
    def json_result(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
