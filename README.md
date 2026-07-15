# MSYS HAL

`msys-hal` is a lightweight, replaceable hardware abstraction layer for the
MSYS user-space system. It runs as ordinary supervised components after Linux
has booted. It does not become PID 1 and does not require systemd, D-Bus, udev,
logind, polkit, a target package manager, or non-standard Python modules.

The manager owns the exclusive `hal-manager` role and declares the stable
`org.msys.role.hal-manager.v1` contract at version `1.0.0`. It supplies the
contract's required `org.msys.hal.manager.v1` interface. Independent on-demand
providers supply `org.msys.hal.provider.v1` for power, thermal, backlight,
display layout/session state, input inventory/transforms, Linux network/Wi-Fi
status and Bluetooth radio status. A board-specific native provider is an
ordinary language-neutral MSYS package and may outrank or be selected instead
of one reference domain.

Version 0.2.10 uses the single-process C11 native manager as the normal resident
HAL and retains the Python manager/providers as idle-reaped on-demand
fallbacks. Its phase-one hardware and compatibility boundaries, strict write
allowlist, build, aarch64 smoke test, and RSS measurement are documented in
[docs/native-hal.md](docs/native-hal.md).

The radio providers are intentionally narrow. Wi-Fi talks directly to an
already-running wpa_supplicant control socket with bounded standard-library
Unix datagram in Python and the same protocol directly in the native manager;
it never invokes `wpa_cli`. The native manager uses the stable Linux Bluetooth
Management socket for controller state, power and a bounded discovery scan,
with rfkill as the verified power fallback. Pairing remains explicitly
unsupported until a replaceable provider supplies a complete pairing contract;
it is never simulated. Missing controllers, control sockets and Management
support are returned as structured unavailable/reason fields. Neither path
requires NetworkManager, BlueZ D-Bus or a target package manager.

Version 0.2.7 also accepts the standard four-byte `current_settings` response
from Linux Management power commands when the caller intentionally discards
the response body. The command is still status-checked and bounded; a
successful kernel power transition no longer becomes a false internal error.

Version 0.2.10 handles controllers such as Qualcomm WCNSS that unregister their
Linux Management index after power-off. A later power-on uses one bounded
rfkill block/unblock edge and at most 20 Management re-probes.
If this kernel retains a down `hciN` sysfs device without re-registering it,
the bounded path retries Linux `HCIDEVUP` inside the same probe window while
the exact Management state remains `index-list:0`; no BlueZ
command, D-Bus service, or target package is introduced.
rfkill's soft=0 is now reported as `rfkill_unblocked`, never as proof that a
Bluetooth controller is powered. When the Management index is absent, the
controller state is explicitly off and remains recoverable through the same
`powered` write contract.

The OpenStick integration also supplies a distinct `display-output` HAL domain
for the package-owned CH347 FPS/touch configuration and supervised restart.
This does not replace the logical `display` domain or the Core
`display-output` role. Its generic and optional method-specific contracts are
documented in [`docs/ch347-control.md`](docs/ch347-control.md).

CH347 `get_debug` adds bounded parsing of the sink's newest `dirty_stats`
line. `sent_frames`, `zero_damage`, `full_refreshes`, `large_refreshes`,
`sent_pixels`, `last_sent_pixels`, and `last_rects` are cumulative unsigned
counters, not a sampled rate or delta.
They are collected independently of the on-panel `DEBUG` overlay. An older
sink with no statistics line, or a malformed/out-of-range newest line, returns
`null` for every optional counter.

The full API and selection rules are in
[`docs/hal-contract-v1.md`](docs/hal-contract-v1.md). Linux behavior and its
safety boundaries are in
[`docs/linux-providers.md`](docs/linux-providers.md).

## Run tests

Only the Python standard library is used:

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## Manifest and isolated package root

[`manifest.json`](manifest.json) is the canonical package manifest. The copy at
[`manifests/msys-hal.json`](manifests/msys-hal.json) is kept equivalent for
older source-tree profile discovery. A development msysd can load the
canonical file explicitly. Its package working directory is this repository
root, so `python -m msys_hal...` resolves without inheriting a host
`PYTHONPATH`.

The same layout is directly installable. The installer commits the directory
as one versioned package root, strips host Python environment variables, and
starts it with that root as `cwd`. Every component opts into MSYS's `baseline`
isolation (no-new-privileges, non-dumpable, bounded descriptors/core files).
No `pip install` or target distribution package is involved.

```sh
PYTHONPATH=/path/to/msys-tools \
  python3 -m msys_tools.dev package validate /path/to/msys-hal
PYTHONPATH=/path/to/msys-tools \
  python3 -m msys_tools.dev package build /path/to/msys-hal --output /path/to/dist
```

Both manifest copies can also be checked strictly against the versioned role
descriptor catalog (without the migration-only `--allow-unversioned` option):

```sh
cd /path/to/msys-contracts
python3 -m tools.contract_tool manifest /path/to/msys-hal/manifest.json
python3 -m tools.contract_tool manifest /path/to/msys-hal/manifests/msys-hal.json
```

The manifest starts only the manager eagerly. Provider discovery wakes the
small per-domain processes through the normal mIPC interface activation path.
Settings and applications call:

```text
interface:org.msys.hal.manager.v1.inventory
interface:org.msys.hal.manager.v1.get_state
interface:org.msys.hal.manager.v1.set_state
interface:org.msys.hal.manager.v1.list_providers
interface:org.msys.hal.manager.v1.get_provider
interface:org.msys.hal.manager.v1.select_provider
interface:org.msys.hal.manager.v1.reset_provider
interface:org.msys.hal.manager.v1.watch
```

CH347 control is visible through those same calls as
`display-output:ch347`. Its optional direct interface is:

```text
interface:org.msys.hal.ch347-control.v1.status
interface:org.msys.hal.ch347-control.v1.get_fps
interface:org.msys.hal.ch347-control.v1.set_fps
interface:org.msys.hal.ch347-control.v1.get_touch_calibration
interface:org.msys.hal.ch347-control.v1.set_touch_calibration
interface:org.msys.hal.ch347-control.v1.restart
```

The same calls work from Qt, Electron, Tk, Python, C/C++ or a bundled custom
runtime. HAL values are data contracts, not Python objects.

The reference manager caches provider discovery and polls low-rate hardware at
30-second intervals by default. `MSYS_HAL_POLL_INTERVAL` can tune this for a
board, while Settings can always request an explicit refresh.

## Settings provider page

Settings should request one domain at a time so the response includes detailed
candidate data:

```text
list_providers({"domain":"display","refresh":true,"probe":true})
get_provider({"domain":"display","component":"org.example.board-hal:display","probe":true})
select_provider({"domain":"display","component":"org.example.board-hal:display","expected_revision":12})
reset_provider({"domain":"display","expected_revision":13})
```

Each candidate reports its static, domain-prefixed `capabilities` and a live
`health` snapshot. `probe:true` performs only the provider v1 read-only
`inventory` call; it does not require a new provider method, so providers made
for earlier MSYS releases remain usable. Providers without an advertised
capability list receive the conservative `domain.inventory` and
`domain.state.read` baseline.

Use the `revision` returned by the list/detail call as `expected_revision` when
switching. `HAL_CONFLICT` means another change won and Settings must reload.
Selection normally accepts only `available` or `degraded` providers. An
operator-confirmed structured `unavailable` result can be pinned with
`allow_unavailable:true`; transport failures and invalid provider responses are
never accepted. Preferences remain atomically persisted in the package state
directory and survive manager restarts.

Display and touch state comes from the active `msys.display-session.v1`
document returned by `window-manager.get_display_session` or published at
`$MSYS_RUNTIME_DIR/display-session.json` (or an explicit
`MSYS_DISPLAY_SESSION_STATE_FILE`). HAL validates and freshness-checks that
document; it never guesses `DISPLAY=:24`. Logical mobile/desktop orientation is
set through the replaceable window-manager. Physical output rotation remains
owned by `display-output`. A published XInput CTM is writable only when its
device name resolves to exactly one live XInput id and the write can be read
back; CH347 direct/XTest transforms remain explicitly read-only.
