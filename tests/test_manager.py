from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

from msys_hal.errors import (
    ConflictError,
    HalError,
    PersistenceError,
    UnavailableError,
    ValidationError,
)
from msys_hal.manager import (
    MANAGER_INTERFACE,
    PROVIDER_INTERFACE,
    HalManager,
    SelectionStore,
)


class FakeGateway:
    def __init__(self, *, providers=True) -> None:
        self.calls: list[tuple[str, str, dict, bool]] = []
        self.providers = providers
        self.fail_inventory: set[str] = set()
        self.unavailable_inventory: set[str] = set()
        self.malformed = False
        self.include_capabilities = True
        self.state_error: tuple[str, str] | None = None
        self.levels = {
            "org.example.high:power": 90,
            "org.example.low:power": 40,
        }

    def call(self, target, method, payload, *, timeout=5.0, idempotent=False):
        self.calls.append((target, method, payload, idempotent))
        if target == "msys.core" and method == "discover":
            rows = []
            if self.providers:
                rows = [
                    {"component": "org.example.high:power", "priority": 100},
                    {"component": "org.example.low:power", "priority": 20},
                ]
            return {
                "type": "return",
                "payload": {
                    "services": [{
                        "kind": "interface",
                        "name": PROVIDER_INTERFACE,
                        "providers": rows,
                    }] if rows else [],
                },
            }
        component = target.removeprefix("component:")
        if method == "describe":
            description = {
                "schema": PROVIDER_INTERFACE,
                "provider": {"id": component, "name": component, "version": "1.0.0"},
                "domains": ["power"],
            }
            if self.include_capabilities:
                description["capabilities"] = [
                    "power.inventory",
                    "power.limit.write",
                    "power.state.read",
                ]
            return {
                "type": "return",
                "payload": description,
            }
        if method == "inventory":
            if component in self.fail_inventory:
                return {"type": "error", "code": "PROVIDER_LOST", "message": component}
            device = {
                "id": "power:BAT0",
                "domain": "power",
                "name": "BAT0",
                "available": True,
                "mutable": ["limit"],
                "metadata": {"type": "Battery"},
            }
            if self.malformed:
                device["raw_path"] = "/sys/private"
            unavailable = component in self.unavailable_inventory
            return {
                "type": "return",
                "payload": {
                    "schema": PROVIDER_INTERFACE,
                    "provider": component,
                    "domains": [{
                        "domain": "power",
                        "status": "unavailable" if unavailable else "available",
                        **({"reason": "no-device"} if unavailable else {}),
                    }],
                    "devices": [] if unavailable else [device],
                },
            }
        if method in {"get_state", "set_state"}:
            if self.state_error is not None:
                code, message = self.state_error
                return {
                    "type": "error",
                    "code": code,
                    "message": message,
                    "payload": {"id": payload["id"]},
                }
            if method == "set_state":
                self.levels[component] = payload["changes"]["limit"]
            return {
                "type": "return",
                "payload": {
                    "schema": PROVIDER_INTERFACE,
                    "provider": component,
                    "state": {
                        "id": payload["id"],
                        "domain": "power",
                        "available": True,
                        "values": {"capacity_percent": 75, "limit": self.levels[component]},
                        "mutable": ["limit"],
                    },
                },
            }
        raise AssertionError((target, method, payload))


class ManagerTests(unittest.TestCase):
    def make_manager(self, gateway=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager = HalManager(
            gateway or FakeGateway(),
            SelectionStore(Path(temporary.name) / "selection.json"),
            catalog_ttl=60,
        )
        return manager

    def test_automatic_priority_inventory_and_state_route(self) -> None:
        gateway = FakeGateway()
        manager = self.make_manager(gateway)
        inventory = manager.handle("inventory", {"domains": ["power"]})
        self.assertEqual(inventory["schema"], MANAGER_INTERFACE)
        self.assertEqual(inventory["domains"][0]["provider"], "org.example.high:power")
        self.assertEqual(inventory["devices"][0]["provider"], "org.example.high:power")
        state = manager.handle("get_state", {"id": "power:BAT0"})
        self.assertEqual(state["provider"], "org.example.high:power")
        self.assertEqual(state["state"]["values"]["capacity_percent"], 75)

    def test_automatic_provider_fails_over_but_manual_pin_does_not(self) -> None:
        gateway = FakeGateway()
        gateway.fail_inventory.add("org.example.high:power")
        manager = self.make_manager(gateway)
        automatic = manager.handle("inventory", {"domains": ["power"]})
        self.assertEqual(automatic["domains"][0]["provider"], "org.example.low:power")
        providers = manager.handle("list_providers", {"domain": "power"})
        self.assertEqual(providers["providers"][0]["active"], "org.example.low:power")

        gateway.fail_inventory.clear()
        manager.handle("select_provider", {
            "domain": "power",
            "component": "org.example.high:power",
        })
        gateway.fail_inventory.add("org.example.high:power")
        pinned = manager.handle("inventory", {"domains": ["power"]})
        self.assertEqual(pinned["domains"][0]["status"], "unavailable")
        self.assertEqual(pinned["domains"][0]["selection"], "manual")
        inventory_calls = [call for call in gateway.calls if call[1] == "inventory"]
        self.assertNotEqual(inventory_calls[-1][0], "component:org.example.low:power")

    def test_automatic_provider_falls_back_from_unavailable_domain(self) -> None:
        gateway = FakeGateway()
        gateway.unavailable_inventory.add("org.example.high:power")
        manager = self.make_manager(gateway)
        inventory = manager.handle("inventory", {"domains": ["power"]})
        self.assertEqual(inventory["domains"][0]["status"], "available")
        self.assertEqual(inventory["domains"][0]["provider"], "org.example.low:power")

    def test_provider_selection_is_atomic_and_resettable(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SelectionStore(Path(temporary.name) / "selection.json")
        manager = HalManager(FakeGateway(), store)
        selected = manager.handle("select_provider", {
            "domain": "power",
            "component": "org.example.low:power",
        })
        self.assertEqual(selected["providers"][0]["active"], "org.example.low:power")
        self.assertEqual(store.load(), {"power": "org.example.low:power"})
        reset = manager.handle("reset_provider", {"domain": "power"})
        self.assertEqual(reset["providers"][0]["selection"], "automatic")
        self.assertEqual(store.load(), {})
        with self.assertRaises(ValidationError):
            manager.handle("select_provider", {
                "domain": "power",
                "component": "org.example.missing:provider",
            })

    def test_provider_catalog_exposes_capabilities_and_probed_health(self) -> None:
        gateway = FakeGateway()
        gateway.unavailable_inventory.add("org.example.high:power")
        manager = self.make_manager(gateway)

        cold = manager.handle("list_providers", {"domain": "power"})
        self.assertEqual(
            cold["providers"][0]["candidates"][0]["health"],
            {"status": "unknown", "reason": "not-checked"},
        )
        live = manager.handle("list_providers", {
            "domain": "power",
            "probe": True,
        })
        row = live["providers"][0]
        self.assertEqual(row["active"], "org.example.low:power")
        candidates = {item["component"]: item for item in row["candidates"]}
        self.assertEqual(
            candidates["org.example.high:power"]["health"]["status"],
            "unavailable",
        )
        self.assertEqual(
            candidates["org.example.low:power"]["health"]["status"],
            "available",
        )
        self.assertEqual(
            candidates["org.example.low:power"]["health"]["mutable"],
            ["limit"],
        )
        self.assertIn(
            "power.limit.write",
            candidates["org.example.low:power"]["capabilities"],
        )

    def test_catalog_bounds_parallel_describes_and_isolates_errors(self) -> None:
        components = (
            "org.example.parallel:broken",
            "org.example.parallel:slow-b",
            "org.example.parallel:slow-c",
            "org.example.parallel:slow-a",
        )

        class ParallelDescribeGateway:
            def __init__(self) -> None:
                self.barrier = threading.Barrier(2, timeout=2.0)
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.started: dict[str, float] = {}
                self.finished: dict[str, float] = {}

            def call(self, target, method, payload, *, timeout=5.0, idempotent=False):
                if target == "msys.core" and method == "discover":
                    return {
                        "type": "return",
                        "payload": {
                            "services": [{
                                "kind": "interface",
                                "name": PROVIDER_INTERFACE,
                                "providers": [
                                    {"component": "org.example.parallel:slow-a", "priority": 50},
                                    {"component": "org.example.parallel:broken", "priority": 200},
                                    {"component": "org.example.parallel:slow-c", "priority": 100},
                                    {"component": "org.example.parallel:slow-b", "priority": 100},
                                ],
                            }],
                        },
                    }
                self.assert_describe_call(target, method, payload, timeout, idempotent)
                component = target.removeprefix("component:")
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.started[component] = time.monotonic()
                try:
                    self.barrier.wait()
                    time.sleep({
                        "org.example.parallel:broken": 0.01,
                        "org.example.parallel:slow-b": 0.08,
                        "org.example.parallel:slow-c": 0.02,
                        "org.example.parallel:slow-a": 0.05,
                    }[component])
                    if component == "org.example.parallel:broken":
                        return {
                            "type": "error",
                            "code": "PROVIDER_BROKEN",
                            "message": "synthetic describe failure",
                        }
                    return {
                        "type": "return",
                        "payload": {
                            "schema": PROVIDER_INTERFACE,
                            "provider": {
                                "id": component,
                                "name": component,
                                "version": "1.0.0",
                            },
                            "domains": ["power"],
                        },
                    }
                finally:
                    with self.lock:
                        self.finished[component] = time.monotonic()
                        self.active -= 1

            @staticmethod
            def assert_describe_call(target, method, payload, timeout, idempotent) -> None:
                if not (
                    target.startswith("component:org.example.parallel:")
                    and method == "describe"
                    and payload == {}
                    and timeout == 5.0
                    and idempotent is True
                ):
                    raise AssertionError((target, method, payload, timeout, idempotent))

        gateway = ParallelDescribeGateway()
        manager = self.make_manager(gateway)
        diagnostics = io.StringIO()
        with contextlib.redirect_stdout(diagnostics):
            candidates = manager.refresh_catalog()

        self.assertEqual(gateway.max_active, 2)
        for concurrent_pair in (
            (
                "org.example.parallel:broken",
                "org.example.parallel:slow-b",
            ),
            (
                "org.example.parallel:slow-c",
                "org.example.parallel:slow-a",
            ),
        ):
            self.assertLess(
                max(gateway.started[item] for item in concurrent_pair),
                min(gateway.finished[item] for item in concurrent_pair),
            )
        self.assertEqual(
            [candidate.component for candidate in candidates],
            [
                "org.example.parallel:slow-b",
                "org.example.parallel:slow-c",
                "org.example.parallel:slow-a",
            ],
        )
        self.assertIn(
            "ignored invalid provider org.example.parallel:broken",
            diagnostics.getvalue(),
        )

    def test_domainless_catalog_preserves_compact_v1_candidate_shape(self) -> None:
        manager = self.make_manager()
        listed = manager.handle("list_providers", {})
        power = next(item for item in listed["providers"] if item["domain"] == "power")
        self.assertEqual(
            set(power["candidates"][0]),
            {"component", "name", "version", "priority"},
        )

    def test_get_provider_can_probe_one_exact_candidate(self) -> None:
        gateway = FakeGateway()
        manager = self.make_manager(gateway)
        result = manager.handle("get_provider", {
            "domain": "power",
            "component": "org.example.low:power",
            "probe": True,
        })
        self.assertEqual(result["provider"]["component"], "org.example.low:power")
        self.assertEqual(result["provider"]["health"]["status"], "available")
        inventory_targets = [target for target, method, _, _ in gateway.calls if method == "inventory"]
        self.assertEqual(inventory_targets, ["component:org.example.low:power"])

    def test_legacy_provider_without_capabilities_gets_v1_baseline(self) -> None:
        gateway = FakeGateway()
        gateway.include_capabilities = False
        manager = self.make_manager(gateway)
        listed = manager.handle("list_providers", {"domain": "power"})
        self.assertEqual(
            listed["providers"][0]["candidates"][0]["capabilities"],
            ["power.inventory", "power.state.read"],
        )

    def test_safe_selection_preflight_and_explicit_unavailable_override(self) -> None:
        gateway = FakeGateway()
        gateway.unavailable_inventory.add("org.example.low:power")
        manager = self.make_manager(gateway)
        with self.assertRaises(UnavailableError) as caught:
            manager.handle("select_provider", {
                "domain": "power",
                "component": "org.example.low:power",
            })
        self.assertEqual(caught.exception.details["component"], "org.example.low:power")
        self.assertEqual(manager._selections, {})
        self.assertEqual(manager._revision, 0)

        selected = manager.handle("select_provider", {
            "domain": "power",
            "component": "org.example.low:power",
            "allow_unavailable": True,
        })
        self.assertEqual(selected["revision"], 1)
        self.assertEqual(selected["providers"][0]["selection"], "manual")

    def test_unavailable_override_never_accepts_invalid_provider_protocol(self) -> None:
        gateway = FakeGateway()
        gateway.malformed = True
        manager = self.make_manager(gateway)
        with self.assertRaises(UnavailableError) as caught:
            manager.handle("select_provider", {
                "domain": "power",
                "component": "org.example.low:power",
                "allow_unavailable": True,
            })
        self.assertEqual(caught.exception.details["error_code"], "HAL_BAD_PAYLOAD")
        self.assertEqual(manager._selections, {})

    def test_selection_compare_and_swap_prevents_stale_settings_write(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SelectionStore(Path(temporary.name) / "selection.json")
        manager = HalManager(FakeGateway(), store)
        manager.handle("select_provider", {
            "domain": "power",
            "component": "org.example.low:power",
            "expected_revision": 0,
        })
        with self.assertRaises(ConflictError) as caught:
            manager.handle("select_provider", {
                "domain": "power",
                "component": "org.example.high:power",
                "expected_revision": 0,
            })
        self.assertEqual(caught.exception.code, "HAL_CONFLICT")
        self.assertEqual(store.load(), {"power": "org.example.low:power"})

    def test_concurrent_compare_and_swap_allows_exactly_one_commit(self) -> None:
        preflight = threading.Barrier(2)

        class RacingManager(HalManager):
            def _probe_candidate(self, *args, **kwargs):
                result = super()._probe_candidate(*args, **kwargs)
                preflight.wait(timeout=2)
                return result

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SelectionStore(Path(temporary.name) / "selection.json")
        manager = RacingManager(FakeGateway(), store)
        outcomes = []

        def choose(component):
            try:
                outcomes.append(manager.handle("select_provider", {
                    "domain": "power",
                    "component": component,
                    "expected_revision": 0,
                }))
            except Exception as exc:
                outcomes.append(exc)

        threads = [
            threading.Thread(target=choose, args=("org.example.high:power",)),
            threading.Thread(target=choose, args=("org.example.low:power",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)
        self.assertEqual(manager._revision, 1)
        self.assertIn(
            store.load().get("power"),
            {"org.example.high:power", "org.example.low:power"},
        )

    def test_selection_survives_manager_restart(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SelectionStore(Path(temporary.name) / "selection.json")
        first = HalManager(FakeGateway(), store)
        first.handle("select_provider", {
            "domain": "power",
            "component": "org.example.low:power",
        })

        restarted = HalManager(FakeGateway(), store)
        listed = restarted.handle("list_providers", {"domain": "power"})
        self.assertEqual(listed["providers"][0]["selection"], "manual")
        self.assertEqual(listed["providers"][0]["active"], "org.example.low:power")

    def test_select_and_reset_are_visible_to_filtered_watchers(self) -> None:
        manager = self.make_manager()

        selected = manager.handle("select_provider", {
            "domain": "power",
            "component": "org.example.low:power",
        })
        reset = manager.handle("reset_provider", {"domain": "power"})
        watched = manager.handle("watch", {
            "after_revision": 0,
            "timeout_ms": 0,
            "domains": ["power"],
        })

        self.assertEqual(selected["revision"], 1)
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(
            [(item["kind"], item["selection"]) for item in watched["events"]],
            [
                ("provider-selected", "manual"),
                ("provider-reset", "automatic"),
            ],
        )

    def test_set_state_records_revision_event(self) -> None:
        manager = self.make_manager()
        events = []
        manager.set_event_sink(lambda topic, payload: events.append((topic, payload)))
        changed = manager.handle("set_state", {
            "id": "power:BAT0",
            "changes": {"limit": 55},
        })
        self.assertEqual(changed["revision"], 1)
        watched = manager.handle("watch", {"after_revision": 0, "timeout_ms": 0})
        self.assertEqual(watched["events"][0]["kind"], "state-changed")
        self.assertEqual(events[0][0], "msys.hal.changed")

    def test_provider_typed_error_survives_manager_boundary(self) -> None:
        gateway = FakeGateway()
        gateway.state_error = ("HAL_READ_ONLY", "device is read only")
        manager = self.make_manager(gateway)
        manager.handle("inventory", {"domains": ["power"]})
        with self.assertRaises(HalError) as caught:
            manager.handle("set_state", {
                "id": "power:BAT0",
                "changes": {"limit": 55},
            })
        self.assertEqual(caught.exception.code, "HAL_READ_ONLY")
        self.assertEqual(caught.exception.details["provider_details"], {"id": "power:BAT0"})

    def test_transport_failure_is_typed_unavailable(self) -> None:
        class OfflineGateway:
            def call(self, *_args, **_kwargs):
                raise ConnectionRefusedError("offline")

        manager = self.make_manager(OfflineGateway())
        with self.assertRaises(UnavailableError) as caught:
            manager.handle("inventory", {"domains": ["power"]})
        self.assertEqual(caught.exception.code, "HAL_UNAVAILABLE")
        self.assertNotIn("offline", caught.exception.message)

    def test_provider_selection_persistence_failure_is_typed_and_atomic(self) -> None:
        class FailingStore(SelectionStore):
            def save(self, selections):
                raise OSError("read-only state root")

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = FailingStore(Path(temporary.name) / "selection.json")
        manager = HalManager(FakeGateway(), store)
        with self.assertRaises(PersistenceError) as caught:
            manager.handle("select_provider", {
                "domain": "power",
                "component": "org.example.low:power",
            })
        self.assertEqual(caught.exception.code, "HAL_PERSISTENCE_ERROR")
        self.assertEqual(manager._selections, {})

    def test_watch_long_poll_wakes_and_filters(self) -> None:
        manager = self.make_manager()
        result = {}

        def watcher():
            result.update(manager.handle("watch", {
                "after_revision": 0,
                "timeout_ms": 1000,
                "domains": ["display"],
            }))

        thread = threading.Thread(target=watcher)
        thread.start()
        time.sleep(0.03)
        manager._record_event("state-changed", "power", identifier="power:BAT0")
        time.sleep(0.03)
        self.assertTrue(thread.is_alive())
        manager._record_event("state-changed", "display", identifier="display:primary")
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([item["domain"] for item in result["events"]], ["display"])

    def test_poll_fingerprint_ignores_display_session_heartbeat_only(self) -> None:
        values = {
            "display_session": {
                "provider": "org.example:display",
                "generation": 4,
                "observed_at_unix_ms": 100,
                "geometry": {"width": 320, "height": 480},
            }
        }

        first = HalManager._stable_poll_values(values)
        values["display_session"]["observed_at_unix_ms"] = 200
        second = HalManager._stable_poll_values(values)

        self.assertEqual(first, second)
        self.assertNotIn("observed_at_unix_ms", first["display_session"])
        values["display_session"]["generation"] = 5
        self.assertNotEqual(first, HalManager._stable_poll_values(values))

    def test_stop_interrupts_long_watch(self) -> None:
        manager = self.make_manager()
        result = {}
        thread = threading.Thread(target=lambda: result.update(manager.handle("watch", {
            "after_revision": 0,
            "timeout_ms": 25_000,
        })))
        thread.start()
        time.sleep(0.03)
        manager.stop()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["events"], [])

    def test_missing_providers_are_not_exceptional_for_inventory(self) -> None:
        manager = self.make_manager(FakeGateway(providers=False))
        inventory = manager.handle("inventory", {"domains": ["thermal"]})
        self.assertEqual(inventory["domains"][0]["status"], "unavailable")
        self.assertEqual(inventory["domains"][0]["reason"], "no-provider")
        self.assertEqual(inventory["devices"], [])

    def test_provider_output_has_strict_field_boundary(self) -> None:
        gateway = FakeGateway()
        gateway.malformed = True
        manager = self.make_manager(gateway)
        inventory = manager.handle("inventory", {"domains": ["power"]})
        self.assertEqual(inventory["domains"][0]["status"], "unavailable")
        self.assertEqual(inventory["domains"][0]["reason"], "provider-failed")

    def test_manager_payload_rejects_unknown_and_wrong_types(self) -> None:
        manager = self.make_manager()
        with self.assertRaises(ValidationError):
            manager.handle("inventory", {"raw_path": "/sys"})
        with self.assertRaises(ValidationError):
            manager.handle("watch", {"after_revision": True})
        with self.assertRaises(ValidationError):
            manager.handle("set_state", {"id": "power:BAT0", "changes": {}})
        with self.assertRaises(ValidationError):
            manager.handle("list_providers", {"probe": True})
        with self.assertRaises(ValidationError):
            manager.handle("select_provider", {
                "domain": "power",
                "component": "org.example.low:power",
                "expected_revision": True,
            })


class SelectionStoreTests(unittest.TestCase):
    def test_corrupt_or_oversized_state_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "selection.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(SelectionStore(path).load(), {})
            path.write_bytes(b"x" * (65 * 1024))
            self.assertEqual(SelectionStore(path).load(), {})


if __name__ == "__main__":
    unittest.main()
