from unittest.mock import Mock

import pytest

from scripts.recover_workflow_9242 import (
    EXPECTED_ASSET_IDS,
    EXPECTED_DB_NAME,
    EXPECTED_DB_PORT,
    EXPECTED_QUEUE,
    EXPECTED_WORKER,
    IMAGE_NODE_ID,
    WORKFLOW_ID,
    ImageGenerationBlockedExecutor,
    build_recovery_engine,
)


def test_operator_recovery_scope_is_fixed_to_workflow_9242() -> None:
    assert WORKFLOW_ID == 9242
    assert IMAGE_NODE_ID == 36125
    assert EXPECTED_ASSET_IDS == (31241, 31242, 31243, 31244, 31245)
    assert EXPECTED_DB_NAME == "vora_test"
    assert EXPECTED_DB_PORT == 55432
    assert EXPECTED_QUEUE == "vora_real_ai_e2e_20261030"
    assert EXPECTED_WORKER == "vora-real-ai-e2e@localhost"


def test_operator_recovery_blocks_image_generation() -> None:
    delegate = Mock()
    guarded = ImageGenerationBlockedExecutor(delegate)

    with pytest.raises(RuntimeError, match="blocked IMAGE_GENERATE"):
        guarded.execute("IMAGE_GENERATE", {}, workflow_execution_id=WORKFLOW_ID)
    delegate.execute.assert_not_called()

    guarded.execute("IMAGE_CHECK", {"asset_ids": list(EXPECTED_ASSET_IDS)})
    delegate.execute.assert_called_once_with("IMAGE_CHECK", {"asset_ids": list(EXPECTED_ASSET_IDS)})


def test_operator_recovery_refuses_before_engine_creation_without_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.recover_workflow_9242 as recovery

    monkeypatch.setattr(recovery.settings, "openai_api_key", None)
    engine_factory = Mock(side_effect=AssertionError("engine must not be built"))
    monkeypatch.setattr("app.main.get_workflow_engine", engine_factory)

    with pytest.raises(RuntimeError, match="provider is not configured"):
        build_recovery_engine(Mock(), 3)
    engine_factory.assert_not_called()


def test_operator_recovery_eagerly_constructs_existing_image_check_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.recover_workflow_9242 as recovery

    monkeypatch.setattr(recovery.settings, "openai_api_key", "configured-only-for-test")
    model = object()
    executor = Mock()
    executor._image_check_model_factory = Mock(return_value=model)
    engine = Mock(_executor=executor)
    monkeypatch.setattr("app.main.get_workflow_engine", Mock(return_value=engine))

    result = build_recovery_engine(Mock(), 3)

    assert result is engine
    assert executor._image_check_model is model
    assert executor._image_check_model_factory is None
