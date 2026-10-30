"""Operator-only, guarded recovery for the real VORA workflow 9242.

The default mode is read-only.  --execute is intentionally required before the
Engine recovery boundary can be called.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def configure_operator_environment() -> None:
    """Use only the dedicated test URL when the isolated runner exported it."""
    # Backend loads Settings from a working directory rooted at backend/.
    # Make the one-off runner use the same .env resolution even when invoked
    # from the repository root; secrets still come only from the operator's
    # environment and are never copied or printed here.
    os.chdir(BACKEND_ROOT)
    connection_url = os.environ.get("VORA_TEST_POSTGRES_URL")
    if connection_url:
        parsed = urlsplit(connection_url.replace("postgresql+psycopg://", "postgresql://", 1))
        os.environ["DB_HOST"] = parsed.hostname or ""
        os.environ["DB_PORT"] = str(parsed.port or 0)
        os.environ["DB_NAME"] = (parsed.path or "").lstrip("/")
        os.environ["DB_USER"] = unquote(parsed.username or "")
        os.environ["DB_PASSWORD"] = unquote(parsed.password or "")
    os.environ["REDIS_URL"] = "redis://localhost:6379/14"
    os.environ["GENERATION_QUEUE"] = "vora_real_ai_e2e_20261030"


configure_operator_environment()

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import engine
from app.models import (
    AssetType,
    FileAsset,
    NodeExecution,
    NodeExecutionStatus,
    WorkflowDispatchIntent,
    WorkflowExecution,
    WorkflowExecutionStatus,
)

WORKFLOW_ID = 9242
IMAGE_NODE_ID = 36125
EXPECTED_ASSET_IDS = (31241, 31242, 31243, 31244, 31245)
EXPECTED_DB_HOSTS = {"localhost", "127.0.0.1", "::1"}
EXPECTED_DB_PORT = 55432
EXPECTED_DB_NAME = "vora_test"
EXPECTED_QUEUE = "vora_real_ai_e2e_20261030"
EXPECTED_WORKER = "vora-real-ai-e2e@localhost"


@dataclass(frozen=True)
class RecoverySnapshot:
    workflow_status: str
    current_node_type: str | None
    image_output_asset_ids: tuple[int, ...]
    dispatch_intent_ids: tuple[int, ...]
    asset_signatures: tuple[tuple[int, int | None, str, str | None], ...]


class ImageGenerationBlockedExecutor:
    """Fail closed if recovery ever tries to execute IMAGE_GENERATE."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def execute(self, node_key: str, *args: Any, **kwargs: Any) -> Any:
        if node_key == "IMAGE_GENERATE":
            raise RuntimeError("Operator recovery safety guard blocked IMAGE_GENERATE")
        return self._delegate.execute(node_key, *args, **kwargs)


def build_recovery_engine(session: Session, workflow_version: int) -> Any:
    """Build the V3 engine and eagerly validate only the IMAGE_CHECK provider."""
    if settings.e2e_fake_providers:
        raise RuntimeError("Refusing recovery with fake providers enabled")
    if not settings.openai_api_key:
        raise RuntimeError("IMAGE_CHECK provider is not configured; refusing before DB write")

    # V3 recovery starts after IMAGE_GENERATE, so the general AI service bundle
    # is neither needed nor constructed.  It would require the unrelated
    # app-level LLMProvider and caused this operator-only path to refuse.
    from app.main import get_workflow_engine

    recovery_engine = get_workflow_engine(session, None, workflow_version)  # type: ignore[arg-type]
    executor = recovery_engine._executor  # type: ignore[attr-defined]
    image_check_factory = getattr(executor, "_image_check_model_factory", None)
    if not callable(image_check_factory):
        raise TypeError("IMAGE_CHECK provider factory is not configured; refusing before DB write")

    # Use the same lazy factory as Backend, but construct it before the Engine
    # can commit recovery state.  ChatOpenAI construction does not call OpenAI.
    executor._image_check_model = image_check_factory()
    executor._image_check_model_factory = None
    recovery_engine._executor = ImageGenerationBlockedExecutor(executor)  # type: ignore[attr-defined]
    return recovery_engine


def validate_runtime_configuration() -> None:
    if settings.db_host not in EXPECTED_DB_HOSTS:
        raise RuntimeError(f"Refusing database host: {settings.db_host}")
    if settings.db_port != EXPECTED_DB_PORT:
        raise RuntimeError(f"Refusing database port: {settings.db_port}")
    if settings.db_name != EXPECTED_DB_NAME:
        raise RuntimeError(f"Refusing database name: {settings.db_name}")
    if settings.generation_queue != EXPECTED_QUEUE:
        raise RuntimeError("Refusing unexpected generation queue")


def validate_celery_idle() -> None:
    inspector = celery_app.control.inspect([EXPECTED_WORKER])
    queues = inspector.active_queues()
    active = inspector.active()
    reserved = inspector.reserved()
    scheduled = inspector.scheduled()
    worker_queues = queues.get(EXPECTED_WORKER) if queues else None
    if worker_queues is None:
        raise RuntimeError("Dedicated Celery worker did not respond")
    queue_names = {item.get("name") for item in worker_queues}
    if queue_names != {EXPECTED_QUEUE}:
        raise RuntimeError(f"Unexpected worker queues: {sorted(queue_names)}")
    if (active or {}).get(EXPECTED_WORKER) or (reserved or {}).get(EXPECTED_WORKER) or (scheduled or {}).get(EXPECTED_WORKER):
        raise RuntimeError("Dedicated Celery worker is not idle")


def read_snapshot(session: Session) -> RecoverySnapshot:
    workflow = session.get(WorkflowExecution, WORKFLOW_ID)
    if workflow is None:
        raise RuntimeError("Workflow 9242 does not exist")
    if workflow.workflow_version != 3:
        raise RuntimeError("Workflow 9242 is not a V3 workflow")
    if workflow.status is not WorkflowExecutionStatus.RUNNING:
        raise RuntimeError(f"Workflow 9242 is not RUNNING: {workflow.status.value}")
    if workflow.current_node_type != "IMAGE_GENERATE":
        raise RuntimeError(f"Unexpected current node: {workflow.current_node_type}")

    image_node = session.get(NodeExecution, IMAGE_NODE_ID)
    if (
        image_node is None
        or image_node.workflow_execution_id != WORKFLOW_ID
        or image_node.node_type != "IMAGE_GENERATE"
        or image_node.status is not NodeExecutionStatus.SUCCEEDED
    ):
        raise RuntimeError("IMAGE_GENERATE NodeExecution 36125 is not the expected successful node")
    if not isinstance(image_node.output_data, dict):
        raise TypeError("IMAGE_GENERATE output_data is missing")
    output_asset_ids = tuple(image_node.output_data.get("asset_ids", ()))
    if output_asset_ids != EXPECTED_ASSET_IDS:
        raise RuntimeError(f"Unexpected generated asset IDs: {output_asset_ids}")

    image_checks = list(session.scalars(select(NodeExecution).where(
        NodeExecution.workflow_execution_id == WORKFLOW_ID,
        NodeExecution.node_type == "IMAGE_CHECK",
    )))
    if image_checks:
        raise RuntimeError("IMAGE_CHECK already exists; refusing duplicate recovery")

    intents = list(session.scalars(select(WorkflowDispatchIntent).where(
        WorkflowDispatchIntent.workflow_execution_id == WORKFLOW_ID,
    ).order_by(WorkflowDispatchIntent.id)))
    if not intents or any(intent.status != "COMPLETED" for intent in intents):
        raise RuntimeError("Workflow has a non-completed DispatchIntent; refusing recovery")

    assets = list(session.scalars(select(FileAsset).where(FileAsset.id.in_(EXPECTED_ASSET_IDS)).order_by(FileAsset.id)))
    if len(assets) != len(EXPECTED_ASSET_IDS) or any(
        asset.workflow_execution_id != WORKFLOW_ID
        or asset.created_by_node_execution_id != IMAGE_NODE_ID
        or asset.asset_type is not AssetType.IMAGE
        for asset in assets
    ):
        raise RuntimeError("Existing image assets do not match the guarded recovery scope")
    asset_signatures = tuple(
        (asset.id, asset.created_by_node_execution_id, asset.asset_type.value, asset.storage_key)
        for asset in assets
    )
    return RecoverySnapshot(
        workflow_status=workflow.status.value,
        current_node_type=workflow.current_node_type,
        image_output_asset_ids=output_asset_ids,
        dispatch_intent_ids=tuple(intent.id for intent in intents),
        asset_signatures=asset_signatures,
    )


def verify_post_state(session: Session, before: RecoverySnapshot) -> None:
    workflow = session.get(WorkflowExecution, WORKFLOW_ID)
    if workflow is None or workflow.status is not WorkflowExecutionStatus.WAITING_APPROVAL:
        raise RuntimeError("Recovery did not stop at WAITING_APPROVAL")
    if workflow.current_node_type != "HUMAN_APPROVAL":
        raise RuntimeError("Recovery did not stop at HUMAN_APPROVAL")
    image_node = session.get(NodeExecution, IMAGE_NODE_ID)
    if image_node is None or image_node.status is not NodeExecutionStatus.SUCCEEDED:
        raise RuntimeError("IMAGE_GENERATE changed during recovery")
    if tuple((image_node.output_data or {}).get("asset_ids", ())) != before.image_output_asset_ids:
        raise RuntimeError("Generated asset IDs changed during recovery")
    image_checks = list(session.scalars(select(NodeExecution).where(
        NodeExecution.workflow_execution_id == WORKFLOW_ID,
        NodeExecution.node_type == "IMAGE_CHECK",
    )))
    if len(image_checks) != 1 or image_checks[0].status is not NodeExecutionStatus.SUCCEEDED:
        raise RuntimeError("Recovery did not create exactly one successful IMAGE_CHECK")
    approvals = list(session.scalars(select(NodeExecution).where(
        NodeExecution.workflow_execution_id == WORKFLOW_ID,
        NodeExecution.node_type == "HUMAN_APPROVAL",
    )))
    if len(approvals) != 1 or approvals[0].status is not NodeExecutionStatus.WAITING:
        raise RuntimeError("Recovery did not create one waiting HUMAN_APPROVAL")
    if session.scalar(select(NodeExecution).where(
        NodeExecution.workflow_execution_id == WORKFLOW_ID,
        NodeExecution.node_type == "PUBLICATION",
    )) is not None:
        raise RuntimeError("Recovery unexpectedly created PUBLICATION")
    after_intents = tuple(session.scalars(select(WorkflowDispatchIntent.id).where(
        WorkflowDispatchIntent.workflow_execution_id == WORKFLOW_ID,
    ).order_by(WorkflowDispatchIntent.id)))
    if after_intents != before.dispatch_intent_ids:
        raise RuntimeError("Recovery created or removed a DispatchIntent")
    after_assets = tuple(session.scalars(select(FileAsset).where(FileAsset.id.in_(EXPECTED_ASSET_IDS)).order_by(FileAsset.id)))
    after_signatures = tuple(
        (asset.id, asset.created_by_node_execution_id, asset.asset_type.value, asset.storage_key)
        for asset in after_assets
    )
    if after_signatures != before.asset_signatures:
        raise RuntimeError("Existing FileAsset rows changed during recovery")


def run(*, execute: bool) -> int:
    validate_runtime_configuration()
    validate_celery_idle()
    with Session(engine) as session:
        before = read_snapshot(session)
        print("PRECHECK PASS: vora_test@localhost:55432, workflow=9242, IMAGE_GENERATE=36125, assets=31241..31245")
        print(f"CELERY PASS: worker={EXPECTED_WORKER}, queue={EXPECTED_QUEUE}, active=0, reserved=0, scheduled=0")
        print(f"ENGINE PATH PASS: completed IMAGE_GENERATE -> {('IMAGE_CHECK', 'HUMAN_APPROVAL')}")
        if not execute:
            print("DRY-RUN ONLY: no Engine call, DB write, task dispatch, Retry API, or provider call was made")
            return 0

        workflow = session.get(WorkflowExecution, WORKFLOW_ID)
        if workflow is None:
            raise RuntimeError("Workflow 9242 disappeared after preflight")
        recovery_engine = build_recovery_engine(session, workflow.workflow_version)
        recovery_engine.recover_interrupted_execution(WORKFLOW_ID)
        verify_post_state(session, before)
        print("RECOVERY PASS: IMAGE_CHECK=SUCCEEDED, HUMAN_APPROVAL=WAITING, PUBLICATION=absent")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded operator recovery for Workflow 9242")
    parser.add_argument("--execute", action="store_true", help="perform recovery; default is read-only dry-run")
    args = parser.parse_args()
    try:
        return run(execute=args.execute)
    except Exception as exc:  # noqa: BLE001
        print(f"RECOVERY REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
