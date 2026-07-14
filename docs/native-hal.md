# Native HAL host 0.2.7

files/bin/msys-hal-native is the first dependency-free native replacement for
the always-resident Python HAL graph. It is one C11 process linked directly
with the repository-adjacent MSYS C SDK JSON-wire source. Runtime dependencies
are only Linux and the system C library: no Python, systemd, D-Bus, udev,
NetworkManager, BlueZ D-Bus, package manager, or third-party JSON library.

The package manifest makes native-manager the priority-200 background
hal-manager. The Python manager remains a priority-100 on-demand fallback.
Every Python HAL provider is also on-demand and has a 30-second idle timeout,
so the normal native path does not keep a Python HAL process resident.

## Phase-one surface

The native process implements the existing JSON mIPC hello/welcome/ready
handshake and these manager calls:

- describe;
- inventory, with an optional bounded domain filter;
- get_state;
- set_state;
- list_providers;
- get_provider;
- select_provider and reset_provider as validated no-ops for the single
  built-in native provider;
- watch, returning the current revision and an empty recovery batch.

Power supplies, thermal zones, backlights, input event nodes, network
interfaces, Bluetooth controllers, and Wi-Fi/Bluetooth rfkill nodes are read
directly from fixed Linux class roots. Enumeration, names, JSON depth, token
count, device count, strings, requests, and responses are bounded. Responses
contain normalized scalar state and never expose host paths.

The logical display and display-output domains remain present in inventory but
report structured unavailable state in phase one, rather than making a whole
manager request fail.

Only two write paths exist:

- backlight device ids accept exactly one bounded brightness or
  brightness_percent field, write the fixed brightness attribute, and verify
  the readback;
- network and Bluetooth rfkill ids accept exactly one boolean powered field,
  refuse hard-blocked devices, write only the fixed soft attribute, and verify
  the readback.

All other state changes return HAL_READ_ONLY or HAL_UNSUPPORTED. Wi-Fi scan,
association, disconnect, saved-profile removal, and rfkill power are supported
through the wpa_supplicant control socket and Linux class files without D-Bus.
Bluetooth discovery uses the Linux Management control channel with a bounded
1.8-second scan and at most 24 results. Pairing, display layout, switching to an
external provider, and long-poll event journaling remain outside phase one.
Pairing is reported as unsupported rather than being simulated. If Wi-Fi's
wpa_supplicant socket or Bluetooth's Management channel is missing, inventory
and state include stable reason fields while preserving any read-only sysfs
inventory that is still real.

Linux Management Command Complete packets may carry a command-specific
response even when the operation caller does not need that response. The
native parser validates the packet length and command status, records the
actual response length, and safely discards the body only when the caller
passes no output buffer. This is required by `Set Powered`, whose successful
response contains the four-byte updated settings mask.

The manager contract reports its fixed built-in provider as the active
automatic choice. It never emits a private `selection` enum, so generic clients
can consume the same `automatic`/`manual` contract as the Python manager.
Unknown fields are rejected before a write. Error messages are constant and
never repeat request field names or values, so a rejected credential cannot
cross the response boundary.

The native roots can be overridden only for tests with the corresponding
MSYS_HAL_POWER_ROOT, MSYS_HAL_THERMAL_ROOT, MSYS_HAL_BACKLIGHT_ROOT,
MSYS_HAL_NETWORK_ROOT, MSYS_HAL_BLUETOOTH_ROOT, MSYS_HAL_RFKILL_ROOT, and
MSYS_HAL_NATIVE_INPUT_ROOT variables. Production manifests do not set them.

## Build

The build consumes the checked-in C SDK source from an adjacent msys-sdk
repository and produces a package-owned binary:

    cd /path/to/msys-hal
    MSYS_SDK_DIR=/path/to/msys-sdk ./scripts/build-native.sh clean all

The Makefile and fallback shell path both use C11 with strict warning flags.
The fallback is useful on development images that have cc but omit make; no
package installation is attempted.

## aarch64 smoke and RSS

Run this from the source tree copied to a development area on the target. It
does not install, activate, or restart MSYS:

    MSYS_SDK_DIR=/opt/msys-dev/msys-sdk ./scripts/native-target-smoke.sh

The script requires uname -m to be aarch64, compiles the binary, verifies its
format when file is available, scans hardware with --self-check, and prints
rss_kib read from /proc/self/status. The first OpenStick run on 2026-07-12
compiled as an ARM aarch64 PIE, found 21 devices, and reported 440-556 KiB RSS
during the scan.

Protocol conformance uses an inherited AF_UNIX/SOCK_SEQPACKET descriptor and
synthetic sysfs roots:

    MSYS_HAL_NATIVE_BINARY=/path/to/msys-hal-native \
      python3 -m unittest tests.test_native_hal -v

The tests cover handshake, deterministic inventory, typed state, verified
backlight/rfkill writes, hard-block refusal, strict unknown-field rejection,
credential non-disclosure, provider metadata, explicit unsupported methods,
and RSS reporting.
