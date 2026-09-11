from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ApprovalDecision,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    UserApproval,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import NodeExecutionResult, NodeExecutor
from app.workflow_definitions import WorkflowDefinition, WorkflowRegistry


class WorkflowEngine:
    def __init__(
        self,
        session: Session,
        registry: WorkflowRegistry,
        executor: NodeExecutor,
    ) -> None:
        self._session = session
        self._registry = registry
        self._executor = executor

    def start_execution(
        self,
        workflow_execution_id: int,
        input_data: Mapping[str, Any] | None = None,
    ) -> WorkflowExecutionStatus:
        execution = self._get_execution(workflow_execution_id)
        if execution.status is not WorkflowExecutionStatus.PENDING:
            raise ValueError("Only a PENDING workflow execution can be started")

        definition = self._definition_for(execution)
        execution.status = WorkflowExecutionStatus.RUNNING
        execution.started_at = self._now()
        self._session.commit()
        return self._run_nodes(execution, definition, definition.start_node_key, input_data or {})

    def resume_after_approval(self, workflow_execution_id: int) -> WorkflowExecutionStatus:
        execution = self._get_execution(workflow_execution_id)
        if execution.status is not WorkflowExecutionStatus.WAITING_APPROVAL:
            raise ValueError("Workflow execution is not waiting for approval")

        definition = self._definition_for(execution)
        waiting_node = self._session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.status == NodeExecutionStatus.WAITING_APPROVAL,
            )
        )
        if waiting_node is None:
            raise ValueError("No node execution is waiting for approval")

        approval = self._session.scalar(
            select(UserApproval).where(
                UserApproval.node_execution_id == waiting_node.id,
                UserApproval.decision == ApprovalDecision.APPROVED,
            )
        )
        if approval is None:
            raise ValueError("The waiting node has not been approved")

        waiting_node.status = NodeExecutionStatus.SUCCESS
        waiting_node.finished_at = self._now()
        execution.status = WorkflowExecutionStatus.RUNNING
        self._session.commit()

        next_node_key = definition.next_node_key(waiting_node.node_key)
        if next_node_key is None:
            execution.status = WorkflowExecutionStatus.SUCCESS
            execution.finished_at = self._now()
            self._session.commit()
            return execution.status

        return self._run_nodes(
            execution,
            definition,
            next_node_key,
            waiting_node.output_data or {},
        )

    def _run_nodes(
        self,
        execution: WorkflowExecution,
        definition: WorkflowDefinition,
        first_node_key: str,
        input_data: Mapping[str, Any],
    ) -> WorkflowExecutionStatus:
        node_key: str | None = first_node_key
        current_input = dict(input_data)

        while node_key is not None:
            node_definition = definition.node(node_key)
            node_execution = NodeExecution(
                workflow_execution_id=execution.id,
                node_key=node_key,
                input_data=current_input,
                status=NodeExecutionStatus.PENDING,
            )
            self._session.add(node_execution)
            self._session.flush()

            started_at = self._now()
            node_execution.status = NodeExecutionStatus.RUNNING
            node_execution.started_at = started_at
            attempt = NodeExecutionAttempt(
                node_execution_id=node_execution.id,
                attempt_no=1,
                status=NodeExecutionAttemptStatus.RUNNING,
                started_at=started_at,
                metadata_={},
            )
            self._session.add(attempt)
            self._session.commit()

            try:
                execution_result = self._executor.execute(
                    node_key,
                    current_input,
                    workflow_execution_id=execution.id,
                )
            # Executors wrap replaceable external implementations, so any execution error
            # must be persisted as a workflow failure at this boundary.
            except Exception as exc:  # noqa: BLE001
                finished_at = self._now()
                attempt.status = NodeExecutionAttemptStatus.FAILED
                attempt.error_message = str(exc)
                attempt.finished_at = finished_at
                node_execution.status = NodeExecutionStatus.FAILED
                node_execution.finished_at = finished_at
                execution.status = WorkflowExecutionStatus.FAILED
                execution.finished_at = finished_at
                self._session.commit()
                return execution.status

            finished_at = self._now()
            attempt.status = NodeExecutionAttemptStatus.SUCCESS
            attempt.finished_at = finished_at
            if isinstance(execution_result, NodeExecutionResult):
                output_data = execution_result.output_data
                attempt.metadata_ = execution_result.attempt_metadata
            else:
                output_data = execution_result
            node_execution.output_data = output_data
            if node_definition.requires_approval:
                node_execution.status = NodeExecutionStatus.WAITING_APPROVAL
                execution.status = WorkflowExecutionStatus.WAITING_APPROVAL
                self._session.commit()
                return execution.status

            node_execution.status = NodeExecutionStatus.SUCCESS
            node_execution.finished_at = finished_at
            self._session.commit()
            current_input = output_data
            node_key = definition.next_node_key(node_key)

        execution.status = WorkflowExecutionStatus.SUCCESS
        execution.finished_at = self._now()
        self._session.commit()
        return execution.status

    def _get_execution(self, workflow_execution_id: int) -> WorkflowExecution:
        execution = self._session.get(WorkflowExecution, workflow_execution_id)
        if execution is None:
            raise ValueError(f"Workflow execution does not exist: {workflow_execution_id}")
        return execution

    def _definition_for(self, execution: WorkflowExecution) -> WorkflowDefinition:
        return self._registry.get(execution.workflow_key, execution.workflow_version)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
