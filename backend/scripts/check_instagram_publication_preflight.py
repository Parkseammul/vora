"""Run offline-only preflight checks before an approved real Instagram smoke test."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from app.instagram_publication_preflight import (
        OfflineSettings,
        ready_for_real_smoke_test,
        run_preflight,
    )

    checks = run_preflight(OfflineSettings.from_current_directory())
    for check in checks:
        print(f"{check.name}: {check.status} - {check.detail}")
    ready = ready_for_real_smoke_test(checks)
    print(f"Ready for Real Smoke Test: {'YES' if ready else 'NO'}")
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
