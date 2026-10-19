"""Operator-only recovery for a committed, unfinished V3 image revision."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Permit ``python scripts/recover_prepared_revision.py ...`` from backend/.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resume one committed-but-unfinished V3 IMAGE_GENERATE revision."
    )
    parser.add_argument("workflow_execution_id", type=int)
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "replace an existing manual recovery claim. Use only after verifying "
            "the process that owns that claim is no longer running."
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    # Keep --help usable on an operator workstation before production settings
    # are installed. Actual recovery still uses the normal application wiring.
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.main import get_ai_workflow_services, get_workflow_engine
    from app.models import WorkflowExecution

    with Session(engine) as session:
        execution = session.get(WorkflowExecution, args.workflow_execution_id)
        if execution is None:
            print(f"Workflow execution {args.workflow_execution_id} was not found.", file=sys.stderr)
            return 2
        try:
            workflow_engine = get_workflow_engine(
                session,
                get_ai_workflow_services(session),
                workflow_version=execution.workflow_version,
            )
            status = workflow_engine.recover_prepared_revision(
                execution.id, force=args.force
            )
        except ValueError as exc:
            session.rollback()
            print(f"Recovery was not started: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            print(f"Recovery failed: {exc}", file=sys.stderr)
            return 1
    print(f"Workflow execution {args.workflow_execution_id} recovery finished: {status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
