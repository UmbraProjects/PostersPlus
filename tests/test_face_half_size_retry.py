"""face_detect retries at half size when a native-size pass finds no faces."""

import unittest
from unittest import mock

import numpy as np
from PIL import Image

import face_detect


class _FakeYuNet:
    """Finds one face only in inputs narrower than ``max_w``."""

    def __init__(self, max_w):
        self.max_w = max_w
        self.sizes = []

    def setInputSize(self, size):
        self.size = size

    def detect(self, arr):
        h, w = arr.shape[:2]
        self.sizes.append((w, h))
        if w >= self.max_w:
            return 0, None
        return 1, np.array([[w * 0.5, h * 0.1, w * 0.2, h * 0.4] + [0] * 10 + [0.9]],
                           dtype=np.float32)


@unittest.skipUnless(face_detect._HAS_CV2, "needs OpenCV")
class HalfSizeRetryTests(unittest.TestCase):

    def _boxes(self, det, size=(1280, 720)):
        with mock.patch.object(face_detect, "_ensure_detector", lambda: det):
            return face_detect.detect_face_boxes(Image.new("RGB", size))

    def test_close_up_found_at_half_size_maps_back(self):
        det = _FakeYuNet(max_w=1000)
        boxes = self._boxes(det)
        self.assertEqual(det.sizes, [(1280, 720), (640, 360)])
        x, y, fw, fh, score = boxes[0]
        self.assertAlmostEqual(x, 640.0)
        self.assertAlmostEqual(fw, 256.0)
        self.assertAlmostEqual(fh, 288.0)
        self.assertAlmostEqual(score, 0.9, places=5)

    def test_native_hit_skips_retry(self):
        det = _FakeYuNet(max_w=10_000)
        self.assertEqual(len(self._boxes(det)), 1)
        self.assertEqual(len(det.sizes), 1)

    def test_small_images_are_not_retried(self):
        det = _FakeYuNet(max_w=0)
        self.assertEqual(self._boxes(det, (500, 750)), [])
        self.assertEqual(len(det.sizes), 1)

    def test_nothing_at_either_size(self):
        det = _FakeYuNet(max_w=0)
        self.assertEqual(self._boxes(det), [])
        self.assertEqual(len(det.sizes), 2)

    def test_signature_names_the_retry(self):
        self.assertIn(":half640", face_detect.DETECTOR_SIGNATURE)


if __name__ == "__main__":
    unittest.main()
