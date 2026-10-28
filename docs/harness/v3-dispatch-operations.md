# V3 Dispatch Relay Operational Boundary

PostgreSQL `WorkflowDispatchIntent` records are the V3 recovery source of
truth. Redis and Celery only deliver an intent after it has been committed.
`GenerationDispatchService.relay_pending()` reconciles a pending enqueue or an
expired claim; an operator or scheduled process must run that relay for
lease-expired work to be redelivered.

The application does not yet configure a production Celery Beat/Reconciler
schedule. That is an operational design decision, not something a CI pass
completes. The process E2E suite covers START crash recovery, heartbeat, and
fencing. It does not yet cover a forced crash during RESUME or REVISION in a
separate worker process.

Instagram delivery is deliberately conservative: when a provider result is
ambiguous, VORA records the attempt for investigation and does not automatically
publish again. Workflow success (publication preparation) and an Instagram
`PUBLISHED` result remain separate states.
