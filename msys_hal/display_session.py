"""Strict reader for the replaceable ``msys.display-session.v1`` contract."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import UnavailableError


DISPLAY_SESSION_SCHEMA = "msys.display-session.v1"
DISPLAY_RE = re.compile(r"^:[0-9]+(?:\.[0-9]+)?$")
MAX_STATE_BYTES = 64 * 1024


def normalized_matrix(value: object, *, label: str = "input transform") -> list[int | float]:
    if not isinstance(value, (list, tuple)) or len(value) != 9:
        raise ValueError(f"{label} must contain exactly nine numbers")
    matrix: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{label} contains a non-number")
        number = float(item)
        if not math.isfinite(number) or abs(number) > 16:
            raise ValueError(f"{label} contains an unsafe value")
        matrix.append(number)
    if any(abs(matrix[index]) > 1e-9 for index in (6, 7)) or abs(matrix[8] - 1.0) > 1e-9:
        raise ValueError(f"{label} must be a normalized affine matrix")
    return [int(item) if item.is_integer() else item for item in matrix]


def validate_display_session(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema") != DISPLAY_SESSION_SCHEMA:
        raise ValueError(f"display session must use {DISPLAY_SESSION_SCHEMA}")
    if set(document) != {
        "schema",
        "state",
        "provider",
        "generation",
        "display",
        "geometry",
        "input_transform",
        "observed_at_unix_ms",
    }:
        raise ValueError("display session fields are invalid")
    if document.get("state") != "ready":
        raise ValueError("display session is not ready")
    provider = document.get("provider")
    if not isinstance(provider, str) or not provider or len(provider) > 256 or "\x00" in provider:
        raise ValueError("display session provider is invalid")
    generation = document.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("display session generation is invalid")
    display = document.get("display")
    if not isinstance(display, str) or DISPLAY_RE.fullmatch(display) is None:
        raise ValueError("display session DISPLAY is invalid")
    geometry = document.get("geometry")
    if not isinstance(geometry, dict) or set(geometry) != {"width", "height", "depth"}:
        raise ValueError("display session geometry is invalid")
    for key, minimum, maximum in (
        ("width", 1, 65_535),
        ("height", 1, 65_535),
        ("depth", 0, 128),
    ):
        value = geometry.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"display session geometry.{key} is invalid")
    transform = document.get("input_transform")
    if not isinstance(transform, dict) or set(transform) != {
        "enabled", "mode", "device", "space", "matrix", "source", "verified"
    }:
        raise ValueError("display session input transform is invalid")
    if not isinstance(transform.get("enabled"), bool):
        raise ValueError("display session input enabled flag is invalid")
    for key in ("mode", "source"):
        value = transform.get(key)
        if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
            raise ValueError(f"display session input {key} is invalid")
    device = transform.get("device")
    if device is not None and (
        not isinstance(device, str) or not device or len(device) > 256 or "\x00" in device
    ):
        raise ValueError("display session input device is invalid")
    if transform.get("space") != "normalized-display" or transform.get("verified") is not True:
        raise ValueError("display session input transform is not verified")
    if transform["enabled"]:
        normalized_matrix(transform.get("matrix"))
    elif transform.get("matrix") is not None:
        raise ValueError("disabled display input must not contain a matrix")
    observed = document.get("observed_at_unix_ms")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise ValueError("display session timestamp is invalid")
    # A JSON round trip returns detached, language-neutral data and prevents a
    # caller from mutating nested objects owned by another backend.
    return json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))


def _read_document(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_STATE_BYTES:
            raise ValueError("display session state is not a bounded regular file")
        data = bytearray()
        while len(data) <= MAX_STATE_BYTES:
            chunk = os.read(fd, min(8192, MAX_STATE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_STATE_BYTES or b"\x00" in data:
            raise ValueError("display session state is oversized or binary")
    finally:
        os.close(fd)
    try:
        decoded = json.loads(bytes(data).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("display session state is not valid JSON") from exc
    return validate_display_session(decoded)


class DisplaySessionReader:
    """Load the active state file without assuming an X11 display number."""

    def __init__(self, paths: Sequence[Path], *, max_age_ms: int = 45_000) -> None:
        unique: list[Path] = []
        for path in paths:
            candidate = Path(path)
            if candidate not in unique:
                unique.append(candidate)
        self.paths = tuple(unique)
        self.max_age_ms = max(0, min(int(max_age_ms), 300_000))

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "DisplaySessionReader":
        values = dict(os.environ if env is None else env)
        paths: list[Path] = []
        explicit = values.get("MSYS_DISPLAY_SESSION_STATE_FILE", "").strip()
        if explicit:
            paths.append(Path(explicit))
        configured = values.get("MSYS_HAL_DISPLAY_SESSION_FILES", "").strip()
        if configured:
            paths.extend(Path(item) for item in configured.split(os.pathsep) if item)
        runtime = values.get("MSYS_RUNTIME_DIR", "").strip()
        if runtime:
            paths.append(Path(runtime) / "display-session.json")
        # Compatibility with providers deployed before the shared runtime path.
        # These are state locations, never assumptions about DISPLAY numbers.
        paths.extend((
            Path("/tmp/ch347_dirty_usb_x11/msys.ready"),
            Path("/tmp/msys-x11-hdmi/display-session.json"),
        ))
        raw_age = values.get("MSYS_HAL_DISPLAY_SESSION_MAX_AGE_MS", "45000")
        try:
            max_age_ms = int(raw_age)
        except ValueError:
            max_age_ms = 45_000
        return cls(paths, max_age_ms=max_age_ms)

    def load(self) -> dict[str, Any]:
        fresh: list[dict[str, Any]] = []
        invalid = False
        stale = False
        for path in self.paths:
            try:
                document = _read_document(path)
                self._require_fresh(document)
                fresh.append(document)
            except FileNotFoundError:
                continue
            except UnavailableError:
                stale = True
            except (OSError, ValueError):
                invalid = True
        if not fresh:
            if stale:
                reason = "stale-state"
            elif invalid:
                reason = "invalid-state"
            else:
                reason = "no-state"
            raise UnavailableError(
                "display-session state is unavailable",
                details={"reason": reason},
            )
        return max(fresh, key=lambda item: int(item["observed_at_unix_ms"]))

    def accept(self, document: object) -> dict[str, Any]:
        """Validate an in-band state with the same freshness policy as files."""

        result = validate_display_session(document)
        self._require_fresh(result)
        return result

    def _require_fresh(self, document: dict[str, Any]) -> None:
        if self.max_age_ms:
            age = int(time.time() * 1000) - int(document["observed_at_unix_ms"])
            if age > self.max_age_ms or age < -5_000:
                raise UnavailableError(
                    "display-session state is stale",
                    details={"reason": "stale-state", "age_ms": age},
                )


__all__ = [
    "DISPLAY_SESSION_SCHEMA",
    "DisplaySessionReader",
    "normalized_matrix",
    "validate_display_session",
]
