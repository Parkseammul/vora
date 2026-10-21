"""Read-only Instagram publication reconciliation; it never creates or publishes media."""

import argparse

from sqlalchemy.orm import Session

from app.database import engine
from app.publication_runtime import get_publication_service


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("publication_id", type=int)
    args = parser.parse_args()
    with Session(engine) as session:
        recovered = get_publication_service(session).reconcile(args.publication_id)
    print("PUBLISHED" if recovered else "MANUAL_REVIEW_REQUIRED")
    return 0 if recovered else 2


if __name__ == "__main__":
    raise SystemExit(main())
