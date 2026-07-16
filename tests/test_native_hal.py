from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="ascii")


class FakeWpaControl:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "wlan0"
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.path))
        self.socket.settimeout(0.1)
        self.commands: list[str] = []
        self.networks = [(2, "Known", "[CURRENT]")]
        self._closed = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                raw, peer = self.socket.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            command = raw.decode("utf-8")
            self.commands.append(command)
            if command == "PING":
                response = "PONG\n"
            elif command == "STATUS":
                response = "wpa_state=COMPLETED\nssid=Known\nbssid=00:11:22:33:44:55\n"
            elif command == "SCAN_RESULTS":
                response = (
                    "bssid / frequency / signal level / flags / ssid\n"
                    "00:11:22:33:44:55\t2412\t-35\t[WPA2-PSK-CCMP][ESS]\tKnown\n"
                    "02:11:22:33:44:66\t2462\t-48\t[ESS]\tOpen Network\n"
                )
            elif command == "LIST_NETWORKS":
                response = "network id / ssid / bssid / flags\n" + "".join(
                    f"{network_id}\t{ssid}\tany\t{flags}\n"
                    for network_id, ssid, flags in self.networks
                )
            elif command == "ADD_NETWORK":
                response = "7\n"
            elif command.startswith("SET_NETWORK 7 ssid "):
                ssid = bytes.fromhex(command.rsplit(" ", 1)[1]).decode("utf-8")
                self.networks.append((7, ssid, ""))
                response = "OK\n"
            elif command.startswith("REMOVE_NETWORK "):
                requested = int(command.rsplit(" ", 1)[1])
                self.networks = [row for row in self.networks if row[0] != requested]
                response = "OK\n"
            else:
                response = "OK\n"
            try:
                self.socket.sendto(response.encode("utf-8"), peer)
            except OSError:
                return

    def close(self) -> None:
        self._closed.set()
        self.thread.join(timeout=1)
        self.socket.close()


class NativeHalProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured = os.environ.get("MSYS_HAL_NATIVE_BINARY")
        if configured:
            cls.binary = Path(configured)
            if not cls.binary.is_file():
                raise RuntimeError(f"MSYS_HAL_NATIVE_BINARY does not exist: {cls.binary}")
            cls.build = None
            return
        compiler = shutil.which("cc")
        sdk = Path(os.environ.get("MSYS_SDK_DIR", WORKSPACE / "msys-sdk"))
        if compiler is None or not (sdk / "src" / "mipc.c").is_file():
            raise unittest.SkipTest("native C compiler or adjacent msys-sdk source is unavailable")
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / "msys-hal-native"
        completed = subprocess.run(
            [
                compiler,
                "-I",
                str(sdk / "include"),
                "-O2",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                str(ROOT / "native" / "src" / "native_hal.c"),
                str(sdk / "src" / "mipc.c"),
                "-o",
                str(cls.binary),
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.build is not None:
            cls.build.cleanup()

    def setUp(self) -> None:
        self.hardware = tempfile.TemporaryDirectory()
        root = Path(self.hardware.name)
        self.roots = {
            "MSYS_HAL_POWER_ROOT": root / "power",
            "MSYS_HAL_THERMAL_ROOT": root / "thermal",
            "MSYS_HAL_BACKLIGHT_ROOT": root / "backlight",
            "MSYS_HAL_NATIVE_INPUT_ROOT": root / "input",
            "MSYS_HAL_NETWORK_ROOT": root / "net",
            "MSYS_HAL_BLUETOOTH_ROOT": root / "bluetooth",
            "MSYS_HAL_RFKILL_ROOT": root / "rfkill",
            "MSYS_HAL_WPA_ROOT": root / "wpa",
        }
        for path in self.roots.values():
            path.mkdir(parents=True)

        power = self.roots["MSYS_HAL_POWER_ROOT"] / "BAT0"
        _write(power / "type", "Battery\n")
        _write(power / "status", "Discharging\n")
        _write(power / "capacity", "73\n")
        _write(power / "online", "1\n")

        thermal = self.roots["MSYS_HAL_THERMAL_ROOT"] / "thermal_zone0"
        _write(thermal / "type", "cpu-thermal\n")
        _write(thermal / "temp", "41250\n")

        backlight = self.roots["MSYS_HAL_BACKLIGHT_ROOT"] / "panel0"
        _write(backlight / "max_brightness", "10\n")
        _write(backlight / "brightness", "2\n")

        (self.roots["MSYS_HAL_NATIVE_INPUT_ROOT"] / "event0").mkdir()

        wlan = self.roots["MSYS_HAL_NETWORK_ROOT"] / "wlan0"
        (wlan / "wireless").mkdir(parents=True)
        _write(wlan / "operstate", "up\n")
        _write(wlan / "address", "02:00:00:00:00:01\n")
        _write(wlan / "carrier", "1\n")
        _write(wlan / "mtu", "1500\n")

        bluetooth = self.roots["MSYS_HAL_BLUETOOTH_ROOT"] / "hci0"
        _write(bluetooth / "address", "02:00:00:00:00:02\n")

        rfkill_bt = self.roots["MSYS_HAL_RFKILL_ROOT"] / "rfkill0"
        _write(rfkill_bt / "type", "bluetooth\n")
        _write(rfkill_bt / "name", "Bluetooth\n")
        _write(rfkill_bt / "hard", "0\n")
        _write(rfkill_bt / "soft", "1\n")

        rfkill_wifi = self.roots["MSYS_HAL_RFKILL_ROOT"] / "rfkill1"
        _write(rfkill_wifi / "type", "wlan\n")
        _write(rfkill_wifi / "name", "Wi-Fi\n")
        _write(rfkill_wifi / "hard", "0\n")
        _write(rfkill_wifi / "soft", "0\n")

        self.wpa = FakeWpaControl(self.roots["MSYS_HAL_WPA_ROOT"])
        self.addCleanup(self.wpa.close)

        storage_block = root / "block"
        storage_dev = root / "dev"
        storage_mount = root / "media" / "msys"
        storage_mountinfo = root / "mountinfo"
        storage_labels = storage_dev / "disk" / "by-label"
        storage_uuids = storage_dev / "disk" / "by-uuid"
        for path in (storage_block, storage_dev, storage_labels, storage_uuids):
            path.mkdir(parents=True, exist_ok=True)
        storage_mount.parent.mkdir(parents=True, exist_ok=True)
        disk = storage_block / "sda"
        partition = storage_block / "sda1"
        disk.mkdir()
        partition.mkdir()
        _write(disk / "dev", "8:0\n")
        _write(disk / "removable", "1\n")
        _write(partition / "dev", "8:1\n")
        _write(partition / "partition", "1\n")
        _write(partition / "size", "4096\n")
        _write(partition / "queue" / "logical_block_size", "512\n")
        _write(partition / "ro", "0\n")
        (storage_dev / "sda").write_bytes(b"")
        (storage_dev / "sda1").write_bytes(b"")
        storage_mountinfo.write_text("", encoding="ascii")
        fake_mount = root / "fake-mount"
        fake_umount = root / "fake-umount"
        fake_mount.write_text(
            "#!/bin/sh\n"
            "printf '36 25 8:1 / %s rw,nosuid,nodev,noexec - vfat %s rw\\n' \"$4\" \"$3\" > \"$MSYS_HAL_MOUNTINFO\"\n",
            encoding="ascii",
        )
        fake_umount.write_text(
            "#!/bin/sh\n: > \"$MSYS_HAL_MOUNTINFO\"\n",
            encoding="ascii",
        )
        fake_mount.chmod(0o755)
        fake_umount.chmod(0o755)

        supervisor, component = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.supervisor = supervisor
        environment = dict(os.environ)
        environment.update({name: str(path) for name, path in self.roots.items()})
        environment.update({
            "MSYS_CONTROL_FD": str(component.fileno()),
            "MSYS_COMPONENT_ID": "org.msys.hal.linux:native-manager",
            "MSYS_GENERATION": "7",
            "MSYS_HAL_BLOCK_ROOT": str(storage_block),
            "MSYS_HAL_DEV_ROOT": str(storage_dev),
            "MSYS_HAL_MOUNTINFO": str(storage_mountinfo),
            "MSYS_HAL_STORAGE_MOUNT_ROOT": str(storage_mount),
            "MSYS_HAL_BY_LABEL_ROOT": str(storage_labels),
            "MSYS_HAL_BY_UUID_ROOT": str(storage_uuids),
            "MSYS_HAL_MOUNT_BINARY": str(fake_mount),
            "MSYS_HAL_UMOUNT_BINARY": str(fake_umount),
            "MSYS_HAL_STORAGE_CONFIG": str(root / "storage.json"),
            "MSYS_HAL_STORAGE_AUTOMOUNT": "0",
            "MSYS_HAL_STORAGE_TEST": "1",
        })
        self.process = subprocess.Popen(
            [str(self.binary)],
            env=environment,
            pass_fds=(component.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        component.close()
        self.addCleanup(self._stop_component)
        hello = self._receive()
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["component"], "org.msys.hal.linux:native-manager")
        self._send({"type": "welcome", "component": hello["component"], "generation": 7})
        self.assertEqual(self._receive(), {"type": "ready"})
        self.request_id = 0

    def tearDown(self) -> None:
        self.hardware.cleanup()

    def _stop_component(self) -> None:
        if self.process.poll() is None:
            try:
                self._send({"type": "shutdown"})
            except OSError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.supervisor.close()
        stdout, stderr = self.process.communicate(timeout=1)
        self.assertEqual(self.process.returncode, 0, stdout + stderr)

    def _send(self, value: dict) -> None:
        self.supervisor.sendall(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    def _receive(self) -> dict:
        self.supervisor.settimeout(3)
        raw = self.supervisor.recv(256 * 1024).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"native HAL emitted invalid JSON: {raw}") from exc

    def _call(self, method: str, payload: dict) -> dict:
        self.request_id += 1
        self._send({
            "type": "call",
            "id": self.request_id,
            "method": method,
            "payload": payload,
            "deadline_ms": 2**63,
            "idempotent": method != "set_state",
        })
        response = self._receive()
        while response.get("type") == "event":
            response = self._receive()
        self.assertEqual(response["id"], self.request_id)
        return response

    def test_describe_inventory_and_get_state_are_typed_and_deterministic(self) -> None:
        described = self._call("describe", {})
        self.assertEqual(described["type"], "return")
        self.assertEqual(described["payload"]["schema"], "org.msys.hal.native-manager.v1")
        self.assertEqual(described["payload"]["provider"]["version"], "0.2.17")

        first = self._call("inventory", {})["payload"]
        second = self._call("inventory", {})["payload"]
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], "org.msys.hal.manager.v1")
        self.assertTrue(
            all(row["selection"] == "automatic" for row in first["domains"])
        )
        ids = [item["id"] for item in first["devices"]]
        self.assertEqual(ids, sorted(ids))
        self.assertIn("power:BAT0", ids)
        self.assertIn("backlight:panel0", ids)
        self.assertIn("network:wlan0", ids)
        self.assertIn("bluetooth:rfkill0", ids)
        self.assertNotIn(str(Path(self.hardware.name)), json.dumps(first))

        power = self._call("get_state", {"id": "power:BAT0"})["payload"]
        self.assertEqual(power["state"]["values"]["capacity_percent"], 73)
        thermal = self._call("get_state", {"id": "thermal:thermal_zone0"})["payload"]
        self.assertEqual(thermal["state"]["values"]["temperature_millicelsius"], 41250)

        wifi = self._call("get_state", {"id": "network:wlan0"})["payload"]["state"]
        self.assertEqual(wifi["values"]["wifi_control"], "available")
        self.assertEqual(wifi["values"]["wifi_status"]["ssid"], "Known")
        self.assertEqual(len(wifi["values"]["scan_results"]), 2)
        self.assertEqual(wifi["mutable"], ["powered", "action"])

        # rfkill soft=0 means only unblocked. With no registered Management
        # controller it must not be promoted to powered=true.
        _write(self.roots["MSYS_HAL_RFKILL_ROOT"] / "rfkill0" / "soft", "0\n")
        bluetooth = self._call("get_state", {"id": "bluetooth:hci0"})["payload"]["state"]
        self.assertFalse(bluetooth["values"]["pairing_available"])
        self.assertEqual(bluetooth["values"]["pairing_reason"], "pairing-not-supported")
        self.assertIn(
            bluetooth["values"]["management_reason"],
            {
                "controller-not-registered",
                "linux-management-control-unavailable",
            },
        )
        self.assertEqual(bluetooth["values"]["discovery_control"], "unavailable")
        self.assertTrue(bluetooth["values"]["rfkill_unblocked"])
        self.assertIsNot(bluetooth["values"].get("powered"), True)
        if bluetooth["values"]["management_reason"] == "controller-not-registered":
            self.assertFalse(bluetooth["values"]["powered"])
            self.assertEqual(bluetooth["values"]["power_state"], "off")

        providers = self._call("list_providers", {"domain": "backlight"})["payload"]
        self.assertEqual(
            providers["providers"][0]["active"],
            "org.msys.hal.linux:native-manager",
        )
        self.assertEqual(providers["providers"][0]["selection"], "automatic")
        self.assertIsNone(providers["providers"][0]["preferred"])

    def test_backlight_and_rfkill_writes_are_verified_and_credentials_never_echo(self) -> None:
        backlight = self._call(
            "set_state",
            {"id": "backlight:panel0", "changes": {"brightness_percent": 50}},
        )
        self.assertEqual(backlight["type"], "return")
        self.assertEqual(
            (self.roots["MSYS_HAL_BACKLIGHT_ROOT"] / "panel0" / "brightness")
            .read_text(encoding="ascii")
            .strip(),
            "5",
        )

        bluetooth = self._call(
            "set_state",
            {"id": "bluetooth:rfkill0", "changes": {"powered": True}},
        )
        self.assertEqual(
            bluetooth["payload"]["state"]["values"]["rfkill_unblocked"],
            True,
        )
        self.assertIsNone(bluetooth["payload"]["state"]["values"]["powered"])
        self.assertEqual(
            (self.roots["MSYS_HAL_RFKILL_ROOT"] / "rfkill0" / "soft")
            .read_text(encoding="ascii")
            .strip(),
            "0",
        )

        secret = "never-return-this-psk"
        rejected = self._call(
            "set_state",
            {
                "id": "backlight:panel0",
                "changes": {"brightness": 4, "psk": secret},
            },
        )
        self.assertEqual(rejected["code"], "HAL_BAD_PAYLOAD")
        self.assertNotIn(secret, json.dumps(rejected))
        self.assertNotIn("psk", json.dumps(rejected).lower())

        _write(self.roots["MSYS_HAL_RFKILL_ROOT"] / "rfkill0" / "hard", "1\n")
        blocked = self._call(
            "set_state",
            {"id": "bluetooth:rfkill0", "changes": {"powered": False}},
        )
        self.assertEqual(blocked["code"], "HAL_READ_ONLY")

    def test_native_wifi_scan_connect_open_forget_and_power_are_real_actions(self) -> None:
        scan = self._call(
            "set_state",
            {"id": "network:wlan0", "changes": {"action": "scan"}},
        )
        self.assertEqual(scan["type"], "return")
        connected = self._call(
            "set_state",
            {
                "id": "network:wlan0",
                "changes": {"action": "connect", "ssid": "Open Network", "security": "open"},
            },
        )
        self.assertEqual(connected["type"], "return")
        self.assertTrue(connected["payload"]["state"]["values"]["configuration_persisted"])
        forgotten = self._call(
            "set_state",
            {"id": "network:wlan0", "changes": {"action": "forget", "network_id": 7}},
        )
        self.assertEqual(forgotten["type"], "return")
        powered_off = self._call(
            "set_state",
            {"id": "network:wlan0", "changes": {"powered": False}},
        )
        self.assertFalse(powered_off["payload"]["state"]["values"]["powered"])
        self.assertIn("SCAN", self.wpa.commands)
        self.assertIn("SET_NETWORK 7 key_mgmt NONE", self.wpa.commands)
        self.assertIn("REMOVE_NETWORK 7", self.wpa.commands)

    def test_unknown_fields_selection_and_unsupported_methods_are_explicit(self) -> None:
        unknown = self._call("inventory", {"raw_path": "/sys/private"})
        self.assertEqual(unknown["code"], "HAL_BAD_PAYLOAD")
        selected = self._call(
            "select_provider",
            {
                "domain": "power",
                "component": "org.msys.hal.linux:native-manager",
            },
        )
        self.assertEqual(
            selected["payload"]["providers"][0]["active"],
            "org.msys.hal.linux:native-manager",
        )
        unsupported = self._call("pair_bluetooth", {})
        self.assertEqual(unsupported["code"], "HAL_UNSUPPORTED")

    def test_native_storage_role_lists_mounts_unmounts_and_persists_policy(self) -> None:
        listed = self._call("list_volumes", {})
        self.assertEqual(listed["type"], "return")
        state = listed["payload"]
        self.assertEqual(state["schema"], "org.msys.hal.storage.v1")
        self.assertFalse(state["auto_mount"])
        self.assertEqual([item["id"] for item in state["volumes"]], ["storage:sda1"])
        self.assertEqual(state["volumes"][0]["size_bytes"], 4096 * 512)

        mounted = self._call("mount", {"volume_id": "storage:sda1"})
        self.assertEqual(mounted["type"], "return")
        self.assertTrue(mounted["payload"]["volume"]["mounted"])
        self.assertTrue(mounted["payload"]["volume"]["managed"])

        unmounted = self._call("unmount", {"volume_id": "storage:sda1"})
        self.assertEqual(unmounted["type"], "return")
        self.assertFalse(unmounted["payload"]["volume"]["mounted"])

        configured = self._call("set_config", {"auto_mount": False})
        self.assertEqual(configured["type"], "return")
        self.assertFalse(configured["payload"]["auto_mount"])
        wrong_field = self._call("mount", {"id": "storage:sda1"})
        self.assertEqual(wrong_field["code"], "HAL_BAD_PAYLOAD")

    def test_self_check_reports_version_and_rss(self) -> None:
        environment = dict(os.environ)
        environment.update({name: str(path) for name, path in self.roots.items()})
        completed = subprocess.run(
            [str(self.binary), "--self-check"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["version"], "0.2.17")
        self.assertTrue(report["ok"])
        self.assertTrue(report["wifi_control"])
        self.assertGreaterEqual(report["devices"], 8)
        self.assertGreater(report["rss_kib"], 0)


class NativeMgmtProtocolUnitTests(unittest.TestCase):
    def test_management_frames_and_wcnss_power_recovery(self) -> None:
        compiler = shutil.which("cc")
        sdk = Path(os.environ.get("MSYS_SDK_DIR", WORKSPACE / "msys-sdk"))
        if compiler is None or not (sdk / "src" / "mipc.c").is_file():
            self.skipTest("native C compiler or adjacent msys-sdk source is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "native-mgmt-protocol-test"
            completed = subprocess.run(
                [
                    compiler,
                    "-I",
                    str(sdk / "include"),
                    "-O2",
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Wpedantic",
                    "-Werror",
                    str(ROOT / "tests" / "native_mgmt_protocol_test.c"),
                    str(sdk / "src" / "mipc.c"),
                    "-o",
                    str(binary),
                ],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            executed = subprocess.run(
                [str(binary)],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)


if __name__ == "__main__":
    unittest.main()
