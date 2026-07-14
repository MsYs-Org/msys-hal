from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from msys_hal.errors import ProviderError, ReadOnlyError, UnavailableError, ValidationError
from msys_hal.linux import (
    BacklightBackend,
    DisplayBackend,
    InputBackend,
    PowerBackend,
    ThermalBackend,
    parse_input_devices,
)
from msys_hal.validation import ensure_bounded_json


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class FakeDisplayGateway:
    def __init__(self, available: bool = True, session=None) -> None:
        self.available = available
        self.session = session
        self.calls: list[tuple[str, str, dict, bool]] = []
        self.profile = "mobile"
        self.orientation = "portrait"

    def call(self, target, method, payload, *, timeout=5.0, idempotent=False):
        self.calls.append((target, method, payload, idempotent))
        if not self.available:
            return {"type": "error", "code": "NO_PROVIDER", "message": "window manager missing"}
        if method == "set_layout":
            self.profile = payload.get("profile", self.profile)
            requested = payload.get("orientation", "auto")
            self.orientation = "portrait" if requested == "auto" else requested
            return {"type": "return", "payload": {"ok": True, "profile": self.profile}}
        response = {
            "type": "return",
            "payload": {
                "ok": True,
                "schema": "msys.layout.effective.v1",
                "profile": self.profile,
                "orientation_policy": "auto",
                "insets_policy": "auto",
                "orientation": self.orientation,
                "screen": {"width": 320, "height": 480},
                "insets": {"top": 24, "right": 0, "bottom": 32, "left": 0},
                "workarea": {"x": 0, "y": 24, "width": 320, "height": 424},
                "navigation_edge": "bottom",
            },
        }
        if self.session is not None:
            response["payload"]["display_session"] = json_clone(self.session)
        return response


def display_session(*, mode="ch347-direct", device="CH347 Touch", display=":24"):
    return {
        "schema": "msys.display-session.v1",
        "state": "ready",
        "provider": "org.example.display:output",
        "generation": 5,
        "display": display,
        "geometry": {"width": 320, "height": 480, "depth": 24},
        "input_transform": {
            "enabled": True,
            "mode": mode,
            "device": device,
            "space": "normalized-display",
            "matrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
            "source": "provider-effective",
            "verified": True,
        },
        "observed_at_unix_ms": int(time.time() * 1000),
    }


class StaticSessionReader:
    def __init__(self, state=None):
        self.state = state

    def load(self):
        if self.state is None:
            raise UnavailableError(
                "display-session state is unavailable",
                details={"reason": "no-state"},
            )
        return json_clone(self.state)

    def accept(self, state):
        return json_clone(state)


def json_clone(value):
    import json
    return json.loads(json.dumps(value))


class FakeXinput:
    def __init__(self, *, ids="12\n", verify_mismatch=False):
        self.ids = ids
        self.matrix = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        self.verify_mismatch = verify_mismatch
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs["env"]["DISPLAY"]))
        if argv[1:3] == ["list", "--id-only"]:
            return subprocess.CompletedProcess(argv, 0, self.ids, "")
        if argv[1] == "set-prop":
            self.matrix = [float(value) for value in argv[4:]]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "list-props":
            matrix = [0] * 8 + [1] if self.verify_mismatch else self.matrix
            text = "Coordinate Transformation Matrix (123):\t" + ", ".join(
                str(value) for value in matrix
            ) + "\n"
            return subprocess.CompletedProcess(argv, 0, text, "")
        raise AssertionError(argv)


class LinuxProviderTests(unittest.TestCase):
    def test_power_inventory_and_normalized_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supply = root / "BAT0"
            write(supply / "type", "Battery\n")
            write(supply / "capacity", "83\n")
            write(supply / "status", "Discharging\n")
            write(supply / "voltage_now", "3890000\n")
            backend = PowerBackend(root)

            inventory = backend.inventory()
            self.assertEqual(inventory["status"], "available")
            self.assertEqual(inventory["devices"][0]["id"], "power:BAT0")
            state = backend.get_state("power:BAT0")
            self.assertEqual(state["values"]["capacity_percent"], 83)
            self.assertEqual(state["values"]["status"], "Discharging")
            self.assertEqual(state["values"]["voltage_now_uv"], 3890000)
            with self.assertRaises(ReadOnlyError):
                backend.set_state("power:BAT0", {"capacity_percent": 1})

    def test_missing_power_class_is_gracefully_unavailable(self) -> None:
        backend = PowerBackend(Path("/definitely/not/a/sysfs/class"))
        inventory = backend.inventory()
        self.assertEqual(inventory["status"], "unavailable")
        self.assertEqual(inventory["devices"], [])

    def test_thermal_temperature_is_millicelsius(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            zone = Path(temporary) / "thermal_zone0"
            write(zone / "type", "cpu-thermal\n")
            write(zone / "temp", "42125\n")
            state = ThermalBackend(Path(temporary)).get_state("thermal:thermal_zone0")
            self.assertEqual(state["values"]["temperature_millicelsius"], 42125)
            self.assertEqual(state["values"]["type"], "cpu-thermal")

    def test_backlight_write_is_bounded_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            panel = Path(temporary) / "panel0"
            write(panel / "max_brightness", "255\n")
            write(panel / "brightness", "20\n")
            write(panel / "actual_brightness", "19\n")
            backend = BacklightBackend(Path(temporary))

            state = backend.set_state("backlight:panel0", {"brightness": 200})
            self.assertEqual(state["values"]["brightness"], 200)
            self.assertEqual((panel / "brightness").read_text(encoding="utf-8"), "200\n")
            with self.assertRaises(ValidationError):
                backend.set_state("backlight:panel0", {"brightness": 256})
            with self.assertRaises(ValidationError):
                backend.set_state("backlight:panel0", {"brightness": True})
            with self.assertRaises(ValidationError):
                backend.set_state("backlight:panel0", {"brightness": 1, "raw": 1})
            shorter = backend.set_state("backlight:panel0", {"brightness": 4})
            self.assertEqual(shorter["values"]["brightness"], 4)
            self.assertEqual((panel / "brightness").read_text(encoding="utf-8"), "4\n")

    def test_backlight_refuses_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "class"
            panel = root / "panel0"
            outside = base / "outside"
            write(panel / "max_brightness", "10\n")
            write(outside, "3\n")
            panel.mkdir(parents=True, exist_ok=True)
            (panel / "brightness").symlink_to(outside)
            backend = BacklightBackend(root)
            with self.assertRaises(ProviderError):
                backend.set_state("backlight:panel0", {"brightness": 5})
            self.assertEqual(outside.read_text(encoding="utf-8"), "3\n")

    def test_backlight_percent_and_read_only_capability_are_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            panel = root / "panel0"
            write(panel / "max_brightness", "200\n")
            write(panel / "brightness", "40\n")
            backend = BacklightBackend(root)

            state = backend.set_state(
                "backlight:panel0",
                {"brightness_percent": 50},
            )
            self.assertEqual(state["values"]["brightness"], 100)
            self.assertEqual(state["values"]["brightness_percent"], 50)
            self.assertEqual(
                backend.inventory()["devices"][0]["mutable"],
                ["brightness", "brightness_percent"],
            )

            (panel / "brightness").chmod(0o444)
            self.assertEqual(backend.get_state("backlight:panel0")["mutable"], [])
            self.assertEqual(backend.inventory()["devices"][0]["metadata"]["control"], "read-only")
            with self.assertRaises(ReadOnlyError):
                backend.set_state("backlight:panel0", {"brightness": 10})

    def test_unreadable_backlight_is_structured_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            panel = Path(temporary) / "panel0"
            write(panel / "max_brightness", "100\n")
            inventory = BacklightBackend(Path(temporary)).inventory()

            self.assertEqual(inventory["status"], "unavailable")
            self.assertFalse(inventory["devices"][0]["available"])
            self.assertEqual(inventory["devices"][0]["mutable"], [])

    def test_input_proc_parser_and_backend(self) -> None:
        data = """I: Bus=0003 Vendor=046d Product=c077 Version=0111
N: Name=\"USB Mouse\"
P: Phys=usb-1/input0
S: Sysfs=/devices/test/input/input2
H: Handlers=mouse0 event2

I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name=\"No Event Device\"
H: Handlers=kbd
"""
        parsed = parse_input_devices(data)
        self.assertEqual([item["id"] for item in parsed], ["input:event2"])
        self.assertEqual(parsed[0]["metadata"]["name"], "USB Mouse")
        with tempfile.TemporaryDirectory() as temporary:
            proc_file = Path(temporary) / "devices"
            write(proc_file, data)
            backend = InputBackend(proc_file, Path(temporary) / "class")
            state = backend.get_state("input:event2")
            self.assertEqual(state["values"]["name"], "USB Mouse")
            with self.assertRaises(ReadOnlyError):
                backend.set_state("input:event2", {"enabled": False})

    def test_direct_display_input_is_visible_but_provider_owned(self) -> None:
        runner = FakeXinput()
        backend = InputBackend(
            Path("/missing/proc"),
            Path("/missing/sysfs"),
            StaticSessionReader(display_session(mode="ch347-direct")),
            runner=runner,
            xinput_binary="/usr/bin/xinput",
        )

        inventory = backend.inventory()
        touch = next(item for item in inventory["devices"] if item["id"] == "input:display-touch")
        self.assertEqual(touch["metadata"]["control"], "read-only")
        self.assertEqual(touch["mutable"], [])
        state = backend.get_state("input:display-touch")
        self.assertEqual(state["values"]["mode"], "ch347-direct")
        self.assertEqual(state["values"]["control"], "read-only")
        with self.assertRaises(ReadOnlyError) as caught:
            backend.set_state("input:display-touch", {"orientation": "left"})
        self.assertEqual(caught.exception.details["owner"], "org.example.display:output")
        self.assertEqual(runner.calls, [])

    def test_input_prefers_in_band_display_session(self) -> None:
        gateway = FakeDisplayGateway(session=display_session(display=":88"))
        backend = InputBackend(
            Path("/missing/proc"),
            Path("/missing/sysfs"),
            StaticSessionReader(),
            gateway=gateway,
            xinput_binary="",
        )

        state = backend.get_state("input:display-touch")

        self.assertEqual(state["values"]["display"], ":88")
        self.assertIn("get_display_session", [call[1] for call in gateway.calls])

    def test_unique_xinput_transform_is_written_and_read_back(self) -> None:
        runner = FakeXinput()
        backend = InputBackend(
            Path("/missing/proc"),
            Path("/missing/sysfs"),
            StaticSessionReader(display_session(mode="xinput", device="Unique Touch", display=":91")),
            runner=runner,
            xinput_binary="/usr/bin/xinput",
        )

        inventory = backend.inventory()
        touch = next(item for item in inventory["devices"] if item["id"] == "input:display-touch")
        self.assertEqual(touch["mutable"], ["orientation", "matrix"])
        state = backend.set_state("input:display-touch", {"orientation": "right"})

        self.assertEqual(state["values"]["orientation"], "right")
        self.assertEqual(state["values"]["matrix"], [0, 1, 0, -1, 0, 1, 0, 0, 1])
        self.assertTrue(all(display == ":91" for _argv, display in runner.calls))
        self.assertTrue(any(argv[1] == "set-prop" for argv, _display in runner.calls))

    def test_xinput_ambiguity_and_verification_failure_are_safe(self) -> None:
        ambiguous = InputBackend(
            Path("/missing/proc"),
            Path("/missing/sysfs"),
            StaticSessionReader(display_session(mode="xinput", device="Duplicate Touch")),
            runner=FakeXinput(ids="12\n13\n"),
            xinput_binary="/usr/bin/xinput",
        )
        self.assertEqual(
            ambiguous.inventory()["devices"][0]["metadata"]["control"],
            "read-only",
        )
        with self.assertRaises(ReadOnlyError):
            ambiguous.set_state("input:display-touch", {"orientation": "left"})

        mismatch = InputBackend(
            Path("/missing/proc"),
            Path("/missing/sysfs"),
            StaticSessionReader(display_session(mode="xinput", device="Unique Touch")),
            runner=FakeXinput(verify_mismatch=True),
            xinput_binary="/usr/bin/xinput",
        )
        with self.assertRaises(ProviderError):
            mismatch.set_state("input:display-touch", {"orientation": "left"})

    def test_display_uses_window_manager_role_only(self) -> None:
        gateway = FakeDisplayGateway()
        backend = DisplayBackend(gateway)
        inventory = backend.inventory()
        self.assertEqual(inventory["devices"][0]["id"], "display:primary")
        result = backend.set_state(
            "display:primary",
            {
                "profile": "desktop",
                "orientation": "landscape",
                "insets": {"top": 20, "right": 0, "bottom": 30, "left": 0},
            },
        )
        self.assertEqual(result["values"]["profile"], "desktop")
        self.assertTrue(all(call[0] == "role:window-manager" for call in gateway.calls))
        self.assertEqual(
            [call[1] for call in gateway.calls],
            [
                "get_layout",
                "get_display_session",
                "set_layout",
                "get_layout",
                "get_display_session",
            ],
        )
        with self.assertRaises(ValidationError):
            backend.set_state("display:primary", {"profile": "tablet"})

    def test_display_merges_live_session_without_assuming_display_number(self) -> None:
        session = display_session(display=":91")
        backend = DisplayBackend(FakeDisplayGateway(), StaticSessionReader(session))

        inventory = backend.inventory()
        state = backend.get_state("display:primary")

        self.assertEqual(inventory["status"], "available")
        self.assertEqual(
            inventory["devices"][0]["metadata"]["display"],
            ":91",
        )
        self.assertEqual(
            inventory["devices"][0]["metadata"]["display_provider"],
            session["provider"],
        )
        self.assertNotIn("display_session", inventory["devices"][0]["metadata"])
        ensure_bounded_json(
            inventory,
            label="provider inventory",
            max_depth=6,
            max_items=512,
        )
        self.assertEqual(state["values"]["display_session"]["geometry"]["height"], 480)
        self.assertEqual(state["mutable"], ["profile", "orientation", "insets"])
        with self.assertRaises(ReadOnlyError):
            backend.set_state("display:primary", {"physical_rotation": "left"})

    def test_display_prefers_fresh_in_band_session(self) -> None:
        embedded = display_session(display=":82")
        backend = DisplayBackend(
            FakeDisplayGateway(session=embedded),
            StaticSessionReader(display_session(display=":17")),
        )

        state = backend.get_state("display:primary")

        self.assertEqual(state["values"]["display_session"]["display"], ":82")

    def test_display_session_remains_observable_without_layout_control(self) -> None:
        backend = DisplayBackend(
            FakeDisplayGateway(available=False),
            StaticSessionReader(display_session(display=":0")),
        )

        inventory = backend.inventory()
        state = backend.get_state("display:primary")

        self.assertEqual(inventory["status"], "degraded")
        self.assertTrue(inventory["devices"][0]["available"])
        self.assertEqual(inventory["devices"][0]["mutable"], [])
        self.assertEqual(state["values"]["display_session"]["display"], ":0")
        self.assertEqual(state["values"]["layout_control"], "unavailable")

    def test_display_missing_role_is_unavailable(self) -> None:
        backend = DisplayBackend(FakeDisplayGateway(available=False))
        inventory = backend.inventory()
        self.assertEqual(inventory["status"], "unavailable")
        self.assertFalse(inventory["devices"][0]["available"])
        self.assertEqual(
            inventory["devices"][0]["metadata"]["physical_rotation"],
            "unavailable",
        )
        with self.assertRaises(UnavailableError):
            backend.get_state("display:primary")


if __name__ == "__main__":
    unittest.main()
