"""Burned-in-text scans: the direct detector path and the two-pass cascade.

Runs against a fake RapidOCR detector, so neither the model nor its session
is needed.
"""

from contextlib import contextmanager
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import text_detect


def _box(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)


class _FakeDet:
    """Stands in for RapidOCR's TextDetector: records the network input and
    returns fixed boxes, bottom one first, with a score per box."""

    def __init__(self, boxes, scores):
        self.boxes, self.scores = boxes, scores
        self.inputs = []

    def session(self, net_in):
        self.inputs.append(net_in)
        return np.zeros((1, 1) + net_in.shape[2:], dtype=np.float32)

    def postprocess_op(self, _pred, ori_shape):
        h, w = ori_shape
        # Boxes are given as fractions of the image the pass saw.
        scale = np.array([w, h], dtype=np.float32)
        return np.stack([b * scale for b in self.boxes]), list(self.scores)


class _FakeOCR:
    def __init__(self, det):
        self.text_det = det


def _patch_detector(det):
    @contextmanager
    def borrow():
        yield _FakeOCR(det)
    return mock.patch.multiple(
        text_detect,
        _borrow_ocr=borrow,
        _ensure_model=lambda: object(),
    )


class DirectDetectorTests(unittest.TestCase):

    def test_scores_stay_with_their_boxes_when_sorted(self):
        bottom, top = _box(0.1, 0.8, 0.9, 0.9), _box(0.2, 0.1, 0.8, 0.2)
        det = _FakeDet([bottom, top], [0.9, 0.4])
        with _patch_detector(det):
            boxes, scores, _w, _h = text_detect._detect(
                Image.new("RGB", (500, 750)))
        # Sorted top to bottom, each still carrying its own score.
        self.assertLess(boxes[0][0, 1], boxes[1][0, 1])
        self.assertEqual(scores, [0.4, 0.9])

    def test_poster_runs_at_native_size_in_bgr(self):
        det = _FakeDet([_box(0.1, 0.1, 0.5, 0.2)], [0.8])
        with _patch_detector(det):
            text_detect._detect(Image.new("RGB", (500, 750), (255, 0, 0)))
        net_in = det.inputs[0]
        self.assertEqual(net_in.shape, (1, 3, 736, 512))
        self.assertEqual(net_in.dtype, np.float32)
        # Pure red, normalised to [-1, 1], lands in the last (R) channel.
        self.assertAlmostEqual(float(net_in[0, 2].mean()), 1.0, places=5)
        self.assertAlmostEqual(float(net_in[0, 0].mean()), -1.0, places=5)

    def test_scaled_pass_reports_source_coordinates(self):
        det = _FakeDet([_box(0.1, 0.5, 0.9, 0.6)], [0.8])
        with _patch_detector(det):
            boxes, _scores, width, height = text_detect._detect(
                Image.new("RGB", (500, 750)), 0.65)
        self.assertEqual((width, height), (500, 750))
        # The network saw ~0.65x (325x488 → 320x480), the box maps back.
        self.assertEqual(det.inputs[0].shape[2:], (480, 320))
        np.testing.assert_allclose(boxes[0][0], [50, 375], atol=1)
        np.testing.assert_allclose(boxes[0][2], [450, 450], atol=1)

    def test_no_boxes(self):
        det = _FakeDet([], [])
        det.postprocess_op = lambda _pred, _shape: ([], [])
        with _patch_detector(det):
            boxes, scores, _w, _h = text_detect._detect(Image.new("RGB", (500, 750)))
        self.assertEqual((boxes, scores), ([], []))


class CascadeTests(unittest.TestCase):

    def _scan(self, low_boxes, low_verdict, full_verdict=True):
        passes = []

        def detect(image, scale=1.0):
            passes.append(scale)
            boxes = low_boxes if scale < 1.0 else [_box(0, 0, 10, 10)]
            return boxes, [0.5] * len(boxes), 500, 750

        def verdict(image, boxes, scores, width, height, **_kw):
            detected = low_verdict if len(passes) == 1 else full_verdict
            return detected, [], [], 0

        with mock.patch.object(text_detect, "_detect", detect), \
             mock.patch.object(text_detect, "_verdict", verdict):
            result = text_detect.poster_has_burned_in_text(
                Image.new("RGB", (500, 750)), title="Title")
        return result, passes

    def test_text_at_small_scale_stands(self):
        result, passes = self._scan([_box(0, 600, 400, 700)], low_verdict=True)
        self.assertTrue(result)
        self.assertEqual(passes, [text_detect._CASCADE_SCALE])

    def test_clear_with_nothing_boxed_stands(self):
        result, passes = self._scan([], low_verdict=False)
        self.assertFalse(result)
        self.assertEqual(len(passes), 1)

    def test_clear_with_only_specks_stands(self):
        # 20x20 px of a 500x750 poster: ~0.1%, under the rescan area.
        result, passes = self._scan([_box(0, 0, 20, 20)], low_verdict=False)
        self.assertFalse(result)
        self.assertEqual(len(passes), 1)

    def test_clear_with_title_sized_box_rescans_at_native_size(self):
        # 200x30 px: 1.6% of the poster.
        result, passes = self._scan(
            [_box(150, 600, 350, 630)], low_verdict=False, full_verdict=True)
        self.assertTrue(result)
        self.assertEqual(passes, [text_detect._CASCADE_SCALE, 1.0])

    def test_native_pass_can_clear_it(self):
        result, passes = self._scan(
            [_box(150, 600, 350, 630)], low_verdict=False, full_verdict=False)
        self.assertFalse(result)
        self.assertEqual(len(passes), 2)

    def test_signature_names_the_cascade(self):
        self.assertIn("-cs65-ca50-", text_detect.DETECT_RES_SIG)


if __name__ == "__main__":
    unittest.main()
