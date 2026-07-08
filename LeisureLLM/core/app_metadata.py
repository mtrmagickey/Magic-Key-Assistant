from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


ROOT_DIR = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = ROOT_DIR / "pyproject.toml"


def get_app_version(default: str = "0.8.0") -> str:
    if tomllib is None or not PYPROJECT_PATH.exists():
        return default
    try:
        with PYPROJECT_PATH.open("rb") as handle:
            data = tomllib.load(handle)
        return str(data.get("project", {}).get("version") or default)
    except Exception:
        return default