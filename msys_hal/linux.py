from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Protocol

from .display_session import DisplaySessionReader, normalized_matrix
from .errors import ProviderError, ReadOnlyError, UnavailableError, ValidationError
from .radios import BluetoothBackend, NetworkBackend, WpaSupplicantControl
from .validation import device_id, integer


ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVENT_RE = re.compile(r"^event[0-9]{1,6}$")


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


class Backend(Protocol):
    domain: str

    def inventory(self) -> dict[str, Any]: ...

    def get_state(self, identifier: str) -> dict[str, Any]: ...

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]: ...


def _read_text(path: Path, *, maximum: int = 512) -> str | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
    except OSError:
        return None
    if len(data) > maximum or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        return None


def _read_int(
    path: Path,
    *,
    minimum: int = -(2**63),
    maximum: int = 2**63 - 1,
) -> int | None:
    raw = _read_text(path, maximum=64)
    if raw is None or not re.fullmatch(r"-?[0-9]+", raw):
        return None
    value = int(raw)
    if value < minimum or value > maximum:
        return None
    return value


def _entries(root: Path, *, prefixes: tuple[str, ...] = ()) -> list[Path]:
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    result: list[Path] = []
    for entry in sorted(entries, key=lambda item: item.name)[:128]:
        if not ENTRY_RE.fullmatch(entry.name):
            continue
        if prefixes and not entry.name.startswith(prefixes):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        result.append(entry)
    return result


def _device(
    domain: str,
    name: str,
    *,
    available: bool = True,
    mutable: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{domain}:{name}",
        "domain": domain,
        "name": name[:128],
        "available": bool(available),
        "mutable": list(mutable or []),
        "metadata": dict(metadata or {}),
    }


def _inventory(domain: str, devices: list[dict[str, Any]], reason: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "domain": domain,
        "status": "available" if any(item.get("available") for item in devices) else "unavailable",
        "devices": devices,
    }
    if result["status"] == "unavailable":
        result["reason"] = reason or "no-device"
    return result


def _name_from_id(identifier: str, expected_domain: str) -> str:
    parsed = device_id(identifier)
    domain, name = parsed.split(":", 1)
    if domain != expected_domain or not ENTRY_RE.fullmatch(name):
        raise ValidationError(f"device {identifier!r} does not belong to {expected_domain}")
    return name


def _session_from_gateway(
    gateway: Gateway | None,
    reader: DisplaySessionReader,
) -> dict[str, Any] | None:
    if gateway is None:
        return None
    try:
        response = gateway.call(
            "role:window-manager",
            "get_display_session",
            {},
            timeout=3.0,
            idempotent=True,
        )
    except Exception:
        return None
    if (
        not isinstance(response, dict)
        or response.get("type") != "return"
        or not isinstance(response.get("payload"), dict)
    ):
        return None
    payload = response["payload"]
    if payload.get("ok") is not True or not isinstance(payload.get("display_session"), dict):
        return None
    try:
        return reader.accept(payload["display_session"])
    except (UnavailableError, ValueError):
        return None


class PowerBackend:
    domain = "power"

    def __init__(self, root: Path = Path("/sys/class/power_supply")) -> None:
        self.root = root

    def inventory(self) -> dict[str, Any]:
        devices = []
        for entry in _entries(self.root):
            supply_type = (_read_text(entry / "type", maximum=64) or "unknown")[:64]
            devices.append(_device(
                self.domain,
                entry.name,
                metadata={"type": supply_type},
            ))
        return _inventory(self.domain, devices, "power-supply-class-unavailable")

    def get_state(self, identifier: str) -> dict[str, Any]:
        name = _name_from_id(identifier, self.domain)
        entry = self.root / name
        if entry not in _entries(self.root):
            raise UnavailableError("power device is unavailable", details={"id": identifier})
        values: dict[str, Any] = {}
        text_fields = {
            "status": "status",
            "technology": "technology",
            "health": "health",
            "type": "type",
        }
        for output, filename in text_fields.items():
            value = _read_text(entry / filename, maximum=128)
            if value is not None:
                values[output] = value[:128]
        numeric_fields = {
            "capacity_percent": ("capacity", 0, 100),
            "energy_now_uwh": ("energy_now", 0, 2**63 - 1),
            "energy_full_uwh": ("energy_full", 0, 2**63 - 1),
            "charge_now_uah": ("charge_now", 0, 2**63 - 1),
            "charge_full_uah": ("charge_full", 0, 2**63 - 1),
            "voltage_now_uv": ("voltage_now", 0, 2**63 - 1),
            "current_now_ua": ("current_now", -(2**63), 2**63 - 1),
            "power_now_uw": ("power_now", -(2**63), 2**63 - 1),
            "temperature_tenths_c": ("temp", -2732, 10000),
        }
        for output, (filename, minimum, maximum) in numeric_fields.items():
            value = _read_int(entry / filename, minimum=minimum, maximum=maximum)
            if value is not None:
                values[output] = value
        online = _read_int(entry / "online", minimum=0, maximum=1)
        if online is not None:
            values["online"] = bool(online)
        return {"id": identifier, "domain": self.domain, "available": True, "values": values, "mutable": []}

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        _name_from_id(identifier, self.domain)
        raise ReadOnlyError("power devices are read-only")


class ThermalBackend:
    domain = "thermal"

    def __init__(self, root: Path = Path("/sys/class/thermal")) -> None:
        self.root = root

    def inventory(self) -> dict[str, Any]:
        devices = []
        for entry in _entries(self.root, prefixes=("thermal_zone",)):
            sensor_type = (_read_text(entry / "type", maximum=128) or entry.name)[:128]
            devices.append(_device(self.domain, entry.name, metadata={"type": sensor_type}))
        return _inventory(self.domain, devices, "thermal-class-unavailable")

    def get_state(self, identifier: str) -> dict[str, Any]:
        name = _name_from_id(identifier, self.domain)
        entry = self.root / name
        if entry not in _entries(self.root, prefixes=("thermal_zone",)):
            raise UnavailableError("thermal sensor is unavailable", details={"id": identifier})
        temperature = _read_int(entry / "temp", minimum=-273_150, maximum=1_000_000)
        if temperature is None:
            raise UnavailableError("thermal sensor has no readable temperature", details={"id": identifier})
        values: dict[str, Any] = {"temperature_millicelsius": temperature}
        sensor_type = _read_text(entry / "type", maximum=128)
        if sensor_type:
            values["type"] = sensor_type[:128]
        return {"id": identifier, "domain": self.domain, "available": True, "values": values, "mutable": []}

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        _name_from_id(identifier, self.domain)
        raise ReadOnlyError("thermal sensors are read-only")


class BacklightBackend:
    domain = "backlight"

    def __init__(self, root: Path = Path("/sys/class/backlight")) -> None:
        self.root = root

    def inventory(self) -> dict[str, Any]:
        devices = []
        for entry in _entries(self.root):
            maximum = _read_int(entry / "max_brightness", minimum=1, maximum=2**31 - 1)
            if maximum is None:
                continue
            brightness = _read_int(entry / "brightness", minimum=0, maximum=maximum)
            writable = brightness is not None and self._is_writable(entry / "brightness")
            devices.append(_device(
                self.domain,
                entry.name,
                available=brightness is not None,
                mutable=["brightness", "brightness_percent"] if writable else [],
                metadata={
                    "max_brightness": maximum,
                    "control": "writable" if writable else "read-only",
                },
            ))
        return _inventory(self.domain, devices, "backlight-class-unavailable")

    def _entry(self, identifier: str) -> Path:
        name = _name_from_id(identifier, self.domain)
        entry = self.root / name
        if entry not in _entries(self.root):
            raise UnavailableError("backlight device is unavailable", details={"id": identifier})
        return entry

    def get_state(self, identifier: str) -> dict[str, Any]:
        entry = self._entry(identifier)
        maximum = _read_int(entry / "max_brightness", minimum=1, maximum=2**31 - 1)
        brightness = _read_int(entry / "brightness", minimum=0, maximum=2**31 - 1)
        if maximum is None or brightness is None:
            raise UnavailableError("backlight attributes are unreadable", details={"id": identifier})
        values: dict[str, Any] = {
            "brightness": brightness,
            "brightness_percent": round(brightness * 100 / maximum),
            "max_brightness": maximum,
        }
        actual = _read_int(entry / "actual_brightness", minimum=0, maximum=maximum)
        if actual is not None:
            values["actual_brightness"] = actual
            values["actual_brightness_percent"] = round(actual * 100 / maximum)
        mutable = (
            ["brightness", "brightness_percent"]
            if self._is_writable(entry / "brightness")
            else []
        )
        return {
            "id": identifier,
            "domain": self.domain,
            "available": True,
            "values": values,
            "mutable": mutable,
        }

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        if len(changes) != 1 or not set(changes) <= {"brightness", "brightness_percent"}:
            raise ValidationError(
                "backlight changes must contain exactly brightness or brightness_percent"
            )
        entry = self._entry(identifier)
        maximum = _read_int(entry / "max_brightness", minimum=1, maximum=2**31 - 1)
        if maximum is None:
            raise UnavailableError("backlight maximum is unreadable", details={"id": identifier})
        if not self._is_writable(entry / "brightness"):
            raise ReadOnlyError(
                "backlight brightness is read-only",
                details={"id": identifier, "mutable": []},
            )
        if "brightness" in changes:
            requested = integer(
                changes["brightness"],
                "changes.brightness",
                minimum=0,
                maximum=maximum,
            )
        else:
            percent = integer(
                changes["brightness_percent"],
                "changes.brightness_percent",
                minimum=0,
                maximum=100,
            )
            requested = (percent * maximum + 50) // 100
        self._safe_write(entry / "brightness", requested)
        observed = _read_int(entry / "brightness", minimum=0, maximum=maximum)
        if observed != requested:
            raise ProviderError(
                "backlight write could not be verified",
                details={"id": identifier, "requested": requested, "observed": observed},
            )
        return self.get_state(identifier)

    def _is_writable(self, attribute: Path) -> bool:
        try:
            resolved = attribute.resolve(strict=True)
            mode = resolved.stat().st_mode
        except OSError:
            return False
        return bool(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)) and os.access(
            resolved,
            os.W_OK,
        )

    def _safe_write(self, attribute: Path, value: int) -> None:
        try:
            resolved = attribute.resolve(strict=True)
            root_resolved = self.root.resolve(strict=True)
        except OSError as exc:
            raise UnavailableError("backlight attribute is unavailable") from exc
        if self.root == Path("/sys/class/backlight"):
            allowed = resolved == Path("/sys/devices") or Path("/sys/devices") in resolved.parents
        else:
            allowed = resolved == root_resolved or root_resolved in resolved.parents
        if not allowed or resolved.name != "brightness":
            raise ProviderError("refusing backlight write outside the validated sysfs attribute")
        flags = (
            os.O_WRONLY
            | getattr(os, "O_TRUNC", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(resolved, flags)
            try:
                data = f"{value}\n".encode("ascii")
                written = os.write(fd, data)
                if written != len(data):
                    raise OSError("short sysfs write")
            finally:
                os.close(fd)
        except PermissionError as exc:
            raise ReadOnlyError("backlight brightness is read-only") from exc
        except OSError as exc:
            raise ProviderError("backlight write failed", details={"error": str(exc)[:256]}) from exc


class DisplayBackend:
    domain = "display"
    identifier = "display:primary"
    _profiles = {"mobile", "kiosk", "desktop"}
    _orientations = {"auto", "portrait", "landscape"}

    def __init__(
        self,
        gateway: Gateway,
        session_reader: DisplaySessionReader | None = None,
    ) -> None:
        self.gateway = gateway
        self.session_reader = session_reader or DisplaySessionReader.from_environment()

    def _call(self, method: str, payload: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
        try:
            response = self.gateway.call(
                "role:window-manager",
                method,
                payload,
                timeout=4.0,
                idempotent=idempotent,
            )
        except Exception as exc:
            raise UnavailableError("window-manager role is unavailable", details={"error": str(exc)[:256]}) from exc
        if (
            not isinstance(response, dict)
            or response.get("type") != "return"
            or not isinstance(response.get("payload"), dict)
        ):
            raise UnavailableError(
                "window-manager rejected the layout call",
                details={
                    "code": str(response.get("code", "NO_PROVIDER"))[:64]
                    if isinstance(response, dict)
                    else "INVALID_RESPONSE"
                },
            )
        result = dict(response["payload"])
        if result.get("ok") is not True:
            raise UnavailableError(
                "window-manager layout state is unavailable",
                details={"reason": str(result.get("reason") or result.get("error") or "unavailable")[:256]},
            )
        return result

    def _layout(self) -> tuple[dict[str, Any] | None, str]:
        try:
            return self._call("get_layout", {}, idempotent=True), ""
        except UnavailableError as exc:
            return None, exc.message

    def _session(
        self,
        layout: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, str]:
        embedded = layout.get("display_session") if isinstance(layout, dict) else None
        if embedded is not None:
            try:
                return self.session_reader.accept(embedded), ""
            except (UnavailableError, ValueError):
                pass
        in_band = _session_from_gateway(self.gateway, self.session_reader)
        if in_band is not None:
            return in_band, ""
        try:
            return self.session_reader.load(), ""
        except UnavailableError as exc:
            return None, str(exc.details.get("reason") or exc.message)

    @staticmethod
    def _mutable(layout: dict[str, Any] | None) -> list[str]:
        return ["profile", "orientation", "insets"] if layout is not None else []

    @staticmethod
    def _session_summary(session: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": session["schema"],
            "state": session["state"],
            "provider": session["provider"],
            "generation": session["generation"],
            "display": session["display"],
            "geometry": session["geometry"],
            "input_transform": session["input_transform"],
            "observed_at_unix_ms": session["observed_at_unix_ms"],
        }

    @staticmethod
    def _session_inventory_metadata(session: dict[str, Any]) -> dict[str, Any]:
        """Expose searchable session facts without nesting the state contract.

        Provider inventory is embedded below manager/domain/device/metadata and
        is deliberately validated with a tighter JSON depth than get_state.
        Keeping the complete display-session document here would put matrix
        values one level beyond that wire limit on a real CH347 session.  The
        full, versioned document remains available from get_state.
        """

        geometry = session["geometry"]
        transform = session["input_transform"]
        return {
            "display": session["display"],
            "display_provider": session["provider"],
            "display_generation": session["generation"],
            "display_width": geometry["width"],
            "display_height": geometry["height"],
            "display_depth": geometry["depth"],
            "input_mode": transform["mode"],
            "input_enabled": transform["enabled"],
            "observed_at_unix_ms": session["observed_at_unix_ms"],
        }

    def inventory(self) -> dict[str, Any]:
        layout, layout_error = self._layout()
        session, session_error = self._session(layout)
        available = layout is not None or session is not None
        metadata = {
            key: layout[key]
            for key in ("profile", "orientation", "navigation", "navigation_edge")
            if layout is not None and key in layout
        }
        metadata["session_status"] = "available" if session is not None else "unavailable"
        metadata["layout_control"] = "writable" if layout is not None else "unavailable"
        metadata["physical_rotation"] = (
            "read-only" if session is not None else "unavailable"
        )
        if session is not None:
            metadata.update(self._session_inventory_metadata(session))
        device = _device(
            self.domain,
            "primary",
            available=available,
            mutable=self._mutable(layout),
            metadata=metadata,
        )
        if not available:
            return {
                "domain": self.domain,
                "status": "unavailable",
                "reason": session_error or layout_error or "no-display-session",
                "devices": [device],
            }
        if layout is None or session is None:
            return {
                "domain": self.domain,
                "status": "degraded",
                "reason": layout_error if layout is None else session_error,
                "devices": [device],
            }
        return {"domain": self.domain, "status": "available", "devices": [device]}

    def get_state(self, identifier: str) -> dict[str, Any]:
        if device_id(identifier) != self.identifier:
            raise ValidationError("unknown display device")
        layout, layout_error = self._layout()
        session, session_error = self._session(layout)
        if layout is None and session is None:
            raise UnavailableError(
                "display state is unavailable",
                details={
                    "id": identifier,
                    "layout": layout_error or "unavailable",
                    "session": session_error or "unavailable",
                },
            )
        allowed = {
            "schema", "profile", "orientation_policy", "insets_policy", "requested_insets",
            "orientation", "screen", "insets", "workarea", "navigation", "navigation_edge",
            "navigation_region", "navigation_input_region", "requested", "display_consistent",
        }
        values = {
            key: value
            for key, value in (layout or {}).items()
            if key in allowed
        }
        values["session_status"] = "available" if session is not None else "unavailable"
        values["layout_control"] = "writable" if layout is not None else "unavailable"
        values["physical_rotation"] = {
            "status": "read-only" if session is not None else "unavailable",
            "owner": session["provider"] if session is not None else None,
        }
        if session is not None:
            values["display_session"] = self._session_summary(session)
        return {
            "id": identifier,
            "domain": self.domain,
            "available": True,
            "values": values,
            "mutable": self._mutable(layout),
        }

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        if device_id(identifier) != self.identifier:
            raise ValidationError("unknown display device")
        physical = set(changes) & {"rotation", "physical_rotation", "input_transform"}
        if physical:
            raise ReadOnlyError(
                "physical display transforms are owned by the selected display-output provider",
                details={"id": identifier, "fields": sorted(physical), "owner": "display-output"},
            )
        unknown = set(changes) - {"profile", "mode", "orientation", "insets"}
        if unknown or ("profile" in changes and "mode" in changes):
            raise ValidationError("display changes accepts profile, orientation and insets")
        payload: dict[str, Any] = {}
        profile = changes.get("profile", changes.get("mode"))
        if profile is not None:
            if not isinstance(profile, str) or profile not in self._profiles:
                raise ValidationError("display profile must be mobile, kiosk or desktop")
            payload["profile"] = profile
        if "orientation" in changes:
            orientation = changes["orientation"]
            if not isinstance(orientation, str) or orientation not in self._orientations:
                raise ValidationError("display orientation must be auto, portrait or landscape")
            payload["orientation"] = orientation
        if "insets" in changes:
            insets = changes["insets"]
            if insets == "auto":
                payload["insets"] = "auto"
            elif isinstance(insets, dict) and set(insets) == {"top", "right", "bottom", "left"}:
                payload["insets"] = {
                    key: integer(insets[key], f"changes.insets.{key}", minimum=0, maximum=8192)
                    for key in ("top", "right", "bottom", "left")
                }
            else:
                raise ValidationError("display insets must be auto or top/right/bottom/left integers")
        if not payload:
            raise ValidationError("display changes is empty")
        self._call("set_layout", payload, idempotent=False)
        return self.get_state(identifier)


def parse_input_devices(text: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for block in text.split("\n\n")[:128]:
        fields: dict[str, str] = {}
        for line in block.splitlines()[:64]:
            if ": " not in line:
                continue
            prefix, value = line.split(": ", 1)
            if prefix in {"N", "P", "S", "H", "I"}:
                fields[prefix] = value.strip()[:512]
        handlers = fields.get("H", "")
        event_name = next((part for part in handlers.split() if EVENT_RE.fullmatch(part)), None)
        if event_name is None:
            continue
        name = fields.get("N", "Name=Unknown")
        if name.startswith("Name="):
            name = name[5:].strip('"')
        metadata: dict[str, Any] = {"name": name[:128] or "Unknown"}
        if fields.get("P", "").startswith("Phys="):
            metadata["physical"] = fields["P"][5:][:128]
        if fields.get("S", "").startswith("Sysfs="):
            metadata["sysfs"] = fields["S"][6:][:256]
        if fields.get("I", "").startswith("Bus="):
            metadata["identity"] = fields["I"][:256]
        devices.append(_device("input", event_name, metadata=metadata))
    unique = {item["id"]: item for item in devices}
    return [unique[key] for key in sorted(unique)]


XINPUT_MATRIX_RE = re.compile(
    r"Coordinate Transformation Matrix[^:]*:\s*([^\r\n]+)",
    re.IGNORECASE,
)
INPUT_ORIENTATIONS: dict[str, list[int]] = {
    "normal": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    "left": [0, -1, 1, 1, 0, 0, 0, 0, 1],
    "right": [0, 1, 0, -1, 0, 1, 0, 0, 1],
    "inverted": [-1, 0, 1, 0, -1, 1, 0, 0, 1],
}


class InputBackend:
    domain = "input"
    display_touch_id = "input:display-touch"

    def __init__(
        self,
        proc_file: Path = Path("/proc/bus/input/devices"),
        sysfs_root: Path = Path("/sys/class/input"),
        session_reader: DisplaySessionReader | None = None,
        *,
        gateway: Gateway | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        xinput_binary: str | None = None,
    ) -> None:
        self.proc_file = proc_file
        self.sysfs_root = sysfs_root
        self.session_reader = session_reader or DisplaySessionReader.from_environment()
        self.gateway = gateway
        self.runner = runner
        self.xinput_binary = xinput_binary if xinput_binary is not None else shutil.which("xinput")

    def _display_input(self) -> tuple[dict[str, Any], dict[str, Any]]:
        session = _session_from_gateway(self.gateway, self.session_reader)
        if session is None:
            session = self.session_reader.load()
        transform = session["input_transform"]
        if not transform["enabled"]:
            raise UnavailableError(
                "display session has no active input transform",
                details={"id": self.display_touch_id, "mode": transform["mode"]},
            )
        return session, transform

    def _run_xinput(
        self,
        arguments: list[str],
        *,
        display: str,
    ) -> subprocess.CompletedProcess[str]:
        if not self.xinput_binary:
            raise ReadOnlyError(
                "xinput control is unavailable",
                details={"id": self.display_touch_id, "reason": "xinput-missing"},
            )
        try:
            return self.runner(
                [self.xinput_binary, *arguments],
                env={**os.environ, "DISPLAY": display},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise UnavailableError(
                "xinput control command is unavailable",
                details={"id": self.display_touch_id, "cause": type(exc).__name__},
            ) from exc

    def _xinput_id(self, session: dict[str, Any], transform: dict[str, Any]) -> str:
        if transform["mode"] != "xinput":
            raise ReadOnlyError(
                "input transform is owned by the display-output provider",
                details={
                    "id": self.display_touch_id,
                    "mode": transform["mode"],
                    "owner": session["provider"],
                },
            )
        name = transform.get("device")
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("-")
            or any(ord(character) < 32 for character in name)
        ):
            raise ReadOnlyError(
                "xinput transform has no uniquely identifiable device",
                details={"id": self.display_touch_id, "reason": "device-unspecified"},
            )
        result = self._run_xinput(["list", "--id-only", name], display=session["display"])
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        if result.returncode != 0 or not ids:
            raise UnavailableError(
                "display input device is unavailable",
                details={"id": self.display_touch_id, "reason": "device-not-found"},
            )
        if len(ids) != 1:
            raise ReadOnlyError(
                "xinput device name is ambiguous",
                details={"id": self.display_touch_id, "reason": "device-ambiguous"},
            )
        return ids[0]

    def _query_matrix(self, session: dict[str, Any], xinput_id: str) -> list[int | float]:
        result = self._run_xinput(["list-props", xinput_id], display=session["display"])
        match = XINPUT_MATRIX_RE.search(result.stdout)
        if result.returncode != 0 or match is None:
            raise UnavailableError(
                "xinput transform could not be read",
                details={"id": self.display_touch_id, "reason": "property-unavailable"},
            )
        raw = [part.strip() for part in match.group(1).replace(",", " ").split()]
        try:
            values: list[int | float] = [float(part) for part in raw]
            return normalized_matrix(values, label="xinput transform")
        except (ValueError, OverflowError) as exc:
            raise ProviderError(
                "xinput returned an invalid transform",
                details={"id": self.display_touch_id},
            ) from exc

    @staticmethod
    def _orientation(matrix: list[int | float]) -> str:
        for name, candidate in INPUT_ORIENTATIONS.items():
            if all(abs(float(left) - float(right)) <= 1e-6 for left, right in zip(matrix, candidate)):
                return name
        return "custom"

    def _control(self, session: dict[str, Any], transform: dict[str, Any]) -> tuple[str, str | None]:
        try:
            return "writable", self._xinput_id(session, transform)
        except ReadOnlyError:
            return "read-only", None
        except UnavailableError:
            return "unavailable", None

    def _touch_state(
        self,
        session: dict[str, Any],
        transform: dict[str, Any],
        matrix: list[int | float],
        control: str,
        *,
        source: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": self.display_touch_id,
            "domain": self.domain,
            "available": True,
            "values": {
                "display": session["display"],
                "provider": session["provider"],
                "generation": session["generation"],
                "mode": transform["mode"],
                "device": transform.get("device"),
                "space": transform["space"],
                "matrix": matrix,
                "orientation": self._orientation(matrix),
                "source": source or transform["source"],
                "verified": True,
                "control": control,
            },
            "mutable": ["orientation", "matrix"] if control == "writable" else [],
        }

    def inventory(self) -> dict[str, Any]:
        text = _read_text(self.proc_file, maximum=256 * 1024)
        devices = parse_input_devices(text) if text else []
        if not devices:
            for entry in _entries(self.sysfs_root, prefixes=("event",)):
                if not EVENT_RE.fullmatch(entry.name):
                    continue
                name = _read_text(entry / "device" / "name", maximum=128) or "Unknown"
                devices.append(_device(self.domain, entry.name, metadata={"name": name[:128]}))
        try:
            session, transform = self._display_input()
        except UnavailableError:
            session = None
            transform = None
        if session is not None and transform is not None:
            control, _xinput_id = self._control(session, transform)
            devices.append(_device(
                self.domain,
                "display-touch",
                mutable=["orientation", "matrix"] if control == "writable" else [],
                metadata={
                    "name": str(transform.get("device") or "Display touch input")[:128],
                    "mode": transform["mode"],
                    "display": session["display"],
                    "provider": session["provider"],
                    "control": control,
                },
            ))
        return _inventory(self.domain, devices, "input-inventory-unavailable")

    def get_state(self, identifier: str) -> dict[str, Any]:
        if device_id(identifier) == self.display_touch_id:
            session, transform = self._display_input()
            control, xinput_id = self._control(session, transform)
            matrix = normalized_matrix(transform["matrix"])
            if control == "writable" and xinput_id is not None:
                try:
                    matrix = self._query_matrix(session, xinput_id)
                except (UnavailableError, ProviderError):
                    control = "unavailable"
            return self._touch_state(session, transform, matrix, control)
        name = _name_from_id(identifier, self.domain)
        for item in self.inventory()["devices"]:
            if item["id"] == identifier:
                return {
                    "id": identifier,
                    "domain": self.domain,
                    "available": True,
                    "values": item["metadata"],
                    "mutable": [],
                }
        raise UnavailableError("input device is unavailable", details={"id": f"input:{name}"})

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        if device_id(identifier) != self.display_touch_id:
            _name_from_id(identifier, self.domain)
            raise ReadOnlyError(
                "kernel input inventory is read-only",
                details={"id": identifier, "mutable": []},
            )
        if len(changes) != 1 or not set(changes) <= {"orientation", "matrix"}:
            raise ValidationError(
                "display input changes must contain exactly orientation or matrix"
            )
        session, transform = self._display_input()
        xinput_id = self._xinput_id(session, transform)
        if "orientation" in changes:
            orientation = changes["orientation"]
            if not isinstance(orientation, str) or orientation not in INPUT_ORIENTATIONS:
                raise ValidationError(
                    "input orientation must be normal, left, right or inverted"
                )
            requested: list[int | float] = list(INPUT_ORIENTATIONS[orientation])
        else:
            try:
                requested = normalized_matrix(changes["matrix"])
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        result = self._run_xinput(
            [
                "set-prop",
                xinput_id,
                "Coordinate Transformation Matrix",
                *(format(float(value), ".12g") for value in requested),
            ],
            display=session["display"],
        )
        if result.returncode != 0:
            raise ProviderError(
                "xinput transform write failed",
                details={"id": identifier, "returncode": result.returncode},
            )
        observed = self._query_matrix(session, xinput_id)
        if any(
            abs(float(left) - float(right)) > 1e-6
            for left, right in zip(requested, observed)
        ):
            raise ProviderError(
                "xinput transform write could not be verified",
                details={"id": identifier, "requested": requested, "observed": observed},
            )
        return self._touch_state(
            session,
            transform,
            observed,
            "writable",
            source="hal-xinput-readback",
        )


def linux_backends(gateway: Gateway) -> dict[str, Backend]:
    """Construct backends; path overrides are trusted operator/test inputs."""
    display_session = DisplaySessionReader.from_environment()
    configured_xinput = os.environ.get("MSYS_HAL_XINPUT_BINARY")
    try:
        wpa_timeout = int(os.environ.get("MSYS_HAL_WPA_TIMEOUT_MS", "750")) / 1000
    except (ValueError, OverflowError):
        wpa_timeout = 0.75
    return {
        "power": PowerBackend(Path(os.environ.get("MSYS_HAL_POWER_ROOT", "/sys/class/power_supply"))),
        "thermal": ThermalBackend(Path(os.environ.get("MSYS_HAL_THERMAL_ROOT", "/sys/class/thermal"))),
        "backlight": BacklightBackend(Path(os.environ.get("MSYS_HAL_BACKLIGHT_ROOT", "/sys/class/backlight"))),
        "input": InputBackend(
            Path(os.environ.get("MSYS_HAL_INPUT_PROC", "/proc/bus/input/devices")),
            Path(os.environ.get("MSYS_HAL_INPUT_ROOT", "/sys/class/input")),
            display_session,
            gateway=gateway,
            xinput_binary=configured_xinput,
        ),
        "display": DisplayBackend(gateway, display_session),
        "network": NetworkBackend(
            Path(os.environ.get("MSYS_HAL_NETWORK_ROOT", "/sys/class/net")),
            WpaSupplicantControl(
                Path(os.environ.get("MSYS_HAL_WPA_CONTROL_ROOT", "/run/wpa_supplicant")),
                timeout=wpa_timeout,
            ),
        ),
        "bluetooth": BluetoothBackend(
            Path(os.environ.get("MSYS_HAL_BLUETOOTH_ROOT", "/sys/class/bluetooth")),
            Path(os.environ.get("MSYS_HAL_RFKILL_ROOT", "/sys/class/rfkill")),
        ),
    }
