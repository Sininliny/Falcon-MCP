import pytest
from mcp.server.fastmcp.exceptions import ToolError

from falcon_mcp.server import create_server
from falcon_mcp.session_alias import alias_name

SID = "a" * 32


@pytest.fixture
def server(config):
    return create_server(config)


def test_alias_names():
    assert alias_name("session_id") == "instance_id"
    assert alias_name("falcon_session_id") == "falcon_instance_id"
    assert alias_name("session") is None
    assert alias_name("my_session_idx") is None


def test_every_session_parameter_is_advertised_as_instance_id(server):
    for tool in server._tool_manager.list_tools():
        props = tool.parameters.get("properties", {})
        for name in props:
            new = alias_name(name)
            if new:
                assert new in props, (tool.name, name)
                # A relay that drops session_id must not leave a required field behind.
                assert name not in tool.parameters.get("required", []), tool.name
                assert new not in tool.parameters.get("required", []), tool.name
    file_props = server._tool_manager.get_tool("falcon_native_file").parameters["properties"]
    assert file_props["instance_id"]["anyOf"][0]["pattern"] == "^[a-f0-9]{32}$"


async def test_instance_id_and_legacy_session_id_reach_the_tool():
    from typing import Annotated, Any

    from mcp.server.fastmcp import FastMCP
    from pydantic import Field

    from falcon_mcp.session_alias import install_session_alias

    mcp = FastMCP("t")
    seen = []

    @mcp.tool()
    async def native_like(session_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")], action: str,
                          part: int = 1) -> dict[str, Any]:
        seen.append((session_id, action, part))
        return {"ok": True}

    assert install_session_alias(mcp) == ["native_like"]
    tool = mcp._tool_manager.get_tool("native_like")
    assert tool.parameters["required"] == ["action"]
    assert tool.fn_metadata.output_schema is not None
    await mcp.call_tool("native_like", {"action": "a", "instance_id": SID})
    await mcp.call_tool("native_like", {"action": "b", "session_id": SID, "part": 2})
    await mcp.call_tool("native_like", {"action": "c", "instance_id": SID, "session_id": SID})
    assert seen == [(SID, "a", 1), (SID, "b", 2), (SID, "c", 1)]
    with pytest.raises(ToolError, match="instance_id is required"):
        await mcp.call_tool("native_like", {"action": "d"})
    with pytest.raises(ToolError, match="differ"):
        await mcp.call_tool("native_like", {"action": "e", "instance_id": SID, "session_id": "b" * 32})
    with pytest.raises(ToolError, match="pattern"):
        await mcp.call_tool("native_like", {"action": "f", "instance_id": "zz"})


async def test_native_host_stop_without_id_names_instance_id(server):
    with pytest.raises(ToolError, match="stop requires the inspected instance_id"):
        await server.call_tool("falcon_native_host", {"action": "stop"})
    status = await server.call_tool("falcon_native_host", {"action": "status"})
    payload = status[1] if isinstance(status, tuple) else status
    assert "instance_id" in str(payload)
