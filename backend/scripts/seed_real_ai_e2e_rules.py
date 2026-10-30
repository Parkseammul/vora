"""Create the explicit rules fixture used only by the isolated real-AI E2E database."""

import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

E2E_RULES_VERSION = 2026103001
E2E_RULES_DATA = {
    "purpose": "VORA isolated real-AI E2E Instagram carousel",
    "format": "instagram_carousel",
    "language": "ko",
    "slide_count": 5,
    "content_rules": [
        "Create exactly five slides: one cover, three body slides, and one closing slide.",
        "Use concise, readable Korean copy suitable for Instagram.",
    ],
    "visual_rules": [
        "Keep a consistent visual system across all five slides.",
        "Do not add platform watermarks or unrelated text.",
    ],
}


def _configure_dedicated_database() -> None:
    value = os.environ.get("VORA_TEST_POSTGRES_URL")
    if not value:
        raise SystemExit("VORA_TEST_POSTGRES_URL is required")
    url = make_url(value)
    if url.get_backend_name() != "postgresql" or url.database != "vora_test" or url.port != 55432:
        raise SystemExit("This seed is restricted to PostgreSQL vora_test on port 55432")
    os.environ["DB_HOST"] = url.host or ""
    os.environ["DB_PORT"] = str(url.port)
    os.environ["DB_NAME"] = url.database or ""
    os.environ["DB_USER"] = url.username or ""
    os.environ["DB_PASSWORD"] = url.password or ""


def main() -> None:
    _configure_dedicated_database()
    # Import after the guarded environment setup so the engine cannot target the default DB.
    from app.database import engine
    from app.models import RulesVersion

    with Session(engine) as session:
        existing = session.query(RulesVersion).filter_by(version=E2E_RULES_VERSION).one_or_none()
        if existing is None:
            existing = RulesVersion(version=E2E_RULES_VERSION, rules_data=E2E_RULES_DATA)
            session.add(existing)
            session.commit()
        elif existing.rules_data != E2E_RULES_DATA:
            raise SystemExit("The dedicated E2E rules version exists with different data")
        print(f"REAL_AI_E2E_RULES_ID={existing.id}|VERSION={existing.version}")


if __name__ == "__main__":
    main()
