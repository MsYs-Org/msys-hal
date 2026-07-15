from __future__ import annotations

import argparse
import os
import re
import signal
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

from . import __version__
from .errors import HalError, PersistenceError, ProviderError, UnavailableError, ValidationError
from .mipc import ComponentServer, PublicGateway
from .provider import PROVIDER_INTERFACE, ProviderService
from .validation import component_id, device_id, integer, object_payload


CONTROL_INTERFACE = "org.msys.hal.ch347-control.v1"
DOMAIN = "display-output"
DEVICE_ID = "display-output:ch347"
DEFAULT_TARGET = "org.msys.openstick.ch347:x11-spi-touch-output"
MAX_CONFIG_BYTES = 16 * 1024
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_PID_ROWS = 32
UINT64_MAX = 2**64 - 1

FPS_KEYS = {
    "DEBUG": "debug_enabled",
    "FPS": "fps",
    "XCAP_MAX_FPS": "max_fps",
    "XCAP_IDLE_FPS": "idle_fps",
}
FPS_DEFAULTS = {
    "debug_enabled": False,
    "fps": 60,
    "max_fps": 60,
    "idle_fps": 0,
}
DEBUG_OVERLAY_ITEM_BITS = {
    "fps": 1,
    "dirty": 2,
    "bytes": 4,
    "bbox": 8,
    "memory": 16,
}
DEBUG_OVERLAY_KEYS = {
    "CH347_DEBUG_OVERLAY": "enabled",
    "CH347_DEBUG_OVERLAY_ALPHA": "alpha",
    "CH347_DEBUG_OVERLAY_SCALE": "scale",
    "CH347_DEBUG_OVERLAY_ITEMS": "items_mask",
    "CH347_DEBUG_OVERLAY_INTERVAL_MS": "interval_ms",
}
DEBUG_OVERLAY_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "alpha": 176,
    "scale": 1,
    "items": ["fps", "dirty", "bytes"],
    "interval_ms": 1000,
}
CURSOR_KEYS = {"CH347_CURSOR": "enabled"}
APPLIED_CURSOR_KEYS = dict(CURSOR_KEYS, MSYS_GENERATION="provider_generation")
APPLIED_OVERLAY_KEYS = dict(
    DEBUG_OVERLAY_KEYS,
    MSYS_GENERATION="provider_generation",
)
APPLIED_KEYS = dict(FPS_KEYS, MSYS_GENERATION="provider_generation")
DEBUG_SAMPLE_RE = re.compile(
    r"^(?:dirty|rect) frame=([0-9]{1,10}) captured=[0-9]{1,10} "
    r"drop=[0-9]{1,10} sent_rects=[0-9]{1,3} "
    r"dirty=[0-9]{1,3}(?:\.[0-9]{1,3})?% "
    r"bus_fps=([0-9]{1,6}(?:\.[0-9]{1,3})?) "
    r"out_fps=([0-9]{1,6}(?:\.[0-9]{1,3})?)$"
)
DIRTY_STATS_FIELDS = (
    "sent_frames",
    "zero_damage",
    "full_refreshes",
    "large_refreshes",
    "sent_pixels",
    "last_sent_pixels",
    "last_rects",
)
DIRTY_STATS_RE = re.compile(
    r"^dirty_stats frame=(\S+) sent_frames=(\S+) zero_damage=(\S+) "
    r"full_refreshes=(\S+) large_refreshes=(\S+) sent_pixels=(\S+) "
    r"last_sent_pixels=(\S+) last_rects=(\S+)$"
)
PHYSICAL_ROTATIONS = ("normal", "right", "left", "inverted")
ROTATION_RE = re.compile(
    r"^CH347_DISPLAY_ROTATION=(normal|right|left|inverted)$"
)

CALIBRATION_KEYS = {
    "CH347_TOUCH": "enabled",
    "CH347_TOUCH_SWAP_XY": "swap_xy",
    "CH347_TOUCH_INVERT_X": "invert_x",
    "CH347_TOUCH_INVERT_Y": "invert_y",
    "CH347_TOUCH_X_MIN": "x_min",
    "CH347_TOUCH_X_MAX": "x_max",
    "CH347_TOUCH_Y_MIN": "y_min",
    "CH347_TOUCH_Y_MAX": "y_max",
    "CH347_TOUCH_WIDTH": "width",
    "CH347_TOUCH_HEIGHT": "height",
    "CH347_TOUCH_Z_MIN": "z_min",
    "CH347_TOUCH_PRESSURE_MIN": "pressure_min",
    "CH347_TOUCH_PRESSURE_MAX": "pressure_max",
}
CALIBRATION_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "swap_xy": False,
    "invert_x": False,
    "invert_y": False,
    "x_min": 200,
    "x_max": 3900,
    "y_min": 200,
    "y_max": 3900,
    "width": 320,
    "height": 480,
    "z_min": 50,
    "pressure_min": 50,
    "pressure_max": 4095,
}
BOOLEAN_CALIBRATION_FIELDS = {"enabled", "swap_xy", "invert_x", "invert_y"}
CALIBRATION_RANGES = {
    "x_min": (0, 65535),
    "x_max": (0, 65535),
    "y_min": (0, 65535),
    "y_max": (0, 65535),
    "width": (1, 8192),
    "height": (1, 8192),
    "z_min": (0, 65535),
    "pressure_min": (0, 65535),
    "pressure_max": (1, 65535),
}
ASSIGNMENT_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=([0-9]{1,10})$")


class Gateway(Protocol):
    def call(
        self,
        target: str,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float = 5.0,
        idempotent: bool = False,
    ) -> dict[str, Any]: ...


def default_config_dir() -> Path:
    override = os.environ.get("MSYS_CH347_CONFIG_DIR")
    if override:
        return Path(override)
    state = Path(os.environ.get("MSYS_STATE_DIR", "/opt/msys-state"))
    return state / "apps" / "org.msys.openstick.ch347" / "ch347"


def _default_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _default_process_executable(proc_root: Path, pid: int) -> str | None:
    try:
        return os.readlink(proc_root / str(pid) / "exe")
    except (OSError, ValueError):
        return None


class Ch347ControlBackend:
    """Typed control plane for the package-owned CH347 mutable state.

    Pixels and input samples never cross this API.  It only owns the small
    configuration documents already consumed by the display-output package,
    plus a narrowly scoped lifecycle call back to msys.core.
    """

    domain = DOMAIN
    identifier = DEVICE_ID

    def __init__(
        self,
        gateway: Gateway,
        *,
        config_dir: Path | None = None,
        run_dir: Path = Path("/tmp/ch347_dirty_usb_x11"),
        proc_root: Path = Path("/proc"),
        target_component: str = DEFAULT_TARGET,
        pid_alive: Callable[[int], bool] = _default_pid_alive,
        process_executable: Callable[[Path, int], str | None] = _default_process_executable,
        signal_process: Callable[[int, int], None] = os.kill,
    ) -> None:
        self.gateway = gateway
        self.config_dir = Path(config_dir) if config_dir is not None else default_config_dir()
        if not self.config_dir.is_absolute() or self.config_dir == Path(self.config_dir.anchor):
            raise ValueError("CH347 config directory must be a non-root absolute path")
        self.run_dir = Path(run_dir)
        if not self.run_dir.is_absolute():
            raise ValueError("CH347 run directory must be absolute")
        self.proc_root = Path(proc_root)
        self.target_component = component_id(target_component, "target component")
        self.pid_alive = pid_alive
        self.process_executable = process_executable
        self.signal_process = signal_process
        self._lock = threading.RLock()

    @property
    def fps_path(self) -> Path:
        return self.config_dir / "fps.env"

    @property
    def calibration_path(self) -> Path:
        return self.config_dir / "touch_calibration.env"

    @property
    def debug_overlay_path(self) -> Path:
        return self.config_dir / "debug_overlay.env"

    @property
    def cursor_path(self) -> Path:
        return self.config_dir / "cursor.env"

    @property
    def rotation_path(self) -> Path:
        return self.config_dir / "rotation.env"

    @property
    def pid_path(self) -> Path:
        return self.run_dir / "pids"

    @property
    def applied_config_path(self) -> Path:
        return self.run_dir / "display-config.applied.env"

    @property
    def applied_overlay_path(self) -> Path:
        return self.run_dir / "debug-overlay.applied.env"

    @property
    def applied_cursor_path(self) -> Path:
        return self.run_dir / "cursor.applied.env"

    @property
    def owner_path(self) -> Path:
        return self.run_dir / "msys.provider.owner"

    @property
    def log_path(self) -> Path:
        return self.run_dir / "live.log"

    def _core_call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        idempotent: bool,
    ) -> dict[str, Any]:
        try:
            response = self.gateway.call(
                "msys.core",
                method,
                payload,
                timeout=timeout,
                idempotent=idempotent,
            )
        except (EOFError, OSError, TimeoutError) as exc:
            raise UnavailableError(
                "msys.core is unavailable for CH347 control",
                details={"operation": method, "cause": type(exc).__name__},
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "CH347 lifecycle control failed",
                details={"operation": method, "cause": type(exc).__name__},
            ) from exc
        if not isinstance(response, dict):
            raise ProviderError("msys.core returned a non-object response")
        if response.get("type") != "return":
            code = str(response.get("code", "CORE_ERROR"))[:64]
            raise UnavailableError(
                "msys.core rejected CH347 control",
                details={"operation": method, "core_code": code},
            )
        result = response.get("payload")
        if not isinstance(result, dict):
            raise ProviderError("msys.core returned a non-object payload")
        return result

    def _target_summary(self) -> dict[str, Any] | None:
        payload = self._core_call("list_components", {}, timeout=5.0, idempotent=True)
        rows = payload.get("components")
        if not isinstance(rows, list) or len(rows) > 4096:
            raise ProviderError("msys.core returned an invalid component catalog")
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("id") == self.target_component
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ProviderError("msys.core returned duplicate CH347 components")
        raw_state = matches[0].get("state", "unknown")
        if not isinstance(raw_state, str) or not raw_state or len(raw_state) > 64:
            raise ProviderError("msys.core returned an invalid CH347 component state")
        return {
            "id": self.target_component,
            "state": raw_state,
            "package_version": str(matches[0].get("package_version", ""))[:64],
        }

    def _read_regular(self, path: Path) -> str | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProviderError(
                "CH347 configuration cannot be inspected",
                details={"file": path.name, "cause": type(exc).__name__},
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProviderError(
                "CH347 configuration must be a regular non-symlink file",
                details={"file": path.name},
            )
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_CONFIG_BYTES + 1)
        except OSError as exc:
            raise ProviderError(
                "CH347 configuration cannot be read",
                details={"file": path.name, "cause": type(exc).__name__},
            ) from exc
        if len(data) > MAX_CONFIG_BYTES or b"\x00" in data:
            raise ProviderError(
                "CH347 configuration is oversized or binary",
                details={"file": path.name},
            )
        try:
            return data.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise ProviderError(
                "CH347 configuration is not ASCII",
                details={"file": path.name},
            ) from exc

    @staticmethod
    def _assignments(
        text: str,
        mapping: dict[str, str],
        *,
        filename: str,
    ) -> dict[str, int]:
        values: dict[str, int] = {}
        for line_number, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            matched = ASSIGNMENT_RE.fullmatch(line)
            if matched is None or matched.group(1) not in mapping:
                raise ProviderError(
                    "CH347 configuration contains an unsupported assignment",
                    details={"file": filename, "line": line_number},
                )
            field = mapping[matched.group(1)]
            if field in values:
                raise ProviderError(
                    "CH347 configuration contains a duplicate assignment",
                    details={"file": filename, "line": line_number},
                )
            values[field] = int(matched.group(2), 10)
        return values

    def _load_fps(self) -> tuple[dict[str, Any], str | None]:
        try:
            text = self._read_regular(self.fps_path)
            if text is None:
                return dict(FPS_DEFAULTS), "fps.env is missing"
            raw = self._assignments(text, FPS_KEYS, filename=self.fps_path.name)
            # DEBUG was introduced after the original FPS-only document.  A
            # legacy three-field document is read as DEBUG=0 and becomes the
            # canonical four-field form on the next typed write/provider boot.
            missing = sorted({"fps", "max_fps", "idle_fps"} - set(raw))
            if missing:
                raise ProviderError(
                    "CH347 FPS configuration is incomplete",
                    details={"fields": missing},
                )
            raw_debug = raw.get("debug_enabled", 0)
            if raw_debug not in {0, 1}:
                raise ProviderError("DEBUG must be 0 or 1")
            fps = integer(raw["fps"], "FPS", minimum=1, maximum=240)
            maximum = integer(raw["max_fps"], "XCAP_MAX_FPS", minimum=1, maximum=240)
            idle = integer(raw["idle_fps"], "XCAP_IDLE_FPS", minimum=0, maximum=60)
            if fps != maximum:
                raise ProviderError("FPS and XCAP_MAX_FPS must match")
            if idle > fps:
                raise ProviderError("XCAP_IDLE_FPS must not exceed FPS")
            return {
                "debug_enabled": bool(raw_debug),
                "fps": fps,
                "max_fps": maximum,
                "idle_fps": idle,
            }, None
        except (ProviderError, ValidationError) as exc:
            return dict(FPS_DEFAULTS), exc.message if hasattr(exc, "message") else str(exc)

    @staticmethod
    def _validated_debug_overlay(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("debug overlay must be an object")
        expected = {"enabled", "alpha", "scale", "items", "interval_ms"}
        unknown = sorted(set(value) - expected)
        missing = sorted(expected - set(value))
        if unknown or missing:
            raise ValidationError(
                "debug overlay fields are invalid",
                details={"unknown": unknown, "missing": missing},
            )
        if not isinstance(value["enabled"], bool):
            raise ValidationError("debug overlay enabled must be a boolean")
        alpha = integer(value["alpha"], "debug overlay alpha", minimum=0, maximum=255)
        scale = integer(value["scale"], "debug overlay scale", minimum=1, maximum=2)
        interval = integer(
            value["interval_ms"],
            "debug overlay interval_ms",
            minimum=250,
            maximum=5000,
        )
        items = value["items"]
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) for item in items)
            or len(items) != len(set(items))
            or any(item not in DEBUG_OVERLAY_ITEM_BITS for item in items)
        ):
            raise ValidationError("debug overlay items are invalid")
        return {
            "enabled": value["enabled"],
            "alpha": alpha,
            "scale": scale,
            "items": [item for item in DEBUG_OVERLAY_ITEM_BITS if item in items],
            "interval_ms": interval,
        }

    def _load_debug_overlay(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            text = self._read_regular(self.debug_overlay_path)
            if text is None:
                return None, None
            raw = self._assignments(
                text,
                DEBUG_OVERLAY_KEYS,
                filename=self.debug_overlay_path.name,
            )
            missing = sorted(set(DEBUG_OVERLAY_KEYS.values()) - set(raw))
            if missing:
                raise ProviderError(
                    "CH347 debug overlay configuration is incomplete",
                    details={"fields": missing},
                )
            if raw["enabled"] not in {0, 1}:
                raise ProviderError("CH347 debug overlay enabled must be 0 or 1")
            mask = integer(raw.pop("items_mask"), "debug overlay items", minimum=1, maximum=31)
            return self._validated_debug_overlay({
                "enabled": raw["enabled"] == 1,
                "alpha": raw["alpha"],
                "scale": raw["scale"],
                "items": [
                    item for item, bit in DEBUG_OVERLAY_ITEM_BITS.items()
                    if mask & bit
                ],
                "interval_ms": raw["interval_ms"],
            }), None
        except (ProviderError, ValidationError) as exc:
            return None, exc.message if hasattr(exc, "message") else str(exc)

    def _load_cursor(self) -> tuple[bool | None, str | None]:
        try:
            text = self._read_regular(self.cursor_path)
            if text is None:
                return None, None
            raw = self._assignments(
                text,
                CURSOR_KEYS,
                filename=self.cursor_path.name,
            )
            if set(raw) != {"enabled"} or raw["enabled"] not in {0, 1}:
                raise ProviderError("CH347 cursor configuration must be 0 or 1")
            return raw["enabled"] == 1, None
        except ProviderError as exc:
            return None, exc.message if hasattr(exc, "message") else str(exc)

    def _load_calibration(self) -> tuple[dict[str, Any], str | None]:
        try:
            text = self._read_regular(self.calibration_path)
            if text is None:
                return dict(CALIBRATION_DEFAULTS), "touch_calibration.env is missing"
            raw = self._assignments(
                text,
                CALIBRATION_KEYS,
                filename=self.calibration_path.name,
            )
            missing = sorted(set(CALIBRATION_DEFAULTS) - set(raw))
            if missing:
                raise ProviderError(
                    "CH347 touch calibration is incomplete",
                    details={"fields": missing},
                )
            decoded: dict[str, Any] = {}
            for field, value in raw.items():
                if field in BOOLEAN_CALIBRATION_FIELDS:
                    if value not in {0, 1}:
                        raise ProviderError(
                            "CH347 touch flags must be 0 or 1",
                            details={"field": field},
                        )
                    decoded[field] = bool(value)
                else:
                    decoded[field] = value
            return self._validated_calibration(decoded, partial=False), None
        except (ProviderError, ValidationError) as exc:
            return dict(CALIBRATION_DEFAULTS), exc.message if hasattr(exc, "message") else str(exc)

    def _load_rotation(self) -> tuple[str, str | None, bool]:
        """Load the optional output-owned physical rotation capability.

        A missing file means the installed output does not consume this
        setting yet, so it remains read-only instead of claiming a successful
        restart that cannot rotate pixels.
        """

        try:
            text = self._read_regular(self.rotation_path)
            if text is None:
                return "normal", None, False
            values = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if len(values) != 1:
                raise ProviderError("CH347 rotation configuration is invalid")
            matched = ROTATION_RE.fullmatch(values[0])
            if matched is None:
                raise ProviderError(
                    "CH347 rotation configuration contains an unsupported value"
                )
            return matched.group(1), None, True
        except ProviderError as exc:
            return (
                "normal",
                exc.message if hasattr(exc, "message") else str(exc),
                True,
            )

    @staticmethod
    def _validated_calibration(
        value: Any,
        *,
        partial: bool,
        base: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > len(CALIBRATION_DEFAULTS):
            raise ValidationError("touch_calibration must be a small object")
        unknown = sorted(set(value) - set(CALIBRATION_DEFAULTS))
        if unknown:
            raise ValidationError(
                "touch_calibration has unknown fields: " + ", ".join(unknown)
            )
        if not partial and set(value) != set(CALIBRATION_DEFAULTS):
            raise ValidationError("touch_calibration must contain every calibration field")
        result = dict(base or CALIBRATION_DEFAULTS)
        for field, raw in value.items():
            if field in BOOLEAN_CALIBRATION_FIELDS:
                if not isinstance(raw, bool):
                    raise ValidationError(f"touch_calibration.{field} must be a boolean")
                result[field] = raw
            else:
                minimum, maximum = CALIBRATION_RANGES[field]
                result[field] = integer(
                    raw,
                    f"touch_calibration.{field}",
                    minimum=minimum,
                    maximum=maximum,
                )
        if result["x_min"] >= result["x_max"]:
            raise ValidationError("touch_calibration.x_min must be less than x_max")
        if result["y_min"] >= result["y_max"]:
            raise ValidationError("touch_calibration.y_min must be less than y_max")
        if result["pressure_min"] >= result["pressure_max"]:
            raise ValidationError(
                "touch_calibration.pressure_min must be less than pressure_max"
            )
        return result

    def _ensure_config_dir(self) -> None:
        try:
            self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.config_dir.lstat()
        except OSError as exc:
            raise PersistenceError(
                "CH347 configuration directory cannot be created",
                details={"cause": type(exc).__name__},
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PersistenceError("CH347 configuration directory is not a safe directory")

    def _atomic_write(self, path: Path, content: str) -> None:
        data = content.encode("ascii", "strict")
        if len(data) > MAX_CONFIG_BYTES:
            raise PersistenceError("CH347 configuration output is too large")
        self._ensure_config_dir()
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise PersistenceError(
                "CH347 configuration target cannot be inspected",
                details={"file": path.name, "cause": type(exc).__name__},
            ) from exc
        if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
            raise PersistenceError(
                "CH347 configuration target is not a safe regular file",
                details={"file": path.name},
            )
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.config_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as stream:
                temporary = stream.name
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = ""
            try:
                directory_fd = os.open(
                    self.config_dir,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except OSError as exc:
            raise PersistenceError(
                "CH347 configuration could not be committed",
                details={"file": path.name, "cause": type(exc).__name__},
            ) from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _write_fps(self, debug_enabled: bool, fps: int, idle_fps: int) -> None:
        self._atomic_write(
            self.fps_path,
            f"DEBUG={int(debug_enabled)}\n"
            f"FPS={fps}\n"
            f"XCAP_MAX_FPS={fps}\n"
            f"XCAP_IDLE_FPS={idle_fps}\n",
        )

    def _write_debug_overlay(self, overlay: dict[str, Any]) -> None:
        selected = self._validated_debug_overlay(overlay)
        mask = sum(DEBUG_OVERLAY_ITEM_BITS[item] for item in selected["items"])
        self._atomic_write(
            self.debug_overlay_path,
            f"CH347_DEBUG_OVERLAY={int(selected['enabled'])}\n"
            f"CH347_DEBUG_OVERLAY_ALPHA={selected['alpha']}\n"
            f"CH347_DEBUG_OVERLAY_SCALE={selected['scale']}\n"
            f"CH347_DEBUG_OVERLAY_ITEMS={mask}\n"
            f"CH347_DEBUG_OVERLAY_INTERVAL_MS={selected['interval_ms']}\n",
        )

    def _write_cursor(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValidationError("touch_cursor_enabled must be a boolean")
        self._atomic_write(self.cursor_path, f"CH347_CURSOR={int(enabled)}\n")

    def _write_calibration(self, calibration: dict[str, Any]) -> None:
        encoded: dict[str, int] = {
            field: int(value) if field in BOOLEAN_CALIBRATION_FIELDS else value
            for field, value in calibration.items()
        }
        inverse = {field: key for key, field in CALIBRATION_KEYS.items()}
        ordered = [CALIBRATION_KEYS[key] for key in CALIBRATION_KEYS]
        lines = ["# Managed atomically by MSYS HAL CH347 control"]
        lines.extend(f"{inverse[field]}={encoded[field]}" for field in ordered)
        self._atomic_write(self.calibration_path, "\n".join(lines) + "\n")

    def _write_rotation(self, rotation: str) -> None:
        if not isinstance(rotation, str) or rotation not in PHYSICAL_ROTATIONS:
            raise ValidationError(
                "physical_rotation must be normal, right, left or inverted"
            )
        self._atomic_write(
            self.rotation_path,
            f"CH347_DISPLAY_ROTATION={rotation}\n",
        )

    def _pid_rows(self) -> list[int]:
        try:
            raw = self._read_regular(self.pid_path)
        except ProviderError:
            return []
        if raw is None:
            return []
        result: list[int] = []
        for line in raw.splitlines()[:MAX_PID_ROWS]:
            text = line.strip()
            if not text.isascii() or not text.isdigit():
                continue
            pid = int(text, 10)
            if 1 <= pid <= 2**31 - 1 and pid not in result and self.pid_alive(pid):
                result.append(pid)
        return result

    def _reload_fps(self) -> bool:
        delivered = False
        for pid in self._pid_rows():
            executable = self.process_executable(self.proc_root, pid)
            if not executable or Path(executable).name != "xdamage_shm_capture":
                continue
            try:
                self.signal_process(pid, signal.SIGUSR1)
            except (OSError, ValueError):
                continue
            delivered = True
        return delivered

    def _load_applied_display_config(
        self,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Read the display provider's generation-bound runtime receipt."""

        try:
            text = self._read_regular(self.applied_config_path)
            owner = self._read_regular(self.owner_path)
            if text is None or owner is None:
                return None, "provider-runtime-receipt-missing"
            raw = self._assignments(
                text,
                APPLIED_KEYS,
                filename=self.applied_config_path.name,
            )
            missing = sorted(set(APPLIED_KEYS.values()) - set(raw))
            if missing:
                raise ProviderError(
                    "CH347 applied display configuration is incomplete",
                    details={"fields": missing},
                )
            debug = raw["debug_enabled"]
            if debug not in {0, 1}:
                raise ProviderError("applied DEBUG must be 0 or 1")
            fps = integer(raw["fps"], "applied FPS", minimum=1, maximum=240)
            maximum = integer(
                raw["max_fps"],
                "applied XCAP_MAX_FPS",
                minimum=1,
                maximum=240,
            )
            idle = integer(
                raw["idle_fps"],
                "applied XCAP_IDLE_FPS",
                minimum=0,
                maximum=60,
            )
            generation = integer(
                raw["provider_generation"],
                "provider generation",
                minimum=0,
                maximum=2**31 - 1,
            )
            if fps != maximum or idle > fps:
                raise ProviderError("applied CH347 FPS configuration is inconsistent")
            owner_match = re.fullmatch(
                r"([0-9]{1,10}):[0-9]{1,10}:[0-9]{1,12}\n?",
                owner,
            )
            if owner_match is None or int(owner_match.group(1), 10) != generation:
                raise ProviderError(
                    "CH347 runtime receipt does not belong to the active generation"
                )
            return {
                "debug_enabled": bool(debug),
                "fps": fps,
                "max_fps": maximum,
                "idle_fps": idle,
                "provider_generation": generation,
            }, None
        except (ProviderError, ValidationError) as exc:
            return None, exc.message if hasattr(exc, "message") else str(exc)

    def _load_applied_debug_overlay(
        self,
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            text = self._read_regular(self.applied_overlay_path)
            owner = self._read_regular(self.owner_path)
            if text is None or owner is None:
                return None, "provider-overlay-receipt-missing"
            raw = self._assignments(
                text,
                APPLIED_OVERLAY_KEYS,
                filename=self.applied_overlay_path.name,
            )
            missing = sorted(set(APPLIED_OVERLAY_KEYS.values()) - set(raw))
            if missing:
                raise ProviderError(
                    "CH347 applied debug overlay configuration is incomplete",
                    details={"fields": missing},
                )
            if raw["enabled"] not in {0, 1}:
                raise ProviderError("applied debug overlay enabled must be 0 or 1")
            generation = integer(
                raw.pop("provider_generation"),
                "provider generation",
                minimum=0,
                maximum=2**31 - 1,
            )
            owner_match = re.fullmatch(
                r"([0-9]{1,10}):[0-9]{1,10}:[0-9]{1,12}\n?",
                owner,
            )
            if owner_match is None or int(owner_match.group(1), 10) != generation:
                raise ProviderError(
                    "CH347 overlay receipt does not belong to the active generation"
                )
            mask = integer(raw.pop("items_mask"), "debug overlay items", minimum=1, maximum=31)
            overlay = self._validated_debug_overlay({
                "enabled": raw["enabled"] == 1,
                "alpha": raw["alpha"],
                "scale": raw["scale"],
                "items": [
                    item for item, bit in DEBUG_OVERLAY_ITEM_BITS.items()
                    if mask & bit
                ],
                "interval_ms": raw["interval_ms"],
            })
            return {**overlay, "provider_generation": generation}, None
        except (ProviderError, ValidationError) as exc:
            return None, exc.message if hasattr(exc, "message") else str(exc)

    def _load_applied_cursor(
        self,
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            text = self._read_regular(self.applied_cursor_path)
            owner = self._read_regular(self.owner_path)
            if text is None or owner is None:
                return None, "provider-cursor-receipt-missing"
            raw = self._assignments(
                text,
                APPLIED_CURSOR_KEYS,
                filename=self.applied_cursor_path.name,
            )
            if set(raw) != {"enabled", "provider_generation"}:
                raise ProviderError("CH347 applied cursor configuration is incomplete")
            if raw["enabled"] not in {0, 1}:
                raise ProviderError("applied CH347 cursor must be 0 or 1")
            generation = integer(
                raw["provider_generation"],
                "provider generation",
                minimum=0,
                maximum=2**31 - 1,
            )
            owner_match = re.fullmatch(
                r"([0-9]{1,10}):[0-9]{1,10}:[0-9]{1,12}\n?",
                owner,
            )
            if owner_match is None or int(owner_match.group(1), 10) != generation:
                raise ProviderError(
                    "CH347 cursor receipt does not belong to the active generation"
                )
            return {
                "enabled": raw["enabled"] == 1,
                "provider_generation": generation,
            }, None
        except (ProviderError, ValidationError) as exc:
            return None, exc.message if hasattr(exc, "message") else str(exc)

    def _cursor_snapshot(
        self,
        configured: bool | None,
        *,
        component_state: str,
        live_pids: list[int],
        config_error: str | None,
    ) -> dict[str, Any] | None:
        if configured is None:
            return None
        receipt, receipt_error = self._load_applied_cursor()
        matches = receipt is not None and receipt["enabled"] == configured
        applied = (
            config_error is None
            and component_state == "ready"
            and bool(live_pids)
            and matches
        )
        return {
            "enabled": configured,
            "applied": applied,
            "requires_restart": not applied,
            "provider_generation": (
                receipt["provider_generation"] if receipt is not None else None
            ),
            "reason": "applied" if applied else receipt_error or "configuration-not-applied",
        }

    def _bounded_log_tail(self) -> tuple[str, ...]:
        """Read a bounded, regular-file-only tail of the current sink log."""

        try:
            info = self.log_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return ()
            with self.log_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - MAX_LOG_TAIL_BYTES), os.SEEK_SET)
                data = stream.read(MAX_LOG_TAIL_BYTES)
        except OSError:
            return ()
        return tuple(data.decode("ascii", "replace").splitlines())

    def _debug_sample(
        self,
        log_lines: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """Return only a real sink measurement from the current generation log."""

        for raw_line in reversed(log_lines):
            matched = DEBUG_SAMPLE_RE.fullmatch(raw_line.strip())
            if matched is None:
                continue
            try:
                frames = int(matched.group(1), 10)
                panel_fps = float(matched.group(2))
                observed_fps = float(matched.group(3))
            except (OverflowError, ValueError):
                continue
            if (
                frames > 2**32 - 1
                or panel_fps < 0.0
                or panel_fps > 1000.0
                or observed_fps < 0.0
                or observed_fps > 1000.0
            ):
                continue
            return {
                "observed_fps": observed_fps,
                "panel_fps": panel_fps,
                "frames": frames,
                # The sink reports cumulative counters and rates, but does not
                # report a real sampling-window duration. Never infer one.
                "window_ms": None,
            }
        return None

    def _dirty_stats_sample(
        self,
        log_lines: tuple[str, ...],
    ) -> dict[str, int] | None:
        """Return the newest cumulative sink counter snapshot, when valid."""

        for raw_line in reversed(log_lines):
            line = raw_line.strip()
            if not line.startswith("dirty_stats "):
                continue
            matched = DIRTY_STATS_RE.fullmatch(line)
            if matched is None:
                return None
            raw_counters = matched.groups()
            if any(re.fullmatch(r"[0-9]+", value) is None for value in raw_counters):
                return None
            try:
                counters = tuple(int(value, 10) for value in raw_counters)
            except ValueError:
                return None
            if any(value < 0 or value > UINT64_MAX for value in counters):
                return None
            return dict(zip(DIRTY_STATS_FIELDS, counters[1:]))
        return None

    def _debug_snapshot(
        self,
        configured: dict[str, Any],
        *,
        overlay: dict[str, Any] | None,
        cursor: bool | None,
        component_state: str,
        live_pids: list[int],
        config_error: str | None,
    ) -> dict[str, Any]:
        applied_config, applied_error = self._load_applied_display_config()
        applied_overlay, _applied_overlay_error = self._load_applied_debug_overlay()
        # ``applied`` describes the detailed sink-log switch. FPS is
        # independently hot-reloaded into the capture process with SIGUSR1,
        # while DEBUG is a sink startup option and therefore generation-bound.
        matches = (
            applied_config is not None
            and applied_config["debug_enabled"] == configured["debug_enabled"]
        )
        applied = (
            config_error is None
            and component_state == "ready"
            and bool(live_pids)
            and matches
        )
        enabled = bool(configured["debug_enabled"])
        log_lines = self._bounded_log_tail()
        sample = self._debug_sample(log_lines) if enabled and applied else None
        dirty_stats = self._dirty_stats_sample(log_lines)
        if config_error is not None:
            status = "unavailable"
            reason = "invalid-display-config"
        elif not enabled:
            status = "idle"
            reason = "debug-disabled"
        elif not applied:
            status = "unavailable"
            reason = (
                "invalid-display-config"
                if config_error is not None
                else applied_error or "configuration-not-applied"
            )
        elif sample is None:
            status = "unavailable"
            reason = "awaiting-debug-sample"
        else:
            status = "active"
            reason = "sink-debug-log"
        result = {
            "enabled": enabled,
            "applied": applied,
            "requires_restart": not applied,
            "provider_generation": (
                applied_config["provider_generation"]
                if applied_config is not None
                else None
            ),
            "fps": configured["fps"],
            "max_fps": configured["max_fps"],
            "idle_fps": configured["idle_fps"],
            "observed_fps": sample["observed_fps"] if sample is not None else None,
            "panel_fps": sample["panel_fps"] if sample is not None else None,
            "frames": sample["frames"] if sample is not None else None,
            "window_ms": sample["window_ms"] if sample is not None else None,
            **{
                field: dirty_stats[field] if dirty_stats is not None else None
                for field in DIRTY_STATS_FIELDS
            },
            "status": status,
            "reason": reason,
        }
        overlay_applied = (
            overlay is not None
            and applied_overlay is not None
            and component_state == "ready"
            and bool(live_pids)
            and all(
                applied_overlay[field] == overlay[field]
                for field in (
                    "enabled",
                    "alpha",
                    "scale",
                    "items",
                    "interval_ms",
                )
            )
        )
        # Settings treats the presence of this optional object as proof that
        # the active provider supports and has applied the overlay contract.
        # Never expose configured-only values: the provider-owned receipt must
        # belong to the active generation and exactly match every field.
        if overlay_applied:
            result["overlay"] = overlay
        cursor_state = self._cursor_snapshot(
            cursor,
            component_state=component_state,
            live_pids=live_pids,
            config_error=None,
        )
        if cursor_state is not None:
            result["touch_cursor"] = cursor_state
        return result

    def _configuration_provisioned(self) -> bool:
        """Return whether the display package has established its state root.

        A built-in development fallback has the same component id but does not
        consume the installed package's app-state directory.  Refusing to
        create a brand-new, disconnected directory prevents HAL from claiming
        a successful write which that fallback would never apply.
        """

        try:
            directory = self.config_dir.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode):
            return False
        for path in (self.fps_path, self.calibration_path):
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                return True
        return False

    @staticmethod
    def _mutable_fields(snapshot: dict[str, Any]) -> list[str]:
        fields: list[str] = []
        if snapshot.get("configuration_provisioned"):
            fields.append("debug_enabled")
        if snapshot.get("debug_overlay_provisioned"):
            fields.append("debug_overlay")
        if snapshot.get("cursor_provisioned"):
            fields.append("touch_cursor_enabled")
        if snapshot.get("configuration_provisioned"):
            fields.extend(("fps", "idle_fps", "touch_calibration"))
        if snapshot.get("rotation_provisioned"):
            fields.append("physical_rotation")
        if snapshot.get("component_state") == "ready":
            fields.append("restart")
        return fields

    def _status_snapshot(self) -> dict[str, Any]:
        summary = self._target_summary()
        live_pids = self._pid_rows()
        fps, fps_error = self._load_fps()
        overlay, overlay_error = self._load_debug_overlay()
        cursor, cursor_error = self._load_cursor()
        calibration, calibration_error = self._load_calibration()
        rotation, rotation_error, rotation_provisioned = self._load_rotation()
        errors = [
            item
            for item in (
                fps_error,
                overlay_error,
                cursor_error,
                calibration_error,
                rotation_error,
            )
            if item
        ]
        provisioned = self._configuration_provisioned()
        component_state = summary["state"] if summary is not None else "missing"
        running = component_state == "ready" and bool(live_pids)
        debug = self._debug_snapshot(
            fps,
            overlay=overlay,
            cursor=cursor,
            component_state=component_state,
            live_pids=live_pids,
            config_error=fps_error,
        )
        if summary is None:
            status = "unavailable"
            reason = "driver-not-installed"
        elif component_state == "ready" and not live_pids:
            status = "degraded"
            reason = "driver-has-no-live-process"
        elif errors:
            status = "degraded"
            reason = "invalid-or-missing-configuration"
        elif component_state == "ready":
            status = "available"
            reason = "healthy"
        else:
            status = "degraded"
            reason = "driver-not-running"
        return {
            "status": status,
            "reason": reason,
            "running": running,
            "component": self.target_component,
            "component_state": component_state,
            "package_version": summary["package_version"] if summary is not None else "",
            "live_processes": len(live_pids),
            "configuration_valid": not errors,
            "configuration_provisioned": provisioned,
            "configuration_errors": errors,
            "debug": debug,
            "debug_enabled": debug["enabled"],
            "debug_overlay_provisioned": overlay is not None,
            "cursor_provisioned": cursor is not None,
            "fps": fps["fps"],
            "max_fps": fps["max_fps"],
            "idle_fps": fps["idle_fps"],
            "touch_calibration": calibration,
            "physical_rotation": rotation,
            "physical_rotation_control": (
                "writable" if rotation_provisioned else "unavailable"
            ),
            "rotation_provisioned": rotation_provisioned,
            # An action field is present so generic Settings can turn false
            # into true and submit it through HAL v1 set_state.
            "restart": False,
        }

    def inventory(self) -> dict[str, Any]:
        try:
            snapshot = self._status_snapshot()
        except (ProviderError, UnavailableError):
            snapshot = {
                "status": "unavailable",
                "reason": "control-plane-unavailable",
                "component_state": "unknown",
            }
        available = snapshot["status"] != "unavailable"
        mutable = self._mutable_fields(snapshot) if available else []
        return {
            "domain": self.domain,
            "status": snapshot["status"],
            "reason": snapshot["reason"],
            "devices": [{
                "id": self.identifier,
                "domain": self.domain,
                "name": "OpenStick CH347 display and touch",
                "available": available,
                "mutable": mutable,
                "metadata": {
                    "driver": self.target_component,
                    "component_state": snapshot["component_state"],
                    "fps_hot_reload": True,
                    "debug_overlay_restart": True,
                    "touch_cursor_restart": True,
                    "touch_calibration_restart": True,
                    "physical_rotation_control": snapshot.get(
                        "physical_rotation_control", "unavailable"
                    ),
                    "control_interface": CONTROL_INTERFACE,
                },
            }],
        }

    def get_state(self, identifier: str) -> dict[str, Any]:
        if device_id(identifier) != self.identifier:
            raise ValidationError("unknown CH347 output device")
        with self._lock:
            snapshot = self._status_snapshot()
        available = snapshot["status"] != "unavailable"
        return {
            "id": self.identifier,
            "domain": self.domain,
            "available": available,
            "values": snapshot,
            "mutable": self._mutable_fields(snapshot) if available else [],
        }

    def _restart(self) -> None:
        summary = self._target_summary()
        if summary is None:
            raise UnavailableError(
                "CH347 display-output component is not installed",
                details={"component": self.target_component},
            )
        if summary["state"] != "ready":
            raise UnavailableError(
                "CH347 display-output component is not running",
                details={"component": self.target_component, "state": summary["state"]},
            )
        self._core_call(
            "stop",
            {"component": self.target_component},
            timeout=15.0,
            idempotent=False,
        )
        try:
            started = self._core_call(
                "start",
                {"component": self.target_component},
                timeout=30.0,
                idempotent=False,
            )
        except Exception as exc:
            raise UnavailableError(
                "CH347 configuration was saved but display-output restart failed",
                details={"component": self.target_component, "phase": "start"},
            ) from exc
        if started.get("state") != "ready":
            raise UnavailableError(
                "CH347 display-output did not become ready after restart",
                details={"component": self.target_component},
            )

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        if device_id(identifier) != self.identifier:
            raise ValidationError("unknown CH347 output device")
        allowed = {
            "debug_enabled",
            "debug_overlay",
            "touch_cursor_enabled",
            "fps",
            "idle_fps",
            "touch_calibration",
            "physical_rotation",
            "restart",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValidationError("CH347 changes have unknown fields: " + ", ".join(unknown))
        with self._lock:
            summary = self._target_summary()
            if summary is None:
                raise UnavailableError(
                    "CH347 display-output component is not installed",
                    details={"component": self.target_component},
                )
            config_changes = set(changes) & {
                "debug_enabled",
                "debug_overlay",
                "touch_cursor_enabled",
                "fps",
                "idle_fps",
                "touch_calibration",
                "physical_rotation",
            }
            if config_changes and not self._configuration_provisioned():
                raise UnavailableError(
                    "CH347 package state has not been provisioned by the display-output",
                    details={
                        "component": self.target_component,
                        "fields": sorted(config_changes),
                    },
                )
            current_fps, _fps_error = self._load_fps()
            current_overlay, _overlay_error = self._load_debug_overlay()
            current_cursor, _cursor_error = self._load_cursor()
            current_calibration, _calibration_error = self._load_calibration()
            debug_enabled = (
                changes["debug_enabled"]
                if "debug_enabled" in changes
                else current_fps["debug_enabled"]
            )
            if not isinstance(debug_enabled, bool):
                raise ValidationError("debug_enabled must be a boolean")
            overlay_changed = "debug_overlay" in changes
            if overlay_changed:
                if current_overlay is None:
                    raise UnavailableError(
                        "CH347 display-output has not provisioned debug overlay settings"
                    )
                debug_overlay = self._validated_debug_overlay(changes["debug_overlay"])
            else:
                debug_overlay = current_overlay
            cursor_changed = "touch_cursor_enabled" in changes
            if cursor_changed:
                if current_cursor is None:
                    raise UnavailableError(
                        "CH347 display-output has not provisioned cursor settings"
                    )
                cursor_enabled = changes["touch_cursor_enabled"]
                if not isinstance(cursor_enabled, bool):
                    raise ValidationError("touch_cursor_enabled must be a boolean")
            else:
                cursor_enabled = current_cursor
            fps = (
                integer(changes["fps"], "fps", minimum=1, maximum=240)
                if "fps" in changes
                else current_fps["fps"]
            )
            idle_fps = (
                integer(changes["idle_fps"], "idle_fps", minimum=0, maximum=60)
                if "idle_fps" in changes
                else current_fps["idle_fps"]
            )
            if idle_fps > fps:
                raise ValidationError("idle_fps must not exceed fps")
            calibration = current_calibration
            calibration_changed = "touch_calibration" in changes
            if calibration_changed:
                calibration = self._validated_calibration(
                    changes["touch_calibration"],
                    partial=True,
                    base=current_calibration,
                )
            rotation_changed = "physical_rotation" in changes
            if rotation_changed:
                _current_rotation, _rotation_error, rotation_provisioned = (
                    self._load_rotation()
                )
                if not rotation_provisioned:
                    raise UnavailableError(
                        "CH347 display-output has not provisioned physical rotation",
                        details={"component": self.target_component},
                    )
                rotation = changes["physical_rotation"]
                if not isinstance(rotation, str) or rotation not in PHYSICAL_ROTATIONS:
                    raise ValidationError(
                        "physical_rotation must be normal, right, left or inverted"
                    )
            restart_requested = changes.get("restart", False)
            if not isinstance(restart_requested, bool):
                raise ValidationError("restart must be a boolean")
            if "restart" in changes and restart_requested is not True:
                raise ValidationError("restart action must be true")

            debug_changed = (
                "debug_enabled" in changes
                and debug_enabled != current_fps["debug_enabled"]
            )
            if debug_changed or "fps" in changes or "idle_fps" in changes:
                self._write_fps(debug_enabled, fps, idle_fps)
            if overlay_changed and debug_overlay is not None:
                self._write_debug_overlay(debug_overlay)
            if cursor_changed and cursor_enabled is not None:
                self._write_cursor(cursor_enabled)
            if "fps" in changes or "idle_fps" in changes:
                self._reload_fps()
            if calibration_changed:
                self._write_calibration(calibration)
            if rotation_changed:
                self._write_rotation(rotation)
            if restart_requested or (
                (
                    debug_changed
                    or overlay_changed
                    or cursor_changed
                    or calibration_changed
                    or rotation_changed
                )
                and summary["state"] == "ready"
            ):
                self._restart()
            return self.get_state(identifier)


class Ch347ControlService:
    """Serve both portable HAL v1 and the optional fully typed control API."""

    def __init__(self, backend: Ch347ControlBackend, *, provider_id: str) -> None:
        self.backend = backend
        self.provider = ProviderService(
            {DOMAIN: backend},
            provider_id=provider_id,
            name="OpenStick CH347 control provider",
            version=__version__,
            capabilities=[
                "display-output.debug-overlay",
                "display-output.debug-overlay.write",
                "display-output.touch-cursor",
                "display-output.touch-cursor.write",
                "display-output.fps",
                "display-output.fps.write",
                "display-output.restart",
                "display-output.physical-rotation",
                "display-output.physical-rotation.write",
                "display-output.status",
                "display-output.touch-calibration",
                "display-output.touch-calibration.write",
            ],
        )

    @staticmethod
    def _typed(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": CONTROL_INTERFACE,
            "device": DEVICE_ID,
            "state": state["values"],
            "mutable": list(state.get("mutable", [])),
        }

    def handle(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method in {"describe", "inventory", "get_state", "set_state"}:
            return self.provider.handle(method, payload)
        if method == "status":
            object_payload(payload, allowed=())
            return self._typed(self.backend.get_state(DEVICE_ID))
        if method == "get_fps":
            object_payload(payload, allowed=())
            state = self.backend.get_state(DEVICE_ID)["values"]
            return {
                "schema": CONTROL_INTERFACE,
                "device": DEVICE_ID,
                "fps": state["fps"],
                "idle_fps": state["idle_fps"],
            }
        if method == "get_debug":
            object_payload(payload, allowed=())
            state = self.backend.get_state(DEVICE_ID)["values"]
            return {
                "schema": CONTROL_INTERFACE,
                "device": DEVICE_ID,
                "debug": state["debug"],
            }
        if method == "set_debug":
            request = object_payload(
                payload,
                allowed=("enabled", "overlay", "cursor_enabled"),
            )
            if not request:
                raise ValidationError(
                    "set_debug requires enabled, overlay or cursor_enabled"
                )
            changes: dict[str, Any] = {}
            if "enabled" in request:
                enabled = request["enabled"]
                if not isinstance(enabled, bool):
                    raise ValidationError("enabled must be a boolean")
                changes["debug_enabled"] = enabled
            if "overlay" in request:
                changes["debug_overlay"] = self.backend._validated_debug_overlay(
                    request["overlay"]
                )
            if "cursor_enabled" in request:
                cursor_enabled = request["cursor_enabled"]
                if not isinstance(cursor_enabled, bool):
                    raise ValidationError("cursor_enabled must be a boolean")
                changes["touch_cursor_enabled"] = cursor_enabled
            state = self.backend.set_state(
                DEVICE_ID,
                changes,
            )["values"]
            return {
                "schema": CONTROL_INTERFACE,
                "device": DEVICE_ID,
                "debug": state["debug"],
            }
        if method == "set_fps":
            request = object_payload(
                payload,
                allowed=("fps", "idle_fps"),
                required=("fps",),
            )
            state = self.backend.set_state(DEVICE_ID, request)["values"]
            return {
                "schema": CONTROL_INTERFACE,
                "device": DEVICE_ID,
                "fps": state["fps"],
                "idle_fps": state["idle_fps"],
            }
        if method == "get_touch_calibration":
            object_payload(payload, allowed=())
            state = self.backend.get_state(DEVICE_ID)["values"]
            return {
                "schema": CONTROL_INTERFACE,
                "device": DEVICE_ID,
                "touch_calibration": state["touch_calibration"],
            }
        if method == "set_touch_calibration":
            request = object_payload(
                payload,
                allowed=("touch_calibration",),
                required=("touch_calibration",),
            )
            state = self.backend.set_state(DEVICE_ID, request)["values"]
            return {
                "schema": CONTROL_INTERFACE,
                "device": DEVICE_ID,
                "touch_calibration": state["touch_calibration"],
                "status": state["status"],
            }
        if method == "get_physical_rotation":
            object_payload(payload, allowed=())
            state = self.backend.get_state(DEVICE_ID)
            return {
                "schema": CONTROL_INTERFACE,
                "device": DEVICE_ID,
                "physical_rotation": state["values"]["physical_rotation"],
                "writable": "physical_rotation" in state.get("mutable", []),
                "control": state["values"]["physical_rotation_control"],
            }
        if method == "set_physical_rotation":
            request = object_payload(
                payload,
                allowed=("physical_rotation",),
                required=("physical_rotation",),
            )
            return self._typed(
                self.backend.set_state(DEVICE_ID, request)
            )
        if method == "restart":
            object_payload(payload, allowed=())
            return self._typed(
                self.backend.set_state(DEVICE_ID, {"restart": True})
            )
        raise HalError("HAL_UNKNOWN_METHOD", f"unknown CH347 control method {method!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MSYS CH347 typed HAL control provider")
    parser.add_argument(
        "--config-dir",
        default=str(default_config_dir()),
        help="package-owned mutable CH347 configuration directory",
    )
    parser.add_argument(
        "--run-dir",
        default=os.environ.get("MSYS_CH347_RUN_DIR", "/tmp/ch347_dirty_usb_x11"),
        help="CH347 runtime directory containing the supervised pid file",
    )
    parser.add_argument(
        "--target-component",
        default=os.environ.get("MSYS_CH347_COMPONENT", DEFAULT_TARGET),
        help="exact MSYS display-output component to inspect and restart",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gateway = PublicGateway()
    backend = Ch347ControlBackend(
        gateway,
        config_dir=Path(args.config_dir),
        run_dir=Path(args.run_dir),
        target_component=args.target_component,
    )
    provider_id = os.environ.get(
        "MSYS_COMPONENT_ID",
        "org.msys.hal.linux:ch347-output-control",
    )
    service = Ch347ControlService(backend, provider_id=provider_id)
    server = ComponentServer(service.handle, workers=4)
    return server.run(ready_event=(
        "msys.hal.provider.ready",
        {"provider": provider_id, "domains": [DOMAIN]},
    ))


if __name__ == "__main__":
    raise SystemExit(main())
