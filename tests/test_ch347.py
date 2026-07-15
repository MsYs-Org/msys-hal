from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path

from msys_hal.ch347 import (
    CALIBRATION_DEFAULTS,
    CONTROL_INTERFACE,
    DEFAULT_TARGET,
    DEVICE_ID,
    Ch347ControlBackend,
    Ch347ControlService,
)
from msys_hal.errors import HalError, PersistenceError, UnavailableError, ValidationError


def calibration_text(**overrides) -> str:
    values = dict(CALIBRATION_DEFAULTS)
    values.update(overrides)
    mapping = (
        ("CH347_TOUCH", "enabled"),
        ("CH347_TOUCH_SWAP_XY", "swap_xy"),
        ("CH347_TOUCH_INVERT_X", "invert_x"),
        ("CH347_TOUCH_INVERT_Y", "invert_y"),
        ("CH347_TOUCH_X_MIN", "x_min"),
        ("CH347_TOUCH_X_MAX", "x_max"),
        ("CH347_TOUCH_Y_MIN", "y_min"),
        ("CH347_TOUCH_Y_MAX", "y_max"),
        ("CH347_TOUCH_WIDTH", "width"),
        ("CH347_TOUCH_HEIGHT", "height"),
        ("CH347_TOUCH_Z_MIN", "z_min"),
        ("CH347_TOUCH_PRESSURE_MIN", "pressure_min"),
        ("CH347_TOUCH_PRESSURE_MAX", "pressure_max"),
    )
    return "\n".join(
        f"{key}={int(values[field]) if isinstance(values[field], bool) else values[field]}"
        for key, field in mapping
    ) + "\n"


def dirty_stats_text(**overrides) -> str:
    values = {
        "frame": 100,
        "sent_frames": 90,
        "zero_damage": 10,
        "full_refreshes": 2,
        "large_refreshes": 3,
        "sent_pixels": 456789,
        "last_sent_pixels": 2048,
        "last_rects": 4,
    }
    values.update(overrides)
    return (
        "dirty_stats "
        + " ".join(f"{field}={values[field]}" for field in values)
        + "\n"
    )


def overlay_text(
    *, enabled: bool = False, alpha: int = 176, scale: int = 1,
    items: int = 7, interval_ms: int = 1000,
) -> str:
    return (
        f"CH347_DEBUG_OVERLAY={int(enabled)}\n"
        f"CH347_DEBUG_OVERLAY_ALPHA={alpha}\n"
        f"CH347_DEBUG_OVERLAY_SCALE={scale}\n"
        f"CH347_DEBUG_OVERLAY_ITEMS={items}\n"
        f"CH347_DEBUG_OVERLAY_INTERVAL_MS={interval_ms}\n"
    )


class FakeGateway:
    def __init__(self, *, present: bool = True, state: str = "ready") -> None:
        self.present = present
        self.state = state
        self.calls: list[tuple[str, str, dict, float, bool]] = []
        self.on_start = lambda: None

    def call(self, target, method, payload, *, timeout=5.0, idempotent=False):
        self.calls.append((target, method, dict(payload), timeout, idempotent))
        if method == "list_components":
            components = []
            if self.present:
                components.append({
                    "id": DEFAULT_TARGET,
                    "state": self.state,
                    "package_version": "0.1.0",
                })
            return {"type": "return", "payload": {"components": components}}
        if method == "stop":
            self.state = "declared"
            return {
                "type": "return",
                "payload": {"component": DEFAULT_TARGET, "state": "stopped"},
            }
        if method == "start":
            self.state = "ready"
            self.on_start()
            return {
                "type": "return",
                "payload": {"component": DEFAULT_TARGET, "state": "ready"},
            }
        raise AssertionError((target, method, payload))


class Ch347ControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "state" / "ch347"
        self.run = self.root / "run"
        self.config.mkdir(parents=True)
        self.run.mkdir(parents=True)
        (self.config / "fps.env").write_text(
            "FPS=60\nXCAP_MAX_FPS=60\nXCAP_IDLE_FPS=1\n",
            encoding="ascii",
        )
        (self.config / "debug_overlay.env").write_text(
            overlay_text(), encoding="ascii"
        )
        (self.config / "cursor.env").write_text(
            "CH347_CURSOR=0\n", encoding="ascii"
        )
        (self.config / "touch_calibration.env").write_text(
            calibration_text(
                x_min=207,
                x_max=3859,
                y_min=239,
                y_max=3836,
                z_min=109,
                pressure_min=100,
                pressure_max=568,
            ),
            encoding="ascii",
        )
        (self.config / "rotation.env").write_text(
            "CH347_DISPLAY_ROTATION=normal\n",
            encoding="ascii",
        )
        (self.run / "pids").write_text("101\n102\n", encoding="ascii")
        (self.run / "msys.provider.owner").write_text(
            "7:900:1700000000\n",
            encoding="ascii",
        )
        (self.run / "display-config.applied.env").write_text(
            "MSYS_GENERATION=7\n"
            "DEBUG=0\n"
            "FPS=60\n"
            "XCAP_MAX_FPS=60\n"
            "XCAP_IDLE_FPS=1\n",
            encoding="ascii",
        )
        (self.run / "debug-overlay.applied.env").write_text(
            "MSYS_GENERATION=7\n" + overlay_text(),
            encoding="ascii",
        )
        (self.run / "cursor.applied.env").write_text(
            "MSYS_GENERATION=7\nCH347_CURSOR=0\n",
            encoding="ascii",
        )
        self.signals: list[tuple[int, int]] = []
        self.gateway = FakeGateway()
        self.generation = 7

        def apply_runtime_config() -> None:
            self.generation += 1
            config = (self.config / "fps.env").read_text(encoding="ascii")
            (self.run / "msys.provider.owner").write_text(
                f"{self.generation}:900:1700000000\n",
                encoding="ascii",
            )
            (self.run / "display-config.applied.env").write_text(
                f"MSYS_GENERATION={self.generation}\n{config}",
                encoding="ascii",
            )
            overlay = (self.config / "debug_overlay.env").read_text(encoding="ascii")
            (self.run / "debug-overlay.applied.env").write_text(
                f"MSYS_GENERATION={self.generation}\n{overlay}",
                encoding="ascii",
            )
            cursor = (self.config / "cursor.env").read_text(encoding="ascii")
            (self.run / "cursor.applied.env").write_text(
                f"MSYS_GENERATION={self.generation}\n{cursor}",
                encoding="ascii",
            )
            (self.run / "live.log").write_text(
                (
                    "dirty frame=20 captured=20 drop=0 sent_rects=1 "
                    "dirty=2.0% bus_fps=1.25 out_fps=12.50\n"
                    if "DEBUG=1\n" in config
                    else ""
                ),
                encoding="ascii",
            )

        self.gateway.on_start = apply_runtime_config
        self.backend = Ch347ControlBackend(
            self.gateway,
            config_dir=self.config,
            run_dir=self.run,
            proc_root=self.root / "proc",
            pid_alive=lambda pid: pid in {101, 102},
            process_executable=lambda _root, pid: (
                "/immutable/bin/xdamage_shm_capture"
                if pid == 101
                else "/immutable/bin/ch347_dirty_usb_sink"
            ),
            signal_process=lambda pid, sig: self.signals.append((pid, sig)),
        )

    def test_inventory_and_state_report_typed_live_control(self) -> None:
        inventory = self.backend.inventory()
        self.assertEqual(inventory["domain"], "display-output")
        self.assertEqual(inventory["status"], "available")
        device = inventory["devices"][0]
        self.assertEqual(device["id"], DEVICE_ID)
        self.assertEqual(
            device["mutable"],
            [
                "debug_enabled",
                "debug_overlay",
                "touch_cursor_enabled",
                "fps",
                "idle_fps",
                "touch_calibration",
                "physical_rotation",
                "restart",
            ],
        )

        state = self.backend.get_state(DEVICE_ID)
        self.assertTrue(state["available"])
        self.assertTrue(state["values"]["running"])
        self.assertEqual(state["values"]["live_processes"], 2)
        self.assertEqual(state["values"]["fps"], 60)
        self.assertEqual(state["values"]["max_fps"], 60)
        self.assertFalse(state["values"]["debug"]["enabled"])
        self.assertTrue(state["values"]["debug"]["applied"])
        self.assertEqual(state["values"]["debug"]["provider_generation"], 7)
        self.assertEqual(
            state["values"]["debug"]["overlay"],
            {
                "enabled": False,
                "alpha": 176,
                "scale": 1,
                "items": ["fps", "dirty", "bytes"],
                "interval_ms": 1000,
            },
        )
        self.assertEqual(
            state["values"]["debug"]["touch_cursor"],
            {
                "enabled": False,
                "applied": True,
                "requires_restart": False,
                "provider_generation": 7,
                "reason": "applied",
            },
        )
        self.assertEqual(state["values"]["idle_fps"], 1)
        self.assertEqual(state["values"]["touch_calibration"]["x_min"], 207)
        self.assertEqual(state["values"]["physical_rotation"], "normal")
        self.assertEqual(state["values"]["physical_rotation_control"], "writable")
        self.assertFalse(state["values"]["restart"])

    def test_fps_write_is_canonical_atomic_and_hot_reloads_only_capture(self) -> None:
        state = self.backend.set_state(
            DEVICE_ID,
            {"fps": 90, "idle_fps": 2},
        )

        self.assertEqual(state["values"]["fps"], 90)
        self.assertEqual(state["values"]["idle_fps"], 2)
        self.assertEqual(
            (self.config / "fps.env").read_text(encoding="ascii"),
            "DEBUG=0\nFPS=90\nXCAP_MAX_FPS=90\nXCAP_IDLE_FPS=2\n",
        )
        self.assertEqual(self.signals, [(101, signal.SIGUSR1)])
        self.assertEqual(
            [path.name for path in self.config.iterdir() if path.name.startswith(".fps.env.")],
            [],
        )
        self.assertNotIn("stop", [call[1] for call in self.gateway.calls])

    def test_fps_validation_rejects_bool_range_unknown_and_inconsistent_idle(self) -> None:
        original = (self.config / "fps.env").read_bytes()
        invalid = (
            {"fps": True},
            {"fps": 0},
            {"fps": 30, "idle_fps": 31},
            {"raw_fps_file": 60},
            {"restart": False},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.backend.set_state(DEVICE_ID, changes)
            self.assertEqual((self.config / "fps.env").read_bytes(), original)

    def test_debug_overlay_is_boolean_persistent_generation_verified_and_measured(self) -> None:
        state = self.backend.set_state(DEVICE_ID, {"debug_enabled": True})

        self.assertEqual(
            (self.config / "fps.env").read_text(encoding="ascii"),
            "DEBUG=1\nFPS=60\nXCAP_MAX_FPS=60\nXCAP_IDLE_FPS=1\n",
        )
        self.assertEqual(
            [call[1] for call in self.gateway.calls if call[1] in {"stop", "start"}],
            ["stop", "start"],
        )
        debug = state["values"]["debug"]
        self.assertTrue(debug["enabled"])
        self.assertTrue(debug["applied"])
        self.assertFalse(debug["requires_restart"])
        self.assertEqual(debug["provider_generation"], 8)
        self.assertEqual(debug["status"], "active")
        self.assertEqual(debug["reason"], "sink-debug-log")
        self.assertEqual(debug["observed_fps"], 12.5)
        self.assertEqual(debug["panel_fps"], 1.25)
        self.assertEqual(debug["frames"], 20)
        self.assertIsNone(debug["window_ms"])
        for field in (
            "sent_frames",
            "zero_damage",
            "full_refreshes",
            "large_refreshes",
            "sent_pixels",
            "last_sent_pixels",
            "last_rects",
        ):
            self.assertIsNone(debug[field])

        with self.assertRaises(ValidationError):
            self.backend.set_state(DEVICE_ID, {"debug_enabled": 1})

    def test_debug_overlay_is_exposed_only_after_exact_active_generation_receipt(self) -> None:
        configured = self.backend.get_state(DEVICE_ID)["values"]["debug"]
        self.assertIn("overlay", configured)

        cases = {
            "missing": None,
            "stale-generation": (
                "MSYS_GENERATION=6\n" + overlay_text()
            ),
            "different-alpha": (
                "MSYS_GENERATION=7\n" + overlay_text(alpha=128)
            ),
            "different-items": (
                "MSYS_GENERATION=7\n" + overlay_text(items=1)
            ),
        }
        for name, receipt in cases.items():
            with self.subTest(name=name):
                if receipt is None:
                    self.backend.applied_overlay_path.unlink(missing_ok=True)
                else:
                    self.backend.applied_overlay_path.write_text(
                        receipt,
                        encoding="ascii",
                    )
                debug = self.backend.get_state(DEVICE_ID)["values"]["debug"]
                self.assertNotIn("overlay", debug)

        self.backend.applied_overlay_path.write_text(
            "MSYS_GENERATION=7\n" + overlay_text(),
            encoding="ascii",
        )
        self.gateway.state = "declared"
        debug = self.backend.get_state(DEVICE_ID)["values"]["debug"]
        self.assertNotIn("overlay", debug)

    def test_touch_cursor_is_persistent_and_generation_verified(self) -> None:
        state = self.backend.set_state(
            DEVICE_ID,
            {"touch_cursor_enabled": True},
        )

        self.assertEqual(
            (self.config / "cursor.env").read_text(encoding="ascii"),
            "CH347_CURSOR=1\n",
        )
        self.assertEqual(
            [call[1] for call in self.gateway.calls if call[1] in {"stop", "start"}],
            ["stop", "start"],
        )
        cursor = state["values"]["debug"]["touch_cursor"]
        self.assertEqual(
            cursor,
            {
                "enabled": True,
                "applied": True,
                "requires_restart": False,
                "provider_generation": 8,
                "reason": "applied",
            },
        )

        (self.run / "cursor.applied.env").write_text(
            "MSYS_GENERATION=7\nCH347_CURSOR=1\n",
            encoding="ascii",
        )
        stale = self.backend.get_state(DEVICE_ID)["values"]["debug"]["touch_cursor"]
        self.assertFalse(stale["applied"])
        self.assertTrue(stale["requires_restart"])
        self.assertIsNone(stale["provider_generation"])
        self.assertIn("active generation", stale["reason"])

        with self.assertRaises(ValidationError):
            self.backend.set_state(DEVICE_ID, {"touch_cursor_enabled": 1})

    def test_old_driver_without_cursor_contract_never_accepts_fake_write(self) -> None:
        (self.config / "cursor.env").unlink()
        (self.run / "cursor.applied.env").unlink()

        state = self.backend.get_state(DEVICE_ID)
        self.assertNotIn("touch_cursor", state["values"]["debug"])
        self.assertNotIn("touch_cursor_enabled", state["mutable"])
        with self.assertRaises(UnavailableError):
            self.backend.set_state(DEVICE_ID, {"touch_cursor_enabled": True})
        self.assertFalse((self.config / "cursor.env").exists())

    def test_latest_dirty_stats_are_exposed_even_when_debug_is_disabled(self) -> None:
        (self.run / "live.log").write_text(
            dirty_stats_text(sent_frames=7, sent_pixels=1000)
            + "unrelated sink output\n"
            + dirty_stats_text(
                frame=200,
                sent_frames=180,
                zero_damage=20,
                full_refreshes=4,
                large_refreshes=6,
                sent_pixels=2**64 - 1,
                last_sent_pixels=4096,
                last_rects=8,
            ),
            encoding="ascii",
        )

        service = Ch347ControlService(
            self.backend,
            provider_id="org.msys.hal.linux:ch347-output-control",
        )
        debug = service.handle("get_debug", {})["debug"]

        self.assertFalse(debug["enabled"])
        self.assertEqual(debug["status"], "idle")
        self.assertEqual(
            {field: debug[field] for field in (
                "sent_frames",
                "zero_damage",
                "full_refreshes",
                "large_refreshes",
                "sent_pixels",
                "last_sent_pixels",
                "last_rects",
            )},
            {
                "sent_frames": 180,
                "zero_damage": 20,
                "full_refreshes": 4,
                "large_refreshes": 6,
                "sent_pixels": 2**64 - 1,
                "last_sent_pixels": 4096,
                "last_rects": 8,
            },
        )

    def test_invalid_newest_dirty_stats_never_fall_back_to_stale_counters(self) -> None:
        invalid_values = (
            {"sent_frames": -1},
            {"zero_damage": "not-a-number"},
            {"sent_pixels": 2**64},
            {"frame": 2**64},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                (self.run / "live.log").write_text(
                    dirty_stats_text() + dirty_stats_text(**overrides),
                    encoding="ascii",
                )
                debug = self.backend.get_state(DEVICE_ID)["values"]["debug"]
                for field in (
                    "sent_frames",
                    "zero_damage",
                    "full_refreshes",
                    "large_refreshes",
                    "sent_pixels",
                    "last_sent_pixels",
                    "last_rects",
                ):
                    self.assertIsNone(debug[field])

    def test_dirty_stats_outside_bounded_log_tail_are_not_read(self) -> None:
        (self.run / "live.log").write_text(
            dirty_stats_text() + ("unrelated sink output\n" * 4096),
            encoding="ascii",
        )

        debug = self.backend.get_state(DEVICE_ID)["values"]["debug"]

        self.assertIsNone(debug["sent_frames"])
        self.assertIsNone(debug["sent_pixels"])

    def test_stopped_debug_write_is_saved_and_explicitly_requires_restart(self) -> None:
        self.gateway.state = "declared"
        state = self.backend.set_state(DEVICE_ID, {"debug_enabled": True})
        debug = state["values"]["debug"]
        self.assertTrue(debug["enabled"])
        self.assertFalse(debug["applied"])
        self.assertTrue(debug["requires_restart"])
        self.assertEqual(debug["status"], "unavailable")
        self.assertFalse(any(call[1] == "start" for call in self.gateway.calls))

    def test_unbounded_or_non_numeric_debug_log_never_becomes_a_sample(self) -> None:
        (self.config / "fps.env").write_text(
            "DEBUG=1\nFPS=60\nXCAP_MAX_FPS=60\nXCAP_IDLE_FPS=1\n",
            encoding="ascii",
        )
        (self.run / "display-config.applied.env").write_text(
            "MSYS_GENERATION=7\n"
            "DEBUG=1\n"
            "FPS=60\n"
            "XCAP_MAX_FPS=60\n"
            "XCAP_IDLE_FPS=1\n",
            encoding="ascii",
        )
        (self.run / "live.log").write_text(
            "dirty frame=99999999999 captured=1 drop=0 sent_rects=1 "
            "dirty=1.0% bus_fps=1.0 out_fps=nan\n",
            encoding="ascii",
        )

        debug = self.backend.get_state(DEVICE_ID)["values"]["debug"]
        self.assertTrue(debug["applied"])
        self.assertEqual(debug["status"], "unavailable")
        self.assertEqual(debug["reason"], "awaiting-debug-sample")
        self.assertIsNone(debug["observed_fps"])
        self.assertIsNone(debug["panel_fps"])
        self.assertIsNone(debug["frames"])

    def test_partial_calibration_write_is_validated_and_restarts_running_output(self) -> None:
        state = self.backend.set_state(
            DEVICE_ID,
            {"touch_calibration": {"invert_x": True, "x_min": 250}},
        )

        calibration = state["values"]["touch_calibration"]
        self.assertTrue(calibration["invert_x"])
        self.assertEqual(calibration["x_min"], 250)
        self.assertEqual(calibration["x_max"], 3859)
        saved = (self.config / "touch_calibration.env").read_text(encoding="ascii")
        self.assertIn("CH347_TOUCH_INVERT_X=1\n", saved)
        self.assertIn("CH347_TOUCH_X_MIN=250\n", saved)
        self.assertEqual(
            [call[1] for call in self.gateway.calls if call[1] in {"stop", "start"}],
            ["stop", "start"],
        )
        start = next(call for call in self.gateway.calls if call[1] == "start")
        self.assertEqual(start[3], 30.0)

    def test_physical_rotation_has_independent_atomic_file_and_restarts_output(self) -> None:
        calibration_before = (self.config / "touch_calibration.env").read_bytes()
        state = self.backend.set_state(
            DEVICE_ID,
            {"physical_rotation": "right"},
        )

        self.assertEqual(state["values"]["physical_rotation"], "right")
        self.assertEqual(
            (self.config / "rotation.env").read_text(encoding="ascii"),
            "CH347_DISPLAY_ROTATION=right\n",
        )
        self.assertEqual(
            (self.config / "touch_calibration.env").read_bytes(),
            calibration_before,
        )
        self.assertEqual(
            [call[1] for call in self.gateway.calls if call[1] in {"stop", "start"}],
            ["stop", "start"],
        )
        for invalid in ("clockwise", "RIGHT", "", 1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.backend.set_state(
                    DEVICE_ID,
                    {"physical_rotation": invalid},
                )

    def test_missing_rotation_file_is_explicitly_read_only(self) -> None:
        (self.config / "rotation.env").unlink()
        state = self.backend.get_state(DEVICE_ID)
        self.assertEqual(state["values"]["physical_rotation"], "normal")
        self.assertEqual(state["values"]["physical_rotation_control"], "unavailable")
        self.assertNotIn("physical_rotation", state["mutable"])
        with self.assertRaises(UnavailableError):
            self.backend.set_state(
                DEVICE_ID,
                {"physical_rotation": "left"},
            )
        self.assertFalse((self.config / "rotation.env").exists())

    def test_calibration_validation_is_strict_and_does_not_restart(self) -> None:
        invalid = (
            {"unknown": 1},
            {"swap_xy": 1},
            {"x_min": 4000},
            {"pressure_min": 600},
        )
        original = (self.config / "touch_calibration.env").read_bytes()
        for calibration in invalid:
            with self.subTest(calibration=calibration), self.assertRaises(ValidationError):
                self.backend.set_state(
                    DEVICE_ID,
                    {"touch_calibration": calibration},
                )
            self.assertEqual(
                (self.config / "touch_calibration.env").read_bytes(),
                original,
            )
        self.assertFalse(any(call[1] in {"stop", "start"} for call in self.gateway.calls))

    def test_stopped_output_saves_calibration_but_explicit_restart_is_refused(self) -> None:
        self.gateway.state = "declared"
        state = self.backend.set_state(
            DEVICE_ID,
            {"touch_calibration": {"invert_y": True}},
        )
        self.assertTrue(state["values"]["touch_calibration"]["invert_y"])
        self.assertFalse(any(call[1] == "start" for call in self.gateway.calls))
        with self.assertRaises(UnavailableError):
            self.backend.set_state(DEVICE_ID, {"restart": True})

    def test_missing_driver_is_structured_unavailable_and_never_creates_state(self) -> None:
        missing_config = self.root / "missing-state" / "ch347"
        backend = Ch347ControlBackend(
            FakeGateway(present=False),
            config_dir=missing_config,
            run_dir=self.run,
            pid_alive=lambda _pid: False,
        )
        inventory = backend.inventory()
        self.assertEqual(inventory["status"], "unavailable")
        self.assertFalse(inventory["devices"][0]["available"])
        with self.assertRaises(UnavailableError):
            backend.set_state(DEVICE_ID, {"fps": 30})
        self.assertFalse(missing_config.exists())

    def test_unprovisioned_fallback_never_accepts_disconnected_config_write(self) -> None:
        missing_config = self.root / "fallback-state" / "ch347"
        backend = Ch347ControlBackend(
            FakeGateway(present=True, state="ready"),
            config_dir=missing_config,
            run_dir=self.run,
            pid_alive=lambda pid: pid in {101, 102},
        )
        state = backend.get_state(DEVICE_ID)
        self.assertFalse(state["values"]["configuration_provisioned"])
        self.assertEqual(state["mutable"], ["restart"])
        with self.assertRaises(UnavailableError):
            backend.set_state(DEVICE_ID, {"fps": 30})
        self.assertFalse(missing_config.exists())

    def test_invalid_persisted_shell_syntax_is_reported_and_repaired_by_typed_write(self) -> None:
        (self.config / "fps.env").write_text(
            "FPS=$(bad)\nXCAP_MAX_FPS=60\nXCAP_IDLE_FPS=1\n",
            encoding="ascii",
        )
        state = self.backend.get_state(DEVICE_ID)["values"]
        self.assertEqual(state["status"], "degraded")
        self.assertFalse(state["configuration_valid"])
        self.assertTrue(state["configuration_errors"])
        self.assertFalse(state["debug"]["applied"])
        self.assertTrue(state["debug"]["requires_restart"])
        self.assertEqual(state["debug"]["status"], "unavailable")
        self.assertEqual(state["debug"]["reason"], "invalid-display-config")

        repaired = self.backend.set_state(DEVICE_ID, {"fps": 45})["values"]
        self.assertEqual(repaired["fps"], 45)
        self.assertTrue(repaired["configuration_valid"])
        self.assertNotIn("$(bad)", (self.config / "fps.env").read_text(encoding="ascii"))

    def test_existing_symlink_config_is_never_followed(self) -> None:
        link = self.config / "fps.env"
        outside = self.root / "outside.env"
        outside.write_text("do-not-change\n", encoding="ascii")
        link.unlink()
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaises(PersistenceError):
            self.backend.set_state(DEVICE_ID, {"fps": 30})
        self.assertEqual(outside.read_text(encoding="ascii"), "do-not-change\n")

    def test_optional_control_interface_has_method_specific_strict_payloads(self) -> None:
        service = Ch347ControlService(
            self.backend,
            provider_id="org.msys.hal.linux:ch347-output-control",
        )
        description = service.handle("describe", {})
        self.assertIn("display-output.debug-overlay.write", description["capabilities"])
        self.assertIn("display-output.fps.write", description["capabilities"])
        self.assertIn(
            "display-output.physical-rotation.write",
            description["capabilities"],
        )
        status = service.handle("status", {})
        self.assertEqual(status["schema"], CONTROL_INTERFACE)
        self.assertEqual(status["device"], DEVICE_ID)
        changed = service.handle("set_fps", {"fps": 75, "idle_fps": 1})
        self.assertEqual(changed["fps"], 75)
        debug_before = service.handle("get_debug", {})
        self.assertFalse(debug_before["debug"]["enabled"])
        debug_after = service.handle("set_debug", {"enabled": True})
        self.assertTrue(debug_after["debug"]["enabled"])
        self.assertTrue(debug_after["debug"]["applied"])
        self.assertEqual(debug_after["debug"]["provider_generation"], 8)
        overlay_after = service.handle("set_debug", {
            "overlay": {
                "enabled": True,
                "alpha": 128,
                "scale": 1,
                "items": ["fps", "bbox", "memory"],
                "interval_ms": 750,
            },
        })
        self.assertEqual(
            overlay_after["debug"]["overlay"],
            {
                "enabled": True,
                "alpha": 128,
                "scale": 1,
                "items": ["fps", "bbox", "memory"],
                "interval_ms": 750,
            },
        )
        self.assertEqual(
            (self.config / "debug_overlay.env").read_text(encoding="ascii"),
            overlay_text(enabled=True, alpha=128, items=25, interval_ms=750),
        )
        cursor_after = service.handle("set_debug", {"cursor_enabled": True})
        self.assertTrue(cursor_after["debug"]["touch_cursor"]["enabled"])
        self.assertTrue(cursor_after["debug"]["touch_cursor"]["applied"])
        calibration = service.handle(
            "set_touch_calibration",
            {"touch_calibration": {"swap_xy": True}},
        )
        self.assertTrue(calibration["touch_calibration"]["swap_xy"])
        rotation = service.handle("get_physical_rotation", {})
        self.assertEqual(rotation["physical_rotation"], "normal")
        self.assertTrue(rotation["writable"])
        rotated = service.handle(
            "set_physical_rotation",
            {"physical_rotation": "inverted"},
        )
        self.assertEqual(rotated["state"]["physical_rotation"], "inverted")
        self.assertIn("physical_rotation", rotated["mutable"])
        with self.assertRaises(ValidationError):
            service.handle("set_fps", {"fps": 60, "path": "/tmp/raw"})
        for invalid in (1, "true", None):
            with self.subTest(debug=invalid), self.assertRaises(ValidationError):
                service.handle("set_debug", {"enabled": invalid})
        with self.assertRaises(HalError) as caught:
            service.handle("raw_write", {})
        self.assertEqual(caught.exception.code, "HAL_UNKNOWN_METHOD")


if __name__ == "__main__":
    unittest.main()
