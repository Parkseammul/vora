# Manual recovery of a prepared image revision

This is an operator-only MVP procedure for a V3 `IMAGE_GENERATE` revision that was committed but whose process stopped before it could finish. There is no scheduler or Celery auto-recovery in this release.

## Preconditions

Use this only for the specific `workflow_execution_id` in the incident record. The recovery target must be `RUNNING`, with a latest running node and running attempt for the prepared revision. The command validates these conditions; it cannot resume a waiting approval, completed workflow, or arbitrary failed node.

Before running it, verify that the original API/worker process is no longer executing the attempt. Recovery can invoke `IMAGE_GENERATE` and downstream `IMAGE_CHECK`; with production providers this can call external AI APIs and incur charges. Do not use this production procedure as a test.

## Procedure

From `backend/`, with normal production database, storage, and provider settings:

```powershell
python scripts/recover_prepared_revision.py <workflow_execution_id>
```

The engine takes a row lock, writes `manual_recovery_claimed_at` on the existing running attempt, then commits that short transaction before any provider call. A simultaneous operator either cannot obtain the lock or observes the persisted claim and exits without executing a provider. No DB transaction is held during AI work.

If the command reports an existing claim, confirm that its owner has stopped. Only then use the explicit override:

```powershell
python scripts/recover_prepared_revision.py <workflow_execution_id> --force
```

`--force` can cause a second provider invocation if that confirmation is wrong, so it is deliberately an operator action rather than automatic behavior.

## Outcomes and follow-up

On success, the existing `NodeExecution` and `NodeExecutionAttempt` are reused; no new user revision is created. The normal workflow engine proceeds through remaining V3 nodes and may stop at `HUMAN_APPROVAL`.

If a provider or storage call fails after the claim commit, the workflow engine records the existing node and attempt as `FAILED` and marks the workflow `FAILED`. It does not delete committed rejection/revision intent. A process that stops after claiming but before execution leaves a `RUNNING` attempt with the claim; repeat the verification above and use `--force` only after the old process is confirmed dead.

Automatic detection, stale-claim expiry, and Celery-based recovery are deferred to operating-stability work. This command adds no REST endpoint.
