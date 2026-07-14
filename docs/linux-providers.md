# Reference Linux providers

The reference package deliberately splits seven domains into seven on-demand
components. A product can replace only its backlight or display provider while
keeping the remaining generic implementations.

## Power

Reads bounded, normalized fields below `/sys/class/power_supply`. It reports
capacity, online state and common micro-unit energy/current/voltage fields when
the kernel exports them. It never writes charge thresholds or charger state.

## Thermal

Reads `thermal_zone*` type and millidegree-Celsius temperature below
`/sys/class/thermal`. Cooling-device control and trip-point writes are outside
the v1 safe provider.

## Backlight

Reads `/sys/class/backlight/<name>/{brightness,max_brightness,actual_brightness}`.
Writable devices expose both raw `brightness` and the board-independent
`brightness_percent`; a request must change exactly one. Read-only attributes
remain visible with `mutable:[]` and a `control:"read-only"` marker. Before
writing, the provider:

1. resolves a known device discovered as a direct class entry;
2. accepts an integer, never a boolean, between zero and `max_brightness`;
3. permits only the resolved `brightness` attribute under the trusted sysfs
   device tree;
4. opens it with close-on-exec and no-follow flags where supported;
5. performs a complete ASCII write and reads the value back.

Percentage writes are bounded to 0..100, converted with integer rounding using
the live `max_brightness`, and then verified as the resulting raw value.

Kernel file ownership still decides whether the process may write. HAL neither
changes modes nor escalates privileges.

## Display layout and live session

The display provider maps `display:primary` to
`role:window-manager.get_layout` and `set_layout`. Profiles are `mobile`,
`kiosk`, and `desktop`; logical orientation is `auto`, `portrait`, or
`landscape`; insets are `auto` or four bounded edges. It also consumes the full
`msys.display-session.v1` returned in-band by `get_layout`/
`get_display_session` from the window policy or from
`${MSYS_DISPLAY_SESSION_STATE_FILE:-$MSYS_RUNTIME_DIR/display-session.json}`.
The file reader uses no-follow bounded reads, strict fields/types, verified
normalized input matrices, and a 45-second freshness limit by default.

If layout and session are both present the domain is `available`. If only one
is present it is `degraded`: Settings can still inspect the live output or edit
logical layout as applicable. If neither exists, inventory/get-state report
structured `unavailable`. The effective display string and geometry are always
taken from the state document; no display number or panel size is guessed.

Physical HDMI/SPI modes, framebuffer rotation, CH347 transport and touch
calibration belong to a replaceable `display-output` provider. The HAL display
domain describes physical rotation as read-only and returns `HAL_READ_ONLY` for
physical transform writes rather than pretending logical layout rotated the
hardware.

## Input inventory

Parses bounded blocks in `/proc/bus/input/devices` and exposes event handlers,
names and descriptive metadata. If procfs is unavailable it falls back to
`/sys/class/input/event*`. It does not open `/dev/input/event*`, consume input,
or inject events.

When the display session owns an input transform, HAL additionally exposes the
stable virtual device `input:display-touch`. `ch347-direct` and `ch347-xtest`
are observable but read-only because their mapping belongs to the selected
display provider. For `mode:"xinput"` only, HAL may expose `orientation` and
`matrix` as mutable after all of these checks succeed:

1. the session names a bounded device and supplies a verified normalized CTM;
2. the `xinput` executable is available through trusted operator configuration;
3. `xinput list --id-only` resolves the name to exactly one numeric id;
4. the requested matrix is finite, bounded, affine, and has exactly nine values;
5. `set-prop` is invoked as an argv array on the session's validated DISPLAY;
6. `list-props` reads the CTM back and every coefficient matches.

Missing commands/devices are `unavailable`; ambiguous devices and
provider-owned modes are `read-only`. HAL never silently applies a matrix to a
different input device.

## Network and Wi-Fi

The network provider enumerates at most 64 validated entries from
`/sys/class/net`. It classifies loopback, Ethernet, WWAN, Wi-Fi and unknown
interfaces from kernel type, class markers and bounded uevent data. State can
include `operstate`, `carrier`, `address` and `mtu`; absent kernel attributes
are simply omitted. Ethernet, WWAN and loopback remain read-only.

For Wi-Fi, the provider looks only for the trusted operator-configured
`/run/wpa_supplicant/<interface>` Unix datagram control socket. It uses the
Python standard library directly, creates an ephemeral Linux abstract client
address, caps each command/response, and applies a 50 ms..3 s timeout. It does
not call `wpa_cli`, D-Bus, NetworkManager or shell commands. State contains a
whitelist from `STATUS`, up to 20 `SCAN_RESULTS`, and up to 16
`LIST_NETWORKS` rows. PSKs are never returned, logged, placed in an error, or
copied into state.

The generic `network:<interface>` write contract is deliberately small:

```json
{"action":"scan"}
{"action":"disconnect"}
{"action":"connect","ssid":"Existing profile"}
{"action":"connect","ssid":"New profile","psk":"8-or-more-ASCII"}
{"action":"forget","network_id":7}
```

An SSID is printable UTF-8 and at most 32 encoded bytes. A passphrase is 8..63
printable ASCII characters or a 64-digit hexadecimal raw PSK. Existing profiles
are selected by one exact SSID match and their credentials are never rewritten.
A new profile is assembled through `ADD_NETWORK` and individually checked
`SET_NETWORK`/`ENABLE_NETWORK`/`SELECT_NETWORK` responses; partial creation is
removed on failure. `SAVE_CONFIG` is attempted after add/forget and the returned
state says `configuration_persisted:false` when the running supplicant forbids
persistence. Runtime success is not misreported as durable storage.

`scan` triggers a bounded scan; callers fetch refreshed results through
`get_state`. Open-network provisioning, hotspot mode, IP configuration, DHCP,
DNS and routing policy are outside this provider. Those belong to a selected
network provider with an explicit implementation, not fragile shell fallbacks.

## Bluetooth radio

The Bluetooth provider reads controllers from `/sys/class/bluetooth` and
Bluetooth rfkill entries from `/sys/class/rfkill`. It reports controller/radio
identity, address when exported, and observable `soft_blocked`, `hard_blocked`
and `powered` values. A unique controller-to-rfkill name match exposes the same
power control on the controller row for Settings convenience.

Only `{"powered":true|false}` is writable. The provider writes the known
rfkill `soft` attribute through a no-follow, close-on-exec descriptor and reads
the effective state back. Hard-blocked radios cannot be powered on. Paths,
write operation and writability probe are injectable in tests; no arbitrary
path comes from RPC.

This reference provider has no pairing daemon protocol. Every controller and
radio therefore returns `pairing_available:false` with a stable reason. It
never claims that discovery, pairing or connection succeeded merely because a
controller exists. A product may replace the `bluetooth` domain with a small
native provider speaking a real daemon's non-D-Bus protocol later.

## Operator path overrides

Tests and board manifests may set `MSYS_HAL_POWER_ROOT`,
`MSYS_HAL_THERMAL_ROOT`, `MSYS_HAL_BACKLIGHT_ROOT`, `MSYS_HAL_INPUT_PROC`, and
`MSYS_HAL_INPUT_ROOT`. Network and radio roots use `MSYS_HAL_NETWORK_ROOT`,
`MSYS_HAL_WPA_CONTROL_ROOT`, `MSYS_HAL_BLUETOOTH_ROOT` and
`MSYS_HAL_RFKILL_ROOT`; `MSYS_HAL_WPA_TIMEOUT_MS` is clamped to the provider's
bounded timeout. Display-state candidates can be configured with
`MSYS_DISPLAY_SESSION_STATE_FILE` or `MSYS_HAL_DISPLAY_SESSION_FILES`, and an
XInput executable with `MSYS_HAL_XINPUT_BINARY`.
`MSYS_HAL_DISPLAY_SESSION_MAX_AGE_MS` can tune the bounded freshness window
(zero disables only the age check). These are trusted component environment
settings, not untrusted RPC parameters. No path or executable is accepted in a
HAL request.
