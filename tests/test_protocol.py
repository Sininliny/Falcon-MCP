import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_initialize_tools_resource_and_validation(config):
    env = os.environ.copy()
    env["FALCON_MCP_HOME"] = str(config.runtime.parent)
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=env)
    async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as client:
        info = await client.initialize()
        assert info.serverInfo.name == "falcon_mcp"
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert {
            "falcon_describe_timbre",
            "falcon_set_parameters",
            "falcon_record_test",
            "falcon_scan_project",
            "falcon_export_uml",
            "falcon_diagnose_audio",
            "falcon_get_logs",
            "falcon_api_coverage",
            "falcon_voice_control",
            "falcon_setup",
            "falcon_bind_instance",
            "falcon_capture_status",
        } <= names
        assert {"falcon_native_host", "falcon_native_file", "falcon_native_edit", "falcon_native_render",
                "falcon_analyze_reference", "falcon_compare_reference", "falcon_audio_meter"} <= names
        assert {"falcon_native_connections", "falcon_revision", "falcon_accept_instrument", "falcon_build_instrument"} <= names
        assert {"falcon_listen", "falcon_audio_diff", "falcon_match_target",
                "falcon_native_sweep", "falcon_native_param_sweep"} <= names
        assert {"falcon_sound_recipe", "falcon_design_check", "falcon_timbre_vocabulary", "falcon_design_verify",
                "falcon_param_lookup", "falcon_param_search", "falcon_param_value", "falcon_param_calibrate"} <= names
        assert {"falcon_native_perf_benchmark", "falcon_perf_static"} <= names
        assert {"falcon_ui_check", "falcon_ui_live_probe", "falcon_native_ui_roundtrip"} <= names
        assert {"falcon_native_stems", "falcon_stem_analyze"} <= names
        assert {"falcon_perform_phrases", "falcon_native_perform", "falcon_native_perform_suite", "falcon_perform_analyze"} <= names
        assert "falcon_capabilities" in names
        assert "falcon_sample_audit" in names
        assert len(names) == 82
        assert (config.runtime / "falcon_bridge.lua").is_file()
        assert all(t.outputSchema is not None for t in tools.tools)
        capture = await client.call_tool("falcon_capture_status", {})
        assert not capture.isError and capture.structuredContent["state"] == "idle"
        invalid_binding = await client.call_tool("falcon_bind_instance", {"instance": ""})
        assert invalid_binding.isError
        result = await client.call_tool("falcon_search_catalog", {"query": "Analog"})
        assert not result.isError and result.structuredContent["total_count"] > 0
        result = await client.call_tool(
            "falcon_get_parameters", {"selector": "Synth;exec()", "names": ["Gain"]}
        )
        assert result.isError
        capabilities = await client.read_resource("falcon://capabilities")
        assert "model_loaded" in capabilities.contents[0].text
        result = await client.call_tool("falcon_api_coverage", {"query": "playNote"})
        assert not result.isError and result.structuredContent["total_count"] > 0
        for _ in range(3):
            result = await client.call_tool("falcon_get_logs", {"limit": 2})
            assert not result.isError
    records = [
        json.loads(line)
        for path in config.log_root.glob("*.jsonl")
        for line in path.read_text(encoding="utf8").splitlines()
    ]
    requests = [r for r in records if r["event"] == "mcp_request"]
    assert any(
        r["request"].get("params", {}).get("name") == "falcon_get_parameters"
        for r in requests
        if r["request"].get("params")
    )
    log_reads = [r for r in records if r["event"] == "mcp_result" and r["result"].get("log_read")]
    assert len(log_reads) == 3  # polling logs must not recursively embed old log results
    assert all(len(json.dumps(r)) < 2000 for r in log_reads)
