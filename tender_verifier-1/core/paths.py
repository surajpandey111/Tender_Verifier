"""
Resolves file paths correctly whether running as normal Python source or as
a PyInstaller-frozen .exe. This matters because inside a frozen build,
`Path(__file__).parent` for pure-Python modules doesn't point to a real,
writable directory on disk (they're bundled inside the app's internal
archive) — using it for the SQLite database path caused
"sqlite3.OperationalError: unable to open database file" the first time
this was packaged as an .exe.

Rule of thumb used throughout the codebase:
  - Read-only bundled data (config/*.json)  -> BASE_DIR / "config" / ...
  - Writable runtime data (the DB, reports) -> BASE_DIR / ...
  BASE_DIR is the folder containing the .exe when frozen, or the project
  root when running from source — either way, a real writable directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # Running as a PyInstaller .exe.
    # - Writable runtime data (DB, reports) must live next to the .exe itself,
    #   a real user-writable folder — NOT inside the bundle.
    # - Read-only bundled data (config/*.json, added via `datas` in
    #   build_exe.spec) is extracted into sys._MEIPASS, which for a onedir
    #   build is the "_internal" folder next to the exe, not the exe's own
    #   folder. Using the wrong one of these two was the exact bug hit while
    #   testing this: config/document_rules.json wasn't found because it
    #   actually lives at <exe_folder>/_internal/config/, not <exe_folder>/config/.
    BASE_DIR = Path(sys.executable).parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    # Running from source — project root (parent of this file's "core" folder)
    BASE_DIR = Path(__file__).parent.parent
    BUNDLE_DIR = BASE_DIR

CONFIG_DIR = BUNDLE_DIR / "config"
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_DIR.mkdir(exist_ok=True)  # safe to call every startup; no-op if it already exists
