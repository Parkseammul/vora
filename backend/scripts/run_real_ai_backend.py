"""Run the isolated E2E Backend with an explicitly pinned Python executable."""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import sys
from pathlib import Path


def _require_project_venv() -> None:
    expected = (Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "python.exe").resolve()
    actual = Path(sys.executable).resolve()
    if actual != expected:
        raise SystemExit(f"Backend must use project .venv Python: expected={expected}, actual={actual}")


def main() -> None:
    _require_project_venv()
    os.environ.pop("PYTHONHOME", None)
    os.environ["VIRTUAL_ENV"] = str(Path(sys.executable).resolve().parents[1])
    multiprocessing.set_executable(sys.executable)

    app_main = importlib.import_module("app.main")
    diagnostic_path = os.environ.get("VORA_RUNTIME_DIAGNOSTIC_PATH")
    if diagnostic_path:
        path = Path(diagnostic_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "sys_executable": sys.executable,
                    "sys_prefix": sys.prefix,
                    "sys_base_prefix": sys.base_prefix,
                    "app_main": str(Path(app_main.__file__).resolve()),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
