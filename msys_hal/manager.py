from __future__ import annotations

import argparse
import collections
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .errors import (
    ConflictError,
    HalError,
    PersistenceError,
    ProviderError,
    UnavailableError,
    ValidationError,
)
from .mipc import ComponentServer, PublicGateway
from .validation import (
    COMPONENT_RE,
    bounded_string,
    capability,
    changes,
    component_id,
    device_id,
    domain,
    ensure_bounded_json,
    integer,
    object_payload,
    safe_scalar_map,
    semantic_version,
    string_list,
)


MANAGER_INTERFACE = "org.msys.hal.manager.v1"
PROVIDER_INTERFACE = "org.msys.hal.provider.v1"
KNOWN_DOMAINS = (
    "power",
    "thermal",
    "backlight",
    "display",
    "display-output",
    "input",
    "network",
    "bluetooth",
)
STATUS_VALUES = {"available", "unavailable", "degraded"}
MAX_PROVIDER_CANDIDATES = 16
MAX_PROVIDER_DESCRIBE_WORKERS = 2
MAX_PROVIDER_DOMAINS = 8
MAX_PROVIDER_CAPABILITIES = 32
PASSTHROUGH_PROVIDER_ERRORS = {
    "CALL_TIMEOUT",
    "HAL_BAD_PAYLOAD",
    "HAL_READ_ONLY",
    "HAL_UNAVAILABLE",
}


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


@dataclass(frozen=True, slots=True)
class Candidate:
    component: str
    target: str
    priority: int
    domains: tuple[str, ...]
    name: str
    version: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    status: str
    reason: str
    checked_at_unix_ms: int
    latency_ms: int
    device_count: int | None = None
    mutable: tuple[str, ...] = ()
    mutable_truncated: bool = False
    error_code: str | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
            "checked_at_unix_ms": self.checked_at_unix_ms,
            "latency_ms": self.latency_ms,
        }
        if self.device_count is not None:
            result["device_count"] = self.device_count
            result["mutable"] = list(self.mutable)
            result["mutable_truncated"] = self.mutable_truncated
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


class SelectionStore:
    """Tiny atomic preference store; it is policy state, never a permission DB."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, str]:
        try:
            data = self.path.read_bytes()
        except OSError:
            return {}
        if len(data) > 64 * 1024:
            return {}
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict) or value.get("schema") != "msys.hal.selection.v1":
            return {}
        selections = value.get("selections")
        if not isinstance(selections, dict) or len(selections) > 64:
            return {}
        result: dict[str, str] = {}
        for raw_domain, raw_component in selections.items():
            if (
                isinstance(raw_domain, str)
                and re.fullmatch(r"[a-z][a-z0-9.-]{0,63}", raw_domain)
                and isinstance(raw_component, str)
                and COMPONENT_RE.fullmatch(raw_component)
            ):
                result[raw_domain] = raw_component
        return result

    def save(self, selections: dict[str, str]) -> None:
        payload = {
            "schema": "msys.hal.selection.v1",
            "selections": dict(sorted(selections.items())),
        }
        ensure_bounded_json(payload, label="HAL provider selection")
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as stream:
                temp_name = stream.name
                os.chmod(temp_name, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass


class HalManager:
    def __init__(
        self,
        gateway: Gateway,
        store: SelectionStore,
        *,
        catalog_ttl: float = 30.0,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.catalog_ttl = max(0.0, min(float(catalog_ttl), 60.0))
        self._lock = threading.RLock()
        self._catalog_lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._candidates: tuple[Candidate, ...] = ()
        self._catalog_at = 0.0
        self._selections = store.load()
        self._device_routes: dict[str, Candidate] = {}
        self._active_providers: dict[str, str | None] = {}
        self._provider_health: dict[tuple[str, str], ProviderHealth] = {}
        self._revision = 0
        self._journal: collections.deque[dict[str, Any]] = collections.deque(maxlen=256)
        self._event_sink: Callable[[str, dict[str, Any]], None] | None = None
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._last_fingerprint: str | None = None

    def set_event_sink(self, sink: Callable[[str, dict[str, Any]], None]) -> None:
        self._event_sink = sink

    def start_polling(self, interval: float) -> None:
        if self._poll_thread is not None:
            return
        interval = max(1.0, min(float(interval), 300.0))

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.poll_once()
                except Exception as exc:
                    print(f"msys-hal-manager: poll failed: {exc}", flush=True)

        self._poll_thread = threading.Thread(target=loop, name="msys-hal-watch", daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        with self._condition:
            self._stop.set()
            self._condition.notify_all()

    def handle(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "inventory":
            request = object_payload(payload, allowed=("domains", "refresh"))
            domains = self._requested_domains(request.get("domains"))
            refresh = request.get("refresh", False)
            if not isinstance(refresh, bool):
                raise ValidationError("refresh must be a boolean")
            return self.inventory(domains=domains, refresh=refresh)
        if method == "get_state":
            request = object_payload(payload, allowed=("id", "refresh"), required=("id",))
            refresh = request.get("refresh", False)
            if not isinstance(refresh, bool):
                raise ValidationError("refresh must be a boolean")
            return self.get_state(device_id(request["id"]), refresh=refresh)
        if method == "set_state":
            request = object_payload(payload, allowed=("id", "changes"), required=("id", "changes"))
            return self.set_state(device_id(request["id"]), changes(request["changes"]))
        if method == "list_providers":
            request = object_payload(payload, allowed=("domain", "refresh", "probe"))
            requested_domain = domain(request["domain"]) if "domain" in request else None
            refresh = request.get("refresh", False)
            if not isinstance(refresh, bool):
                raise ValidationError("refresh must be a boolean")
            probe = request.get("probe", False)
            if not isinstance(probe, bool):
                raise ValidationError("probe must be a boolean")
            if probe and requested_domain is None:
                raise ValidationError("probe requires a domain")
            return self.list_providers(requested_domain, refresh=refresh, probe=probe)
        if method == "get_provider":
            request = object_payload(
                payload,
                allowed=("domain", "component", "refresh", "probe"),
                required=("domain",),
            )
            requested_domain = domain(request["domain"])
            selected_component = (
                component_id(request["component"])
                if "component" in request
                else None
            )
            refresh = request.get("refresh", False)
            probe = request.get("probe", False)
            if not isinstance(refresh, bool):
                raise ValidationError("refresh must be a boolean")
            if not isinstance(probe, bool):
                raise ValidationError("probe must be a boolean")
            return self.get_provider(
                requested_domain,
                selected_component,
                refresh=refresh,
                probe=probe,
            )
        if method == "select_provider":
            request = object_payload(
                payload,
                allowed=("domain", "component", "expected_revision", "allow_unavailable"),
                required=("domain",),
            )
            requested_domain = domain(request["domain"])
            raw_component = request.get("component")
            selected_component = None if raw_component is None else component_id(raw_component)
            expected_revision = self._expected_revision(request)
            allow_unavailable = request.get("allow_unavailable", False)
            if not isinstance(allow_unavailable, bool):
                raise ValidationError("allow_unavailable must be a boolean")
            if selected_component is None and allow_unavailable:
                raise ValidationError("allow_unavailable requires a component")
            return self.select_provider(
                requested_domain,
                selected_component,
                expected_revision=expected_revision,
                allow_unavailable=allow_unavailable,
            )
        if method == "reset_provider":
            request = object_payload(
                payload,
                allowed=("domain", "expected_revision"),
                required=("domain",),
            )
            return self.select_provider(
                domain(request["domain"]),
                None,
                expected_revision=self._expected_revision(request),
            )
        if method == "watch":
            request = object_payload(payload, allowed=("after_revision", "timeout_ms", "domains"))
            after_revision = integer(
                request.get("after_revision", 0),
                "after_revision",
                minimum=0,
                maximum=2**63 - 1,
            )
            timeout_ms = integer(
                request.get("timeout_ms", 0),
                "timeout_ms",
                minimum=0,
                maximum=25_000,
            )
            domains = self._requested_domains(request.get("domains"), include_known=False)
            return self.watch(after_revision, timeout_ms, domains)
        raise HalError("HAL_UNKNOWN_METHOD", f"unknown manager method {method!r}")

    @staticmethod
    def _expected_revision(request: dict[str, Any]) -> int | None:
        if "expected_revision" not in request:
            return None
        return integer(
            request["expected_revision"],
            "expected_revision",
            minimum=0,
            maximum=2**63 - 1,
        )

    def _requested_domains(self, raw: Any, *, include_known: bool = True) -> list[str] | None:
        if raw is None:
            return None
        result = [domain(item, f"domains[{index}]") for index, item in enumerate(
            string_list(raw, "domains", maximum_items=32)
        )]
        if not result and include_known:
            raise ValidationError("domains must not be empty")
        return result

    def _unwrap(self, response: dict[str, Any], *, operation: str) -> dict[str, Any]:
        if not isinstance(response, dict) or response.get("type") != "return":
            code = (
                str(response.get("code", "HAL_PROVIDER_ERROR"))[:64]
                if isinstance(response, dict)
                else "HAL_PROVIDER_ERROR"
            )
            message = (
                str(response.get("message", f"{operation} failed"))[:512]
                if isinstance(response, dict)
                else f"{operation} failed"
            )
            raw_details = response.get("payload") if isinstance(response, dict) else None
            details: dict[str, Any] = {"operation": operation}
            if isinstance(raw_details, dict):
                details["provider_details"] = raw_details
            if code in PASSTHROUGH_PROVIDER_ERRORS:
                raise HalError(code, message, details=details)
            raise ProviderError(
                f"{operation} failed",
                details={
                    **details,
                    "provider_code": code,
                    "provider_message": message[:256],
                },
            )
        payload = response.get("payload")
        if not isinstance(payload, dict):
            raise ProviderError(f"{operation} returned a non-object")
        ensure_bounded_json(payload, label=f"{operation} response", max_depth=8, max_items=2048)
        return payload

    def _invoke(
        self,
        target: str,
        method: str,
        payload: dict[str, Any],
        *,
        operation: str,
        timeout: float,
        idempotent: bool,
    ) -> dict[str, Any]:
        try:
            response = self.gateway.call(
                target,
                method,
                payload,
                timeout=timeout,
                idempotent=idempotent,
            )
        except HalError:
            raise
        except (EOFError, OSError, TimeoutError) as exc:
            raise UnavailableError(
                f"{operation} is unavailable",
                details={"operation": operation, "cause": type(exc).__name__},
            ) from exc
        except Exception as exc:
            raise ProviderError(
                f"{operation} could not be completed",
                details={"operation": operation, "cause": type(exc).__name__},
            ) from exc
        return self._unwrap(response, operation=operation)

    def _describe_candidate(self, component: str, priority: int) -> Candidate:
        target = f"component:{component}"
        description = self._invoke(
            target,
            "describe",
            {},
            operation=f"describe {component}",
            timeout=5.0,
            idempotent=True,
        )
        description = object_payload(
            description,
            allowed=("schema", "provider", "domains", "capabilities"),
            required=("schema", "provider", "domains"),
            label="provider description",
        )
        if description.get("schema") != PROVIDER_INTERFACE:
            raise ProviderError("provider schema mismatch")
        described_domains = tuple(sorted(
            domain(item, "provider domain")
            for item in string_list(
                description.get("domains"),
                "provider domains",
                maximum_items=MAX_PROVIDER_DOMAINS,
            )
        ))
        if not described_domains:
            raise ProviderError("provider must describe at least one domain")
        provider_info = object_payload(
            description["provider"],
            allowed=("id", "name", "version"),
            required=("id", "name", "version"),
            label="provider identity",
        )
        if provider_info.get("id") != component:
            raise ProviderError("provider description id does not match its component")
        name = bounded_string(provider_info["name"], "provider name", maximum=128)
        version = semantic_version(provider_info["version"], "provider version")
        raw_capabilities = description.get("capabilities")
        if raw_capabilities is None:
            capabilities = tuple(
                feature
                for described_domain in described_domains
                for feature in (
                    f"{described_domain}.inventory",
                    f"{described_domain}.state.read",
                )
            )
        else:
            declared_capabilities = {
                capability(item, f"provider capabilities[{index}]")
                for index, item in enumerate(string_list(
                    raw_capabilities,
                    "provider capabilities",
                    maximum_items=MAX_PROVIDER_CAPABILITIES,
                    item_maximum=128,
                ))
            }
            prefixes = tuple(f"{item}." for item in described_domains)
            if any(not item.startswith(prefixes) for item in declared_capabilities):
                raise ProviderError("provider capability has an undeclared domain")
            declared_capabilities.update(
                feature
                for described_domain in described_domains
                for feature in (
                    f"{described_domain}.inventory",
                    f"{described_domain}.state.read",
                )
            )
            if len(declared_capabilities) > MAX_PROVIDER_CAPABILITIES:
                raise ProviderError("provider declares too many capabilities")
            capabilities = tuple(sorted(declared_capabilities))
        return Candidate(
            component=component,
            target=target,
            priority=priority,
            domains=described_domains,
            name=name,
            version=version,
            capabilities=capabilities,
        )

    def refresh_catalog(self, *, force: bool = False) -> tuple[Candidate, ...]:
        with self._lock:
            if not force and self._candidates and time.monotonic() - self._catalog_at < self.catalog_ttl:
                return self._candidates
        with self._catalog_lock:
            with self._lock:
                if not force and self._candidates and time.monotonic() - self._catalog_at < self.catalog_ttl:
                    return self._candidates
            discovery = self._invoke(
                "msys.core",
                "discover",
                {"kind": "interface", "name": PROVIDER_INTERFACE},
                operation="provider discovery",
                timeout=5.0,
                idempotent=True,
            )
            services = discovery.get("services", [])
            if not isinstance(services, list) or len(services) > 16:
                raise ProviderError("provider discovery returned invalid services")
            provider_rows: list[dict[str, Any]] = []
            for service in services:
                if not isinstance(service, dict) or service.get("name") != PROVIDER_INTERFACE:
                    continue
                raw_providers = service.get("providers", [])
                if isinstance(raw_providers, list):
                    provider_rows.extend(item for item in raw_providers if isinstance(item, dict))
                    if len(provider_rows) >= 128:
                        provider_rows = provider_rows[:128]
                        break
            provider_rows.sort(key=lambda row: (
                -row.get("priority", 0)
                if isinstance(row.get("priority", 0), int)
                and not isinstance(row.get("priority", 0), bool)
                else 0,
                str(row.get("component", ""))[:192],
            ))
            provider_specs: list[tuple[str, int]] = []
            seen: set[str] = set()
            for row in provider_rows:
                raw_component = row.get("component")
                if not isinstance(raw_component, str) or not COMPONENT_RE.fullmatch(raw_component):
                    continue
                if raw_component in seen:
                    continue
                seen.add(raw_component)
                raw_priority = row.get("priority", 0)
                if not isinstance(raw_priority, int) or isinstance(raw_priority, bool):
                    continue
                priority = max(-10_000, min(raw_priority, 10_000))
                provider_specs.append((raw_component, priority))
            candidates: list[Candidate] = []
            if provider_specs:
                worker_count = min(MAX_PROVIDER_DESCRIBE_WORKERS, len(provider_specs))
                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="msys-hal-describe",
                ) as executor:
                    for offset in range(0, len(provider_specs), worker_count):
                        batch = provider_specs[offset:offset + worker_count]
                        futures = [
                            executor.submit(self._describe_candidate, component, priority)
                            for component, priority in batch
                        ]
                        for (component, _priority), future in zip(batch, futures):
                            try:
                                candidate = future.result()
                            except Exception as exc:
                                print(
                                    f"msys-hal-manager: ignored invalid provider {component}: {exc}",
                                    flush=True,
                                )
                                continue
                            if len(candidates) < MAX_PROVIDER_CANDIDATES:
                                candidates.append(candidate)
                        if len(candidates) >= MAX_PROVIDER_CANDIDATES:
                            break
            candidates.sort(key=lambda item: (-item.priority, item.component))
            with self._lock:
                previous_identity = {
                    candidate.component: (
                        candidate.version,
                        candidate.domains,
                        candidate.capabilities,
                    )
                    for candidate in self._candidates
                }
                new_identity = {
                    candidate.component: (
                        candidate.version,
                        candidate.domains,
                        candidate.capabilities,
                    )
                    for candidate in candidates
                }
                self._candidates = tuple(candidates)
                self._catalog_at = time.monotonic()
                live = {
                    (candidate.component, item)
                    for candidate in candidates
                    for item in candidate.domains
                }
                self._provider_health = {
                    key: value
                    for key, value in self._provider_health.items()
                    if key in live
                    and previous_identity.get(key[0]) == new_identity.get(key[0])
                }
                return self._candidates

    def _candidates_for(self, requested_domain: str, *, force: bool = False) -> list[Candidate]:
        return [
            candidate
            for candidate in self.refresh_catalog(force=force)
            if requested_domain in candidate.domains
        ]

    def _ordered_for(self, requested_domain: str, *, force: bool = False) -> tuple[list[Candidate], str]:
        candidates = self._candidates_for(requested_domain, force=force)
        with self._lock:
            preferred = self._selections.get(requested_domain)
        if preferred:
            for candidate in candidates:
                if candidate.component == preferred:
                    return [candidate], "manual"
            return candidates, "stale"
        return candidates, "automatic"

    def inventory(self, *, domains: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
        candidates = self.refresh_catalog(force=refresh)
        available_domains = sorted(set(KNOWN_DOMAINS) | {item for candidate in candidates for item in candidate.domains})
        requested = available_domains if domains is None else domains
        domain_rows: list[dict[str, Any]] = []
        devices: list[dict[str, Any]] = []
        routes: dict[str, Candidate] = {}
        active_providers: dict[str, str | None] = {}
        for requested_domain in requested:
            ordered, selection_mode = self._ordered_for(requested_domain)
            if not ordered:
                domain_rows.append({
                    "domain": requested_domain,
                    "status": "unavailable",
                    "reason": "no-provider",
                    "selection": selection_mode,
                    "provider": None,
                })
                active_providers[requested_domain] = None
                continue
            errors: list[str] = []
            inventory_payload: dict[str, Any] | None = None
            active: Candidate | None = None
            unavailable_result: tuple[dict[str, Any], Candidate] | None = None
            for candidate in ordered:
                try:
                    normalized = self._probe_candidate(candidate, requested_domain)
                    if normalized["domain"]["status"] == "unavailable" and selection_mode != "manual":
                        if unavailable_result is None:
                            unavailable_result = (normalized, candidate)
                        continue
                    inventory_payload = normalized
                    active = candidate
                    break
                except Exception as exc:
                    errors.append(str(exc)[:160])
                    if selection_mode == "manual":
                        break
            if inventory_payload is None and unavailable_result is not None:
                inventory_payload, active = unavailable_result
            if inventory_payload is None or active is None:
                domain_rows.append({
                    "domain": requested_domain,
                    "status": "unavailable",
                    "reason": "provider-failed",
                    "selection": selection_mode,
                    "provider": ordered[0].component,
                    "error": errors[0] if errors else "provider failed",
                })
                active_providers[requested_domain] = None
                continue
            row = dict(inventory_payload["domain"])
            row.update({"provider": active.component, "selection": selection_mode})
            active_providers[requested_domain] = active.component
            domain_rows.append(row)
            for item in inventory_payload["devices"]:
                if item["id"] in routes:
                    continue
                item["provider"] = active.component
                devices.append(item)
                routes[item["id"]] = active
        with self._lock:
            self._device_routes = routes
            self._active_providers.update(active_providers)
            revision = self._revision
        return {
            "schema": MANAGER_INTERFACE,
            "revision": revision,
            "domains": domain_rows,
            "devices": devices,
        }

    def _normalize_inventory(
        self,
        raw: dict[str, Any],
        requested_domain: str,
        candidate: Candidate,
    ) -> dict[str, Any]:
        result = object_payload(
            raw,
            allowed=("schema", "provider", "domains", "devices"),
            required=("schema", "provider", "domains", "devices"),
            label="provider inventory",
        )
        if result["schema"] != PROVIDER_INTERFACE:
            raise ProviderError("provider inventory schema mismatch")
        if result["provider"] != candidate.component:
            raise ProviderError("provider inventory id does not match its component")
        raw_domains = result["domains"]
        if not isinstance(raw_domains, list) or len(raw_domains) != 1 or not isinstance(raw_domains[0], dict):
            raise ProviderError("provider inventory must return exactly one requested domain")
        domain_row = object_payload(
            raw_domains[0],
            allowed=("domain", "status", "reason"),
            required=("domain", "status"),
            label="provider domain",
        )
        if domain(domain_row["domain"]) != requested_domain:
            raise ProviderError("provider returned the wrong domain")
        status = domain_row["status"]
        if status not in STATUS_VALUES:
            raise ProviderError("provider returned an invalid status")
        normalized_domain: dict[str, Any] = {"domain": requested_domain, "status": status}
        if "reason" in domain_row:
            normalized_domain["reason"] = bounded_string(
                domain_row["reason"],
                "provider domain reason",
                maximum=256,
            )
        raw_devices = result["devices"]
        if not isinstance(raw_devices, list) or len(raw_devices) > 256:
            raise ProviderError("provider returned too many devices")
        normalized_devices = [
            self._normalize_device(item, requested_domain)
            for item in raw_devices
        ]
        return {"domain": normalized_domain, "devices": normalized_devices}

    def _probe_candidate(
        self,
        candidate: Candidate,
        requested_domain: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            raw = self._invoke(
                candidate.target,
                "inventory",
                {"domains": [requested_domain]},
                operation=f"inventory {candidate.component}",
                timeout=timeout,
                idempotent=True,
            )
            normalized = self._normalize_inventory(raw, requested_domain, candidate)
        except Exception as exc:
            code = exc.code if isinstance(exc, HalError) else "HAL_PROVIDER_ERROR"
            health = ProviderHealth(
                status="unavailable",
                reason="transport-error" if code in {"CALL_TIMEOUT", "HAL_UNAVAILABLE"} else "provider-error",
                checked_at_unix_ms=int(time.time() * 1000),
                latency_ms=self._elapsed_ms(started),
                error_code=code,
            )
            with self._lock:
                self._provider_health[(candidate.component, requested_domain)] = health
            raise
        domain_row = normalized["domain"]
        mutable_fields = sorted({
            field
            for item in normalized["devices"]
            for field in item["mutable"]
        })
        health = ProviderHealth(
            status=domain_row["status"],
            reason=str(domain_row.get("reason", "healthy"))[:128],
            checked_at_unix_ms=int(time.time() * 1000),
            latency_ms=self._elapsed_ms(started),
            device_count=len(normalized["devices"]),
            mutable=tuple(mutable_fields[:32]),
            mutable_truncated=len(mutable_fields) > 32,
        )
        with self._lock:
            self._provider_health[(candidate.component, requested_domain)] = health
        return normalized

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, min(int((time.monotonic() - started) * 1000), 2**31 - 1))

    @staticmethod
    def _normalize_device(raw: Any, requested_domain: str) -> dict[str, Any]:
        item = object_payload(
            raw,
            allowed=("id", "domain", "name", "available", "mutable", "metadata"),
            required=("id", "domain", "name", "available", "mutable", "metadata"),
            label="HAL device",
        )
        identifier = device_id(item["id"], "device.id")
        if identifier.split(":", 1)[0] != requested_domain or domain(item["domain"]) != requested_domain:
            raise ProviderError("provider device has the wrong domain")
        name = item["name"]
        if not isinstance(name, str) or not name or len(name) > 128:
            raise ProviderError("provider device name is invalid")
        if not isinstance(item["available"], bool):
            raise ProviderError("provider device availability is invalid")
        mutable = string_list(item["mutable"], "device.mutable", maximum_items=32)
        metadata = safe_scalar_map(item["metadata"], "device.metadata")
        return {
            "id": identifier,
            "domain": requested_domain,
            "name": name,
            "available": item["available"],
            "mutable": mutable,
            "metadata": metadata,
        }

    def _route(self, identifier: str, *, refresh: bool) -> Candidate:
        if not refresh:
            with self._lock:
                route = self._device_routes.get(identifier)
            if route is not None:
                return route
        self.inventory(domains=[identifier.split(":", 1)[0]], refresh=refresh)
        with self._lock:
            route = self._device_routes.get(identifier)
        if route is None:
            raise UnavailableError("HAL device is unavailable", details={"id": identifier})
        return route

    def get_state(self, identifier: str, *, refresh: bool = False) -> dict[str, Any]:
        route = self._route(identifier, refresh=refresh)
        raw = self._invoke(
            route.target,
            "get_state",
            {"id": identifier},
            operation=f"get state {identifier}",
            timeout=5.0,
            idempotent=True,
        )
        state = self._normalize_state(raw, identifier, route.component)
        with self._lock:
            revision = self._revision
        result = {
            "schema": MANAGER_INTERFACE,
            "revision": revision,
            "provider": route.component,
            "state": state,
        }
        return result

    def set_state(self, identifier: str, requested_changes: dict[str, Any]) -> dict[str, Any]:
        route = self._route(identifier, refresh=True)
        raw = self._invoke(
            route.target,
            "set_state",
            {"id": identifier, "changes": requested_changes},
            operation=f"set state {identifier}",
            # Output configuration can include a supervised display-provider
            # restart.  The CH347 start path has a 15 second readiness budget,
            # so the manager must not impose the old seven second ceiling.
            timeout=30.0,
            idempotent=False,
        )
        state = self._normalize_state(raw, identifier, route.component)
        revision = self._record_event(
            "state-changed",
            identifier.split(":", 1)[0],
            identifier=identifier,
            provider=route.component,
        )
        result = {
            "schema": MANAGER_INTERFACE,
            "revision": revision,
            "provider": route.component,
            "state": state,
        }
        return result

    @staticmethod
    def _normalize_state(
        raw: dict[str, Any],
        identifier: str,
        provider_component: str,
    ) -> dict[str, Any]:
        result = object_payload(
            raw,
            allowed=("schema", "provider", "state"),
            required=("schema", "provider", "state"),
            label="provider state response",
        )
        if result["schema"] != PROVIDER_INTERFACE:
            raise ProviderError("provider state schema mismatch")
        if result["provider"] != provider_component:
            raise ProviderError("provider state id does not match its component")
        state = object_payload(
            result["state"],
            allowed=("id", "domain", "available", "values", "mutable"),
            required=("id", "domain", "available", "values", "mutable"),
            label="provider state",
        )
        if device_id(state["id"]) != identifier:
            raise ProviderError("provider returned state for another device")
        requested_domain = identifier.split(":", 1)[0]
        if domain(state["domain"]) != requested_domain:
            raise ProviderError("provider state has the wrong domain")
        if not isinstance(state["available"], bool):
            raise ProviderError("provider state availability is invalid")
        return {
            "id": identifier,
            "domain": requested_domain,
            "available": state["available"],
            "values": safe_scalar_map(state["values"], "state.values"),
            "mutable": string_list(state["mutable"], "state.mutable", maximum_items=32),
        }

    def list_providers(
        self,
        requested_domain: str | None,
        *,
        refresh: bool,
        probe: bool = False,
    ) -> dict[str, Any]:
        candidates = self.refresh_catalog(force=refresh)
        if probe and requested_domain is not None:
            matching = [
                candidate
                for candidate in candidates
                if requested_domain in candidate.domains
            ]
            if matching:
                def probe_one(candidate: Candidate) -> None:
                    try:
                        self._probe_candidate(candidate, requested_domain, timeout=2.0)
                    except Exception:
                        pass

                with ThreadPoolExecutor(
                    max_workers=min(8, len(matching)),
                    thread_name_prefix="msys-hal-probe",
                ) as executor:
                    list(executor.map(probe_one, matching))
                with self._lock:
                    preferred = self._selections.get(requested_domain)
                    if preferred is None or not any(
                        candidate.component == preferred for candidate in matching
                    ):
                        healthy = next(
                            (
                                candidate.component
                                for candidate in matching
                                if (
                                    self._provider_health.get(
                                        (candidate.component, requested_domain)
                                    )
                                    is not None
                                    and self._provider_health[
                                        (candidate.component, requested_domain)
                                    ].status in {"available", "degraded"}
                                )
                            ),
                            None,
                        )
                        structured_unavailable = next(
                            (
                                candidate.component
                                for candidate in matching
                                if (
                                    self._provider_health.get(
                                        (candidate.component, requested_domain)
                                    )
                                    is not None
                                    and self._provider_health[
                                        (candidate.component, requested_domain)
                                    ].status == "unavailable"
                                    and self._provider_health[
                                        (candidate.component, requested_domain)
                                    ].error_code is None
                                )
                            ),
                            None,
                        )
                        self._active_providers[requested_domain] = (
                            healthy or structured_unavailable
                        )
        domains = sorted(set(KNOWN_DOMAINS) | {item for candidate in candidates for item in candidate.domains})
        if requested_domain is not None:
            domains = [requested_domain]
        rows = []
        for item in domains:
            matching = [candidate for candidate in candidates if item in candidate.domains]
            preferred, active, selection = self._selection_for(item, matching)
            rows.append({
                "domain": item,
                "selection": selection,
                "preferred": preferred,
                "active": active,
                "candidates": [
                    (
                        self._candidate_payload(candidate, item, preferred, active)
                        if requested_domain is not None
                        else {
                            "component": candidate.component,
                            "name": candidate.name,
                            "version": candidate.version,
                            "priority": candidate.priority,
                        }
                    )
                    for candidate in matching
                ],
            })
        with self._lock:
            revision = self._revision
        result = {"schema": MANAGER_INTERFACE, "revision": revision, "providers": rows}
        ensure_bounded_json(
            result,
            label="provider catalog",
            max_depth=8,
            max_items=2048,
        )
        return result

    def get_provider(
        self,
        requested_domain: str,
        component: str | None,
        *,
        refresh: bool,
        probe: bool,
    ) -> dict[str, Any]:
        matching = self._candidates_for(requested_domain, force=refresh)
        preferred, active, selection = self._selection_for(requested_domain, matching)
        selected = next(
            (candidate for candidate in matching if candidate.component == component),
            None,
        ) if component is not None else next(
            (candidate for candidate in matching if candidate.component == active),
            None,
        )
        if component is not None and selected is None:
            raise ValidationError(
                "component is not a provider for this domain",
                details={"domain": requested_domain, "component": component},
            )
        if selected is not None and probe:
            try:
                self._probe_candidate(selected, requested_domain, timeout=2.0)
            except Exception:
                pass
        with self._lock:
            revision = self._revision
        result = {
            "schema": MANAGER_INTERFACE,
            "revision": revision,
            "domain": requested_domain,
            "selection": selection,
            "preferred": preferred,
            "active": active,
            "provider": (
                self._candidate_payload(selected, requested_domain, preferred, active)
                if selected is not None
                else None
            ),
        }
        ensure_bounded_json(
            result,
            label="provider detail",
            max_depth=8,
            max_items=512,
        )
        return result

    def _selection_for(
        self,
        requested_domain: str,
        matching: list[Candidate],
    ) -> tuple[str | None, str | None, str]:
        with self._lock:
            preferred = self._selections.get(requested_domain)
            observed_active = self._active_providers.get(requested_domain)
        matching_ids = {candidate.component for candidate in matching}
        active = (
            preferred
            if preferred in matching_ids
            else observed_active
            if observed_active in matching_ids
            else matching[0].component
            if matching
            else None
        )
        selection = (
            "manual"
            if preferred == active and preferred is not None
            else "stale"
            if preferred
            else "automatic"
        )
        return preferred, active, selection

    def _candidate_payload(
        self,
        candidate: Candidate,
        requested_domain: str,
        preferred: str | None,
        active: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            health = self._provider_health.get((candidate.component, requested_domain))
        return {
            "component": candidate.component,
            "name": candidate.name,
            "version": candidate.version,
            "priority": candidate.priority,
            "domains": list(candidate.domains),
            "capabilities": [
                item
                for item in candidate.capabilities
                if item.startswith(f"{requested_domain}.")
            ],
            "selected": candidate.component == preferred,
            "active": candidate.component == active,
            "health": (
                health.payload()
                if health is not None
                else {"status": "unknown", "reason": "not-checked"}
            ),
        }

    def select_provider(
        self,
        requested_domain: str,
        component: str | None,
        *,
        expected_revision: int | None = None,
        allow_unavailable: bool = False,
    ) -> dict[str, Any]:
        candidates = self._candidates_for(requested_domain, force=True)
        selected = next(
            (item for item in candidates if item.component == component),
            None,
        ) if component is not None else None
        if component is not None and selected is None:
            raise ValidationError(
                "component is not a provider for this domain",
                details={"domain": requested_domain, "component": component},
            )
        with self._lock:
            self._check_revision(expected_revision)
            unchanged = self._selections.get(requested_domain) == component
        if unchanged:
            return self.list_providers(requested_domain, refresh=False)

        if selected is not None:
            try:
                preflight = self._probe_candidate(selected, requested_domain, timeout=3.0)
            except Exception as exc:
                code = exc.code if isinstance(exc, HalError) else "HAL_PROVIDER_ERROR"
                raise UnavailableError(
                    "HAL provider failed selection preflight",
                    details={
                        "domain": requested_domain,
                        "component": component,
                        "error_code": code,
                    },
                ) from exc
            else:
                if preflight["domain"]["status"] == "unavailable" and not allow_unavailable:
                    raise UnavailableError(
                        "HAL provider is unavailable for this domain",
                        details={
                            "domain": requested_domain,
                            "component": component,
                            "reason": preflight["domain"].get("reason", "unavailable"),
                        },
                    )

        with self._condition:
            self._check_revision(expected_revision)
            old = self._selections.get(requested_domain)
            if old == component:
                event = None
            else:
                if component is None:
                    self._selections.pop(requested_domain, None)
                else:
                    self._selections[requested_domain] = component
                try:
                    self.store.save(self._selections)
                except Exception as exc:
                    if old is None:
                        self._selections.pop(requested_domain, None)
                    else:
                        self._selections[requested_domain] = old
                    raise PersistenceError(
                        "HAL provider preference could not be saved",
                        details={"domain": requested_domain, "cause": type(exc).__name__},
                    ) from exc
                self._device_routes = {
                    identifier: route
                    for identifier, route in self._device_routes.items()
                    if identifier.split(":", 1)[0] != requested_domain
                }
                self._active_providers.pop(requested_domain, None)
                event = self._append_event_locked(
                    "provider-selected" if component is not None else "provider-reset",
                    requested_domain,
                    provider=component,
                    selection="manual" if component is not None else "automatic",
                )
        if event is None:
            return self.list_providers(requested_domain, refresh=False)
        self._emit_event(event)
        return self.list_providers(requested_domain, refresh=False)

    def _check_revision(self, expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision != self._revision:
            raise ConflictError(
                "HAL provider selection changed concurrently",
                details={
                    "expected_revision": expected_revision,
                    "actual_revision": self._revision,
                },
            )

    def _append_event_locked(
        self,
        kind: str,
        requested_domain: str,
        *,
        identifier: str | None = None,
        provider: str | None = None,
        selection: str | None = None,
    ) -> dict[str, Any]:
        self._revision += 1
        event: dict[str, Any] = {
            "revision": self._revision,
            "kind": kind,
            "domain": requested_domain,
        }
        if identifier is not None:
            event["id"] = identifier
        if provider is not None:
            event["provider"] = provider
        if selection is not None:
            event["selection"] = selection
        self._journal.append(event)
        self._condition.notify_all()
        return event

    def _emit_event(self, event: dict[str, Any]) -> None:
        sink = self._event_sink
        if sink is not None:
            try:
                sink("msys.hal.changed", event)
            except Exception as exc:
                print(f"msys-hal-manager: event delivery failed: {exc}", flush=True)

    def _record_event(
        self,
        kind: str,
        requested_domain: str,
        *,
        identifier: str | None = None,
        provider: str | None = None,
        selection: str | None = None,
    ) -> int:
        with self._condition:
            event = self._append_event_locked(
                kind,
                requested_domain,
                identifier=identifier,
                provider=provider,
                selection=selection,
            )
        self._emit_event(event)
        return int(event["revision"])

    def watch(
        self,
        after_revision: int,
        timeout_ms: int,
        domains: list[str] | None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_ms / 1000
        with self._condition:
            selected_domains = set(domains or [])
            while timeout_ms > 0:
                if self._stop.is_set():
                    break
                oldest = self._journal[0]["revision"] if self._journal else self._revision + 1
                resync = after_revision < oldest - 1
                matching = any(
                    item["revision"] > after_revision
                    and (
                        not selected_domains
                        or item["domain"] == "all"
                        or item["domain"] in selected_domains
                    )
                    for item in self._journal
                )
                if resync or matching:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            oldest = self._journal[0]["revision"] if self._journal else self._revision + 1
            resync = after_revision < oldest - 1
            events = [
                dict(item)
                for item in self._journal
                if item["revision"] > after_revision
                and (
                    not selected_domains
                    or item["domain"] == "all"
                    or item["domain"] in selected_domains
                )
            ][:128]
            revision = self._revision
        return {
            "schema": MANAGER_INTERFACE,
            "revision": revision,
            "events": events,
            "resync": resync,
            "topic": "msys.hal.changed",
        }

    def poll_once(self) -> None:
        inventory = self.inventory(refresh=True)
        states: list[dict[str, Any]] = []
        for item in inventory["devices"][:256]:
            if not item.get("available"):
                continue
            try:
                state = self.get_state(str(item["id"]))
                states.append({
                    "id": item["id"],
                    "values": self._stable_poll_values(state["state"]["values"]),
                })
            except Exception:
                states.append({"id": item["id"], "unavailable": True})
        fingerprint = json.dumps(
            {"domains": inventory["domains"], "states": states},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = self._last_fingerprint
        self._last_fingerprint = fingerprint
        if previous is not None and previous != fingerprint:
            self._record_event("inventory-changed", "all")

    @staticmethod
    def _stable_poll_values(values: dict[str, Any]) -> dict[str, Any]:
        """Exclude publisher heartbeat fields while retaining session identity."""

        snapshot = json.loads(json.dumps(values, ensure_ascii=False, allow_nan=False))
        session = snapshot.get("display_session")
        if isinstance(session, dict):
            session.pop("observed_at_unix_ms", None)
        return snapshot


def default_store() -> SelectionStore:
    base = os.environ.get("MSYS_APP_STATE_DIR") or os.environ.get("MSYS_STATE_DIR")
    root = Path(base) if base else Path("/var/lib/msys/hal")
    return SelectionStore(root / "provider-selection.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MSYS HAL manager")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("MSYS_HAL_POLL_INTERVAL", "30")),
        help="hardware change polling interval in seconds",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = HalManager(PublicGateway(), default_store())
    server = ComponentServer(manager.handle, workers=8)
    manager.set_event_sink(server.event)
    manager.start_polling(args.poll_interval)
    try:
        return server.run(ready_event=(
            "msys.hal.ready",
            {"interface": MANAGER_INTERFACE, "component": server.component_id},
        ))
    finally:
        manager.stop()


if __name__ == "__main__":
    raise SystemExit(main())
