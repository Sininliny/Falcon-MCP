"""Expose owned-session parameters as ``instance_id`` so relays that reserve ``session_id`` can call them.

Some cloud-to-device MCP relays treat ``session_id`` as their own transport field and drop it from
tool arguments, so every native tool failed with "Field required". Each tool parameter named
``session_id`` (or ``<prefix>_session_id``) is advertised as ``instance_id`` (``<prefix>_instance_id``);
the old name stays accepted for existing clients. Exactly one of the two must be given when the
original parameter was required.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Optional

from mcp.server.fastmcp.tools import Tool

OLD, NEW = "session_id", "instance_id"


def alias_name(name: str) -> str | None:
    if name == OLD or name.endswith("_" + OLD):
        return name[: -len(OLD)] + NEW
    return None


def _wrap(original, pairs: dict[str, tuple[str, bool]], is_async: bool):
    """pairs maps new name -> (old name, required)."""

    def resolve(kwargs: dict[str, Any]) -> dict[str, Any]:
        for new, (old, required) in pairs.items():
            given_new, given_old = kwargs.pop(new, None), kwargs.pop(old, None)
            if given_new is not None and given_old is not None and given_new != given_old:
                raise ValueError(f"{new} and {old} differ; pass only {new}")
            value = given_new if given_new is not None else given_old
            if value is None and required:
                raise ValueError(f"{new} is required (the id returned by falcon_native_host; "
                                 f"{old} is accepted as a legacy alias)")
            kwargs[old] = value
        return kwargs

    if is_async:
        @functools.wraps(original)
        async def wrapped(*args, **kwargs):
            return await original(*args, **resolve(kwargs))
    else:
        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            return original(*args, **resolve(kwargs))
    return wrapped


def _aliased_tool(tool: Tool) -> Tool | None:
    signature = inspect.signature(tool.fn, eval_str=True)
    pairs, params = {}, []
    for param in signature.parameters.values():
        new = alias_name(param.name)
        if new is None or new in signature.parameters:
            params.append(param)
            continue
        required = param.default is inspect.Parameter.empty
        annotation = param.annotation if not required else Optional[param.annotation]  # noqa: UP045
        pairs[new] = (param.name, required)
        # Keyword-only: the optional pair may precede required parameters without default.
        params.append(param.replace(name=new, kind=inspect.Parameter.KEYWORD_ONLY,
                                    annotation=annotation, default=None if required else param.default))
        params.append(param.replace(kind=inspect.Parameter.KEYWORD_ONLY, annotation=annotation,
                                    default=None if required else param.default))
    if not pairs:
        return None
    fn = _wrap(tool.fn, pairs, tool.is_async)
    ordered = [p for p in params if p.kind is not inspect.Parameter.KEYWORD_ONLY]
    ordered += [p for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY]
    fn.__signature__ = signature.replace(parameters=ordered)
    del fn.__wrapped__  # Signature and annotations now come from __signature__ only.
    fn.__annotations__ = {p.name: p.annotation for p in ordered}
    if signature.return_annotation is not inspect.Signature.empty:
        fn.__annotations__["return"] = signature.return_annotation
    aliased = Tool.from_function(fn, name=tool.name, title=tool.title, description=tool.description,
                                 context_kwarg=tool.context_kwarg, annotations=tool.annotations,
                                 icons=tool.icons, meta=tool.meta,
                                 structured_output=tool.fn_metadata.output_schema is not None)
    for new, (old, required) in pairs.items():
        props = aliased.parameters.get("properties", {})
        if new in props:
            props[new]["description"] = ("Owned VST3 instance id from falcon_native_host"
                                         + (" (required unless the legacy " + old + " is given)" if required else ""))
        if old in props:
            props[old]["description"] = f"Deprecated alias of {new}; some relays drop this field."
    return aliased


def install_session_alias(mcp) -> list[str]:
    """Rewrite registered tools in place; returns the names of tools that gained an alias."""
    manager = mcp._tool_manager
    changed = []
    for tool in manager.list_tools():
        aliased = _aliased_tool(tool)
        if aliased is not None:
            manager._tools[tool.name] = aliased
            changed.append(tool.name)
    return changed
