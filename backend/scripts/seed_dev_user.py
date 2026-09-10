import sys
from pathlib import Path

# Allow `python scripts/seed_dev_user.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
from app.models import User
from app.workflow_execution_service import DEV_USER_EMAIL


def main() -> None:
    with Session(engine) as session:
        user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
        if user is None:
            session.add(User(email=DEV_USER_EMAIL, name="VORA Dev"))
            session.commit()


if __name__ == "__main__":
    main()
