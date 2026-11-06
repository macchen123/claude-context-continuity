"""Run managed native hooks in the existing controller's serialized process."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import select
import socket
import stat

from . import core, native_control
from .history import redact


class HookExecutionError(ValueError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _path(context_id: str) -> Path:
    return core.safe_path(core.HOME, f"runtime/control/{core.uuid(context_id)}.hook", exists=False)


def _socket_identity(path: Path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("managed hook endpoint is not an owned socket")
    return info.st_dev, info.st_ino


def request(context_id: str, event: dict):
    path = _path(context_id)
    try:
        _socket_identity(path)
    except FileNotFoundError:
        return None
    auth = os.environ.get(native_control.HOOK_AUTH_ENV)
    native_control._auth(auth)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(600)
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            from .diagnostics import record_event
            record_event(core.HOME / "runtime" / "contexts" / context_id, "hook_controller_unavailable",
                         context_id=context_id, source="hook_transport", error_origin="cclaude",
                         error_type=type(error).__name__)
            return None  # No event bytes were sent; the original hook path repairs the observer.
        packet = json.dumps({"auth": auth, "event": event}, ensure_ascii=False) + "\n"
        client.sendall(packet.encode("utf-8"))
        with client.makefile("rb") as reader:
            line = reader.readline()
    if not line:
        raise HookExecutionError("HookTransportError", "managed hook connection ended before its result")
    response = json.loads(line)
    if "error_type" in response:
        raise HookExecutionError(response["error_type"], response["error"])
    return response["result"]


class HookServer:
    def __init__(self, context_id: str, auth: str, handler) -> None:
        native_control._auth(auth)
        self.path = _path(context_id)
        self.auth = auth
        self.handler = handler
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.path.lexists(self.path):
            _socket_identity(self.path)
            self.path.unlink()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.identity = _socket_identity(self.path)
            self.socket.listen()
        except BaseException:
            self.socket.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.socket.close()
        try:
            if _socket_identity(self.path) == self.identity:
                self.path.unlink()
        except FileNotFoundError:
            pass

    def serve_one(self, timeout: float) -> None:
        ready, _, _ = select.select([self.socket], [], [], timeout)
        if not ready:
            return
        connection, _ = self.socket.accept()
        with connection:
            connection.settimeout(5)
            try:
                with connection.makefile("rb") as reader:
                    packet = json.loads(reader.readline())
                auth = packet.get("auth")
                if not isinstance(auth, str) or not hmac.compare_digest(auth, self.auth):
                    raise ValueError("managed hook authentication rejected")
                event = packet["event"]
                if not isinstance(event, dict):
                    raise ValueError("native hook event must be an object")
                result = self.handler(event)
                core.no_secrets(result)
                encoded = (json.dumps({"result": result}, ensure_ascii=False) + "\n").encode("utf-8")
            except Exception as error:
                response = {"error_type": type(error).__name__,
                            "error": redact(str(error), core.secret_values())[:400]}
                encoded = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                connection.sendall(encoded)
            except (BrokenPipeError, ConnectionResetError, TimeoutError) as error:
                from .diagnostics import record_event
                record_event(core.HOME / "runtime" / "contexts" / self.path.stem, "hook_reply_unavailable",
                             context_id=self.path.stem, source="hook_transport", error_origin="cclaude",
                             error_type=type(error).__name__)
