from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Keep every temporary artifact private to the project instead of relying on a
# shared system /tmp directory. This takes effect before test modules import
# tempfile helpers.
TEST_WORK_ROOT = Path(__file__).resolve().parents[1] / ".test-work"
TEST_WORK_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(TEST_WORK_ROOT)
os.environ["DUALMAKER_CONFIG_HOME"] = str(TEST_WORK_ROOT / "config-home")
tempfile.tempdir = str(TEST_WORK_ROOT)
