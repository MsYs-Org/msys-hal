# CH347 display-output control

`org.msys.hal.linux:ch347-output-control` is an on-demand HAL provider for the
package-owned OpenStick CH347 display and touch pipeline. It does not own
pixels, X11 windows, USB transport, or raw touch samples. It only manages the
the mutable files already consumed by `org.msys.openstick.ch347`:

```text
${MSYS_STATE_DIR}/apps/org.msys.openstick.ch347/ch347/fps.env
${MSYS_STATE_DIR}/apps/org.msys.openstick.ch347/ch347/touch_calibration.env
```

`MSYS_CH347_CONFIG_DIR` is an explicit board/development override. Both files
are parsed as bounded data, never sourced by HAL. Unknown assignments,
duplicates, shell syntax, invalid types and values outside the documented
bounds are rejected. Writes use a same-directory owner-only temporary file,
`fsync`, `os.replace`, and a directory `fsync`. Existing symlink or non-regular
targets are refused.

No systemd, D-Bus, package manager, or shell command is used. Lifecycle is
performed by exact private mIPC calls to `msys.core.stop` and `msys.core.start`
for `org.msys.openstick.ch347:x11-spi-touch-output`.

## Portable HAL v1 device

The provider adds domain `display-output` and stable device
`display-output:ch347`. Generic clients use the normal manager:

```text
inventory({"domains":["display-output"],"refresh":true})
get_state({"id":"display-output:ch347","refresh":true})
set_state({"id":"display-output:ch347","changes":{"fps":60,"idle_fps":1}})
set_state({"id":"display-output:ch347","changes":{"restart":true}})
```

State values are:

- `status`: `available`, `degraded`, or `unavailable`;
- `reason`, `running`, `component`, `component_state`, `package_version`, and
  `live_processes`;
- `configuration_valid` and bounded `configuration_errors`;
- `fps` (1–240) and `idle_fps` (0–60 and never greater than `fps`);
- `touch_calibration`, the object below;
- `restart:false`, an action field that generic JSON editors can change to
  `true`.

`fps`, `idle_fps`, `touch_calibration`, and `restart` are the only mutable
fields. FPS changes are persisted and sent to a live
`xdamage_shm_capture` with `SIGUSR1` only after the PID file, liveness and
executable basename all match. This updates the capture cap without tearing
down X11. A calibration write automatically restarts a running output so the
touch sink and display-session publisher receive the same values. If the
output is stopped, the values are saved for its next normal start. Explicit
restart is accepted only as the boolean `true` and only while the component is
ready.

The controller never creates an empty package-state tree for the source-tree
fallback that happens to share the same component id. At least one regular
configuration file must first have been provisioned by the installable display
package. Until then status remains observable, but configuration fields are
not mutable and writes return `HAL_UNAVAILABLE` instead of reporting a value
that the active driver would ignore.

The calibration object supports partial updates and is returned in full:

```json
{
  "enabled": true,
  "swap_xy": false,
  "invert_x": false,
  "invert_y": false,
  "x_min": 207,
  "x_max": 3859,
  "y_min": 239,
  "y_max": 3836,
  "width": 320,
  "height": 480,
  "z_min": 109,
  "pressure_min": 100,
  "pressure_max": 568
}
```

Coordinates and pressure values are 0–65535; width/height are 1–8192;
`x_min < x_max`, `y_min < y_max`, and `pressure_min < pressure_max`. Flags
must be JSON booleans, not integers.

## Optional typed interface

Native tools that want method-specific payloads may call the non-exclusive
`org.msys.hal.ch347-control.v1` interface implemented by the same component:

```text
status({})
get_fps({})
set_fps({"fps":60,"idle_fps":1})
get_debug({})
set_debug({"enabled":true})
get_touch_calibration({})
set_touch_calibration({"touch_calibration":{"invert_x":true}})
restart({})
```

All responses contain `schema:"org.msys.hal.ch347-control.v1"` and
`device:"display-output:ch347"`. The portable HAL manager API remains the
preferred Settings integration because another output-control provider can
replace this one without a CH347-specific dependency.

The FPS document also contains a strict `DEBUG=0|1` field. `DEBUG=1` enables
the sink's on-panel FPS/dirty overlay; it is not a target-FPS alias. Both debug
methods return a `debug` object containing `enabled`, configured `fps`,
`max_fps`, `idle_fps`, `applied`, `requires_restart`, and the exact
`provider_generation` from the provider-owned runtime receipt. A changed flag
is committed atomically and a running display provider is replaced through the
same exact Core stop/start calls. If the output is stopped, the saved result is
explicitly `applied:false` and `requires_restart:true` until its next start.

`observed_fps`, `panel_fps`, and `frames` are populated only from a real,
bounded parse of the current generation sink log (`out_fps`, `bus_fps`, and
the cumulative frame counter). Missing samples remain `null`; configured FPS
is never presented as an observation. `window_ms` remains `null` because the
sink does not publish a real sampling-window duration.

## mIPC authorization note

HAL manager discovery returns exact component identities and selection/probing
must call each exact component. The package therefore declares exact grants
for all providers shipped in this package in addition to the interface grant.
For third-party providers current Core authorizes an exact component target
only when the live catalog proves that component provides the interface
covered by the caller's `mipc.call:org.msys.hal.provider.v1` grant. The exact
in-package grants remain compatible with older Core releases. Routing a call
to the interface target is not equivalent: it lets Core choose one provider
and cannot inspect or select a specific candidate.
