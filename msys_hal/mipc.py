from __future__ import annotations

import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .errors import HalError, ValidationError
from .validation import ensure_bounded_json, integer


MAX_PACKET = 256 * 1024
MAX_METHOD = 96


def encode_packet(message: dict[str, Any]) -> bytes:
    ensure_bounded_json(message, label="mIPC message", max_depth=10, max_items=2048)
    data = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(data) > MAX_PACKET:
        raise ValidationError("mIPC packet is too large")
    return data


def decode_packet(data: bytes) -> dict[str, Any]:
    if not data or len(data) > MAX_PACKET:
        raise ValidationError("mIPC packet size is invalid")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("mIPC packet is invalid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValidationError("mIPC packet requires an object and string type")
    ensure_bounded_json(value, label="mIPC message", max_depth=10, max_items=2048)
    return value


class PacketTransport:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._send_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "PacketTransport":
        raw_fd = os.environ.get("MSYS_CONTROL_FD", "")
        if not raw_fd.isdigit():
            raise RuntimeError("MSYS_CONTROL_FD is missing or invalid")
        return cls(socket.socket(fileno=int(raw_fd)))

    def send(self, message: dict[str, Any]) -> None:
        data = encode_packet(message)
        with self._send_lock:
            self.sock.sendall(data)

    def recv(self) -> dict[str, Any]:
        data = self.sock.recv(MAX_PACKET + 1)
        if not data:
            return {"type": "eof"}
        return decode_packet(data)


class PublicGateway:
    """Small stdlib-only client for the public MSYS control socket."""

    def __init__(self, runtime_dir: str | Path | None = None) -> None:
        self.runtime_dir = Path(
            runtime_dir or os.environ.get("MSYS_RUNTIME_DIR", "/run/msys/main")
        )

    def call(
        self,
        target: str,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float = 5.0,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(target, str) or not target or len(target) > 256:
            raise ValidationError("mIPC target is invalid")
        if not isinstance(method, str) or not method or len(method) > MAX_METHOD:
            raise ValidationError("mIPC method is invalid")
        if not isinstance(payload, dict):
            raise ValidationError("mIPC payload must be an object")
        timeout = min(max(float(timeout), 0.05), 30.0)
        request = {
            "type": "call",
            "id": 1,
            "target": target,
            "method": method,
            "payload": payload,
            "deadline_ms": int(time.monotonic() * 1000 + timeout * 1000),
            "idempotent": bool(idempotent),
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.runtime_dir / "control.sock"))
            self._recv_line(sock)
            sock.sendall(encode_packet(request) + b"\n")
            return self._recv_line(sock)

    @staticmethod
    def _recv_line(sock: socket.socket) -> dict[str, Any]:
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = sock.recv(min(65536, MAX_PACKET + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_PACKET:
                raise ValidationError("mIPC public response is too large")
        if not data:
            raise EOFError("empty mIPC public response")
        return decode_packet(bytes(data).rstrip(b"\n"))


Handler = Callable[[str, dict[str, Any]], dict[str, Any]]


class ComponentServer:
    """Concurrent mIPC component server with bounded work admission."""

    def __init__(
        self,
        handler: Handler,
        *,
        transport: PacketTransport | None = None,
        workers: int = 6,
    ) -> None:
        self.handler = handler
        self.transport = transport or PacketTransport.from_env()
        self.component_id = os.environ.get("MSYS_COMPONENT_ID", "unknown")[:192]
        self.generation = int(os.environ.get("MSYS_GENERATION", "0") or 0)
        self._workers = max(1, min(int(workers), 16))
        self._slots = threading.BoundedSemaphore(self._workers * 2)
        self._executor = ThreadPoolExecutor(
            max_workers=self._workers,
            thread_name_prefix="msys-hal-rpc",
        )
        self._stopping = threading.Event()

    def event(self, topic: str, payload: dict[str, Any]) -> None:
        if not isinstance(topic, str) or not topic or len(topic) > 128:
            raise ValidationError("event topic is invalid")
        self.transport.send({"type": "event", "topic": topic, "payload": payload})

    def run(self, *, ready_event: tuple[str, dict[str, Any]] | None = None) -> int:
        self.transport.send({
            "type": "hello",
            "component": self.component_id,
            "generation": self.generation,
        })
        welcome = self.transport.recv()
        if welcome.get("type") not in {"welcome", "return"}:
            raise RuntimeError("msysd did not accept HAL component hello")
        self.transport.send({"type": "ready"})
        if ready_event:
            self.event(*ready_event)
        try:
            while not self._stopping.is_set():
                message = self.transport.recv()
                message_type = message.get("type")
                if message_type in {"eof", "shutdown"}:
                    return 0
                if message_type != "call":
                    continue
                self._accept(message)
        finally:
            self._stopping.set()
            self._executor.shutdown(wait=False, cancel_futures=True)
        return 0

    def _accept(self, message: dict[str, Any]) -> None:
        request_id = message.get("id", 0)
        if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 0:
            request_id = 0
        if not self._slots.acquire(blocking=False):
            self.transport.send({
                "type": "error",
                "id": request_id,
                "code": "HAL_BUSY",
                "message": "HAL component request limit reached",
            })
            return
        self._executor.submit(self._dispatch, request_id, message)

    def _dispatch(self, request_id: int, message: dict[str, Any]) -> None:
        try:
            deadline = message.get("deadline_ms")
            if deadline is not None:
                integer(deadline, "deadline_ms", minimum=0, maximum=2**63 - 1)
                if deadline <= int(time.monotonic() * 1000):
                    raise HalError("CALL_TIMEOUT", "call deadline already expired")
            method = message.get("method")
            if not isinstance(method, str) or not method or len(method) > MAX_METHOD:
                raise ValidationError("method is invalid")
            payload = message.get("payload", {})
            if not isinstance(payload, dict):
                raise ValidationError("payload must be an object")
            ensure_bounded_json(payload, label="payload", max_depth=8, max_items=1024)
            result = self.handler(method, payload)
            if not isinstance(result, dict):
                raise RuntimeError("HAL handler returned a non-object")
            ensure_bounded_json(result, label="result", max_depth=8, max_items=2048)
            self.transport.send({"type": "return", "id": request_id, "payload": result})
        except HalError as exc:
            error: dict[str, Any] = {
                "type": "error",
                "id": request_id,
                "code": exc.code[:64],
                "message": exc.message[:512],
            }
            if exc.details:
                error["payload"] = exc.details
            self.transport.send(error)
        except Exception as exc:
            print(
                f"msys-hal: internal RPC error {type(exc).__name__}: {exc}",
                flush=True,
            )
            self.transport.send({
                "type": "error",
                "id": request_id,
                "code": "HAL_INTERNAL_ERROR",
                "message": "HAL component internal error",
            })
        finally:
            self._slots.release()
