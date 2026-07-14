from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from .errors import ValidationError


DOMAIN_RE = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
DEVICE_ID_RE = re.compile(r"^[a-z][a-z0-9.-]{0,63}:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COMPONENT_RE = re.compile(
    r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+:[a-z][a-z0-9._-]*$"
)
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9.-]{0,127}$")
SEMVER_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
MAX_HAL_JSON = 128 * 1024


def object_payload(
    value: Any,
    *,
    allowed: Iterable[str],
    required: Iterable[str] = (),
    label: str = "payload",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    if len(value) > 32:
        raise ValidationError(f"{label} has too many fields")
    allowed_set = set(allowed)
    unknown = sorted(set(value) - allowed_set)
    if unknown:
        raise ValidationError(f"{label} has unknown fields: {', '.join(unknown)}")
    missing = sorted(set(required) - set(value))
    if missing:
        raise ValidationError(f"{label} is missing fields: {', '.join(missing)}")
    ensure_bounded_json(value, label=label)
    return value


def bounded_string(value: Any, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    if not value or len(value) > maximum or "\x00" in value:
        raise ValidationError(f"{label} length is invalid")
    return value


def domain(value: Any, label: str = "domain") -> str:
    result = bounded_string(value, label, maximum=64)
    if not DOMAIN_RE.fullmatch(result):
        raise ValidationError(f"{label} is invalid")
    return result


def device_id(value: Any, label: str = "id") -> str:
    result = bounded_string(value, label, maximum=192)
    if not DEVICE_ID_RE.fullmatch(result):
        raise ValidationError(f"{label} is invalid")
    return result


def component_id(value: Any, label: str = "component") -> str:
    result = bounded_string(value, label, maximum=192)
    if not COMPONENT_RE.fullmatch(result):
        raise ValidationError(f"{label} is invalid")
    return result


def semantic_version(value: Any, label: str = "version") -> str:
    result = bounded_string(value, label, maximum=64)
    if not SEMVER_RE.fullmatch(result):
        raise ValidationError(f"{label} is not a semantic version")
    return result


def capability(value: Any, label: str = "capability") -> str:
    result = bounded_string(value, label, maximum=128)
    if not CAPABILITY_RE.fullmatch(result):
        raise ValidationError(f"{label} is invalid")
    return result


def integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ValidationError(f"{label} is outside {minimum}..{maximum}")
    return value


def string_list(
    value: Any,
    label: str,
    *,
    maximum_items: int = 32,
    item_maximum: int = 64,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValidationError(f"{label} must be a list with at most {maximum_items} items")
    result: list[str] = []
    for index, item in enumerate(value):
        text = bounded_string(item, f"{label}[{index}]", maximum=item_maximum)
        if text in result:
            raise ValidationError(f"{label} contains a duplicate")
        result.append(text)
    return result


def changes(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or len(value) > 16:
        raise ValidationError("changes must be a non-empty object with at most 16 fields")
    for key in value:
        if not isinstance(key, str) or not FIELD_RE.fullmatch(key):
            raise ValidationError("changes contains an invalid field name")
    ensure_bounded_json(value, label="changes", max_depth=4, max_items=64)
    return dict(value)


def ensure_bounded_json(
    value: Any,
    *,
    label: str = "value",
    max_depth: int = 6,
    max_items: int = 512,
) -> None:
    count = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > max_items:
            raise ValidationError(f"{label} contains too many values")
        if depth > max_depth:
            raise ValidationError(f"{label} is nested too deeply")
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValidationError(f"{label} contains a non-finite number")
            return
        if isinstance(item, str):
            if len(item) > 4096 or "\x00" in item:
                raise ValidationError(f"{label} contains an invalid string")
            return
        if isinstance(item, list):
            if len(item) > 256:
                raise ValidationError(f"{label} contains an oversized list")
            for child in item:
                walk(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 128:
                raise ValidationError(f"{label} contains an oversized object")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128 or "\x00" in key:
                    raise ValidationError(f"{label} contains an invalid object key")
                walk(child, depth + 1)
            return
        raise ValidationError(f"{label} contains a non-JSON value")

    walk(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc
    if len(encoded) > MAX_HAL_JSON:
        raise ValidationError(f"{label} is too large")


def safe_scalar_map(value: Any, label: str, *, maximum_fields: int = 64) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > maximum_fields:
        raise ValidationError(f"{label} must be a small object")
    ensure_bounded_json(value, label=label, max_depth=4, max_items=256)
    return dict(value)
