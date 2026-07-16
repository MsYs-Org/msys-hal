# Transactional touch calibration

The optional mIPC interface `org.msys.hal.touch-calibration.v1` is provided by
the on-demand `ch347-output-control` component. It implements `get`, `preview`,
`confirm`, `cancel`, and `undo`. No daemon is added: Core may stop the Python
provider after its 70-second idle timeout.

The effective normalized affine is separate from the legacy raw XPT2046
calibration. The exact mapping order is raw X/Y swap, raw min/max normalization,
normalized X/Y inversion, normalized 3x3 affine, then physical output rotation
into X11 logical coordinates.

The affine final row must be `0,0,1`, the 2x2 part must be invertible, all
coefficients are bounded to `[-4,4]`, and mapped panel corners are bounded to
`[-1,2]`. The sink clamps the final normalized coordinate to the panel.

`preview` writes only the runtime effective document and signals the current
generation-owned output provider. The running C sink reloads the document and
writes its own generation/revision/matrix receipt. HAL reports success only
after reading that receipt back. It never restarts X11, SPI streaming, Shell,
or applications.

Only one preview exists at a time. Its token is bounded, opaque, and
single-use. A 1-60 second TTL, provider exit, cancellation, or rotation/geometry
change restores the saved effective matrix. `confirm` atomically persists the
verified matrix and one previous value. `undo` swaps those two values, so the
same operation also provides one-level redo without a history database.
