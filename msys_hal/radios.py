from __future__ import annotations

import os
import re
import secrets
import socket
import stat
from pathlib import Path
from typing import Any, Callable, Protocol

from .errors import HalError, ProviderError, ReadOnlyError, UnavailableError, ValidationError
from .validation import integer


ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5,31}[0-9A-Fa-f]{2}$")
WPA_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_INTERFACES = 64
MAX_SCAN_RESULTS = 20
MAX_CONFIGURED_NETWORKS = 16
MAX_WPA_RESPONSE = 64 * 1024


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


def _read_int(path: Path, *, minimum: int, maximum: int) -> int | None:
    raw = _read_text(path, maximum=64)
    if raw is None or not re.fullmatch(r"-?[0-9]+", raw):
        return None
    value = int(raw)
    if value < minimum or value > maximum:
        return None
    return value


def _entries(root: Path, *, limit: int = 128) -> list[Path]:
    result: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if len(result) >= limit:
                    break
                if not ENTRY_RE.fullmatch(entry.name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                result.append(Path(entry.path))
    except OSError:
        return []
    return sorted(result, key=lambda item: item.name)


def _device(
    domain: str,
    name: str,
    *,
    mutable: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{domain}:{name}",
        "domain": domain,
        "name": name[:128],
        "available": True,
        "mutable": list(mutable or []),
        "metadata": dict(metadata or {}),
    }


def _state(
    domain: str,
    name: str,
    values: dict[str, Any],
    *,
    mutable: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{domain}:{name}",
        "domain": domain,
        "available": True,
        "values": values,
        "mutable": list(mutable or []),
    }


def _device_name(identifier: str, expected_domain: str) -> str:
    if not isinstance(identifier, str) or ":" not in identifier:
        raise ValidationError("device id is invalid")
    domain, name = identifier.split(":", 1)
    if domain != expected_domain or not ENTRY_RE.fullmatch(name):
        raise ValidationError(f"device does not belong to {expected_domain}")
    return name


class WifiControl(Protocol):
    def available(self, interface: str) -> bool: ...

    def request(self, interface: str, command: str) -> str: ...


class WpaSupplicantControl:
    """Small, bounded wpa_supplicant Unix-datagram control client."""

    def __init__(
        self,
        control_root: Path = Path("/run/wpa_supplicant"),
        *,
        timeout: float = 0.75,
        socket_factory: Callable[..., Any] = socket.socket,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self.control_root = control_root
        self.timeout = max(0.05, min(float(timeout), 3.0))
        self.socket_factory = socket_factory
        self.token_factory = token_factory

    def _path(self, interface: str) -> Path:
        if not ENTRY_RE.fullmatch(interface):
            raise ValidationError("network interface is invalid")
        return self.control_root / interface

    def available(self, interface: str) -> bool:
        path = self._path(interface)
        try:
            # A trusted root override is allowed, but an interface entry may
            # not redirect the client to an unrelated socket.
            return stat.S_ISSOCK(path.lstat().st_mode)
        except OSError:
            return False

    def request(self, interface: str, command: str) -> str:
        path = self._path(interface)
        if (
            not isinstance(command, str)
            or not command
            or len(command.encode("utf-8")) > 4096
            or any(character in command for character in ("\x00", "\n", "\r"))
        ):
            raise ValidationError("wpa_supplicant command is invalid")
        if not self.available(interface):
            raise UnavailableError(
                "wpa_supplicant control is unavailable",
                details={"interface": interface},
            )
        sock = None
        try:
            sock = self.socket_factory(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            token = self.token_factory()
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", token):
                raise ProviderError("wpa_supplicant client token is invalid")
            # Linux abstract addresses leave no client socket file behind and
            # still allow wpa_supplicant to reply to this datagram peer.
            sock.bind("\x00msys-hal-wpa-" + token)
            sock.connect(str(path))
            encoded = command.encode("utf-8")
            if sock.send(encoded) != len(encoded):
                raise OSError("short datagram send")
            response = sock.recv(MAX_WPA_RESPONSE + 1)
        except HalError:
            raise
        except (OSError, TimeoutError) as exc:
            raise UnavailableError(
                "wpa_supplicant control request failed",
                details={"interface": interface},
            ) from exc
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if len(response) > MAX_WPA_RESPONSE or b"\x00" in response:
            raise ProviderError("wpa_supplicant returned an invalid response")
        return response.decode("utf-8", "replace").strip()


def _classify_interface(entry: Path) -> str:
    name = entry.name
    interface_type = _read_int(entry / "type", minimum=0, maximum=65535)
    if name == "lo" or interface_type == 772:
        return "loopback"
    uevent = _read_text(entry / "uevent", maximum=2048) or ""
    if (entry / "wireless").is_dir() or "DEVTYPE=wlan" in uevent.splitlines():
        return "wifi"
    lowered = name.casefold()
    if (
        (entry / "wwan").is_dir()
        or "DEVTYPE=wwan" in uevent.splitlines()
        or lowered.startswith(("wwan", "wwp", "rmnet", "ccmni", "pdp"))
    ):
        return "wwan"
    if interface_type == 1:
        return "ethernet"
    return "other"


def _interface_values(entry: Path) -> dict[str, Any]:
    values: dict[str, Any] = {
        "interface": entry.name,
        "kind": _classify_interface(entry),
    }
    operstate = _read_text(entry / "operstate", maximum=32)
    if operstate and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", operstate):
        values["operstate"] = operstate
    carrier = _read_int(entry / "carrier", minimum=0, maximum=1)
    if carrier is not None:
        values["carrier"] = bool(carrier)
    address = _read_text(entry / "address", maximum=128)
    if address and MAC_RE.fullmatch(address):
        values["address"] = address.casefold()
    mtu = _read_int(entry / "mtu", minimum=68, maximum=2**31 - 1)
    if mtu is not None:
        values["mtu"] = mtu
    return values


def _parse_status(response: str) -> dict[str, Any]:
    if response.startswith(("FAIL", "UNKNOWN COMMAND")):
        raise ProviderError("wpa_supplicant status query failed")
    allowed = {
        "bssid",
        "freq",
        "id",
        "ip_address",
        "key_mgmt",
        "mode",
        "ssid",
        "wpa_state",
    }
    result: dict[str, Any] = {}
    for line in response.splitlines()[:64]:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in allowed or not WPA_KEY_RE.fullmatch(key) or len(value) > 256:
            continue
        if key in {"freq", "id"} and re.fullmatch(r"[0-9]+", value):
            result[key] = min(int(value), 2**31 - 1)
        elif "\x00" not in value and value.isprintable():
            result[key] = value
    return result


def _parse_scan_results(response: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    lines = response.splitlines()
    if not lines or not lines[0].startswith("bssid /"):
        raise ProviderError("wpa_supplicant scan-results query failed")
    for line in lines[1 : MAX_SCAN_RESULTS + 1]:
        fields = line.split("\t", 4)
        if len(fields) != 5:
            continue
        bssid, frequency, signal, flags, ssid = fields
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", bssid):
            continue
        if not re.fullmatch(r"[0-9]{1,10}", frequency) or not re.fullmatch(r"-?[0-9]{1,5}", signal):
            continue
        if (
            len(flags) > 192
            or len(ssid.encode("utf-8")) > 128
            or not flags.isprintable()
            or (ssid and not ssid.isprintable())
        ):
            continue
        result.append({
            "bssid": bssid.casefold(),
            "frequency_mhz": int(frequency),
            "signal_dbm": int(signal),
            "flags": flags,
            "ssid": ssid,
        })
    return result


def _parse_networks(response: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    lines = response.splitlines()
    if not lines or not lines[0].startswith("network id /"):
        raise ProviderError("wpa_supplicant configured-network query failed")
    for line in lines[1 : MAX_CONFIGURED_NETWORKS + 1]:
        fields = line.split("\t", 3)
        if len(fields) != 4 or not re.fullmatch(r"[0-9]{1,4}", fields[0]):
            continue
        network_id = int(fields[0])
        if network_id > 4095:
            continue
        ssid, bssid, flags = fields[1:]
        if len(ssid.encode("utf-8")) > 128 or len(bssid) > 64 or len(flags) > 192:
            continue
        if any(value and not value.isprintable() for value in (ssid, bssid, flags)):
            continue
        result.append({
            "network_id": network_id,
            "ssid": ssid,
            "bssid": bssid,
            "flags": flags,
        })
    return result


def _validate_ssid(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or not value.isprintable():
        raise ValidationError("ssid must be a non-empty printable string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValidationError("ssid is not valid UTF-8") from exc
    if len(encoded) > 32:
        raise ValidationError("ssid must be at most 32 UTF-8 bytes")
    return value


def _validate_psk(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("psk must be a string")
    if len(value) == 64 and re.fullmatch(r"[0-9A-Fa-f]{64}", value):
        return value.casefold()
    if not 8 <= len(value) <= 63 or any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ValidationError("psk must be 8..63 printable ASCII characters or 64 hexadecimal digits")
    return value


def _psk_argument(psk: str) -> str:
    if len(psk) == 64 and re.fullmatch(r"[0-9a-f]{64}", psk):
        return psk
    return '"' + psk.replace("\\", "\\\\").replace('"', '\\"') + '"'


class NetworkBackend:
    domain = "network"

    def __init__(
        self,
        root: Path = Path("/sys/class/net"),
        control: WifiControl | None = None,
        *,
        wpa_root: Path = Path("/run/wpa_supplicant"),
    ) -> None:
        self.root = root
        self.control = control if control is not None else WpaSupplicantControl(wpa_root)

    def _entry(self, identifier: str) -> Path:
        name = _device_name(identifier, self.domain)
        for entry in _entries(self.root, limit=MAX_INTERFACES):
            if entry.name == name:
                return entry
        raise UnavailableError("network interface is unavailable", details={"id": identifier})

    def inventory(self) -> dict[str, Any]:
        devices: list[dict[str, Any]] = []
        for entry in _entries(self.root, limit=MAX_INTERFACES):
            values = _interface_values(entry)
            is_wifi = values["kind"] == "wifi"
            controlled = is_wifi and self.control.available(entry.name)
            devices.append(_device(
                self.domain,
                entry.name,
                mutable=["action"] if controlled else [],
                metadata={
                    key: value
                    for key, value in values.items()
                    if key in {"kind", "operstate", "carrier", "address", "mtu"}
                } | ({"wifi_control": "available" if controlled else "unavailable"} if is_wifi else {}),
            ))
        return {
            "domain": self.domain,
            "status": "available" if devices else "unavailable",
            **({} if devices else {"reason": "network-class-unavailable"}),
            "devices": devices,
        }

    def get_state(self, identifier: str) -> dict[str, Any]:
        entry = self._entry(identifier)
        values = _interface_values(entry)
        mutable: list[str] = []
        if values["kind"] == "wifi":
            if self.control.available(entry.name):
                try:
                    wifi_status = _parse_status(self.control.request(entry.name, "STATUS"))
                    scan_results = _parse_scan_results(
                        self.control.request(entry.name, "SCAN_RESULTS")
                    )
                    configured_networks = _parse_networks(
                        self.control.request(entry.name, "LIST_NETWORKS")
                    )
                    values["wifi_control"] = "available"
                    values["wifi_status"] = wifi_status
                    values["scan_results"] = scan_results
                    values["configured_networks"] = configured_networks
                    mutable = ["action"]
                except (ProviderError, UnavailableError):
                    values["wifi_control"] = "degraded"
            else:
                values["wifi_control"] = "unavailable"
        return _state(self.domain, entry.name, values, mutable=mutable)

    def _request_ok(self, interface: str, command: str, operation: str) -> None:
        if self.control.request(interface, command) != "OK":
            raise ProviderError(f"wpa_supplicant {operation} failed")

    def _configured(self, interface: str) -> list[dict[str, Any]]:
        return _parse_networks(self.control.request(interface, "LIST_NETWORKS"))

    def _finish_state(
        self,
        identifier: str,
        *,
        persisted: bool | None = None,
    ) -> dict[str, Any]:
        result = self.get_state(identifier)
        if persisted is not None:
            result["values"]["configuration_persisted"] = persisted
        return result

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        entry = self._entry(identifier)
        if _classify_interface(entry) != "wifi":
            raise ReadOnlyError("non-Wi-Fi network interfaces are read-only")
        if not self.control.available(entry.name):
            raise UnavailableError(
                "wpa_supplicant control is unavailable",
                details={"id": identifier},
            )
        if not isinstance(changes, dict) or not isinstance(changes.get("action"), str):
            raise ValidationError("network changes require an action")
        action = changes["action"]
        interface = entry.name
        if action in {"scan", "disconnect"}:
            if set(changes) != {"action"}:
                raise ValidationError(f"{action} does not accept additional fields")
            self._request_ok(interface, "SCAN" if action == "scan" else "DISCONNECT", action)
            return self._finish_state(identifier)
        if action == "connect":
            if not set(changes) <= {"action", "ssid", "psk", "security"} or "ssid" not in changes:
                raise ValidationError("connect accepts action, ssid, and psk or open security")
            if "psk" in changes and "security" in changes:
                raise ValidationError("connect cannot combine psk and security")
            is_open = changes.get("security") == "open"
            if "security" in changes and not is_open:
                raise ValidationError("security must be open when present")
            ssid = _validate_ssid(changes["ssid"])
            matches = [item for item in self._configured(interface) if item["ssid"] == ssid]
            if len(matches) > 1:
                raise ProviderError("multiple configured networks have the requested SSID")
            if matches:
                if "psk" in changes or "security" in changes:
                    raise ValidationError("credentials must be omitted for a configured network")
                network_id = matches[0]["network_id"]
                self._request_ok(interface, f"ENABLE_NETWORK {network_id}", "enable network")
                self._request_ok(interface, f"SELECT_NETWORK {network_id}", "select network")
                return self._finish_state(identifier, persisted=True)
            if "psk" not in changes and not is_open:
                raise ValidationError("an unconfigured secured network requires psk")
            psk = _validate_psk(changes["psk"]) if not is_open else ""
            added = self.control.request(interface, "ADD_NETWORK")
            if not re.fullmatch(r"[0-9]{1,4}", added) or int(added) > 4095:
                raise ProviderError("wpa_supplicant could not allocate a network")
            network_id = int(added)
            try:
                ssid_hex = ssid.encode("utf-8").hex()
                self._request_ok(interface, f"SET_NETWORK {network_id} ssid {ssid_hex}", "set SSID")
                if is_open:
                    self._request_ok(
                        interface,
                        f"SET_NETWORK {network_id} key_mgmt NONE",
                        "set open security",
                    )
                else:
                    self._request_ok(
                        interface,
                        f"SET_NETWORK {network_id} psk {_psk_argument(psk)}",
                        "set credentials",
                    )
                self._request_ok(interface, f"ENABLE_NETWORK {network_id}", "enable network")
                self._request_ok(interface, f"SELECT_NETWORK {network_id}", "select network")
            except Exception:
                try:
                    self.control.request(interface, f"REMOVE_NETWORK {network_id}")
                except Exception:
                    pass
                raise
            persisted = self.control.request(interface, "SAVE_CONFIG") == "OK"
            return self._finish_state(identifier, persisted=persisted)
        if action == "forget":
            if not set(changes) <= {"action", "network_id", "ssid"}:
                raise ValidationError("forget accepts only action and network_id or ssid")
            if ("network_id" in changes) == ("ssid" in changes):
                raise ValidationError("forget requires exactly one of network_id or ssid")
            configured = self._configured(interface)
            if "network_id" in changes:
                network_id = integer(changes["network_id"], "network_id", minimum=0, maximum=4095)
                matches = [item for item in configured if item["network_id"] == network_id]
            else:
                ssid = _validate_ssid(changes["ssid"])
                matches = [item for item in configured if item["ssid"] == ssid]
            if len(matches) != 1:
                raise ValidationError("configured network does not resolve uniquely")
            network_id = matches[0]["network_id"]
            self._request_ok(interface, f"REMOVE_NETWORK {network_id}", "forget network")
            persisted = self.control.request(interface, "SAVE_CONFIG") == "OK"
            return self._finish_state(identifier, persisted=persisted)
        raise ValidationError("network action must be scan, connect, disconnect or forget")


def _default_write(path: Path, value: str) -> None:
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        data = value.encode("ascii")
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError("short sysfs write")
    finally:
        os.close(descriptor)


def _default_writable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.W_OK)
    except OSError:
        return False


class BluetoothBackend:
    domain = "bluetooth"

    def __init__(
        self,
        controller_root: Path = Path("/sys/class/bluetooth"),
        rfkill_root: Path = Path("/sys/class/rfkill"),
        *,
        writer: Callable[[Path, str], None] = _default_write,
        writable: Callable[[Path], bool] = _default_writable,
    ) -> None:
        self.controller_root = controller_root
        self.rfkill_root = rfkill_root
        self.writer = writer
        self.writable = writable

    def _controllers(self) -> list[Path]:
        return [
            entry
            for entry in _entries(self.controller_root, limit=32)
            if re.fullmatch(r"hci[0-9]{1,6}", entry.name)
        ]

    def _radios(self) -> list[Path]:
        return [
            entry
            for entry in _entries(self.rfkill_root, limit=64)
            if (_read_text(entry / "type", maximum=32) or "").casefold() == "bluetooth"
        ]

    def _radio_values(self, entry: Path) -> dict[str, Any]:
        soft = _read_int(entry / "soft", minimum=0, maximum=1)
        hard = _read_int(entry / "hard", minimum=0, maximum=1)
        legacy_state = _read_int(entry / "state", minimum=0, maximum=2)
        if soft is None and legacy_state is not None:
            soft = 1 if legacy_state == 0 else 0
        if hard is None and legacy_state is not None:
            hard = 1 if legacy_state == 2 else 0
        values: dict[str, Any] = {
            "kind": "radio",
            "pairing_available": False,
            "pairing_reason": "no-supported-bluetooth-control-channel",
        }
        name = _read_text(entry / "name", maximum=128)
        if name:
            values["radio_name"] = name[:128]
        if soft is not None:
            values["soft_blocked"] = bool(soft)
        if hard is not None:
            values["hard_blocked"] = bool(hard)
        if soft is not None or hard is not None:
            values["powered"] = not bool(soft) and not bool(hard)
        return values

    def _radio_for_controller(self, name: str) -> Path | None:
        matching = [
            entry
            for entry in self._radios()
            if _read_text(entry / "name", maximum=128) == name
        ]
        return matching[0] if len(matching) == 1 else None

    def inventory(self) -> dict[str, Any]:
        devices: list[dict[str, Any]] = []
        for entry in self._controllers():
            radio = self._radio_for_controller(entry.name)
            address = _read_text(entry / "address", maximum=128)
            metadata: dict[str, Any] = {
                "kind": "controller",
                "pairing_available": False,
                "pairing_reason": "no-supported-bluetooth-control-channel",
                "power_control": "available" if radio and self.writable(radio / "soft") else "unavailable",
            }
            if address and MAC_RE.fullmatch(address):
                metadata["address"] = address.casefold()
            devices.append(_device(
                self.domain,
                entry.name,
                mutable=["powered"] if radio and self.writable(radio / "soft") else [],
                metadata=metadata,
            ))
        for entry in self._radios():
            values = self._radio_values(entry)
            devices.append(_device(
                self.domain,
                entry.name,
                mutable=["powered"] if self.writable(entry / "soft") else [],
                metadata=values,
            ))
        return {
            "domain": self.domain,
            "status": "available" if devices else "unavailable",
            **({} if devices else {"reason": "bluetooth-classes-unavailable"}),
            "devices": devices,
        }

    def _resolve(self, identifier: str) -> tuple[str, Path, Path | None]:
        name = _device_name(identifier, self.domain)
        for entry in self._controllers():
            if entry.name == name:
                return "controller", entry, self._radio_for_controller(name)
        for entry in self._radios():
            if entry.name == name:
                return "radio", entry, entry
        raise UnavailableError("Bluetooth device is unavailable", details={"id": identifier})

    def get_state(self, identifier: str) -> dict[str, Any]:
        kind, entry, radio = self._resolve(identifier)
        if kind == "radio":
            values = self._radio_values(entry)
        else:
            values = {
                "kind": "controller",
                "pairing_available": False,
                "pairing_reason": "no-supported-bluetooth-control-channel",
            }
            address = _read_text(entry / "address", maximum=128)
            if address and MAC_RE.fullmatch(address):
                values["address"] = address.casefold()
            if radio is not None:
                for key, value in self._radio_values(radio).items():
                    if key not in {"kind", "pairing_available", "pairing_reason"}:
                        values[key] = value
            else:
                values["power_control"] = "unavailable"
        mutable = ["powered"] if radio is not None and self.writable(radio / "soft") else []
        return _state(self.domain, entry.name, values, mutable=mutable)

    def set_state(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        _kind, _entry, radio = self._resolve(identifier)
        if set(changes) != {"powered"} or not isinstance(changes.get("powered"), bool):
            raise ValidationError("Bluetooth changes must contain exactly boolean powered")
        if radio is None or not self.writable(radio / "soft"):
            raise ReadOnlyError("Bluetooth radio power is read-only")
        requested = changes["powered"]
        before = self._radio_values(radio)
        if requested and before.get("hard_blocked") is True:
            raise UnavailableError("Bluetooth radio is hard blocked", details={"id": identifier})
        try:
            self.writer(radio / "soft", "0\n" if requested else "1\n")
        except OSError as exc:
            raise ProviderError("Bluetooth soft-block write failed", details={"id": identifier}) from exc
        after = self._radio_values(radio)
        if after.get("powered") is not requested:
            raise ProviderError("Bluetooth soft-block write could not be verified", details={"id": identifier})
        return self.get_state(identifier)
