"""Shared paths for the eval harness. backend/app/evals/paths.py -> parents[3] is the repo root."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"
SQL_DIR = BACKEND / "sql"
FIXTURE_DIR = REPO_ROOT / "eval" / "fixtures"
FIXTURE_ZIP = FIXTURE_DIR / "gtfs_subset.zip"
FIXTURE_RT_DIR = FIXTURE_DIR / "rt"
FIXTURE_META = FIXTURE_DIR / "fixture.json"
CASES_FILE = REPO_ROOT / "eval" / "cases.json"
RESULTS_DIR = REPO_ROOT / "eval" / "results"
