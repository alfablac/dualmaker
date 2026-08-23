from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .defaults import BINARY_NAMES, BINARY_VERSION_ARGS, DEFAULT_MIN_MKVMERGE_VERSION
from .errors import DependencyError, ProcessingError

LOGGER = logging.getLogger("dualmaker")


@dataclass(slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ToolRunner:
    def __init__(
        self,
        *,
        quiet: bool = False,
        binaries: Mapping[str, str] | None = None,
    ) -> None:
        self.quiet = quiet
        self.binaries = dict(binaries or {})
        self.environment = os.environ.copy()
        configured_parents = []
        for value in self.binaries.values():
            path = Path(value).expanduser()
            if path.parent != Path(".") or path.is_absolute():
                configured_parents.append(str(path.resolve().parent))
        if configured_parents:
            current_path = self.environment.get("PATH", "")
            self.environment["PATH"] = os.pathsep.join((*configured_parents, current_path))

    def which(self, name: str) -> str | None:
        configured = self.binaries.get(name, name)
        path = Path(configured).expanduser()
        if path.parent != Path(".") or path.is_absolute():
            return str(path.resolve()) if path.is_file() else None
        return shutil.which(configured, path=self.environment.get("PATH"))

    def require(self, name: str) -> str:
        executable = self.which(name)
        if not executable:
            raise DependencyError(f"Required executable not found on PATH: {name}")
        return executable

    def run(
        self,
        args: Sequence[str | Path],
        *,
        check: bool = True,
        cwd: Path | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        command = tuple(str(item) for item in args)
        if command and command[0] in BINARY_NAMES:
            executable = self.require(command[0])
            command = (executable, *command[1:])
        LOGGER.debug("Running: %s", shlex.join(command))
        completed = subprocess.run(
            command,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            check=False,
            env=self.environment,
        )
        result = CommandResult(
            args=command,
            returncode=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise ProcessingError(
                f"{command[0]} exited with status {result.returncode}: {detail[-4000:]}"
            )
        return result

    def json(self, args: Sequence[str | Path], *, cwd: Path | None = None) -> dict[str, Any]:
        result = self.run(args, cwd=cwd)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProcessingError(f"Invalid JSON from {result.args[0]}: {exc}") from exc

    def run_live(
        self,
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        """Stream a long-running command while retaining its final diagnostics."""

        if self.quiet:
            return self.run(args, check=False, cwd=cwd)
        command = tuple(str(item) for item in args)
        if command and command[0] in BINARY_NAMES:
            executable = self.require(command[0])
            command = (executable, *command[1:])
        LOGGER.debug("Running: %s", shlex.join(command))
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment,
        )
        tail: deque[str] = deque(maxlen=300)
        assert process.stdout is not None
        for line in process.stdout:
            tail.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()
        returncode = process.wait()
        return CommandResult(command, returncode, "", "".join(tail))


def check_dependencies(
    runner: ToolRunner | None = None,
    *,
    minimum_mkvmerge_version: int = DEFAULT_MIN_MKVMERGE_VERSION,
) -> dict[str, dict[str, Any]]:
    runner = runner or ToolRunner()
    checks: dict[str, dict[str, Any]] = {}
    for tool in BINARY_NAMES:
        path = runner.which(tool)
        if not path:
            checks[tool] = {"ok": False, "path": None, "version": None}
            continue
        result = runner.run((path, *BINARY_VERSION_ARGS[tool]), check=False)
        first_line = (result.stdout or result.stderr).splitlines()
        version = first_line[0] if first_line else "unknown"
        ok = result.returncode in (0, 1)
        if tool == "mkvmerge":
            match = re.search(r"mkvmerge v(\d+)", version)
            if not match or int(match.group(1)) < minimum_mkvmerge_version:
                ok = False
                version += f" (dualmaker requires v{minimum_mkvmerge_version} or newer)"
        checks[tool] = {
            "ok": ok,
            "path": path,
            "version": version,
        }
    return checks
