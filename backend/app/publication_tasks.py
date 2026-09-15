from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.database import engine
from app.publication_runtime import get_publication_service


@celery_app.task(name="app.publication_tasks.execute_publication")
def execute_publication(publication_id: int) -> None:
    """Publication writes only its own aggregate and never advances a Workflow."""
    with Session(engine) as session:
        get_publication_service(session).execute(publication_id)
