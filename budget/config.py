"""Path and environment configuration.

Every filesystem location the application uses is resolved here, so relocating the
database (see DESIGN.md section 6.5) is a config change rather than a code hunt.
"""

from __future__ import annotations

import os
from pathlib import Path

# The live database never lives in the project folder: a git clean, a repo move or an
# accidental commit must not be able to take financial data with it.
_DEFAULT_DB_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "BudgetDashboard"

DB_PATH = Path(os.environ.get("BUDGET_DB_PATH", _DEFAULT_DB_DIR / "budget.db"))

# The ancestor snapshot required by the three-way merge (DESIGN.md 6.3.3).
BASE_DB_PATH = Path(os.environ.get("BUDGET_BASE_DB_PATH", DB_PATH.with_name("budget.base.db")))

# Source workbook, used by the one-off migration and by reconciliation.
WORKBOOK_PATH = Path(
    os.environ.get("BUDGET_WORKBOOK_PATH", r"K:\Private\Finance\Budget 26-27.xlsm")
)

# The pension tracker, read once by budget.seed_pension to bring the history across. Nothing
# else reads it: from then on the pension lives in the database like everything else.
PENSION_PATH = Path(
    os.environ.get(
        "BUDGET_PENSION_PATH", r"K:\Private\Finance\Pension\Pension Tracker.xlsx"
    )
)

# Master copy on the NAS, alongside the workbook (DESIGN.md 6.3).
NAS_DIR = Path(os.environ.get("BUDGET_NAS_DIR", r"K:\Private\Finance\budget_db"))


def ensure_db_dir() -> Path:
    """Create the database directory if absent and return it."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH.parent
