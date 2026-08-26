"""Optional cross-platform desktop front end for dualmaker.

The GUI deliberately stays thin: discovery, matching, planning, processing, and
configuration resolution are all delegated to the same library APIs used by the
terminal command.  Tk is imported lazily so installing or using ``dualmaker``
without a desktop environment remains supported.
"""

from __future__ import annotations

import argparse
import queue
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .configuration import CONFIG_SETTING_COMMENTS, load_configuration, validate_configuration
from .matching import collect_pair_candidates, require_explicit_pair
from .metadata import MediaInspector
from .models import DualMakerConfig, JobResult, PairCandidate, jsonable
from .pipeline import plan_candidates, process_job, scan_assets
from .reporting import default_report_path, write_report
from .runner import ToolRunner


def _tk_modules() -> tuple[Any, Any, Any, Any]:
    """Import Tk only when the optional GUI is actually requested."""

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:  # pragma: no cover - depends on the host Python build
        raise RuntimeError(
            "Tk is not installed. On Debian/Ubuntu install python3-tk; on macOS and "
            "Windows use a Python distribution that includes Tk."
        ) from exc
    return tk, ttk, filedialog, messagebox


class DualMakerGUI:
    """Tk application for folder discovery, match selection, and processing."""

    def __init__(self, root: Any, *, initial_path: Path | None = None, config_path: Path | None = None):
        self.tk, self.ttk, self.filedialog, self.messagebox = _tk_modules()
        self.root = root
        self.root.title(f"dualmaker GUI {__version__}")
        self.root.minsize(1050, 680)
        self.root.geometry("1320x820")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_requested = threading.Event()
        self.config: DualMakerConfig | None = None
        self.assets = []
        self.grouped: dict[tuple[object, ...], list[PairCandidate]] = {}
        self.rows: dict[str, PairCandidate] = {}
        self.selected_rows: set[str] = set()
        self.settings_loaded = False

        self.path_var = self.tk.StringVar(value=str(initial_path or Path.cwd()))
        self.config_path_var = self.tk.StringVar(value=str(config_path or ""))
        self.output_dir_var = self.tk.StringVar()
        self.tag_var = self.tk.StringVar()
        self.dub_language_var = self.tk.StringVar()
        self.conflict_var = self.tk.StringVar()
        self.subtitle_policy_var = self.tk.StringVar()
        self.recursive_var = self.tk.BooleanVar(value=False)
        self.trim_recap_var = self.tk.BooleanVar(value=True)
        self.end_trim_var = self.tk.BooleanVar(value=True)
        self.reconcile_av_var = self.tk.BooleanVar(value=True)
        self.experimental_fps_var = self.tk.BooleanVar(value=False)
        self.tvrip_sync_var = self.tk.BooleanVar(value=False)
        self.dry_run_var = self.tk.BooleanVar(value=False)
        self.progress_var = self.tk.BooleanVar(value=True)
        self.status_var = self.tk.StringVar(value="Choose a folder, then scan for release matches.")

        self._build()
        self.root.after(100, self._poll_events)
        self.root.after(200, self.scan)

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        self.root.rowconfigure(3, weight=1)

        heading = self.ttk.Frame(self.root, padding=(14, 12, 14, 4))
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        self.ttk.Label(heading, text="dualmaker", font=("TkDefaultFont", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.ttk.Label(
            heading,
            text="Discover matching releases, review every candidate, and run the same pipeline as the CLI.",
        ).grid(row=1, column=0, sticky="w")

        source = self.ttk.LabelFrame(self.root, text="Input and configuration", padding=8)
        source.grid(row=1, column=0, padx=14, pady=6, sticky="ew")
        source.columnconfigure(1, weight=1)
        self._path_row(source, 0, "Folder", self.path_var, self._choose_folder)
        self._path_row(source, 1, "Config file", self.config_path_var, self._choose_config)
        self.ttk.Checkbutton(source, text="Scan subfolders", variable=self.recursive_var).grid(
            row=2, column=1, sticky="w", pady=(4, 0)
        )
        self.ttk.Button(source, text="Scan matches", command=self.scan).grid(
            row=2, column=3, padx=(8, 0), pady=(4, 0)
        )

        settings = self.ttk.LabelFrame(self.root, text="Common settings (all other settings remain available in the config file)", padding=8)
        settings.grid(row=2, column=0, padx=14, pady=6, sticky="new")
        for column in (1, 4, 7):
            settings.columnconfigure(column, weight=1)
        self._entry_setting(settings, 0, 0, "Output folder", self.output_dir_var, "paths.output_dir")
        self._entry_setting(settings, 0, 3, "Release tag", self.tag_var, "dualmaker.tag")
        self._entry_setting(settings, 0, 6, "Dub language", self.dub_language_var, "dualmaker.dub_language")
        self._combo_setting(settings, 1, 0, "Conflict policy", self.conflict_var, ("increment", "skip", "error"), "dualmaker.conflict")
        self._combo_setting(settings, 1, 3, "Subtitles", self.subtitle_policy_var, ("prefer-master", "exact-union"), "dualmaker.subtitle_policy")
        toggles = self.ttk.Frame(settings)
        toggles.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(8, 0))
        self._check_setting(toggles, 0, "Trim recap", self.trim_recap_var, "features.trim_recap")
        self._check_setting(toggles, 1, "Trim video end", self.end_trim_var, "features.end_trim")
        self._check_setting(toggles, 2, "Reconcile A/V", self.reconcile_av_var, "features.reconcile_av")
        self._check_setting(toggles, 3, "Allow FPS beta", self.experimental_fps_var, "features.allow_experimental_fps_sync")
        self._check_setting(toggles, 4, "Allow TVRip beta", self.tvrip_sync_var, "tvrip.allow_tvrip_segment_sync")
        self._check_setting(toggles, 5, "Dry run", self.dry_run_var, "dualmaker.dry_run")
        self._check_setting(toggles, 6, "Progress", self.progress_var, "interface.progress")

        matches = self.ttk.LabelFrame(self.root, text="Matches — click the checkbox column to choose one candidate per title", padding=8)
        matches.grid(row=3, column=0, padx=14, pady=6, sticky="nsew")
        matches.columnconfigure(0, weight=1)
        matches.rowconfigure(0, weight=1)
        self.tree = self.ttk.Treeview(
            matches,
            columns=("selected", "title", "score", "master", "dual", "shared"),
            show="headings",
            selectmode="browse",
        )
        headings = {
            "selected": ("Use", 55),
            "title": ("Title", 230),
            "score": ("Score", 75),
            "master": ("Master video", 260),
            "dual": ("DUAL audio source", 260),
            "shared": ("Reference", 140),
        }
        for name, (label, width) in headings.items():
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = self.ttk.Scrollbar(matches, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Button-1>", self._tree_click)

        actions = self.ttk.Frame(matches)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.ttk.Button(actions, text="Select best candidates", command=self.select_best).pack(side="left")
        self.ttk.Button(actions, text="Clear selection", command=self.clear_selection).pack(side="left", padx=6)
        self.ttk.Button(actions, text="Add manual pair…", command=self.add_manual_pair).pack(side="left")
        self.ttk.Button(actions, text="Process selected", command=self.process_selected).pack(side="right")

        bottom = self.ttk.Frame(self.root, padding=(14, 0, 14, 12))
        bottom.grid(row=4, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.ttk.Label(bottom, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = self.ttk.Progressbar(bottom, mode="determinate", maximum=1, value=0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(5, 0))

        log_frame = self.ttk.LabelFrame(self.root, text="Activity", padding=6)
        log_frame.grid(row=5, column=0, padx=14, pady=(0, 12), sticky="ew")
        log_frame.columnconfigure(0, weight=1)
        self.log = self.tk.Text(log_frame, height=6, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="ew")

    def _path_row(self, parent: Any, row: int, label: str, variable: Any, command: Any) -> None:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
        self.ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew")
        self.ttk.Button(parent, text="Browse…", command=command).grid(row=row, column=3, padx=(8, 0))

    def _help(self, parent: Any, row: int, column: int, key: str, *, pack: bool = False) -> None:
        description = CONFIG_SETTING_COMMENTS.get(key, "This setting is documented in the generated dualmaker configuration file.")
        button = self.ttk.Button(
            parent,
            text="?",
            width=3,
            command=lambda: self.messagebox.showinfo(key, description, parent=self.root),
        )
        if pack:
            button.pack(side="left", padx=(4, 8))
        else:
            button.grid(row=row, column=column, padx=(4, 8))

    def _entry_setting(self, parent: Any, row: int, column: int, label: str, variable: Any, key: str) -> None:
        self.ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w")
        self._help(parent, row, column + 1, key)
        self.ttk.Entry(parent, textvariable=variable).grid(row=row, column=column + 2, sticky="ew", padx=(0, 12))

    def _combo_setting(self, parent: Any, row: int, column: int, label: str, variable: Any, values: tuple[str, ...], key: str) -> None:
        self.ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w")
        self._help(parent, row, column + 1, key)
        self.ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=16).grid(
            row=row, column=column + 2, sticky="ew", padx=(0, 12)
        )

    def _check_setting(self, parent: Any, column: int, label: str, variable: Any, key: str) -> None:
        frame = self.ttk.Frame(parent)
        frame.grid(row=0, column=column, sticky="w", padx=(0, 12))
        self.ttk.Checkbutton(frame, text=label, variable=variable).pack(side="left")
        self._help(frame, 0, 1, key, pack=True)

    def _choose_folder(self) -> None:
        chosen = self.filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.cwd()))
        if chosen:
            self.path_var.set(chosen)

    def _choose_config(self) -> None:
        chosen = self.filedialog.askopenfilename(
            filetypes=(("YAML/TOML", "*.yml *.yaml *.toml"), ("All files", "*.*"))
        )
        if chosen:
            self.config_path_var.set(chosen)
            self.settings_loaded = False

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_form_from_config(self, config: DualMakerConfig) -> None:
        self.output_dir_var.set(str(config.output_dir or ""))
        self.tag_var.set(config.tag)
        self.dub_language_var.set(config.dub_language)
        self.conflict_var.set(config.conflict)
        self.subtitle_policy_var.set(config.subtitle_policy)
        self.recursive_var.set(config.recursive)
        self.trim_recap_var.set(config.trim_recap)
        self.end_trim_var.set(config.end_trim)
        self.reconcile_av_var.set(config.reconcile_av)
        self.experimental_fps_var.set(config.allow_experimental_fps_sync)
        self.tvrip_sync_var.set(config.allow_tvrip_segment_sync)
        self.dry_run_var.set(config.dry_run)
        self.progress_var.set(config.progress)

    def _form_config(self) -> DualMakerConfig:
        assert self.config is not None
        output_dir = self.output_dir_var.get().strip()
        return replace(
            self.config,
            path=Path(self.path_var.get()).expanduser().resolve(),
            recursive=self.recursive_var.get(),
            output_dir=Path(output_dir).expanduser().resolve() if output_dir else None,
            tag=self.tag_var.get().strip(),
            dub_language=self.dub_language_var.get().strip(),
            conflict=self.conflict_var.get(),
            subtitle_policy=self.subtitle_policy_var.get(),
            trim_recap=self.trim_recap_var.get(),
            end_trim=self.end_trim_var.get(),
            reconcile_av=self.reconcile_av_var.get(),
            allow_experimental_fps_sync=self.experimental_fps_var.get(),
            allow_tvrip_segment_sync=self.tvrip_sync_var.get(),
            dry_run=self.dry_run_var.get(),
            progress=self.progress_var.get(),
            interactive=False,
            output_format="plain",
        )

    def scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        path = Path(self.path_var.get()).expanduser()
        if not path.is_dir():
            self.messagebox.showerror("Scan folder", f"Not a directory: {path}", parent=self.root)
            return
        config_path = self.config_path_var.get().strip()
        recursive = self.recursive_var.get()
        self.status_var.set("Inspecting media files and calculating match candidates…")
        self._write_log(f"Scanning {path}")
        self.worker = threading.Thread(
            target=self._scan_worker,
            args=(path, Path(config_path) if config_path else None, recursive),
            daemon=True,
        )
        self.worker.start()

    def _scan_worker(self, path: Path, config_path: Path | None, recursive: bool) -> None:
        try:
            config = load_configuration(
                {"path": path, "recursive": recursive, "interactive": False, "progress": True},
                config_path=config_path,
                bootstrap_user_config=False,
            )
            runner = ToolRunner(quiet=True, binaries=config.binaries)
            assets, skipped = scan_assets(
                path,
                recursive=config.recursive,
                inspector=MediaInspector(runner),
                config=config,
            )
            grouped, unmatched = collect_pair_candidates(assets)
            self.events.put(("scanned", (config, assets, grouped, [*skipped, *unmatched])))
        except Exception as exc:  # noqa: BLE001 - worker errors must reach the desktop UI
            self.events.put(("error", str(exc)))

    def _populate_matches(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.rows.clear()
        self.selected_rows.clear()
        for candidates in self.grouped.values():
            for index, candidate in enumerate(candidates):
                iid = f"match-{len(self.rows)}"
                self.rows[iid] = candidate
                self.tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=("☑" if index == 0 else "☐", candidate.identity.title, f"{candidate.score:.3f}", candidate.normal.path.name, candidate.dual.path.name, ", ".join(candidate.shared_original_languages) or "event anchors"),
                )
                if index == 0:
                    self.selected_rows.add(iid)

    def _tree_click(self, event: Any) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row and column == "#1":
            self._toggle_row(row)

    def _tree_double_click(self, event: Any) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle_row(row)

    def _toggle_row(self, iid: str) -> None:
        candidate = self.rows[iid]
        if iid in self.selected_rows:
            self.selected_rows.remove(iid)
        else:
            for other in list(self.selected_rows):
                if self.rows[other].identity.key == candidate.identity.key:
                    self.selected_rows.remove(other)
                    self.tree.set(other, "selected", "☐")
            self.selected_rows.add(iid)
        self.tree.set(iid, "selected", "☑" if iid in self.selected_rows else "☐")

    def select_best(self) -> None:
        self.selected_rows.clear()
        for iid, candidate in self.rows.items():
            if all(self.rows[other].identity.key != candidate.identity.key for other in self.selected_rows):
                self.selected_rows.add(iid)
            self.tree.set(iid, "selected", "☑" if iid in self.selected_rows else "☐")

    def clear_selection(self) -> None:
        self.selected_rows.clear()
        for iid in self.rows:
            self.tree.set(iid, "selected", "☐")

    def add_manual_pair(self) -> None:
        normal = self.filedialog.askopenfilename(title="Choose master video", filetypes=(("Media", "*.mkv *.avi"),))
        if not normal:
            return
        dual = self.filedialog.askopenfilename(title="Choose DUAL audio source", filetypes=(("Media", "*.mkv *.avi"),))
        if not dual:
            return
        self.status_var.set("Inspecting manually selected pair…")
        self.worker = threading.Thread(target=self._manual_worker, args=(Path(normal), Path(dual)), daemon=True)
        self.worker.start()

    def _manual_worker(self, normal: Path, dual: Path) -> None:
        try:
            config = self.config or load_configuration({"path": normal.parent, "interactive": False}, bootstrap_user_config=False)
            runner = ToolRunner(quiet=True, binaries=config.binaries)
            candidate = require_explicit_pair(
                MediaInspector(runner).inspect(normal), MediaInspector(runner).inspect(dual)
            )
            self.events.put(("manual", (config, candidate)))
        except Exception as exc:  # noqa: BLE001 - worker errors must reach the desktop UI
            self.events.put(("error", str(exc)))

    def process_selected(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.selected_rows:
            self.messagebox.showwarning("No matches selected", "Select at least one match first.", parent=self.root)
            return
        try:
            config = self._form_config()
            validate_configuration(config, require_scan_path=True, validate_binaries=True)
        except Exception as exc:  # noqa: BLE001 - form validation is shown in a dialog
            self.messagebox.showerror("Configuration", str(exc), parent=self.root)
            return
        selected = [self.rows[iid] for iid in self.selected_rows]
        self.stop_requested.clear()
        self.progress.configure(maximum=max(1, len(selected)), value=0)
        self.status_var.set(f"Preparing {len(selected)} selected job(s)…")
        self.worker = threading.Thread(target=self._process_worker, args=(config, selected), daemon=True)
        self.worker.start()

    def _process_worker(self, config: DualMakerConfig, selected: list[PairCandidate]) -> None:
        try:
            plans, skipped, assets = plan_candidates(config, selected, assets=self.assets, skipped=[])
            self.events.put(("plans", (len(plans), skipped)))
            runner = ToolRunner(quiet=True, binaries=config.binaries)
            results: list[JobResult] = []
            for index, plan in enumerate(plans, 1):
                if self.stop_requested.is_set():
                    break
                title = plan.identity.title
                self.events.put(("phase", f"Synchronizing {title}"))
                if config.dry_run:
                    result = JobResult(status="planned", output=plan.output, message="Dry-run plan", plan=plan)
                else:
                    result = process_job(
                        plan,
                        config,
                        runner=runner,
                        on_phase=lambda phase, title=title: self.events.put(("phase", f"{phase}: {title}")),
                    )
                results.append(result)
                self.events.put(("progress", index))
            report_root = config.output_dir or config.path / config.output_dir_name
            report_path = config.report or default_report_path(Path(report_root).expanduser().resolve())
            write_report(
                report_path,
                {"version": __version__, "config": jsonable(config), "assets": [jsonable(asset) for asset in assets], "results": [result.to_dict() for result in results], "skipped": skipped},
            )
            self.events.put(("finished", (len(results), skipped, report_path)))
        except Exception as exc:  # noqa: BLE001 - worker errors must reach the desktop UI
            self.events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "scanned":
                    self.config, self.assets, self.grouped, skipped = payload
                    if not self.settings_loaded:
                        self._set_form_from_config(self.config)
                        self.settings_loaded = True
                    self._populate_matches()
                    self.status_var.set(f"Found {len(self.assets)} media file(s), {len(self.rows)} candidate pair(s).")
                    self._write_log(f"Found {len(self.rows)} match candidates.")
                    for item in skipped:
                        self._write_log(f"Skipped: {item}")
                elif event == "manual":
                    config, candidate = payload
                    if self.config is None:
                        self.config = config
                        self._set_form_from_config(config)
                        self.settings_loaded = True
                    self.assets.extend([candidate.normal, candidate.dual])
                    iid = f"match-{len(self.rows)}"
                    self.rows[iid] = candidate
                    self.tree.insert("", "end", iid=iid, values=("☑", candidate.identity.title, f"{candidate.score:.3f}", candidate.normal.path.name, candidate.dual.path.name, ", ".join(candidate.shared_original_languages) or "event anchors"))
                    self._toggle_row(iid)
                    self.status_var.set("Manual pair added and selected.")
                elif event == "plans":
                    count, skipped = payload
                    self.status_var.set(f"Prepared {count} job(s).")
                    for item in skipped:
                        self._write_log(f"Skipped: {item}")
                elif event == "phase":
                    self.status_var.set(payload)
                    self._write_log(payload)
                elif event == "progress":
                    self.progress.configure(value=payload)
                elif event == "finished":
                    count, skipped, report_path = payload
                    self.status_var.set(f"Finished {count} job(s). Report: {report_path}")
                    self._write_log(f"Report written to {report_path}")
                    for item in skipped:
                        self._write_log(f"Skipped: {item}")
                elif event == "error":
                    self.status_var.set("Operation failed.")
                    self._write_log(f"ERROR: {payload}")
                    self.messagebox.showerror("dualmaker", payload, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Cross-platform dualmaker desktop GUI")
    parser.add_argument("path", nargs="?", type=Path, help="Folder to scan")
    parser.add_argument("--config", type=Path, help="YAML/TOML configuration file")
    args = parser.parse_args(argv)
    tk, _ttk, _filedialog, _messagebox = _tk_modules()
    root = tk.Tk()
    DualMakerGUI(root, initial_path=args.path, config_path=args.config)
    root.mainloop()


if __name__ == "__main__":  # pragma: no cover - exercised by the installed console script
    main()


__all__ = ["DualMakerGUI", "main"]
