from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from msys_hal import __version__
from msys_hal.errors import ReadOnlyError, ValidationError
from msys_hal.mipc import ComponentServer, PacketTransport, decode_packet, encode_packet


class ContractAndMipcTests(unittest.TestCase):
    def test_manifest_uses_only_standard_language_neutral_components(self) -> None:
        root = Path(__file__).resolve().parents[1]
        canonical = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        development = json.loads(
            (root / "manifests" / "msys-hal.json").read_text(encoding="utf-8")
        )
        self.assertEqual(canonical, development)
        manifest = canonical
        self.assertEqual(manifest["schema"], "msys.manifest.v1")
        self.assertEqual(__version__, "0.2.16")
        self.assertEqual(manifest["package"]["version"], __version__)
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        project_version = re.search(
            r'(?m)^version\s*=\s*"([^"]+)"\s*$',
            pyproject,
        )
        self.assertIsNotNone(project_version)
        self.assertEqual(project_version.group(1), __version__)
        ids = [item["id"] for item in manifest["components"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item["isolation"] == "baseline" for item in manifest["components"]))
        manager = next(item for item in manifest["components"] if item["id"] == "manager")
        native = next(item for item in manifest["components"] if item["id"] == "native-manager")
        self.assertEqual(native["runtime"], "native")
        self.assertEqual(native["lifecycle"], "background")
        self.assertEqual(native["exec"], ["@package/files/bin/msys-hal-native"])
        native_role = next(
            provided
            for provided in native["provides"]
            if provided.get("role") == "hal-manager"
        )
        self.assertEqual(native_role["priority"], 200)
        self.assertEqual(
            native_role["x-msys-contract"],
            {"id": "org.msys.role.hal-manager.v1", "version": "1.0.0"},
        )
        self.assertEqual(manager["lifecycle"], "on-demand")
        self.assertEqual(manager["idle_timeout_ms"], 30000)
        hal_role = next(
            provided
            for provided in manager["provides"]
            if provided.get("role") == "hal-manager"
        )
        self.assertEqual(
            hal_role["x-msys-contract"],
            {"id": "org.msys.role.hal-manager.v1", "version": "1.0.0"},
        )
        self.assertTrue(any(
            item.get("interface") == "org.msys.hal.manager.v1"
            for item in manager["provides"]
        ))
        self.assertIn("mipc.event:publish:msys.hal.ready", manager["permissions"])
        self.assertIn("mipc.event:publish:msys.hal.changed", manager["permissions"])
        providers = [
            item
            for item in manifest["components"]
            if item["id"] not in {"manager", "native-manager"}
        ]
        self.assertTrue(all(item["lifecycle"] == "on-demand" for item in providers))
        self.assertTrue(all(item["idle_timeout_ms"] == 30000 for item in providers))
        self.assertTrue(all(any(
            provided.get("interface") == "org.msys.hal.provider.v1"
            for provided in item["provides"]
        ) for item in providers))
        self.assertTrue(all(
            "mipc.event:publish:msys.hal.provider.ready" in item["permissions"]
            for item in providers
        ))
        by_id = {item["id"]: item for item in providers}
        self.assertIn(
            "runtime:read:display-session",
            by_id["window-layout-display"]["permissions"],
        )
        self.assertIn(
            "x11:write:input-transform",
            by_id["linux-input-inventory"]["permissions"],
        )
        self.assertIn("sysfs:read:net", by_id["linux-network"]["permissions"])
        self.assertIn(
            "runtime:connect:wpa-supplicant-control",
            by_id["linux-network"]["permissions"],
        )
        self.assertIn(
            "sysfs:write:rfkill.soft",
            by_id["linux-bluetooth"]["permissions"],
        )
        ch347 = by_id["ch347-output-control"]
        self.assertTrue(any(
            provided.get("interface") == "org.msys.hal.ch347-control.v1"
            for provided in ch347["provides"]
        ))
        self.assertEqual(
            {
                "mipc.call:msys.core.list_components",
                "mipc.call:msys.core.stop",
                "mipc.call:msys.core.start",
            },
            {
                permission
                for permission in ch347["permissions"]
                if permission.startswith("mipc.call:")
            },
        )
        # The manager calls discovered candidates by exact component target.
        # Keep compatibility with Core releases predating the catalog-backed
        # interface-to-provider ACL bridge without granting mipc.call:*.
        self.assertNotIn("mipc.call:*", manager["permissions"])
        for provider in providers:
            self.assertIn(
                f"mipc.call:component:org.msys.hal.linux:{provider['id']}",
                manager["permissions"],
            )

    def test_canonical_manifest_exec_resolves_in_source_and_clean_install_root(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        clean_env = dict(os.environ)
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "PYTHONUSERBASE",
            "VIRTUAL_ENV",
        ):
            clean_env.pop(name, None)
        clean_env["PYTHONNOUSERSITE"] = "1"

        def check_exec(package_root: Path, component: dict) -> None:
            argv = list(component["exec"])
            if component["runtime"] == "native":
                self.assertEqual(argv, ["@package/files/bin/msys-hal-native"])
                binary = package_root / "files" / "bin" / "msys-hal-native"
                self.assertTrue(binary.is_file())
                image = binary.read_bytes()
                self.assertEqual(image[:4], b"\x7fELF")
                self.assertIn(
                    __version__.encode("ascii"),
                    image,
                    "packaged native HAL was not rebuilt for the manifest version",
                )
                return
            self.assertEqual(argv[0], "python")
            completed = subprocess.run(
                [sys.executable, *argv[1:], "--help"],
                cwd=package_root,
                env=clean_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        for component in manifest["components"]:
            check_exec(root, component)
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "org.msys.hal.linux" / __version__
            installed.mkdir(parents=True)
            shutil.copytree(root / "msys_hal", installed / "msys_hal")
            shutil.copytree(root / "files", installed / "files")
            shutil.copy2(root / "manifest.json", installed / "manifest.json")
            installed_manifest = json.loads(
                (installed / "manifest.json").read_text(encoding="utf-8")
            )
            for component in installed_manifest["components"]:
                check_exec(installed, component)

    def test_packet_round_trip_and_nonfinite_rejection(self) -> None:
        message = {"type": "call", "id": 1, "payload": {"name": "电池"}}
        self.assertEqual(decode_packet(encode_packet(message)), message)
        with self.assertRaises(ValidationError):
            encode_packet({"type": "return", "payload": {"value": float("nan")}})
        with self.assertRaises(ValidationError):
            decode_packet(b"[]")

    def test_packet_depth_and_size_are_bounded(self) -> None:
        nested = {}
        current = nested
        for _ in range(12):
            current["next"] = {}
            current = current["next"]
        with self.assertRaises(ValidationError):
            encode_packet({"type": "return", "payload": nested})
        with self.assertRaises(ValidationError):
            encode_packet({"type": "return", "payload": {"value": "x" * (129 * 1024)}})

    def test_component_server_handshake_rpc_deadline_and_shutdown(self) -> None:
        server_socket, broker_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(server_socket.close)
        self.addCleanup(broker_socket.close)
        server_transport = PacketTransport(server_socket)
        broker_transport = PacketTransport(broker_socket)
        server = ComponentServer(
            lambda method, payload: {"method": method, "echo": payload},
            transport=server_transport,
            workers=2,
        )
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(server.run()))
        thread.start()

        self.assertEqual(broker_transport.recv()["type"], "hello")
        broker_transport.send({"type": "welcome", "component": "test", "generation": 1})
        self.assertEqual(broker_transport.recv(), {"type": "ready"})
        broker_transport.send({
            "type": "call",
            "id": 7,
            "method": "ping",
            "payload": {"value": 3},
            "deadline_ms": int(time.monotonic() * 1000 + 1000),
        })
        response = broker_transport.recv()
        self.assertEqual(response["type"], "return")
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["payload"], {"method": "ping", "echo": {"value": 3}})

        broker_transport.send({
            "type": "call",
            "id": 8,
            "method": "late",
            "payload": {},
            "deadline_ms": 1,
        })
        expired = broker_transport.recv()
        self.assertEqual(expired["type"], "error")
        self.assertEqual(expired["code"], "CALL_TIMEOUT")
        broker_transport.send({"type": "shutdown"})
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])

    def test_component_server_preserves_typed_errors_and_redacts_internal_errors(self) -> None:
        server_socket, broker_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(server_socket.close)
        self.addCleanup(broker_socket.close)

        def handler(method, _payload):
            if method == "write":
                raise ReadOnlyError("device is read only", details={"id": "power:BAT0"})
            raise RuntimeError("private /sys/path must not cross mIPC")

        server = ComponentServer(handler, transport=PacketTransport(server_socket), workers=1)
        broker = PacketTransport(broker_socket)
        thread = threading.Thread(target=server.run)
        thread.start()
        self.assertEqual(broker.recv()["type"], "hello")
        broker.send({"type": "welcome"})
        self.assertEqual(broker.recv()["type"], "ready")

        broker.send({"type": "call", "id": 1, "method": "write", "payload": {}})
        typed = broker.recv()
        self.assertEqual(typed["code"], "HAL_READ_ONLY")
        self.assertEqual(typed["payload"], {"id": "power:BAT0"})
        broker.send({"type": "call", "id": 2, "method": "crash", "payload": {}})
        internal = broker.recv()
        self.assertEqual(internal["code"], "HAL_INTERNAL_ERROR")
        self.assertEqual(internal["message"], "HAL component internal error")
        self.assertNotIn("/sys/path", json.dumps(internal))
        broker.send({"type": "shutdown"})
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
