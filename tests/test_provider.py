from __future__ import annotations

import unittest

from msys_hal.errors import HalError, ValidationError
from msys_hal.provider import PROVIDER_INTERFACE, ProviderService


class FakeBackend:
    domain = "power"

    def inventory(self):
        return {
            "domain": "power",
            "status": "available",
            "devices": [{
                "id": "power:test0",
                "domain": "power",
                "name": "test0",
                "available": True,
                "mutable": ["level"],
                "metadata": {"type": "test"},
            }],
        }

    def get_state(self, identifier):
        return {
            "id": identifier,
            "domain": "power",
            "available": True,
            "values": {"level": 2},
            "mutable": ["level"],
        }

    def set_state(self, identifier, changes):
        result = self.get_state(identifier)
        result["values"].update(changes)
        return result


class ProviderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ProviderService(
            {"power": FakeBackend()},
            provider_id="org.example.hal:power",
            name="Test provider",
            version="1.2.3",
        )

    def test_describe_and_inventory_have_stable_schema(self) -> None:
        description = self.service.handle("describe", {})
        self.assertEqual(description["schema"], PROVIDER_INTERFACE)
        self.assertEqual(description["domains"], ["power"])
        self.assertEqual(
            description["capabilities"],
            ["power.inventory", "power.state.read"],
        )
        inventory = self.service.handle("inventory", {"domains": ["power"]})
        self.assertEqual(inventory["devices"][0]["id"], "power:test0")

    def test_declared_capability_must_belong_to_a_backend_domain(self) -> None:
        with self.assertRaises(ValueError):
            ProviderService(
                {"power": FakeBackend()},
                provider_id="org.example.hal:power",
                capabilities=["thermal.temperature"],
            )

    def test_capability_declaration_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            ProviderService(
                {"power": FakeBackend()},
                provider_id="org.example.hal:power",
                capabilities=[f"power.feature.{index}" for index in range(33)],
            )

    def test_state_calls_are_language_neutral_data(self) -> None:
        state = self.service.handle("get_state", {"id": "power:test0"})
        self.assertEqual(state["state"]["values"], {"level": 2})
        changed = self.service.handle(
            "set_state",
            {"id": "power:test0", "changes": {"level": 5}},
        )
        self.assertEqual(changed["state"]["values"]["level"], 5)

    def test_unknown_fields_domains_and_methods_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.handle("inventory", {"path": "/sys"})
        with self.assertRaises(ValidationError):
            self.service.handle("inventory", {"domains": ["thermal"]})
        with self.assertRaises(ValidationError):
            self.service.handle("get_state", {"id": "thermal:test0"})
        with self.assertRaises(HalError) as raised:
            self.service.handle("raw_sysfs_write", {})
        self.assertEqual(raised.exception.code, "HAL_UNKNOWN_METHOD")
        with self.assertRaises(HalError) as raised:
            self.service.handle("watch", {"after_revision": 0})
        self.assertEqual(raised.exception.code, "HAL_UNKNOWN_METHOD")


if __name__ == "__main__":
    unittest.main()
