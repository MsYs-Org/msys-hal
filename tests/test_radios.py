from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msys_hal.errors import ProviderError, ReadOnlyError, UnavailableError, ValidationError
from msys_hal.manager import HalManager
from msys_hal.provider import LINUX_DOMAIN_CAPABILITIES, ProviderService
from msys_hal.radios import BluetoothBackend, NetworkBackend, WpaSupplicantControl


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def network_entry(root: Path, name: str, *, interface_type: int = 1) -> Path:
    entry = root / name
    write(entry / "type", f"{interface_type}\n")
    write(entry / "operstate", "up\n")
    write(entry / "carrier", "1\n")
    write(entry / "address", "02:11:22:33:44:55\n")
    write(entry / "mtu", "1500\n")
    return entry


class FakeWifiControl:
    def __init__(self, *, present: bool = True, save: str = "OK") -> None:
        self.present = present
        self.save = save
        self.commands: list[tuple[str, str]] = []
        self.networks = [
            {"network_id": 2, "ssid": "Known", "bssid": "any", "flags": "[CURRENT]"}
        ]
        self.fail_prefix = ""

    def available(self, interface: str) -> bool:
        return self.present and interface == "wlan0"

    def request(self, interface: str, command: str) -> str:
        self.commands.append((interface, command))
        if self.fail_prefix and command.startswith(self.fail_prefix):
            return "FAIL"
        if command == "STATUS":
            return "wpa_state=COMPLETED\nssid=Known\nbssid=00:11:22:33:44:55\npsk=must-not-leak\n"
        if command == "SCAN_RESULTS":
            rows = ["bssid / frequency / signal level / flags / ssid"]
            rows.extend(
                f"02:00:00:00:00:{index:02x}\t2412\t{-30-index}\t[WPA2-PSK-CCMP][ESS]\tAP {index}"
                for index in range(30)
            )
            return "\n".join(rows)
        if command == "LIST_NETWORKS":
            rows = ["network id / ssid / bssid / flags"]
            rows.extend(
                f"{item['network_id']}\t{item['ssid']}\t{item['bssid']}\t{item['flags']}"
                for item in self.networks
            )
            return "\n".join(rows)
        if command == "ADD_NETWORK":
            return "7"
        if command.startswith("SET_NETWORK 7 ssid "):
            ssid = bytes.fromhex(command.rsplit(" ", 1)[1]).decode("utf-8")
            self.networks.append({"network_id": 7, "ssid": ssid, "bssid": "any", "flags": ""})
            return "OK"
        if command.startswith("REMOVE_NETWORK "):
            requested = int(command.rsplit(" ", 1)[1])
            self.networks = [item for item in self.networks if item["network_id"] != requested]
            return "OK"
        if command == "SAVE_CONFIG":
            return self.save
        return "OK"


class FakeSocket:
    def __init__(self, response: bytes = b"OK\n", *, fail: Exception | None = None) -> None:
        self.response = response
        self.fail = fail
        self.timeout = None
        self.bound = None
        self.connected = None
        self.sent = b""
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def bind(self, value: str) -> None:
        self.bound = value

    def connect(self, value: str) -> None:
        if self.fail:
            raise self.fail
        self.connected = value

    def send(self, value: bytes) -> int:
        self.sent = value
        return len(value)

    def recv(self, _maximum: int) -> bytes:
        return self.response

    def close(self) -> None:
        self.closed = True


class InjectableWpa(WpaSupplicantControl):
    def available(self, interface: str) -> bool:
        self._path(interface)
        return True


class RadioBackendTests(unittest.TestCase):
    def test_network_inventory_classifies_interfaces_without_external_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "net"
            wifi = network_entry(root, "wlan0")
            (wifi / "wireless").mkdir()
            network_entry(root, "eth0")
            network_entry(root, "wwan0")
            network_entry(root, "lo", interface_type=772)
            control = FakeWifiControl()
            backend = NetworkBackend(root, control)

            inventory = backend.inventory()

            kinds = {item["name"]: item["metadata"]["kind"] for item in inventory["devices"]}
            self.assertEqual(
                kinds,
                {"eth0": "ethernet", "lo": "loopback", "wlan0": "wifi", "wwan0": "wwan"},
            )
            wlan = next(item for item in inventory["devices"] if item["name"] == "wlan0")
            self.assertEqual(wlan["mutable"], ["action"])
            self.assertEqual(wlan["metadata"]["wifi_control"], "available")

    def test_wifi_state_is_bounded_and_never_exposes_psk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "net"
            wifi = network_entry(root, "wlan0")
            (wifi / "wireless").mkdir()
            backend = NetworkBackend(root, FakeWifiControl())

            state = backend.get_state("network:wlan0")

            self.assertEqual(state["values"]["wifi_status"]["wpa_state"], "COMPLETED")
            self.assertNotIn("psk", state["values"]["wifi_status"])
            self.assertEqual(len(state["values"]["scan_results"]), 20)
            self.assertNotIn("must-not-leak", json.dumps(state))
            provider = ProviderService(
                {"network": backend},
                provider_id="org.example.hal:network",
            )
            normalized = HalManager._normalize_state(
                provider.handle("get_state", {"id": "network:wlan0"}),
                "network:wlan0",
                "org.example.hal:network",
            )
            self.assertEqual(len(normalized["values"]["scan_results"]), 20)

    def test_wifi_scan_switch_disconnect_forget_and_new_connect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "net"
            wifi = network_entry(root, "wlan0")
            (wifi / "wireless").mkdir()
            control = FakeWifiControl(save="FAIL")
            backend = NetworkBackend(root, control)

            backend.set_state("network:wlan0", {"action": "scan"})
            backend.set_state("network:wlan0", {"action": "disconnect"})
            backend.set_state("network:wlan0", {"action": "connect", "ssid": "Known"})
            created = backend.set_state(
                "network:wlan0",
                {"action": "connect", "ssid": "New Wi-Fi", "psk": 'safe"pass'},
            )
            self.assertFalse(created["values"]["configuration_persisted"])
            self.assertNotIn('safe"pass', json.dumps(created))
            backend.set_state("network:wlan0", {"action": "forget", "network_id": 7})
            opened = backend.set_state(
                "network:wlan0",
                {"action": "connect", "ssid": "Open Wi-Fi", "security": "open"},
            )
            self.assertFalse(opened["values"]["configuration_persisted"])

            commands = [command for _interface, command in control.commands]
            self.assertIn("SCAN", commands)
            self.assertIn("DISCONNECT", commands)
            self.assertIn("SELECT_NETWORK 2", commands)
            self.assertTrue(any(command.startswith("SET_NETWORK 7 ssid ") for command in commands))
            self.assertTrue(any(command.startswith("SET_NETWORK 7 psk ") for command in commands))
            self.assertIn("REMOVE_NETWORK 7", commands)
            self.assertIn("SET_NETWORK 7 key_mgmt NONE", commands)

    def test_wifi_payloads_are_strict_and_errors_do_not_echo_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "net"
            wifi = network_entry(root, "wlan0")
            (wifi / "wireless").mkdir()
            control = FakeWifiControl()
            backend = NetworkBackend(root, control)
            for changes in (
                {"action": "scan", "ssid": "extra"},
                {"action": "connect", "ssid": "bad\nssid", "psk": "12345678"},
                {"action": "connect", "ssid": "New", "psk": "short"},
                {"action": "forget", "ssid": "Known", "network_id": 2},
            ):
                with self.assertRaises(ValidationError):
                    backend.set_state("network:wlan0", changes)

            secret = "secret-pass"
            control.fail_prefix = "SET_NETWORK 7 psk"
            with self.assertRaises(ProviderError) as caught:
                backend.set_state(
                    "network:wlan0",
                    {"action": "connect", "ssid": "Other", "psk": secret},
                )
            self.assertNotIn(secret, str(caught.exception))
            self.assertIn("REMOVE_NETWORK 7", [command for _iface, command in control.commands])

    def test_wpa_socket_timeout_address_and_response_are_injected_and_bounded(self) -> None:
        fake = FakeSocket(b"PONG\n")
        client = InjectableWpa(
            Path("/trusted/wpa"),
            timeout=0.4,
            socket_factory=lambda *_args: fake,
            token_factory=lambda: "fixed-token",
        )

        self.assertEqual(client.request("wlan0", "PING"), "PONG")
        self.assertEqual(fake.timeout, 0.4)
        self.assertEqual(fake.bound, "\x00msys-hal-wpa-fixed-token")
        self.assertEqual(fake.connected, "/trusted/wpa/wlan0")
        self.assertEqual(fake.sent, b"PING")
        self.assertTrue(fake.closed)

        unavailable_socket = FakeSocket(fail=TimeoutError())
        unavailable = InjectableWpa(
            Path("/trusted/wpa"),
            socket_factory=lambda *_args: unavailable_socket,
            token_factory=lambda: "another",
        )
        with self.assertRaises(UnavailableError):
            unavailable.request("wlan0", "STATUS")

        oversized_socket = FakeSocket(b"x" * (64 * 1024 + 1))
        oversized = InjectableWpa(
            Path("/trusted/wpa"),
            socket_factory=lambda *_args: oversized_socket,
            token_factory=lambda: "oversized",
        )
        with self.assertRaises(ProviderError):
            oversized.request("wlan0", "STATUS")

    def test_bluetooth_controller_and_rfkill_power_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            controllers = base / "bluetooth"
            rfkill = base / "rfkill"
            write(controllers / "hci0" / "address", "AA:BB:CC:DD:EE:FF\n")
            radio = rfkill / "rfkill0"
            write(radio / "type", "bluetooth\n")
            write(radio / "name", "hci0\n")
            write(radio / "soft", "0\n")
            write(radio / "hard", "0\n")
            write(radio / "state", "1\n")

            def writer(path: Path, value: str) -> None:
                path.write_text(value, encoding="ascii")

            backend = BluetoothBackend(
                controllers,
                rfkill,
                writer=writer,
                writable=lambda path: path.name == "soft",
            )
            inventory = backend.inventory()
            self.assertEqual({item["id"] for item in inventory["devices"]}, {
                "bluetooth:hci0",
                "bluetooth:rfkill0",
            })
            controller = backend.get_state("bluetooth:hci0")
            self.assertFalse(controller["values"]["pairing_available"])
            self.assertEqual(controller["mutable"], ["powered"])
            self.assertTrue(controller["values"]["powered"])

            off = backend.set_state("bluetooth:hci0", {"powered": False})
            self.assertFalse(off["values"]["powered"])
            on = backend.set_state("bluetooth:rfkill0", {"powered": True})
            self.assertTrue(on["values"]["powered"])

            write(radio / "hard", "1\n")
            with self.assertRaises(UnavailableError):
                backend.set_state("bluetooth:hci0", {"powered": True})

    def test_bluetooth_missing_control_is_read_only_not_fake_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = Path(temporary) / "bluetooth" / "hci0"
            controller.mkdir(parents=True)
            backend = BluetoothBackend(
                controller.parents[0],
                Path(temporary) / "missing-rfkill",
                writable=lambda _path: False,
            )

            state = backend.get_state("bluetooth:hci0")
            self.assertFalse(state["values"]["pairing_available"])
            self.assertEqual(state["values"]["power_control"], "unavailable")
            with self.assertRaises(ReadOnlyError):
                backend.set_state("bluetooth:hci0", {"powered": True})

    def test_provider_contract_advertises_radio_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "net"
            network_entry(root, "eth0")
            backend = NetworkBackend(root, FakeWifiControl(present=False))
            service = ProviderService(
                {"network": backend},
                provider_id="org.example.hal:network",
                capabilities=LINUX_DOMAIN_CAPABILITIES["network"],
            )
            described = service.handle("describe", {})
            self.assertIn("network.wifi.scan", described["capabilities"])
            self.assertEqual(service.handle("inventory", {"domains": ["network"]})["domains"][0]["status"], "available")


if __name__ == "__main__":
    unittest.main()
