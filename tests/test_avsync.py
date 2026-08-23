from __future__ import annotations

import unittest

import numpy as np

from dualmaker.avsync import _active_audio_delay, _match_window


class AVSyncTests(unittest.TestCase):
    def test_frame_window_returns_source_position(self) -> None:
        rng = np.random.default_rng(17)
        source = rng.integers(0, 256, size=(80, 64), dtype=np.uint8).astype(np.float32)
        target = source[23:55].copy()
        match = _match_window(source, target)
        self.assertIsNotNone(match)
        assert match is not None
        position, score = match
        self.assertEqual(position, 23)
        self.assertGreater(score, 0.99)

    def test_active_audio_delay_excludes_manual_override(self) -> None:
        points = [(0.0, 0.0, 2.25), (100.0, 100.0, 3.25)]
        self.assertAlmostEqual(_active_audio_delay(points, 50.0, 0.25), 2.0)
        self.assertAlmostEqual(_active_audio_delay(points, 150.0, 0.25), 3.0)


if __name__ == "__main__":
    unittest.main()
