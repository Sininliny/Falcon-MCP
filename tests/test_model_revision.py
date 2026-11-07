from falcon_mcp.config import DEFAULT_MODEL_REVISION, Config


def test_default_model_is_pinned_to_tested_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("FALCON_MCP_HOME", str(tmp_path))
    monkeypatch.delenv("FALCON_MODEL_ID", raising=False)
    monkeypatch.delenv("FALCON_MODEL_REVISION", raising=False)
    assert Config.from_env().model_revision == DEFAULT_MODEL_REVISION
    assert len(DEFAULT_MODEL_REVISION) == 40


def test_custom_model_follows_main_unless_revision_given(monkeypatch, tmp_path):
    monkeypatch.setenv("FALCON_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("FALCON_MODEL_ID", "someone/other-clap")
    monkeypatch.delenv("FALCON_MODEL_REVISION", raising=False)
    assert Config.from_env().model_revision == ""
    monkeypatch.setenv("FALCON_MODEL_REVISION", "abc123")
    assert Config.from_env().model_revision == "abc123"
