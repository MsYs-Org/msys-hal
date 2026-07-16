# Removable storage HAL

The `storage` role is implemented inside the existing small C native HAL
process. No second resident process or duplicate storage state machine is
introduced. It is not PID 1 and requires no systemd, D-Bus, udev, udisks or
target package manager.

## Discovery and mounting

The provider scans bounded entries in `/sys/class/block` and accepts only
`sd*` and `mmcblk*` partitions (or an unpartitioned removable device). A device
must be marked removable, be on a USB sysfs path, or be named in the trusted
operator setting `MSYS_HAL_STORAGE_ALLOW`. Devices backing `/`, `/boot`,
`/usr`, `/var`, `/opt` or `/home` are excluded. `/dev/disk/by-label` and
`/dev/disk/by-uuid` are optional read-only metadata; the mount directory uses
the validated block-device id so duplicate labels cannot stack mounts.

The default mount root is `/media/msys`. Generated child names contain no path
separators, and an existing symbolic-link target is rejected. Commands are
invoked as argv arrays, never through a shell:

```text
mount -o nosuid,nodev,noexec /dev/<validated-name> /media/msys/<stable-id>
umount /media/msys/<stable-id>
```

Read-only devices add `ro`. Only mounts discovered below the managed root may
be unmounted through the API. A mount made elsewhere remains visible but is
not taken over. Command failures are returned as typed `HAL_STORAGE_*` errors,
and the bounded state retains the command return code; success is never
simulated.

Automatic mounting is enabled by default and persisted atomically in
`$MSYS_STATE_DIR/storage.json`. Set `MSYS_HAL_STORAGE_AUTOMOUNT=0` for a fresh
installation default, or call `set_config`. A manual unmount is respected
until the medium is removed and inserted again.

## RPC

Target `role:storage` or interface `org.msys.hal.storage.v1`:

- `list_volumes({"refresh": false})` returns the cached bounded volume list.
- `get_state({})` returns configuration, revision and the cached volume list.
- `refresh({})` performs an explicit scan and applies the auto-mount policy.
- `mount({"volume_id": "storage:sda1", "read_only": false})` mounts one discovered volume.
- `unmount({"volume_id": "storage:sda1"})` unmounts one MSYS-managed volume.
- `set_config({"auto_mount": true})` persists the auto-mount preference.

The component publishes `msys.hal.storage.changed` only when its bounded state
changes.

The preferred event source is Linux `NETLINK_KOBJECT_UEVENT` filtered to the
block subsystem. If opening it fails, one low-frequency refresh runs every 30
seconds (configurable from 10 to 300 seconds). Calls can always request an
immediate refresh.
