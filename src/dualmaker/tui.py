"""Textual applications used by dualmaker's interactive mode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    SelectionList,
    Static,
)

from .errors import ConfigurationError, UserCancelledError
from .models import (
    AudioTrackSelection,
    FPSDecision,
    PairCandidate,
    SidecarSubtitleCandidate,
    TVRipFallback,
    TVRipSyncReport,
)
from .sidecars import normalize_sidecar_language


@dataclass(slots=True)
class InteractiveSelection:
    candidates: list[PairCandidate]
    original_languages: dict[tuple[Any, ...], str]


class PairPickerApp(App[list[int] | None]):
    """Keyboard-driven checklist followed by an explicit review screen."""

    TITLE = "dualmaker — Select release pairs"
    SUB_TITLE = "Space toggles • A selects all • D clears all • Enter reviews • Esc cancels safely"
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("a", "select_all", "Select all"),
        ("d", "deselect_all", "Deselect all"),
        ("enter", "review", "Review"),
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]
    CSS = """
    Screen { background: #0b1020; color: #f8fafc; }
    Header, Footer { background: #111827; color: #f8fafc; }
    #body { height: 1fr; padding: 1 2; }
    #intro { margin-bottom: 1; color: #dbeafe; background: #172554; border: round #3b82f6; padding: 0 1; }
    #pairs { height: 1fr; border: round #38bdf8; background: #111827; color: #f8fafc; }
    #pairs:focus { border: round #facc15; }
    #pair-details { height: 8; border: round #facc15; background: #1f2937; color: #f8fafc; padding: 0 1; margin-top: 1; }
    #actions, #review-actions { height: auto; align-horizontal: right; margin-top: 1; }
    Button { margin-left: 1; background: #334155; color: #ffffff; }
    Button:hover, Button:focus { background: #475569; color: #ffffff; }
    #select-all { background: #166534; color: #ffffff; }
    #deselect-all { background: #9a3412; color: #ffffff; }
    #review, #confirm { background: #1d4ed8; color: #ffffff; }
    #review-pane { display: none; height: 1fr; padding: 1 2; background: #0b1020; }
    #review-summary { height: 1fr; border: round #22c55e; background: #111827; color: #f8fafc; padding: 1 2; }
    """

    def __init__(self, candidates: list[PairCandidate], selected: set[int] | None = None) -> None:
        super().__init__()
        self.candidates = candidates
        self.initial_selected = selected or set()

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Static(
                "Choose the release pairs to process. The best candidate for each title is "
                "preselected; alternatives remain available for ambiguous matches. Move through "
                "the checklist to inspect the exact source and master paths below, then review "
                "before confirming.",
                id="intro",
            )
            options = []
            for index, candidate in enumerate(self.candidates):
                identity = candidate.identity.title
                if candidate.identity.kind == "episode":
                    episode = "/".join(f"E{item:02d}" for item in candidate.identity.episodes)
                    identity += f" S{candidate.identity.season:02d}{episode}"
                elif candidate.identity.year:
                    identity += f" ({candidate.identity.year})"
                label = (
                    f"{identity}  •  score {candidate.score:.3f}  •  "
                    f"MASTER: {candidate.normal.path.name}  •  "
                    f"DUAL: {candidate.dual.path.name}"
                )
                options.append((label, index, index in self.initial_selected))
            yield SelectionList(*options, id="pairs")
            yield Static("", id="pair-details")
            with Horizontal(id="actions"):
                yield Button("Cancel", id="cancel", variant="error")
                yield Button("Deselect all", id="deselect-all")
                yield Button("Select all", id="select-all")
                yield Button("Review selection", id="review", variant="primary")
        with Vertical(id="review-pane"):
            yield Static("", id="review-summary")
            with Horizontal(id="review-actions"):
                yield Button("Back", id="back")
                yield Button("Cancel", id="cancel-review", variant="error")
                yield Button("Confirm and continue", id="confirm", variant="success")
        yield Footer()

    def _selected(self) -> list[int]:
        return list(self.query_one("#pairs", SelectionList).selected)

    def _show_pair_details(self, index: int) -> None:
        """Keep the currently highlighted pair identifiable at any terminal width."""
        candidate = self.candidates[index]
        identity = candidate.identity.title
        if candidate.identity.kind == "episode":
            episodes = "/".join(f"E{item:02d}" for item in candidate.identity.episodes)
            identity += f" S{candidate.identity.season:02d}{episodes}"
        elif candidate.identity.year:
            identity += f" ({candidate.identity.year})"
        details = "\n".join(
            (
                f"Highlighted pair: {identity}  •  score {candidate.score:.3f}",
                f"MASTER video/chapters: {candidate.normal.path}",
                f"DUAL audio source:      {candidate.dual.path}",
                "Shared original: " + ", ".join(candidate.shared_original_languages),
            )
        )
        self.query_one("#pair-details", Static).update(details)

    def on_mount(self) -> None:
        if self.candidates:
            self._show_pair_details(0)

    @on(SelectionList.SelectionHighlighted)
    def pair_highlighted(self, event: SelectionList.SelectionHighlighted[int]) -> None:
        self._show_pair_details(event.selection_index)

    def _validate_selected(self, selected: list[int]) -> bool:
        keys: set[tuple[Any, ...]] = set()
        for index in selected:
            key = self.candidates[index].identity.key
            if key in keys:
                self.notify(
                    "Choose at most one pair for each movie or episode.",
                    title="Conflicting selection",
                    severity="error",
                )
                return False
            keys.add(key)
        return True

    def action_review(self) -> None:
        selected = self._selected()
        if not selected:
            self.notify("Select at least one pair, or cancel.", severity="warning")
            return
        if not self._validate_selected(selected):
            return
        lines = [f"Selected {len(selected)} job(s):", ""]
        for number, index in enumerate(selected, 1):
            candidate = self.candidates[index]
            lines.extend(
                (
                    f"{number}. {candidate.identity.title} — score {candidate.score:.3f}",
                    f"   DUAL: {candidate.dual.path}",
                    f"   MASTER: {candidate.normal.path}",
                    f"   Shared original: {', '.join(candidate.shared_original_languages)}",
                    "",
                )
            )
        self.query_one("#review-summary", Static).update("\n".join(lines))
        self.query_one("#body").display = False
        self.query_one("#review-pane").display = True

    def action_select_all(self) -> None:
        self.query_one("#pairs", SelectionList).select_all()
        self.notify(f"Selected all {len(self.candidates)} pair(s).", severity="information")

    def action_deselect_all(self) -> None:
        self.query_one("#pairs", SelectionList).deselect_all()
        self.notify("Cleared the pair selection.", severity="information")

    def action_cancel(self) -> None:
        self.exit(None)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "review":
            self.action_review()
        elif button_id == "select-all":
            self.action_select_all()
        elif button_id == "deselect-all":
            self.action_deselect_all()
        elif button_id in {"cancel", "cancel-review"}:
            self.action_cancel()
        elif button_id == "back":
            self.query_one("#review-pane").display = False
            self.query_one("#body").display = True
        elif button_id == "confirm":
            selected = self._selected()
            if self._validate_selected(selected):
                self.exit(selected)


class LanguagePickerApp(App[str | None]):
    """Resolve a multi-language original choice without free-form text entry."""

    TITLE = "dualmaker — Select original language"
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]
    CSS = """
    Screen { align: center middle; }
    #dialog { width: 72; height: auto; border: round $primary; padding: 1 2; }
    RadioSet { margin: 1 0; }
    #language-actions { height: auto; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    def __init__(self, candidate: PairCandidate) -> None:
        super().__init__()
        self.candidate = candidate

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(f"Choose the shared original language for {self.candidate.identity.title}")
            with RadioSet(id="languages"):
                for index, language in enumerate(self.candidate.shared_original_languages):
                    yield RadioButton(language, value=index == 0, id=f"language-{index}")
            with Horizontal(id="language-actions"):
                yield Button("Cancel", id="language-cancel", variant="error")
                yield Button("Confirm", id="language-confirm", variant="success")

    def action_cancel(self) -> None:
        self.exit(None)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "language-cancel":
            self.action_cancel()
        elif event.button.id == "language-confirm":
            pressed = self.query_one("#languages", RadioSet).pressed_index
            if pressed < 0:
                self.notify("Select a language first.", severity="warning")
                return
            self.exit(self.candidate.shared_original_languages[pressed])


class SidecarLanguagePickerApp(App[str | None]):
    """Assign a language to one external subtitle without inferring it from its name."""

    TITLE = "dualmaker — Identify external subtitle"
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]
    CSS = """
    Screen { align: center middle; }
    #dialog { width: 88; height: auto; border: round $primary; padding: 1 2; }
    RadioSet { margin: 1 0; }
    Input { margin: 1 0; }
    #sidecar-actions { height: auto; align-horizontal: right; }
    Button { margin-left: 1; }
    """
    LANGUAGE_CHOICES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Portuguese (Brazil)", "pt-BR"),
        ("Portuguese (Portugal)", "pt-PT"),
        ("English", "en"),
        ("Spanish (Latin America)", "es-419"),
        ("Spanish (Spain)", "es-ES"),
        ("French", "fr"),
        ("German", "de"),
        ("Japanese", "ja"),
        ("Other — enter an ISO/BCP-47 tag below", ""),
    )

    def __init__(self, sidecar: SidecarSubtitleCandidate) -> None:
        super().__init__()
        self.sidecar = sidecar

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Which language does this external subtitle contain?")
            yield Static(f"Source: {self.sidecar.source}\nFile:   {self.sidecar.path}")
            with RadioSet(id="sidecar-languages"):
                for index, (label, _language) in enumerate(self.LANGUAGE_CHOICES):
                    yield RadioButton(label, value=index == 0, id=f"sidecar-language-{index}")
            yield Input(
                placeholder="Custom tag, e.g. ar, fr-CA, zh-Hant (only for Other)",
                id="sidecar-custom-language",
            )
            with Horizontal(id="sidecar-actions"):
                yield Button("Cancel", id="sidecar-cancel", variant="error")
                yield Button("Confirm", id="sidecar-confirm", variant="success")

    def action_cancel(self) -> None:
        self.exit(None)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sidecar-cancel":
            self.action_cancel()
            return
        if event.button.id != "sidecar-confirm":
            return
        pressed = self.query_one("#sidecar-languages", RadioSet).pressed_index
        if pressed < 0:
            self.notify("Select a language first.", severity="warning")
            return
        language = self.LANGUAGE_CHOICES[pressed][1]
        if not language:
            language = self.query_one("#sidecar-custom-language", Input).value
        try:
            language = normalize_sidecar_language(
                language, label=f"language for {self.sidecar.path.name}"
            )
        except ConfigurationError as exc:
            self.notify(str(exc), severity="error")
            return
        self.exit(language)


def select_candidates_interactively(
    grouped: dict[tuple[object, ...], list[PairCandidate]],
) -> InteractiveSelection:
    flattened: list[PairCandidate] = []
    defaults: set[int] = set()
    for options in grouped.values():
        defaults.add(len(flattened))
        flattened.extend(options)
    if not flattened:
        return InteractiveSelection([], {})
    selected_indices = PairPickerApp(flattened, defaults).run()
    if selected_indices is None:
        raise UserCancelledError("Interactive selection cancelled; no files were changed")
    candidates = [flattened[index] for index in selected_indices]
    languages: dict[tuple[Any, ...], str] = {}
    for candidate in candidates:
        if len(candidate.shared_original_languages) > 1:
            chosen = LanguagePickerApp(candidate).run()
            if chosen is None:
                raise UserCancelledError("Language selection cancelled; no processing was started")
            languages[candidate.identity.key] = chosen
    return InteractiveSelection(candidates, languages)


def select_original_language(candidate: PairCandidate) -> str:
    if len(candidate.shared_original_languages) == 1:
        return candidate.shared_original_languages[0]
    selected = LanguagePickerApp(candidate).run()
    if selected is None:
        raise UserCancelledError("Language selection cancelled; no processing was started")
    return selected


def select_sidecar_languages(
    sidecars: list[SidecarSubtitleCandidate],
) -> dict[Path, str]:
    """Prompt once per selected sidecar; cancellation aborts before media work starts."""
    languages: dict[Path, str] = {}
    for sidecar in sidecars:
        selected = SidecarLanguagePickerApp(sidecar).run()
        if selected is None:
            raise UserCancelledError("External subtitle language selection cancelled; no files were changed")
        languages[sidecar.path] = selected
    return languages


class AudioTrackPickerApp(App[int | None]):
    """Choose between close-scoring audio candidates with metadata in view."""

    TITLE = "dualmaker — Select audio track"
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]
    CSS = LanguagePickerApp.CSS

    def __init__(self, role: str, choices: list[AudioTrackSelection]) -> None:
        super().__init__()
        self.role = role
        self.choices = choices

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(
                f"Choose the {self.role}. Automatic confidence is low because the leading "
                "candidates have equivalent quality."
            )
            with RadioSet(id="audio-options"):
                for index, choice in enumerate(self.choices):
                    track = choice.track
                    details = " • ".join(
                        (
                            choice.label,
                            track.codec_id or track.codec or "unknown codec",
                            f"{track.channels or '?'}ch",
                            f"{track.bitrate or '?'} bps",
                            f"{track.sample_rate or '?'} Hz",
                            f"score {choice.score:.2f}",
                        )
                    )
                    yield RadioButton(details, value=index == 0, id=f"audio-{index}")
            with Horizontal(id="language-actions"):
                yield Button("Cancel job", id="audio-cancel", variant="error")
                yield Button("Use selected track", id="audio-confirm", variant="success")

    def action_cancel(self) -> None:
        self.exit(None)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "audio-cancel":
            self.action_cancel()
        elif event.button.id == "audio-confirm":
            pressed = self.query_one("#audio-options", RadioSet).pressed_index
            if pressed < 0:
                self.notify("Select an audio track first.", severity="warning")
                return
            self.exit(pressed)


def select_audio_track(
    role: str, choices: list[AudioTrackSelection]
) -> AudioTrackSelection:
    selected = AudioTrackPickerApp(role, choices).run()
    if selected is None:
        raise UserCancelledError("Audio-track selection cancelled; no processing was started")
    return choices[selected]


class ExperimentalFPSApp(App[bool | None]):
    """Require an explicit acknowledgement before a different-FPS beta job."""

    TITLE = "dualmaker — Experimental frame-rate synchronization"
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Do not process"),
        ("q", "cancel", "Do not process"),
    ]
    CSS = LanguagePickerApp.CSS

    def __init__(self, candidate: PairCandidate, decision: FPSDecision) -> None:
        super().__init__()
        self.candidate = candidate
        self.decision = decision

    def compose(self) -> ComposeResult:
        master = self.decision.master_rate
        dual = self.decision.dual_rate
        with VerticalScroll(id="dialog"):
            yield Label(
                "A likely source match was found, but the inputs use different frame rates:\n\n"
                f"Master: {master.display if master else 'unknown'}\n"
                f"Dual-audio source: {dual.display if dual else 'unknown'}\n"
                f"Expected nominal full-length drift: "
                f"{self.decision.expected_drift_seconds:+.3f}s\n\n"
                "This uses an experimental synchronization mode. Audio may drift, cuts may "
                "not align perfectly, and results are not guaranteed. Dualmaker will analyze "
                "content anchors before deciding whether any speed correction is justified."
            )
            with Horizontal(id="language-actions"):
                yield Button("Go back / skip", id="fps-cancel")
                yield Button("Continue beta", id="fps-confirm", variant="warning")

    def action_cancel(self) -> None:
        self.exit(False)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fps-cancel":
            self.action_cancel()
        elif event.button.id == "fps-confirm":
            self.exit(True)


def confirm_experimental_fps(candidate: PairCandidate, decision: FPSDecision) -> bool:
    return bool(ExperimentalFPSApp(candidate, decision).run())


class TVRipSegmentReviewApp(App[tuple[set[int], TVRipFallback] | None]):
    """Review every proposed segment and choose an explicit gap policy."""

    TITLE = "dualmaker — Segment synchronization review"
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Abort safely"),
        ("q", "cancel", "Abort safely"),
    ]
    CSS = """
    Screen { background: $surface; }
    #tvrip-dialog { width: 110; height: 95%; border: round $warning; padding: 1 2; }
    #tvrip-summary { height: auto; margin-bottom: 1; }
    #tvrip-segments { height: 1fr; border: round $primary; }
    #tvrip-fallback { height: auto; margin: 1 0; }
    #tvrip-actions { height: auto; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    def __init__(
        self,
        report: TVRipSyncReport,
        configured_fallback: TVRipFallback,
        *,
        workflow_label: str = "TVRip",
        fallback_choices: tuple[TVRipFallback, ...] = (
            "original",
            "alternate-dub",
            "silence",
            "omit",
        ),
    ) -> None:
        super().__init__()
        self.report = report
        self.configured_fallback = configured_fallback
        self.workflow_label = workflow_label
        self.fallback_choices = fallback_choices

    def compose(self) -> ComposeResult:
        source_only = sum(interval.duration for interval in self.report.tvrip_only)
        master_only = sum(interval.duration for interval in self.report.master_only)
        intro = (
            "A TVRip-to-master match contains editorial differences. Each checked "
            "segment will be retained; unchecked or rejected ranges become "
            "master-only gaps.\n\n"
            if self.report.workflow == "tvrip"
            else "The reference-audio comparison found master scenes missing from the "
            "DUAL timeline. Checked segments retain the synchronized Portuguese dub; "
            "master-only gaps use the selected fallback.\n\n"
        )
        with Vertical(id="tvrip-dialog"):
            yield Static(
                intro
                + f"Coverage: {self.report.coverage:.1%}  •  Segments: "
                f"{self.report.accepted_segments} accepted / "
                f"{self.report.ambiguous_segments} ambiguous / "
                f"{self.report.rejected_segments} rejected\n"
                f"{self.workflow_label}-only removed: {source_only:.3f}s  •  "
                f"Master-only: {master_only:.3f}s  •  "
                f"Speed: {self.report.speed_correction:.9f}",
                id="tvrip-summary",
            )
            options = []
            for segment in self.report.segments:
                residual = (
                    f"{segment.residual_seconds * 1000:.0f} ms"
                    if segment.residual_seconds is not None
                    else "unavailable"
                )
                label = (
                    f"#{segment.index:02d} {segment.status.upper()}  "
                    f"{self.workflow_label} {segment.source_start:.3f}–{segment.source_end:.3f}  →  "
                    f"Master {segment.master_start:.3f}–{segment.master_end:.3f}  •  "
                    f"confidence {segment.confidence:.1%}  •  residual {residual}\n"
                    f"    {segment.operation}"
                )
                if segment.status != "rejected":
                    options.append((label, segment.index, segment.status == "accepted"))
            if options:
                yield SelectionList(*options, id="tvrip-segments")
            else:
                yield Static("No segment is eligible for approval.", id="tvrip-segments")
            yield Label("Fallback for master-only intervals:")
            with RadioSet(id="tvrip-fallback"):
                selected = (
                    self.configured_fallback
                    if self.configured_fallback != "ask"
                    else "original"
                )
                for value in self.fallback_choices:
                    yield RadioButton(
                        value.replace("-", " ").title(),
                        value=value == selected,
                        id=f"tvrip-fallback-{value}",
                    )
            with Horizontal(id="tvrip-actions"):
                yield Button("Abort; write no output", id="tvrip-cancel", variant="error")
                yield Button("Apply reviewed mapping", id="tvrip-confirm", variant="warning")

    def action_cancel(self) -> None:
        self.exit(None)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tvrip-cancel":
            self.action_cancel()
            return
        if event.button.id != "tvrip-confirm":
            return
        segments_widget = self.query_one("#tvrip-segments")
        accepted = (
            set(segments_widget.selected)
            if isinstance(segments_widget, SelectionList)
            else set()
        )
        pressed = self.query_one("#tvrip-fallback", RadioSet).pressed_index
        if pressed < 0:
            self.notify("Choose a fallback policy.", severity="warning")
            return
        self.exit((accepted, self.fallback_choices[pressed]))


def review_tvrip_segments(
    report: TVRipSyncReport,
    configured_fallback: TVRipFallback,
) -> tuple[set[int], TVRipFallback] | None:
    return TVRipSegmentReviewApp(report, configured_fallback).run()


def review_dub_gap_segments(
    report: TVRipSyncReport,
    configured_fallback: TVRipFallback,
) -> tuple[set[int], TVRipFallback] | None:
    """Review a standard DUAL map before mixing master-original gap audio."""

    return TVRipSegmentReviewApp(
        report,
        configured_fallback,
        workflow_label="DUAL",
        fallback_choices=("original", "silence"),
    ).run()


class RecapPickerApp(App[int | None]):
    """Review detected recap cuts using radio controls instead of raw text input."""

    TITLE = "dualmaker — Review recap detection"
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]
    CSS = LanguagePickerApp.CSS

    def __init__(self, reason: str, candidates: list[dict[str, float]]) -> None:
        super().__init__()
        self.reason = reason
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(f"Automatic recap decision was uncertain.\n{self.reason}")
            with RadioSet(id="recap-options"):
                yield RadioButton("Do not trim", value=True, id="recap-0")
                for index, candidate in enumerate(self.candidates, 1):
                    yield RadioButton(
                        f"Trim master {candidate['normal_trim']:.3f}s • "
                        f"trim DUAL {candidate['dual_trim']:.3f}s • "
                        f"score {candidate['score']:.3f}",
                        id=f"recap-{index}",
                    )
            with Horizontal(id="language-actions"):
                yield Button("Cancel job", id="recap-cancel", variant="error")
                yield Button("Apply choice", id="recap-confirm", variant="success")

    def action_cancel(self) -> None:
        self.exit(None)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "recap-cancel":
            self.action_cancel()
        elif event.button.id == "recap-confirm":
            pressed = self.query_one("#recap-options", RadioSet).pressed_index
            if pressed < 0:
                self.notify("Select a recap action first.", severity="warning")
                return
            self.exit(pressed)


def select_recap_candidate(reason: str, candidates: list[dict[str, float]]) -> int:
    selected = RecapPickerApp(reason, candidates).run()
    if selected is None:
        raise UserCancelledError("Recap selection cancelled before synchronization")
    return selected
