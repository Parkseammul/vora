from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ApprovalDecision,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    Revision,
    UserApproval,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import (
    NodeExecutionResult,
    NodeExecutor,
    NodeExecutorConfigurationError,
)
from app.video_async import VideoTaskDispatcher
from app.video_generation import TransientVideoProviderError
from app.workflow_definitions import WorkflowDefinition, WorkflowRegistry


class WorkflowEngine:
    def __init__(
        self,
        session: Session,
        registry: WorkflowRegistry,
        executor: NodeExecutor,
        video_task_dispatcher: VideoTaskDispatcher | None = None,
        execution_guard: Callable[[], bool] | None = None,
        execution_fence: Callable[[], bool] | None = None,
    ) -> None:
        self._session = session
        self._registry = registry
        self._executor = executor
        self._video_task_dispatcher = video_task_dispatcher
        self._execution_guard = execution_guard
        self._execution_fence = execution_fence

    def set_execution_guard(self, execution_guard: Callable[[], bool] | None) -> None:
        """Attach worker-owned fencing without giving executors state authority."""
        self._execution_guard = execution_guard

    def set_execution_fence(self, execution_fence: Callable[[], bool] | None) -> None:
        """Attach a same-session row lock for the final workflow state commit."""
        self._execution_fence = execution_fence

    def start_execution(
        self,
        workflow_execution_id: int,
        input_data: Mapping[str, Any] | None = None,
    ) -> WorkflowExecutionStatus:
        self._assert_execution_guard()
        execution = self._get_execution(workflow_execution_id)
        if execution.status is not WorkflowExecutionStatus.PENDING:
            raise ValueError("Only a PENDING workflow execution can be started")

        definition = self._definition_for(execution)
        execution.status = WorkflowExecutionStatus.RUNNING
        execution.started_at = self._now()
        self._commit_state()
        return self._run_nodes(execution, definition, definition.start_node_key, input_data or {})

    def resume_after_approval(self, workflow_execution_id: int) -> WorkflowExecutionStatus:
        self._assert_execution_guard()
        execution = self._get_execution(workflow_execution_id)
        if execution.status is not WorkflowExecutionStatus.WAITING_APPROVAL:
            raise ValueError("Workflow execution is not waiting for approval")

        definition = self._definition_for(execution)
        waiting_node = self._session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.status == NodeExecutionStatus.WAITING,
            ).order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
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

        waiting_node.status = NodeExecutionStatus.SUCCEEDED
        waiting_node.completed_at = self._now()
        execution.status = WorkflowExecutionStatus.RUNNING
        self._commit_state()

        next_node_key = definition.next_node_key(waiting_node.node_type)
        if next_node_key is None:
            execution.status = WorkflowExecutionStatus.SUCCEEDED
            execution.current_node_type = None
            execution.completed_at = self._now()
            self._commit_state()
            return execution.status

        next_input = waiting_node.output_data or {}
        if next_node_key == "video_generation":
            planning = self._latest_success(execution.id, "content_planning")
            if planning is None or planning.output_data is None:
                raise ValueError("No successful planning result exists for video_generation")
            next_input = {"planning": planning.output_data, "script": next_input}
        return self._run_nodes(
            execution,
            definition,
            next_node_key,
            next_input,
            waiting_node.revision_no,
        )

    def start_revision(
        self,
        workflow_execution_id: int,
        target_node_key: str,
        revision_request: str,
    ) -> WorkflowExecutionStatus:
        """Starts a new user version without overwriting prior node execution history."""
        if not revision_request.strip():
            raise ValueError("revision_request must not be blank")
        execution = self._get_execution(workflow_execution_id)
        if execution.status is not WorkflowExecutionStatus.WAITING_APPROVAL:
            raise ValueError("Only a WAITING_APPROVAL workflow execution can be revised")

        definition = self._definition_for(execution)
        definition.node(target_node_key)
        waiting_node = self._latest_waiting_node(execution.id)
        if waiting_node is None:
            raise ValueError("No node execution is waiting for approval")
        node_keys = [node.key for node in definition.nodes]
        if node_keys.index(target_node_key) > node_keys.index(waiting_node.node_type):
            raise ValueError("Revision target cannot be downstream of the current approval node")
        revision_input = self._revision_input(
            execution, definition, target_node_key, revision_request
        )
        next_revision_no = self._next_revision_no(execution.id)
        execution.status = WorkflowExecutionStatus.RUNNING
        execution.completed_at = None
        self._commit_state()
        return self._run_nodes(
            execution,
            definition,
            target_node_key,
            revision_input,
            next_revision_no,
        )

    def start_rejected_revision(
        self,
        workflow_execution_id: int,
        target_node_key: str,
        revision_request: str,
        *,
        user_approval_id: int,
    ) -> WorkflowExecutionStatus:
        """Commit a user revision intent before executing its first external node."""
        try:
            execution, definition, node_execution, attempt = self.prepare_rejected_revision(
                workflow_execution_id,
                target_node_key,
                revision_request,
                user_approval_id=user_approval_id,
            )
            # Commit the rejection, revision intent, node, and attempt before external work.
            self._commit_state()
        except Exception:
            self._session.rollback()
            raise
        return self._execute_prepared_node(execution, definition, node_execution, attempt)

    def prepare_rejected_revision(
        self,
        workflow_execution_id: int,
        target_node_key: str,
        revision_request: str,
        *,
        user_approval_id: int,
    ) -> tuple[WorkflowExecution, WorkflowDefinition, NodeExecution, NodeExecutionAttempt]:
        """Prepare a revision without committing or executing an external provider.

        The API composes this Engine-owned state transition with its durable
        dispatch intent in one transaction.  A worker later resumes this exact
        prepared attempt, so a duplicate delivery cannot create another revision.
        """
        if not revision_request.strip():
            raise ValueError("revision_request must not be blank")
        execution = self._get_execution(workflow_execution_id)
        if execution.status is not WorkflowExecutionStatus.WAITING_APPROVAL:
            raise ValueError("Only a WAITING_APPROVAL workflow execution can be revised")
        definition = self._definition_for(execution)
        definition.node(target_node_key)
        waiting_node = self._latest_waiting_node(execution.id)
        if waiting_node is None:
            raise ValueError("No node execution is waiting for approval")
        node_keys = [node.key for node in definition.nodes]
        if node_keys.index(target_node_key) > node_keys.index(waiting_node.node_type):
            raise ValueError("Revision target cannot be downstream of the current approval node")
        approval = self._session.get(UserApproval, user_approval_id)
        if (
            approval is None
            or approval.workflow_execution_id != execution.id
            or approval.node_execution_id != waiting_node.id
            or approval.decision is not ApprovalDecision.REJECTED
        ):
            raise ValueError("Rejected approval does not match the current approval node")
        revision_input = self._revision_input(execution, definition, target_node_key, revision_request)
        revision_no = self._next_revision_no(execution.id)
        started_at = self._now()
        self._session.add(
            Revision(
                workflow_execution_id=execution.id,
                user_approval_id=approval.id,
                revision_no=revision_no,
                start_node_type=target_node_key,
                request_text=revision_request,
            )
        )
        node_execution = NodeExecution(
            workflow_execution_id=execution.id,
            node_type=target_node_key,
            revision_no=revision_no,
            input_data=revision_input,
            status=NodeExecutionStatus.RUNNING,
            started_at=started_at,
        )
        self._session.add(node_execution)
        self._session.flush()
        attempt = NodeExecutionAttempt(
            node_execution_id=node_execution.id,
            attempt_no=1,
            status=NodeExecutionAttemptStatus.RUNNING,
            started_at=started_at,
            metadata_={"retry_kind": "USER_REQUESTED"},
        )
        self._session.add(attempt)
        execution.status = WorkflowExecutionStatus.RUNNING
        execution.current_node_type = target_node_key
        execution.completed_at = None
        return execution, definition, node_execution, attempt

    def start_rejected_image_revision(
        self, workflow_execution_id: int, revision_request: str
    ) -> WorkflowExecutionStatus:
        """Backward-compatible IMAGE-only entry point for manual recovery callers."""
        approval = self._session.scalar(
            select(UserApproval)
            .where(
                UserApproval.workflow_execution_id == workflow_execution_id,
                UserApproval.decision == ApprovalDecision.REJECTED,
            )
            .order_by(UserApproval.id.desc())
            .limit(1)
        )
        if approval is None:
            raise ValueError("No rejected approval is available for IMAGE revision")
        return self.start_rejected_revision(
            workflow_execution_id,
            "IMAGE_GENERATE",
            revision_request,
            user_approval_id=approval.id,
        )

    def recover_prepared_revision(
        self, workflow_execution_id: int, *, force: bool = False
    ) -> WorkflowExecutionStatus:
        """Resume a committed-but-unfinished revision after a process restart."""
        execution = self._session.scalar(
            select(WorkflowExecution)
            .where(WorkflowExecution.id == workflow_execution_id)
            .with_for_update(skip_locked=True)
        )
        if execution is None:
            raise ValueError("Workflow execution is unavailable or another recovery is in progress")
        if execution.status is not WorkflowExecutionStatus.RUNNING:
            raise ValueError("Workflow execution is not running")
        node_execution = self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.status == NodeExecutionStatus.RUNNING,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
            .limit(1)
        )
        if node_execution is None:
            raise ValueError("No prepared revision node is available")
        attempt = self._session.scalar(
            select(NodeExecutionAttempt)
            .where(
                NodeExecutionAttempt.node_execution_id == node_execution.id,
                NodeExecutionAttempt.status == NodeExecutionAttemptStatus.RUNNING,
            )
            .order_by(NodeExecutionAttempt.attempt_no.desc())
            .limit(1)
        )
        if attempt is None:
            raise ValueError("Prepared revision node has no running attempt")
        if (
            self._execution_guard is None
            and attempt.metadata_.get("manual_recovery_claimed_at")
            and not force
        ):
            raise ValueError(
                "Prepared revision is already claimed for manual recovery; verify it is not running"
            )
        # Manual recovery remains operator-fenced. Generation workers instead use
        # the dispatch claim token supplied through execution_guard.
        if self._execution_guard is None:
            attempt.metadata_ = {
                **attempt.metadata_,
                "manual_recovery_claimed_at": self._now().isoformat(),
            }
            self._session.commit()
        if "execution_started_at" in attempt.metadata_:
            interrupted_at = self._now()
            attempt = self._replace_interrupted_attempt(node_execution, attempt, interrupted_at)
            self._session.commit()
        self._assert_execution_guard()
        return self._execute_prepared_node(
            execution, self._definition_for(execution), node_execution, attempt
        )

    def recover_interrupted_execution(self, workflow_execution_id: int) -> WorkflowExecutionStatus:
        """Continue only the durable RUNNING boundary left by a stopped generation worker."""
        self._assert_execution_guard()
        execution = self._get_execution(workflow_execution_id)
        if execution.status is WorkflowExecutionStatus.SUCCEEDED:
            return execution.status
        if execution.status is not WorkflowExecutionStatus.RUNNING:
            raise ValueError("Workflow execution is not recoverable")
        definition = self._definition_for(execution)
        running_node = self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.status == NodeExecutionStatus.RUNNING,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
            .limit(1)
        )
        if running_node is not None:
            attempt = self._session.scalar(
                select(NodeExecutionAttempt)
                .where(
                    NodeExecutionAttempt.node_execution_id == running_node.id,
                    NodeExecutionAttempt.status == NodeExecutionAttemptStatus.RUNNING,
                )
                .order_by(NodeExecutionAttempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is None:
                raise ValueError("Running node has no running attempt")
            if "execution_started_at" not in attempt.metadata_:
                # A prepared revision is durable before its first provider call.
                # If the process stopped before that boundary, reuse the prepared
                # Attempt rather than manufacturing a technical retry.
                return self._execute_prepared_node(execution, definition, running_node, attempt)
            # A worker crash must remain visible in the attempt history.  Do not
            # execute the same RUNNING attempt again: close it as an interrupted
            # system attempt and create the next attempt on the same NodeExecution.
            # This keeps the user revision number (revision_no) intact
            # while separating technical retries from user-requested revisions.
            interrupted_at = self._now()
            next_attempt = self._replace_interrupted_attempt(running_node, attempt, interrupted_at)
            self._commit_state()
            return self._execute_prepared_node(
                execution, definition, running_node, next_attempt
            )

        completed = list(
            self._session.scalars(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution.id,
                    NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
                )
            )
        )
        if not completed:
            raise ValueError("Running workflow has no recoverable node")
        node_order = {node.key: index for index, node in enumerate(definition.nodes)}
        latest = max(completed, key=lambda node: (node.revision_no, node_order[node.node_type], node.id))
        next_node_key = definition.next_node_key(latest.node_type)
        if next_node_key is None:
            execution.status = WorkflowExecutionStatus.SUCCEEDED
            execution.current_node_type = None
            execution.completed_at = self._now()
            self._commit_state()
            return execution.status
        return self._run_nodes(
            execution,
            definition,
            next_node_key,
            latest.output_data or {},
            latest.revision_no,
        )

    def retry_failed_execution(self, workflow_execution_id: int) -> WorkflowExecutionStatus:
        """Retries the latest failed node without creating a user revision version."""
        execution = self._get_execution(workflow_execution_id)
        if execution.status is not WorkflowExecutionStatus.FAILED:
            raise ValueError("Only a FAILED workflow execution can be retried")

        node_execution = self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.status == NodeExecutionStatus.FAILED,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        )
        if node_execution is None:
            raise ValueError("No failed node execution is available for retry")

        latest_attempt_no = self._session.scalar(
            select(NodeExecutionAttempt.attempt_no)
            .where(NodeExecutionAttempt.node_execution_id == node_execution.id)
            .order_by(NodeExecutionAttempt.attempt_no.desc())
            .limit(1)
        )
        if latest_attempt_no is None:
            raise ValueError("Failed node execution has no attempt history")
        definition = self._definition_for(execution)
        node_definition = definition.node(node_execution.node_type)
        started_at = self._now()
        node_execution.status = NodeExecutionStatus.RUNNING
        execution.current_node_type = node_execution.node_type
        node_execution.completed_at = None
        execution.status = WorkflowExecutionStatus.RUNNING
        execution.completed_at = None
        attempt = NodeExecutionAttempt(
            node_execution_id=node_execution.id,
            attempt_no=latest_attempt_no + 1,
            status=NodeExecutionAttemptStatus.RUNNING,
            started_at=started_at,
            metadata_={"retry_kind": "USER_REQUESTED"},
        )
        self._session.add(attempt)
        self._commit_state()

        if node_execution.node_type == "video_generation" and self._video_task_dispatcher is not None:
            return self._enqueue_video_attempt(execution, node_execution, attempt)

        try:
            execution_result = self._executor.execute(
                node_execution.node_type,
                node_execution.input_data,
                workflow_execution_id=execution.id,
                node_execution_id=node_execution.id,
            )
        except Exception as exc:  # noqa: BLE001
            finished_at = self._now()
            attempt.status = NodeExecutionAttemptStatus.FAILED
            attempt.error_code = self._error_code(exc)
            attempt.error_message = str(exc)
            attempt.completed_at = finished_at
            attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
            node_execution.status = NodeExecutionStatus.FAILED
            node_execution.completed_at = finished_at
            execution.status = WorkflowExecutionStatus.FAILED
            execution.current_node_type = None
            execution.completed_at = finished_at
            self._commit_state()
            return execution.status

        finished_at = self._now()
        attempt.status = NodeExecutionAttemptStatus.SUCCEEDED
        attempt.completed_at = finished_at
        attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
        if isinstance(execution_result, NodeExecutionResult):
            node_execution.output_data = execution_result.output_data
            attempt.metadata_ = {**attempt.metadata_, **execution_result.attempt_metadata}
            self._record_attempt_execution_details(attempt, execution_result)
        else:
            node_execution.output_data = execution_result
        if node_definition.requires_approval:
            node_execution.status = NodeExecutionStatus.WAITING
            execution.status = WorkflowExecutionStatus.WAITING_APPROVAL
            execution.current_node_type = node_execution.node_type
        else:
            node_execution.status = NodeExecutionStatus.SUCCEEDED
        node_execution.completed_at = finished_at
        self._commit_state()
        return execution.status

    def _run_nodes(
        self,
        execution: WorkflowExecution,
        definition: WorkflowDefinition,
        first_node_key: str,
        input_data: Mapping[str, Any],
        revision_no: int = 0,
    ) -> WorkflowExecutionStatus:
        node_key: str | None = first_node_key
        current_input = dict(input_data)

        while node_key is not None:
            self._assert_execution_guard()
            node_definition = definition.node(node_key)
            node_execution = NodeExecution(
                workflow_execution_id=execution.id,
                node_type=node_key,
                revision_no=revision_no,
                input_data=current_input,
                status=NodeExecutionStatus.PENDING,
            )
            self._session.add(node_execution)
            self._session.flush()

            started_at = self._now()
            node_execution.status = NodeExecutionStatus.RUNNING
            execution.current_node_type = node_execution.node_type
            node_execution.started_at = started_at
            if node_definition.requires_approval and not node_definition.creates_attempt:
                # HUMAN_APPROVAL has no executor. Preserve the verified upstream
                # result so the approved PUBLICATION node can consume it later.
                node_execution.output_data = current_input
                node_execution.status = NodeExecutionStatus.WAITING
                execution.status = WorkflowExecutionStatus.WAITING_APPROVAL
                execution.current_node_type = node_execution.node_type
                self._commit_state()
                return execution.status
            attempt = NodeExecutionAttempt(
                node_execution_id=node_execution.id,
                attempt_no=1,
                status=NodeExecutionAttemptStatus.RUNNING,
                started_at=started_at,
                metadata_={},
            )
            self._session.add(attempt)
            attempt.metadata_ = {"execution_started_at": started_at.isoformat()}
            self._commit_state()

            if node_key == "video_generation" and self._video_task_dispatcher is not None:
                return self._enqueue_video_attempt(execution, node_execution, attempt)

            try:
                execution_result = self._executor.execute(
                    node_key,
                    current_input,
                    workflow_execution_id=execution.id,
                    node_execution_id=node_execution.id,
                )
            # Executors wrap replaceable external implementations, so any execution error
            # must be persisted as a workflow failure at this boundary.
            except Exception as exc:  # noqa: BLE001
                finished_at = self._now()
                attempt.status = NodeExecutionAttemptStatus.FAILED
                attempt.error_code = self._error_code(exc)
                attempt.error_message = str(exc)
                attempt.completed_at = finished_at
                attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
                node_execution.status = NodeExecutionStatus.FAILED
                node_execution.completed_at = finished_at
                execution.status = WorkflowExecutionStatus.FAILED
                execution.current_node_type = None
                execution.completed_at = finished_at
                self._commit_state()
                return execution.status

            self._assert_execution_guard()
            finished_at = self._now()
            attempt.status = NodeExecutionAttemptStatus.SUCCEEDED
            attempt.completed_at = finished_at
            attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
            if isinstance(execution_result, NodeExecutionResult):
                output_data = execution_result.output_data
                attempt.metadata_ = execution_result.attempt_metadata
                self._record_attempt_execution_details(attempt, execution_result)
            else:
                output_data = execution_result
            node_execution.output_data = output_data
            if node_key == "TEXT_CHECK" and output_data.get("passed") is False:
                node_execution.status = NodeExecutionStatus.FAILED
                node_execution.completed_at = finished_at
                execution.status = WorkflowExecutionStatus.FAILED
                execution.current_node_type = None
                execution.completed_at = finished_at
                self._commit_state()
                return execution.status
            if node_definition.requires_approval:
                node_execution.status = NodeExecutionStatus.WAITING
                execution.status = WorkflowExecutionStatus.WAITING_APPROVAL
                execution.current_node_type = node_execution.node_type
                self._commit_state()
                return execution.status

            node_execution.status = NodeExecutionStatus.SUCCEEDED
            node_execution.completed_at = finished_at
            self._commit_state()
            current_input = output_data
            node_key = definition.next_node_key(node_key)

        execution.status = WorkflowExecutionStatus.SUCCEEDED
        execution.current_node_type = None
        execution.completed_at = self._now()
        self._commit_state()
        return execution.status

    def _execute_prepared_node(
        self,
        execution: WorkflowExecution,
        definition: WorkflowDefinition,
        node_execution: NodeExecution,
        attempt: NodeExecutionAttempt,
    ) -> WorkflowExecutionStatus:
        """Execute an already-committed node intent without opening a long DB transaction."""
        self._assert_execution_guard()
        node_definition = definition.node(node_execution.node_type)
        # Persist the execution boundary before invoking provider code.  Recovery
        # can then distinguish a prepared-but-never-started Attempt from one that
        # may have been interrupted during an external call.
        attempt.metadata_ = {
            **attempt.metadata_,
            "execution_started_at": self._now().isoformat(),
        }
        self._commit_state()
        try:
            execution_result = self._executor.execute(
                node_execution.node_type,
                node_execution.input_data,
                workflow_execution_id=execution.id,
                node_execution_id=node_execution.id,
            )
        except Exception as exc:  # noqa: BLE001
            finished_at = self._now()
            attempt.status = NodeExecutionAttemptStatus.FAILED
            attempt.error_code = self._error_code(exc)
            attempt.error_message = str(exc)
            attempt.completed_at = finished_at
            attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
            node_execution.status = NodeExecutionStatus.FAILED
            node_execution.completed_at = finished_at
            execution.status = WorkflowExecutionStatus.FAILED
            execution.current_node_type = None
            execution.completed_at = finished_at
            self._commit_state()
            return execution.status

        self._assert_execution_guard()
        finished_at = self._now()
        attempt.status = NodeExecutionAttemptStatus.SUCCEEDED
        attempt.completed_at = finished_at
        attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
        if isinstance(execution_result, NodeExecutionResult):
            output_data = execution_result.output_data
            attempt.metadata_ = {**attempt.metadata_, **execution_result.attempt_metadata}
            self._record_attempt_execution_details(attempt, execution_result)
        else:
            output_data = execution_result
        node_execution.output_data = output_data
        node_execution.completed_at = finished_at
        if node_execution.node_type == "TEXT_CHECK" and output_data.get("passed") is False:
            node_execution.status = NodeExecutionStatus.FAILED
            execution.status = WorkflowExecutionStatus.FAILED
            execution.current_node_type = None
            execution.completed_at = finished_at
            self._commit_state()
            return execution.status
        if node_definition.requires_approval:
            node_execution.status = NodeExecutionStatus.WAITING
            execution.status = WorkflowExecutionStatus.WAITING_APPROVAL
            execution.current_node_type = node_execution.node_type
            self._commit_state()
            return execution.status

        node_execution.status = NodeExecutionStatus.SUCCEEDED
        self._commit_state()
        next_node_key = definition.next_node_key(node_execution.node_type)
        if next_node_key is None:
            execution.status = WorkflowExecutionStatus.SUCCEEDED
            execution.current_node_type = None
            execution.completed_at = self._now()
            self._commit_state()
            return execution.status
        return self._run_nodes(
            execution,
            definition,
            next_node_key,
            output_data,
            node_execution.revision_no,
        )

    def _replace_interrupted_attempt(
        self,
        node_execution: NodeExecution,
        interrupted_attempt: NodeExecutionAttempt,
        started_at: datetime,
    ) -> NodeExecutionAttempt:
        """Close an interrupted attempt and create a technical retry."""
        interrupted_attempt.status = NodeExecutionAttemptStatus.FAILED
        interrupted_attempt.error_code = "INTERNAL_ERROR"
        interrupted_attempt.error_message = "Worker interrupted before attempt completion"
        interrupted_attempt.completed_at = started_at
        interrupted_attempt.latency_ms = self._latency_ms(
            interrupted_attempt.started_at, started_at
        )
        interrupted_attempt.metadata_ = {
            **interrupted_attempt.metadata_,
            "recovery_reason": "WORKER_INTERRUPTED",
        }
        next_attempt = NodeExecutionAttempt(
            node_execution_id=node_execution.id,
            attempt_no=interrupted_attempt.attempt_no + 1,
            status=NodeExecutionAttemptStatus.RUNNING,
            started_at=started_at,
            metadata_={
                "retry_kind": "SYSTEM",
                "recovery_reason": "WORKER_INTERRUPTED",
            },
        )
        self._session.add(next_attempt)
        return next_attempt

    @staticmethod
    def _record_attempt_execution_details(
        attempt: NodeExecutionAttempt, result: NodeExecutionResult
    ) -> None:
        """Persist only executor-supplied AI provenance; state remains Engine-owned."""
        attempt.ai_company = result.ai_company
        attempt.model = result.model
        attempt.prompt_version_id = result.prompt_version_id

    def _assert_execution_guard(self) -> None:
        if self._execution_guard is not None and not self._execution_guard():
            raise RuntimeError("Generation dispatch lease ownership was lost")

    def _commit_state(self) -> None:
        if self._execution_fence is not None and not self._execution_fence():
            self._session.rollback()
            raise RuntimeError("Generation dispatch lease ownership was lost")
        self._session.commit()

    @staticmethod
    def _latency_ms(started_at: datetime | None, completed_at: datetime) -> int | None:
        if started_at is None:
            return None
        return max(0, int((completed_at - started_at).total_seconds() * 1000))

    @staticmethod
    def _error_code(error: Exception) -> str:
        """Classify durable observability data without affecting retry decisions."""
        if isinstance(error, HTTPError):
            if error.code == 429:
                return "RATE_LIMIT"
            if 400 <= error.code < 500:
                return "PROVIDER_4XX"
            if 500 <= error.code < 600:
                return "PROVIDER_5XX"
        if isinstance(error, TimeoutError):
            return "TIMEOUT"
        if isinstance(error, (URLError, ConnectionError)):
            return "NETWORK_ERROR"
        if isinstance(error, TransientVideoProviderError):
            return "PROVIDER_TRANSIENT"
        if isinstance(error, NodeExecutorConfigurationError):
            return "CONFIGURATION_ERROR"
        if isinstance(error, ValueError):
            return "VALIDATION_ERROR"
        return "INTERNAL_ERROR"

    def complete_video_attempt_success(
        self, node_execution_id: int, attempt_id: int, execution_result: NodeExecutionResult
    ) -> WorkflowExecutionStatus:
        """Only the Engine turns a worker result into durable workflow state."""
        node_execution, attempt, execution = self._video_attempt_context(
            node_execution_id, attempt_id
        )
        if attempt.status is not NodeExecutionAttemptStatus.RUNNING:
            return execution.status

        finished_at = self._now()
        attempt.status = NodeExecutionAttemptStatus.SUCCEEDED
        attempt.completed_at = finished_at
        attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
        attempt.metadata_ = {
            **attempt.metadata_,
            **execution_result.attempt_metadata,
            "queue_state": "COMPLETED",
        }
        node_execution.output_data = execution_result.output_data
        node_execution.status = NodeExecutionStatus.WAITING
        execution.status = WorkflowExecutionStatus.WAITING_APPROVAL
        execution.current_node_type = node_execution.node_type
        self._session.commit()
        self._publish_video_progress(execution.id, "COMPLETED", "Video generation completed")
        return execution.status

    def complete_video_attempt_failure(
        self, node_execution_id: int, attempt_id: int, error: Exception
    ) -> WorkflowExecutionStatus:
        """Records a technical attempt, then queues a fresh Attempt only when retryable."""
        node_execution, attempt, execution = self._video_attempt_context(
            node_execution_id, attempt_id
        )
        if attempt.status is not NodeExecutionAttemptStatus.RUNNING:
            return execution.status

        finished_at = self._now()
        attempt.status = NodeExecutionAttemptStatus.FAILED
        attempt.error_code = self._error_code(error)
        attempt.error_message = str(error)
        attempt.completed_at = finished_at
        attempt.latency_ms = self._latency_ms(attempt.started_at, finished_at)
        attempt.metadata_ = {**attempt.metadata_, "queue_state": "FAILED"}
        if self._is_retryable_video_error(error) and attempt.attempt_no < 3:
            node_execution.status = NodeExecutionStatus.RUNNING
            execution.current_node_type = node_execution.node_type
            retry_attempt = NodeExecutionAttempt(
                node_execution_id=node_execution.id,
                attempt_no=attempt.attempt_no + 1,
                status=NodeExecutionAttemptStatus.RUNNING,
                started_at=finished_at,
                metadata_={},
            )
            self._session.add(retry_attempt)
            self._session.commit()
            return self._enqueue_video_attempt(execution, node_execution, retry_attempt)

        node_execution.status = NodeExecutionStatus.FAILED
        node_execution.completed_at = finished_at
        execution.status = WorkflowExecutionStatus.FAILED
        execution.current_node_type = None
        execution.completed_at = finished_at
        self._session.commit()
        self._publish_video_progress(execution.id, "FAILED", str(error))
        return execution.status

    def _enqueue_video_attempt(
        self,
        execution: WorkflowExecution,
        node_execution: NodeExecution,
        attempt: NodeExecutionAttempt,
    ) -> WorkflowExecutionStatus:
        if self._video_task_dispatcher is None:
            raise RuntimeError("video task dispatcher is not configured")
        # A worker may consume this task immediately, so its RUNNING state must be durable first.
        node_execution.status = NodeExecutionStatus.RUNNING
        execution.current_node_type = node_execution.node_type
        execution.status = WorkflowExecutionStatus.RUNNING
        attempt.metadata_ = {**attempt.metadata_, "queue_state": "QUEUED"}
        self._session.commit()
        try:
            queued = self._video_task_dispatcher.enqueue(execution.id, node_execution.id, attempt.id)
        except Exception as exc:  # noqa: BLE001
            return self.complete_video_attempt_failure(node_execution.id, attempt.id, exc)

        # The worker can finish between enqueue and this point. Refresh before adding task
        # tracking so stale request-process objects never regress terminal workflow state.
        self._session.refresh(attempt)
        self._session.refresh(node_execution)
        self._session.refresh(execution)
        attempt.metadata_ = {
            **attempt.metadata_,
            "celery_task_id": queued.task_id,
            "queue": queued.queue,
        }
        self._session.commit()
        return execution.status

    def _video_attempt_context(
        self, node_execution_id: int, attempt_id: int
    ) -> tuple[NodeExecution, NodeExecutionAttempt, WorkflowExecution]:
        node_execution = self._session.get(NodeExecution, node_execution_id)
        attempt = self._session.get(NodeExecutionAttempt, attempt_id)
        if node_execution is None or attempt is None or attempt.node_execution_id != node_execution.id:
            raise ValueError("Video generation attempt does not exist")
        execution = self._get_execution(node_execution.workflow_execution_id)
        return node_execution, attempt, execution

    def _publish_video_progress(self, workflow_execution_id: int, stage: str, message: str) -> None:
        if self._video_task_dispatcher is not None:
            # Progress delivery is best-effort and cannot invalidate PostgreSQL workflow state.
            try:
                self._video_task_dispatcher.publish_progress(workflow_execution_id, stage, message)
            except Exception:  # noqa: BLE001
                return

    @staticmethod
    def _is_retryable_video_error(error: Exception) -> bool:
        if isinstance(
            error, (TimeoutError, ConnectionError, URLError, TransientVideoProviderError)
        ):
            return True
        return isinstance(error, HTTPError) and 500 <= error.code < 600

    def _revision_input(
        self,
        execution: WorkflowExecution,
        definition: WorkflowDefinition,
        target_node_key: str,
        revision_request: str,
    ) -> dict[str, Any]:
        node_keys = [node.key for node in definition.nodes]
        target_index = node_keys.index(target_node_key)
        previous_target = self._latest_node_execution(execution.id, target_node_key)

        if target_index == 0:
            if previous_target is None:
                raise ValueError("No prior input_analysis execution is available for revision")
            revision_input = dict(previous_target.input_data)
        else:
            upstream_key = node_keys[target_index - 1]
            upstream = self._latest_success(execution.id, upstream_key)
            if upstream is None or upstream.output_data is None:
                raise ValueError(f"No successful upstream result exists for {target_node_key}")
            revision_input = dict(upstream.output_data)

        # A revision needs both the approved upstream result and the prior target result.
        if previous_target is not None and previous_target.output_data is not None:
            revision_input["previous_output"] = previous_target.output_data
        revision_input["revision_request"] = revision_request
        return revision_input

    def _latest_success(
        self, workflow_execution_id: int, node_key: str
    ) -> NodeExecution | None:
        return self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == workflow_execution_id,
                NodeExecution.node_type == node_key,
                NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        )

    def _latest_node_execution(
        self, workflow_execution_id: int, node_key: str
    ) -> NodeExecution | None:
        return self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == workflow_execution_id,
                NodeExecution.node_type == node_key,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        )

    def _latest_waiting_node(self, workflow_execution_id: int) -> NodeExecution | None:
        return self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == workflow_execution_id,
                NodeExecution.status == NodeExecutionStatus.WAITING,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        )

    def _next_revision_no(self, workflow_execution_id: int) -> int:
        latest = self._session.scalar(
            select(NodeExecution.revision_no)
            .where(NodeExecution.workflow_execution_id == workflow_execution_id)
            .order_by(NodeExecution.revision_no.desc())
            .limit(1)
        )
        return (latest or 0) + 1

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
