from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import Config


class BridgeError(RuntimeError):
    pass


def load_token(path: Path) -> str:
    try:
        token = json.loads(path.read_text(encoding="utf8"))["token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BridgeError(f"Unreadable bridge credential {path}; delete it and run falcon_setup") from exc
    if not isinstance(token, str) or not token:
        raise BridgeError(f"Unreadable bridge credential {path}; delete it and run falcon_setup")
    return token


@contextmanager
def preparation_lock(root: Path, timeout: float):
    """Serialize startup publishers, including creation of the shared credential."""
    with (root / "prepare.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BridgeError("Another MCP process is preparing this bridge; reconnect shortly")
                time.sleep(0.01)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def mailbox_lock(root: Path, timeout: float):
    """OS file lock prevents two MCP processes overwriting the same mailbox."""
    path = root / "mailbox.lock"
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BridgeError(
                        "Another local MCP client owns this Falcon mailbox; wait for it to finish"
                    )
                await asyncio.sleep(0.03)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def replace_file(source: Path, target: Path, timeout: float = 1.0) -> None:
    """os.replace, retrying Windows sharing violations while Falcon/AV/indexers hold the target open."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def atomic_json(path: Path, data: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, content: str) -> bool:
    """Publish whole source only when changed: Falcon watches this pathname."""
    if path.is_file() and path.read_text(encoding="utf8") == content:
        return False
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf8")
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


class FalconBridge:
    """One serialized mailbox. No automatic retries of engine mutations."""

    def __init__(self, config: Config, audit: AuditLog | None = None):
        self.config = config
        self.lock = asyncio.Lock()
        self.uncertain_request: str | None = None
        self._token: str | None = None
        self.bound_instance: str | None = None
        self.bound_programs: list[dict[str, str]] = []
        self.last_observed_instance: str | None = None
        self.audit = audit or AuditLog(config.log_root)

    async def bind_instance(self, instance: str, programs: list[dict[str, str]]) -> dict:
        """Bind this MCP process only after a fresh, unguarded identity check."""
        async with self.lock, mailbox_lock(self.config.runtime, self.config.bridge_timeout):
            engine = await self._request_locked("ping", None, False, self._read_token(), use_binding=False)
            if engine.get("instance") != instance:
                raise BridgeError("Instance changed before binding; inspect falcon_status and select the intended instance")
            if engine.get("instanceGuard") is not True:
                raise BridgeError("This runtime lacks instance guards; deploy the matching bridge while idle")
            if self._programs_changed(engine, programs):
                raise BridgeError("Program target changed before binding; inspect the current tree and names")
            self.bound_instance = instance
            self.bound_programs = [dict(p) for p in programs]
            result = {"instance": instance, "programs": self.bound_programs, "scope": "This MCP server process"}
            self.audit.event("bridge_bound", **result)
            return result

    def connection_status(self, engine: dict | None, error: str | None = None) -> dict:
        instance = (engine or {}).get("instance")
        previous = self.last_observed_instance
        changed = bool(instance and previous and instance != previous)
        if instance:
            self.last_observed_instance = instance
        reason = "connected"
        if error:
            reason = "no_authenticated_response"
        elif self.bound_instance and instance != self.bound_instance:
            reason = "bound_instance_changed"
        elif self.bound_programs and self._programs_changed(engine or {}):
            reason = "bound_program_changed"
        elif changed:
            reason = "instance_changed"
        return {
            "reason": reason, "current_instance": instance, "previous_instance": previous,
            "changed_since_last_status": changed, "bound_instance": self.bound_instance,
            "bound_programs": self.bound_programs, "runtime_home": str(self.config.runtime.parent),
            "possible_causes": [
                "Falcon closed or audio engine/plugin sleeping", "Master launcher removed by a Multi change",
                "Different runtime home or credential", "Multiple launchers sharing one mailbox",
            ] if error else [],
            "note": "A response identifies the answering runtime, not the foreground window. Use a separate home per instance.",
        }

    def _programs_changed(self, engine: dict, programs: list[dict[str, str]] | None = None) -> bool:
        targets = {p.get("selector"): p for p in engine.get("targets", [])}
        return any(
            targets.get(p["selector"], {}).get("name") != p["name"]
            or ("display_name" in p and targets.get(p["selector"], {}).get("displayName") != p["display_name"])
            for p in (self.bound_programs if programs is None else programs)
        )

    def prepare(self, *, probe: bool = False) -> dict:
        root = self.config.runtime
        root.mkdir(parents=True, exist_ok=True)
        with preparation_lock(root, self.config.bridge_timeout):
            return self._prepare(probe=probe)

    def _prepare(self, *, probe: bool) -> dict:
        root = self.config.runtime
        config_path = root / "connection.json"
        if config_path.exists():
            self._token = load_token(config_path)
        else:
            self._token = secrets.token_hex(32)
            atomic_json(config_path, {"protocol": 1, "token": self._token})
        # The generated script is one self-contained file; no Lua socket/io/os modules.
        source = files("falcon_mcp").joinpath("lua/bridge.lua").read_text(encoding="utf8")
        source += "\n" + files("falcon_mcp").joinpath("lua/extended.lua").read_text(encoding="utf8")
        source += "\n" + files("falcon_mcp").joinpath("lua/capture.lua").read_text(encoding="utf8")
        extension = root / "extension.lua"
        if extension.exists():
            source += (
                "\nlocal _coreInit = onInit\nlocal function _installExtension()\n"
                + extension.read_text(encoding="utf8")
                + "\nend\n_installExtension()\n"
                + "local _userInit = onInit\nfunction onInit()\nspawn(_coreInit)\n"
                + "if _userInit ~= _coreInit then _userInit() end\nend\n"
            )
        # A probe changes the file without changing the extension or user program.
        # Keep its nonce across normal starts to avoid a gratuitous script reload.
        nonce_path = root / "watch-nonce.json"
        if probe:
            atomic_json(nonce_path, {"nonce": uuid.uuid4().hex})
        nonce = json.loads(nonce_path.read_text(encoding="utf8"))["nonce"] if nonce_path.exists() else ""
        prefix = f"local BRIDGE_ROOT = {json.dumps(root.as_posix(), ensure_ascii=False)}\nlocal TOKEN = {json.dumps(self._token)}\n"
        build_id = hashlib.sha256((prefix + source + nonce).encode("utf8")).hexdigest()
        prefix += f"local BUILD_ID = {json.dumps(build_id)}\n"
        if len((prefix + source).encode("utf8")) > 262144:
            raise ValueError("Generated runtime exceeds 256 KiB; shorten the native extension")
        target = root / "falcon_bridge.lua"
        runtime_target = root / "falcon_runtime.lua"
        bootstrap = f"local BRIDGE_ROOT = {json.dumps(root.as_posix(), ensure_ascii=False)}\n"
        bootstrap += files("falcon_mcp").joinpath("lua/bootstrap.lua").read_text(encoding="utf8")
        if (root / "uncertain.json").exists() and (
            not runtime_target.exists()
            or runtime_target.read_text(encoding="utf8") != prefix + source
            or not target.exists()
            or target.read_text(encoding="utf8") != bootstrap
        ):
            raise BridgeError("Reconcile the previous uncertain mutation before updating the watched bridge")
        changed = atomic_text(runtime_target, prefix + source)
        loader_changed = atomic_text(target, bootstrap)
        atomic_text(
            root / "bootstrap-trigger.uvs",
            '<?xml version="1.0"?><UVI4>'
            '<ScriptProcessor API_version="23"><ScriptData/><state>'
            '{"_falconBootstrapControl":1}</state></ScriptProcessor></UVI4>',
        )
        result = {
            "script": str(target),
            "runtime_script": str(runtime_target),
            "runtime": str(root),
            "protocol": 1,
            "build_id": build_id,
            "changed": changed,
            "loader_changed": loader_changed,
            "instructions": "One-time setup: load falcon_bridge.lua in ONE Master (Synth) Script Processor; "
            "save as default Multi and in your DAW template. The persistent bootstrap loads runtime updates "
            "without WATCH FOR SCRIPT CHANGES. Run falcon-mcp setup --verify-reload to verify deployment. "
            "Keep the audio engine running.",
        }
        atomic_json(root / "deployment.json", {k: v for k, v in result.items() if k != "changed"})
        self.audit.event("bridge_prepared", **result)
        return result

    def _read_token(self) -> str:
        if self._token is None:
            path = self.config.runtime / "connection.json"
            if not path.is_file():
                raise BridgeError("Run falcon-mcp prepare, then load the generated Lua in Falcon.")
            self._token = load_token(path)
        return self._token

    async def request(
        self, op: str, args: dict | None = None, *, mutation: bool = False, use_binding: bool = True
    ) -> dict:
        if not use_binding and (op != "ping" or mutation):
            raise ValueError("Only diagnostic ping may bypass instance binding")
        self.audit.event("bridge_request", op=op, args=args or {}, mutation=mutation)
        started = time.monotonic()
        try:
            async with self.lock:
                token = self._read_token()
                async with mailbox_lock(self.config.runtime, self.config.bridge_timeout):
                    result = await self._request_locked(op, args, mutation, token, use_binding=use_binding)
            self.audit.event(
                "bridge_result", op=op, result=result, duration_ms=(time.monotonic() - started) * 1000
            )
            return result
        except BaseException as exc:
            self.audit.error("bridge_error", exc, op=op, duration_ms=(time.monotonic() - started) * 1000)
            raise

    async def _request_locked(
        self, op: str, args: dict | None, mutation: bool, token: str, *, timeout: float | None = None,
        use_binding: bool = True,
    ) -> dict:
        uncertain_file = self.config.runtime / "uncertain.json"
        if uncertain_file.exists():
            self.uncertain_request = json.loads(uncertain_file.read_text(encoding="utf8"))["id"]
        else:
            self.uncertain_request = None  # another client may have reconciled the shared mailbox
        if mutation and self.uncertain_request:
            raise BridgeError(
                "Previous mutation timed out; its outcome is unknown. Call falcon_reconcile "
                "to wait for the bridge barrier and inspect the last mutation before editing."
            )
        identifier = uuid.uuid4().hex
        request = {"protocol": 1, "token": token, "id": identifier, "op": op, "args": args or {}}
        expected_instance = self.bound_instance if use_binding else None
        if expected_instance:
            request["expectedInstance"] = expected_instance
            if mutation:
                request["expectedPrograms"] = self.bound_programs
        if mutation:
            # A read-only handshake precedes EVERY write. After reload, a stale
            # command cannot initialize a new session or replay a previous edit.
            try:
                engine = await self._request_locked("ping", None, False, token, timeout=timeout)
            except TimeoutError as exc:
                raise TimeoutError(
                    "Falcon readiness handshake failed; the engine mutation was NOT dispatched. "
                    "Run falcon_setup to check the one-time installation and audio engine."
                ) from exc
            if engine.get("sessionRequired"):
                request["session"] = engine["instance"]
            if expected_instance and engine.get("instance") != expected_instance:
                raise BridgeError("Bound instance changed; mutation was NOT dispatched. Inspect status and rebind explicitly")
        if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf8")) > 65536:
            raise ValueError("Bridge request exceeds 64 KiB; split the parameter batch")
        self.audit.event("bridge_dispatch", request_id=identifier, op=op, mutation=mutation)
        if mutation:
            atomic_json(uncertain_file, {"id": identifier, "op": op})
        try:
            atomic_json(self.config.runtime / "request.json", request)
        except BaseException:
            if mutation:
                # The request file was never replaced, so Falcon cannot have seen this mutation.
                uncertain_file.unlink(missing_ok=True)
            raise
        deadline = time.monotonic() + (self.config.bridge_timeout if timeout is None else timeout)
        try:
            while time.monotonic() < deadline:
                try:
                    response = json.loads((self.config.runtime / "response.json").read_text(encoding="utf8"))
                except (OSError, ValueError):
                    response = {}
                if not isinstance(response, dict):
                    response = {}
                if response.get("id") == identifier and response.get("token") == token:
                    if expected_instance and response.get("instance") != expected_instance:
                        # Another responder may overwrite the intended engine's receipt.
                        # Never clear a pending write on the strength of that response.
                        raise BridgeError("Response came from a different instance; inspect status and mailbox ownership before rebinding")
                    if response.get("sessionRejected"):
                        # A reloaded runtime refused a request id its predecessor may have run.
                        raise BridgeError(
                            "Bridge runtime changed while this mutation was pending; its outcome is unknown. "
                            "Inspect state, then falcon_reconcile(acknowledge_unknown=true)."
                        )
                    if mutation:
                        uncertain_file.unlink(missing_ok=True)
                    if not response.get("ok"):
                        raise BridgeError(str(response.get("error", "Falcon rejected operation")))
                    result = response.get("result", {})
                    if result == []:
                        result = {}  # UVI serializes an empty Lua object as []
                    if not isinstance(result, dict):
                        raise BridgeError("Invalid bridge result shape; reload the matching bridge version")
                    if result.get("items") == {}:
                        result["items"] = []
                    return result
                await asyncio.sleep(0.03)
            raise TimeoutError(
                "Falcon bridge did not respond. Load the generated Lua script once and "
                "ensure Falcon's audio engine is running. Mutation outcome may be unknown."
            )
        except (TimeoutError, asyncio.CancelledError):
            if mutation:
                self.uncertain_request = identifier
                atomic_json(uncertain_file, {"id": identifier, "op": op})
            raise

    async def reconcile(self, acknowledge_unknown: bool = False) -> dict:
        # Script dispatch is serial, including completion of async effect/sample tasks.
        # A successful ping therefore acts as a barrier for a previous timed-out command.
        self.audit.event("bridge_reconcile", acknowledge_unknown=acknowledge_unknown)
        try:
            async with self.lock:
                token = self._read_token()
                async with mailbox_lock(self.config.runtime, self.config.bridge_timeout):
                    result = await self._request_locked("ping", None, False, token)
                    identifier = self.uncertain_request
                    receipt = result.get("lastMutation")
                    matched = (
                        isinstance(receipt, dict)
                        and receipt.get("id") == identifier
                        and isinstance(receipt.get("ok"), bool)
                        and not receipt.get("sessionRejected")
                    )
                    resolved = identifier is None or matched
                    acknowledged = bool(identifier and not resolved and acknowledge_unknown)
                    if identifier is None:
                        outcome = "no_pending_mutation"
                    elif matched:
                        outcome = "completed" if receipt["ok"] else "engine_error"
                    else:
                        outcome = "unknown"
                    result.update(
                        uncertain_request=identifier,
                        resolved=resolved,
                        acknowledged_unknown=acknowledged,
                        outcome=outcome,
                    )
                    if resolved or acknowledged:
                        self.uncertain_request = None
                        (self.config.runtime / "uncertain.json").unlink(missing_ok=True)
                    else:
                        result["next_step"] = (
                            "Receipt unavailable (possibly after script reload). Inspect affected state; only then call reconcile(acknowledge_unknown=true). Never blindly retry the mutation."
                        )
            self.audit.event("bridge_reconciled", result=result)
            return result
        except BaseException as exc:
            self.audit.error("bridge_reconcile_error", exc)
            raise
