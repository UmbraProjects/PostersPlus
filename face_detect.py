"""
face_detect.py — face-aware backdrop cropping.

When a landscape backdrop is cropped to a portrait poster, the hand-crafted
saliency heuristic can be fooled by large warm-toned, textured backgrounds
(e.g. a wooden bench / railings) and push the crop to an empty edge, slicing
the actual subject off.  A proper face detector is the robust signal for
"keep the people in frame".

Uses OpenCV's YuNet detector — accurate on small and profile faces, with a tiny
(~230 KB) ONNX model bundled in models/ (no runtime download).  If OpenCV or the
model are unavailable, face detection soft-disables and the caller falls back to
the saliency crop.
"""
from __future__ import annotations

import logging
import os
import threading

import numpy as np

import config as _cfg

logger = logging.getLogger(__name__)

try:
    import cv2 as _cv2
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

_MODEL_PATH = _cfg.YUNET_MODEL_PATH or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "face_detection_yunet.onnx",
)
_SCORE_THRESHOLD = 0.6
# An image this wide with no faces found is scanned again at half size.  YuNet
# misses big close-ups at native size — a 430 px face in a 1280 px backdrop
# (TMDB 1751701) scores 0.84 at half size and nothing at full — so without the
# retry the crop falls back to saliency and can frame a wall or a hood.  On 300
# cached backdrops it found faces in 12 of the 56 with none, improving 4 crops
# and worsening none.
_RETRY_MIN_WIDTH = 640

# Part of every face_box_cache key, so a different model or threshold never
# reuses boxes another detector found.
DETECTOR_SIGNATURE = (
    f"yunet:{os.path.basename(_MODEL_PATH)}:{_SCORE_THRESHOLD}:half{_RETRY_MIN_WIDTH}"
)

_detector = None
_load_failed = False
_lock = threading.Lock()
# cv2 detectors aren't thread-safe; serialise inference (setInputSize+detect)
# so concurrent callers can't corrupt the shared detector state.
_infer_lock = threading.Lock()


def _ensure_detector():
    """Create (once) the YuNet detector. Thread-safe; soft-fails to None."""
    global _detector, _load_failed
    if _detector is not None or _load_failed or not _HAS_CV2:
        return _detector
    with _lock:
        if _detector is not None or _load_failed:
            return _detector
        try:
            if not os.path.exists(_MODEL_PATH) or not hasattr(_cv2, "FaceDetectorYN"):
                logger.warning("YuNet model/API unavailable — face-aware crop disabled")
                _load_failed = True
                return None
            _detector = _cv2.FaceDetectorYN.create(
                _MODEL_PATH, "", (320, 320), score_threshold=_SCORE_THRESHOLD
            )
            logger.info("YuNet face detector loaded")
        except Exception as exc:
            logger.warning(f"YuNet load failed — face-aware crop disabled: {exc}")
            _load_failed = True
    return _detector


def detect_faces(image) -> list[tuple[float, float, float]]:
    """
    Return [(centre_x, width, weight), …] for detected faces, where
    weight = score³ × area.

    Empty list when there are no faces or detection is unavailable.  Called from
    the thread pool (backdrop crop), so inference is serialised with _infer_lock.

    Why score is cubed rather than used linearly: YuNet occasionally throws a
    low-confidence "face" that's actually a large blurred blob in the
    background (a dark dresser / sofa cushions reading as a face-ish shape at
    just-above-_SCORE_THRESHOLD confidence).  Such a blob's bounding box can be
    2-3× the area of the genuine face elsewhere in frame, so a linear
    `score × area` lets it outrank — and the crop centres on empty background,
    slicing the actual subject almost entirely out of frame (observed in the
    wild on TMDB 450545 "Secret Santa", where a score=0.6/509×613px background
    blob beat the real score=0.9/293×482px face purely on size, landing the
    crop on a couch instead of the actress).  Cubing the score punishes
    borderline-confidence detections heavily enough that a genuine, confident
    face reliably wins regardless of the false positive's inflated bbox, while
    still letting bounding-box area break ties between two similarly-confident
    real faces (e.g. lead vs. background extra) as originally intended.
    """
    return [(x + fw / 2.0, fw, max(score, 0.0) ** 3 * fw * fh)
            for x, _y, fw, fh, score in detect_face_boxes(image)]


def available() -> bool:
    """Whether a detector is loaded — so an empty result can be told apart from
    "detection unavailable", which must not be cached as "no faces"."""
    return _ensure_detector() is not None


def detect_face_boxes(image) -> list[tuple[float, float, float, float, float]]:
    """[(x, y, width, height, score), …] in pixels for detected faces.

    Empty list when there are no faces or detection is unavailable.  The tinted
    vignette uses the boxes to keep faces out of its colour vote (see
    main._fog_profile); detect_faces reduces them to the crop's weights.
    """
    det = _ensure_detector()
    if det is None:
        return []
    try:
        arr = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()  # RGB→BGR
        h, w = arr.shape[:2]
        if h == 0 or w == 0:
            return []
        faces = _detect_at(det, arr)
        if not faces and w >= _RETRY_MIN_WIDTH:
            # Nothing at native size: look again at half size, for faces too
            # large for the model to pick up at native size.
            small = _cv2.resize(arr, (w // 2, h // 2), interpolation=_cv2.INTER_AREA)
            sx, sy = w / (w // 2), h / (h // 2)
            faces = [(x * sx, y * sy, fw * sx, fh * sy, score)
                     for x, y, fw, fh, score in _detect_at(det, small)]
        return faces
    except Exception as exc:
        logger.warning(f"face detect error: {exc}")
        return []


def _detect_at(det, bgr) -> list[tuple[float, float, float, float, float]]:
    h, w = bgr.shape[:2]
    with _infer_lock:
        det.setInputSize((w, h))
        _n, faces = det.detect(bgr)
    if faces is None:
        return []
    return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1]))
            for f in faces]
