# MSYS HAL contract v1

HAL is a replaceable MSYS service, not a privileged kernel daemon and not a
hard-coded Python plugin registry. Components use the ordinary
`msys.manifest.v1` declaration and mIPC. There is no systemd, D-Bus, udev,
logind, polkit, target package manager, or third-party Python dependency.

## Discovery and addressing

The selected manager provides both:

- role `hal-manager`;
- interface `org.msys.hal.manager.v1`.

Providers declare the non-exclusive interface
`org.msys.hal.provider.v1` and a discoverable domain capability such as
`hal.power`. The manager asks `msys.core.discover` for every interface
provider, calls each exact `component:<package>:<component>` target, and then
selects one candidate per domain. Thus two power providers can coexist without
acquiring an exclusive role lease.

The Python fallback manager describes cold-start candidates with at most two
concurrent calls. This avoids an on-demand process startup storm on small
boards while still overlapping provider readiness waits. Work remains bounded
by Core's discovery response and the 16-candidate catalog limit. Results are
committed in deterministic priority and component-id order; one timeout or
invalid description only removes that provider. A catalog mutex still prevents
overlapping refreshes, while the manager state lock is never held across an
external provider call.

`display` is logical layout/session policy. `display-output` is a separate
device-control domain, so a board output driver can expose FPS, calibration or
restart without displacing the window-manager-backed display provider. The
reference OpenStick contract is in [`ch347-control.md`](ch347-control.md).

`network` and `bluetooth` are also replaceable domains. The reference network
provider owns interface observation and a deliberately small wpa_supplicant
control subset; it does not own IP/DNS policy. The reference Bluetooth provider
owns only controller/rfkill observation and soft-block power. It explicitly
reports pairing as unavailable rather than treating kernel presence as a
working Bluetooth user-space stack. Detailed fields and actions are in
[`linux-providers.md`](linux-providers.md).

A higher manifest priority wins in automatic mode. If it reports the domain as
unavailable or loses liveness, the next automatic candidate is tried. A user selection pins one
component and is stored atomically in
`${MSYS_APP_STATE_DIR:-${MSYS_STATE_DIR:-/var/lib/msys/hal}}/provider-selection.json`.
If an automatic provider fails, the manager tries the next candidate. A pinned
provider fails visibly instead of silently changing the selected hardware
implementation. A removed preference is reported as `stale` and automatic
selection resumes.

`describe` may also advertise a bounded list of domain-prefixed capability
strings, for example `backlight.brightness` and `backlight.state.write`.
Capabilities describe what the implementation knows how to do; they are not a
liveness claim. The manager derives current availability and mutable fields
from `inventory`. For compatibility, a provider that omits `capabilities`
receives the conservative `<domain>.inventory` and `<domain>.state.read`
baseline. No new health RPC is required from old providers.

## Manager methods

All payloads are objects. Unknown fields, malformed ids, booleans supplied as
integers, oversized lists, excessive nesting, NUL bytes and non-finite numbers
are rejected. Device ids have the stable form `domain:provider-local-id`.

### `inventory`

```text
inventory({"domains":["power","display"],"refresh":false})
```

Both fields are optional. The result is:

```json
{
  "schema": "org.msys.hal.manager.v1",
  "revision": 4,
  "domains": [
    {
      "domain": "power",
      "status": "available",
      "provider": "org.msys.hal.linux:linux-power",
      "selection": "automatic"
    }
  ],
  "devices": [
    {
      "id": "power:BAT0",
      "domain": "power",
      "name": "BAT0",
      "available": true,
      "mutable": [],
      "metadata": {"type": "Battery"},
      "provider": "org.msys.hal.linux:linux-power"
    }
  ]
}
```

Missing kernel classes and missing roles are normal. They produce an
`unavailable` domain and either an empty list or an unavailable descriptive
device, not a component crash. A domain can be `degraded` when state remains
observable but its safe control path is absent.

### `get_state` and `set_state`

```text
get_state({"id":"power:BAT0","refresh":false})
set_state({"id":"backlight:panel0","changes":{"brightness":80}})
set_state({"id":"backlight:panel0","changes":{"brightness_percent":50}})
set_state({"id":"display:primary","changes":{"profile":"mobile","orientation":"landscape","insets":"auto"}})
set_state({"id":"input:display-touch","changes":{"orientation":"left"}})
set_state({"id":"network:wlan0","changes":{"action":"scan"}})
set_state({"id":"network:wlan0","changes":{"action":"connect","ssid":"Lab"}})
set_state({"id":"bluetooth:rfkill0","changes":{"powered":true}})
```

The response names the exact provider and contains a typed state with
`values` and `mutable`. Writes are never generic sysfs paths: the chosen
provider owns a small allowlist and validates each field.

Network credentials are accepted only by the selected network provider during
the `set_state` call. They are never returned in state, provider health or
watch events. Bluetooth pairing is not part of the Linux reference provider's
write allowlist.

`display:primary.orientation` is logical window-layout policy. Physical output
rotation is returned as a read-only capability owned by `display-output`.
`input:display-touch` represents the transform from the active
`msys.display-session.v1`; it is mutable only for a unique, live XInput device.
Provider-owned CH347 direct/XTest transforms return `HAL_READ_ONLY`.

### Provider management

```text
list_providers({"domain":"backlight","refresh":true,"probe":true})
get_provider({"domain":"backlight","component":"org.example.board-hal:backlight-provider","probe":true})
select_provider({"domain":"power","component":"org.example.board-hal:power-provider","expected_revision":8})
reset_provider({"domain":"backlight","expected_revision":9})
```

With a domain filter, `list_providers` returns detailed candidates:

```json
{
  "schema": "org.msys.hal.manager.v1",
  "revision": 8,
  "providers": [{
    "domain": "backlight",
    "selection": "automatic",
    "preferred": null,
    "active": "org.msys.hal.linux:linux-backlight",
    "candidates": [{
      "component": "org.msys.hal.linux:linux-backlight",
      "name": "Linux safe provider",
      "version": "0.1.4",
      "priority": 50,
      "domains": ["backlight"],
      "capabilities": [
        "backlight.brightness",
        "backlight.brightness.percent",
        "backlight.inventory",
        "backlight.state.read",
        "backlight.state.write"
      ],
      "selected": false,
      "active": true,
      "health": {
        "status": "available",
        "reason": "healthy",
        "checked_at_unix_ms": 1770000000000,
        "latency_ms": 3,
        "device_count": 1,
        "mutable": ["brightness", "brightness_percent"],
        "mutable_truncated": false
      }
    }]
  }]
}
```

`refresh:true` refreshes discovery and descriptions. `probe:true` additionally
calls each candidate's existing, idempotent `inventory` method in parallel and
updates its in-memory health snapshot. Probing requires a domain filter.
Without a probe, health is the last observed snapshot or
`{"status":"unknown","reason":"not-checked"}`. `get_provider` returns one
exact candidate (or the current effective candidate when `component` is
omitted) and can probe only that candidate. A domain-less `list_providers`
keeps the compact v1 candidate shape; Settings should use a domain filter for
capabilities and health.

Health status is `available`, `degraded`, `unavailable`, or `unknown`.
`error_code` is present for a failed probe; operator-sensitive downstream
details are not copied into health. `checked_at_unix_ms` is observational, not
a lease. `mutable` is the bounded union of currently reported device fields;
`mutable_truncated` states whether more than 32 distinct fields existed.

`select_provider` accepts only a currently discovered provider that describes
the requested domain. Before persisting, it performs a read-only inventory
preflight and accepts `available` or `degraded`. A provider that returned a
valid structured `unavailable` result can be pinned only when Settings obtains
operator confirmation and sends `allow_unavailable:true`. A transport failure
or invalid provider response is never overridable. Passing `null`, or calling
`reset_provider`, restores automatic priority selection.

Settings should copy the catalog `revision` into `expected_revision`.
If another state/provider event advanced the revision before commit, the
manager returns `HAL_CONFLICT` and changes neither memory nor the persisted
selection. Omitting the field preserves v1 last-writer-wins behavior. A
successful commit atomically saves the selection, invalidates cached device
routes and emits a `provider-selected`/`provider-reset` watch entry with
`selection:"manual"` or `selection:"automatic"`.

After inventory performs automatic failover, `list_providers.active` reports
the provider that actually answered and owns the device routes, not merely the
first manifest candidate. A manual pin remains the visible active selection
even when its domain is currently unavailable, so Settings never hides the
operator's choice.

### `watch`

```text
watch({"after_revision":4,"timeout_ms":25000,"domains":["power"]})
```

`watch` is a bounded long poll. It returns at most 128 journal entries, the new
revision, `resync:true` if the caller fell behind, and the topic
`msys.hal.changed`. A long-lived component may subscribe to that topic instead
and use `inventory`/`get_state` after an event. JSON carries low-rate hardware
state only; pixels, audio and raw input never pass through HAL JSON.

`watch` belongs to the manager, which combines provider polling, successful
writes and provider-selection changes into one revision sequence. Individual
provider v1 processes deliberately do not hold long-poll clients or invent a
second revision space; they implement the four bounded methods below.
The display-session publisher's `observed_at_unix_ms` heartbeat is excluded
from change fingerprints; provider generation, geometry and input-transform
changes still wake watchers.

## Error contract

mIPC failures are always `type:"error"` packets with a stable `code`, a short
operator-safe `message`, and an optional object `payload`. Callers branch on
`code`, not message text:

| Code | Meaning |
| --- | --- |
| `HAL_BAD_PAYLOAD` | Unknown field, malformed id, invalid type, bound violation, or empty changes. |
| `HAL_UNKNOWN_METHOD` | The requested manager/provider v1 method does not exist. |
| `HAL_UNAVAILABLE` | The manager, selected device, transport, or required downstream role is unavailable. |
| `HAL_READ_ONLY` | The device/domain exists but the requested write is unsupported. |
| `HAL_PROVIDER_ERROR` | A provider failed or violated the provider v1 response contract. |
| `HAL_PERSISTENCE_ERROR` | A provider selection could not be committed; memory is rolled back. |
| `HAL_CONFLICT` | `expected_revision` is stale; reload before retrying a provider switch. |
| `HAL_BUSY` | The bounded request queue is full; retry with backoff. |
| `CALL_TIMEOUT` | The request deadline expired before dispatch/completion. |
| `HAL_INTERNAL_ERROR` | An unexpected component bug; the public message is redacted. |

Safe provider errors `HAL_BAD_PAYLOAD`, `HAL_UNAVAILABLE`, and `HAL_READ_ONLY`
survive the manager boundary. Unknown downstream codes become
`HAL_PROVIDER_ERROR`; their bounded original code/message remain in the error
payload for diagnostics. `inventory` treats an absent device class as a
successful response containing an `unavailable` domain. This is distinct from
an RPC error and lets Settings remain useful on boards without every hardware
class.

## Provider methods

Every `org.msys.hal.provider.v1` component implements:

```text
describe({})
inventory({"domains":["power"]})
get_state({"id":"power:BAT0"})
set_state({"id":"backlight:panel0","changes":{"brightness":80}})
```

`describe` returns its domains. `inventory` returns one status row per
requested domain plus bounded devices. A provider must keep ids stable across
restarts and must reject writes it does not explicitly implement. It can be
written in C, C++, Rust, Python, Qt, Electron or any runtime able to speak
mIPC; the manager never imports provider code.

A 0.1.4 `describe` result can include capabilities:

```json
{
  "schema": "org.msys.hal.provider.v1",
  "provider": {
    "id": "org.example.board-hal:backlight-provider",
    "name": "Board backlight provider",
    "version": "1.2.0"
  },
  "domains": ["backlight"],
  "capabilities": [
    "backlight.inventory",
    "backlight.state.read",
    "backlight.state.write"
  ]
}
```

Capability strings contain only lowercase ASCII letters, digits, dots and
hyphens, begin with one of the declared domains, and are plain JSON strings.
The reference boundary accepts at most 8 domains, 32 capabilities and 16
discovered provider candidates. All public payloads remain bounded JSON
objects; capability and health objects never contain Python-specific values.

## Trust boundary

Manifest `permissions` remain cooperative policy/audit metadata in the current
MSYS release. The HAL manager is not an authorization server, same-UID clients
are not isolated by this repository, and a provider declaration does not prove
that its kernel access is safe. Filesystem ownership, an optional MSYS
isolation profile, and future mIPC ACL enforcement are separate controls.
