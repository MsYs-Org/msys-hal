from __future__ import annotations

import argparse
import os
from typing import Any

from . import __version__
from .errors import HalError, ValidationError
from .linux import Backend, linux_backends
from .mipc import ComponentServer, PublicGateway
from .validation import (
    bounded_string,
    capability,
    changes,
    component_id,
    device_id,
    domain,
    object_payload,
    semantic_version,
    string_list,
)


PROVIDER_INTERFACE = "org.msys.hal.provider.v1"
MAX_PROVIDER_DOMAINS = 8
MAX_PROVIDER_CAPABILITIES = 32

LINUX_DOMAIN_CAPABILITIES = {
    "power": ("power.battery", "power.capacity", "power.status"),
    "thermal": ("thermal.temperature",),
    "backlight": (
        "backlight.brightness",
        "backlight.brightness.percent",
        "backlight.state.write",
    ),
    "display": (
        "display.layout.insets",
        "display.layout.orientation",
        "display.layout.profile",
        "display.session.inspect",
        "display.state.write",
    ),
    "input": (
        "input.device.inventory",
        "input.transform.inspect",
        "input.transform.orientation",
        "input.state.write",
    ),
    "network": (
        "network.interface.inventory",
        "network.interface.state",
        "network.wifi.configured",
        "network.wifi.connect",
        "network.wifi.disconnect",
        "network.wifi.forget",
        "network.wifi.scan",
        "network.state.write",
    ),
    "bluetooth": (
        "bluetooth.controller.inventory",
        "bluetooth.pairing.unavailable",
        "bluetooth.radio.power",
        "bluetooth.radio.state",
        "bluetooth.state.write",
    ),
}


class ProviderService:
    def __init__(
        self,
        backends: dict[str, Backend],
        *,
        provider_id: str,
        name: str = "Linux safe provider",
        version: str = __version__,
        capabilities: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if not backends:
            raise ValueError("provider requires at least one backend")
        if len(backends) > MAX_PROVIDER_DOMAINS:
            raise ValueError(f"provider supports at most {MAX_PROVIDER_DOMAINS} domains")
        normalized_backends: dict[str, Backend] = {}
        for key, backend in backends.items():
            normalized_key = domain(key, "backend domain")
            if getattr(backend, "domain", None) != normalized_key:
                raise ValueError(f"backend {key!r} has a mismatched domain")
            normalized_backends[normalized_key] = backend
        self.backends = normalized_backends
        self.provider_id = component_id(provider_id, "provider_id")
        self.name = bounded_string(name, "provider name", maximum=128)
        self.version = semantic_version(version, "provider version")
        declared = [
            f"{item}.inventory"
            for item in sorted(self.backends)
        ] + [
            f"{item}.state.read"
            for item in sorted(self.backends)
        ]
        if capabilities is not None:
            if not isinstance(capabilities, (list, tuple)):
                raise ValueError("capabilities must be a list or tuple")
            if len(capabilities) > MAX_PROVIDER_CAPABILITIES:
                raise ValueError(
                    f"provider declares at most {MAX_PROVIDER_CAPABILITIES} capabilities"
                )
            declared.extend(capabilities)
        normalized_capabilities: list[str] = []
        prefixes = tuple(f"{item}." for item in self.backends)
        for index, raw_capability in enumerate(declared):
            item = capability(raw_capability, f"capabilities[{index}]")
            if not item.startswith(prefixes):
                raise ValueError(f"capability {item!r} has no matching backend domain")
            if item not in normalized_capabilities:
                normalized_capabilities.append(item)
        if len(normalized_capabilities) > MAX_PROVIDER_CAPABILITIES:
            raise ValueError(
                f"provider declares at most {MAX_PROVIDER_CAPABILITIES} capabilities"
            )
        self.capabilities = tuple(sorted(normalized_capabilities))

    def handle(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "describe":
            object_payload(payload, allowed=())
            return {
                "schema": PROVIDER_INTERFACE,
                "provider": {
                    "id": self.provider_id,
                    "name": self.name,
                    "version": self.version,
                },
                "domains": sorted(self.backends),
                "capabilities": list(self.capabilities),
            }
        if method == "inventory":
            request = object_payload(payload, allowed=("domains",))
            selected = sorted(self.backends)
            if "domains" in request:
                requested = [domain(item, f"domains[{index}]") for index, item in enumerate(
                    string_list(request["domains"], "domains", maximum_items=32)
                )]
                unknown = sorted(set(requested) - set(self.backends))
                if unknown:
                    raise ValidationError(f"provider does not implement domains: {', '.join(unknown)}")
                selected = requested
            inventories = [self.backends[item].inventory() for item in selected]
            return {
                "schema": PROVIDER_INTERFACE,
                "provider": self.provider_id,
                "domains": [
                    {key: value for key, value in inventory.items() if key != "devices"}
                    for inventory in inventories
                ],
                "devices": [
                    device
                    for inventory in inventories
                    for device in inventory.get("devices", [])
                ],
            }
        if method == "get_state":
            request = object_payload(payload, allowed=("id",), required=("id",))
            identifier = device_id(request["id"])
            backend = self._backend_for(identifier)
            return {
                "schema": PROVIDER_INTERFACE,
                "provider": self.provider_id,
                "state": backend.get_state(identifier),
            }
        if method == "set_state":
            request = object_payload(payload, allowed=("id", "changes"), required=("id", "changes"))
            identifier = device_id(request["id"])
            requested_changes = changes(request["changes"])
            backend = self._backend_for(identifier)
            return {
                "schema": PROVIDER_INTERFACE,
                "provider": self.provider_id,
                "state": backend.set_state(identifier, requested_changes),
            }
        raise HalError("HAL_UNKNOWN_METHOD", f"unknown provider method {method!r}")

    def _backend_for(self, identifier: str) -> Backend:
        requested_domain = identifier.split(":", 1)[0]
        backend = self.backends.get(requested_domain)
        if backend is None:
            raise ValidationError(f"provider does not implement domain {requested_domain!r}")
        return backend


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MSYS dependency-free Linux HAL provider")
    parser.add_argument(
        "--domains",
        # The package runs one bounded domain per manifest component.  Keep the
        # legacy combined development default below the v1 capability limit;
        # radio domains are selected explicitly by their on-demand components.
        default=os.environ.get("MSYS_HAL_DOMAINS", "power,thermal,backlight,display,input"),
        help="comma-separated provider domains",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected = [item.strip() for item in args.domains.split(",") if item.strip()]
    if not selected or len(selected) != len(set(selected)):
        raise SystemExit("--domains must contain unique domain names")
    gateway = PublicGateway()
    available = linux_backends(gateway)
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise SystemExit(f"unknown HAL domains: {', '.join(unknown)}")
    provider_id = os.environ.get("MSYS_COMPONENT_ID", "org.msys.hal.linux:provider")
    service = ProviderService(
        {key: available[key] for key in selected},
        provider_id=provider_id,
        capabilities=[
            item
            for key in selected
            for item in LINUX_DOMAIN_CAPABILITIES[key]
        ],
    )
    server = ComponentServer(service.handle)
    return server.run(ready_event=(
        "msys.hal.provider.ready",
        {"provider": provider_id, "domains": sorted(selected)},
    ))


if __name__ == "__main__":
    raise SystemExit(main())
