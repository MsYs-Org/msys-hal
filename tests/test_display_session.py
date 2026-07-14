from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from msys_hal.display_session import (
    DISPLAY_SESSION_SCHEMA,
    DisplaySessionReader,
    normalized_matrix,
    validate_display_session,
)
from msys_hal.errors import UnavailableError


def session_document(
    *,
    display: str = ":73",
    observed: int | None = None,
    mode: str = "ch347-direct",
) -> dict:
    return {
        "schema": DISPLAY_SESSION_SCHEMA,
        "state": "ready",
        "provider": "org.example.display:output",
        "generation": 4,
        "display": display,
        "geometry": {"width": 320, "height": 480, "depth": 24},
        "input_transform": {
            "enabled": True,
            "mode": mode,
            "device": "Example Touch",
            "space": "normalized-display",
            "matrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
            "source": "test-observation",
            "verified": True,
        },
        "observed_at_unix_ms": observed or int(time.time() * 1000),
    }


class DisplaySessionContractTests(unittest.TestCase):
    def test_contract_is_strict_and_matrix_is_normalized(self) -> None:
        document = session_document(display=":91")
        self.assertEqual(validate_display_session(document)["display"], ":91")
        self.assertEqual(
            normalized_matrix([1.0, 0, 0, 0, 1, 0, 0, 0, 1]),
            [1, 0, 0, 0, 1, 0, 0, 0, 1],
        )
        for matrix in (
            [1] * 8,
            [1, 0, 0, 0, 1, 0, 1, 0, 1],
            [1, 0, 0, 0, 1, 0, 0, 0, float("nan")],
        ):
            with self.subTest(matrix=matrix), self.assertRaises(ValueError):
                normalized_matrix(matrix)
        malformed = session_document()
        malformed["private_path"] = "/sys/private"
        with self.assertRaises(ValueError):
            validate_display_session(malformed)

    def test_reader_prefers_newest_valid_state_without_display_assumptions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = root / "older.json"
            newer = root / "newer.json"
            invalid_future = root / "future.json"
            now = int(time.time() * 1000)
            older.write_text(json.dumps(session_document(display=":24", observed=now - 10)), encoding="utf-8")
            newer.write_text(json.dumps(session_document(display=":0", observed=now)), encoding="utf-8")
            invalid_future.write_text(
                json.dumps(session_document(display=":99", observed=now + 60_000)),
                encoding="utf-8",
            )

            state = DisplaySessionReader(
                (older, newer, invalid_future),
                max_age_ms=1000,
            ).load()

            self.assertEqual(state["display"], ":0")

    def test_missing_stale_and_symlink_state_are_structured_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "stale.json"
            stale.write_text(
                json.dumps(session_document(observed=int(time.time() * 1000) - 5000)),
                encoding="utf-8",
            )
            with self.assertRaises(UnavailableError) as caught:
                DisplaySessionReader((stale,), max_age_ms=100).load()
            self.assertEqual(caught.exception.details["reason"], "stale-state")

            missing = root / "missing.json"
            with self.assertRaises(UnavailableError) as caught:
                DisplaySessionReader((missing,), max_age_ms=0).load()
            self.assertEqual(caught.exception.details["reason"], "no-state")

            target = root / "target.json"
            target.write_text(json.dumps(session_document()), encoding="utf-8")
            link = root / "link.json"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            with self.assertRaises(UnavailableError) as caught:
                DisplaySessionReader((link,), max_age_ms=0).load()
            self.assertEqual(caught.exception.details["reason"], "invalid-state")

    def test_environment_uses_runtime_contract_path(self) -> None:
        reader = DisplaySessionReader.from_environment({
            "MSYS_RUNTIME_DIR": "/tmp/example-runtime",
            "MSYS_HAL_DISPLAY_SESSION_MAX_AGE_MS": "1000",
        })
        self.assertEqual(reader.paths[0], Path("/tmp/example-runtime/display-session.json"))
        self.assertNotIn(":24", os.fspath(reader.paths[0]))


if __name__ == "__main__":
    unittest.main()
