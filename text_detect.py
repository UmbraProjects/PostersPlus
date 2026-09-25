"""PP-OCRv5 burned-in text detection for posters and backdrop crops."""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
from contextlib import contextmanager
from difflib import SequenceMatcher
import logging
from queue import LifoQueue
import os
import re
import threading
import unicodedata
import urllib.request

import cv2
import numpy as np
from PIL import Image

from config import EFFECTIVE_CPUS, TEXTLESS_DETECTION_CONCURRENCY as _CONCURRENCY
import config as _cfg

logger = logging.getLogger(__name__)

try:
    from rapidocr import RapidOCR
    _HAS_RAPIDOCR = True
    _RAPIDOCR_IMPORT_ERROR = None
except Exception as exc:
    RapidOCR = None
    _HAS_RAPIDOCR = False
    _RAPIDOCR_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_MODEL_URL = _cfg.PPOCR_MODEL_URL
_MODEL_SHA256 = _cfg.PPOCR_MODEL_SHA256
_BAKED_MODEL = "/app/models/ch_PP-OCRv5_det_mobile.onnx"
_MODEL_PATH = _cfg.PPOCR_MODEL_PATH or (
    _BAKED_MODEL if os.path.exists(_BAKED_MODEL)
    else "/app/cache/ch_PP-OCRv5_det_mobile.onnx"
)
_RAPIDOCR_MODELS = importlib.resources.files("rapidocr").joinpath("models") if _HAS_RAPIDOCR else None


def _find_bundled_model(models_path, keyword: str) -> str:
    """Find a bundled rapidocr .onnx model by keyword, tolerating version renames."""
    if models_path is None:
        return ""
    import pathlib
    d = pathlib.Path(str(models_path))
    if not d.exists():
        return ""
    try:
        matches = sorted(
            str(f) for f in d.iterdir()
            if f.suffix == ".onnx" and keyword in f.stem.lower()
        )
        if not matches:
            logger.warning(
                f"No bundled rapidocr model found for keyword '{keyword}' in {d}; "
                f"available: {[f.name for f in d.iterdir() if f.suffix == '.onnx']}"
            )
        return matches[0] if matches else ""
    except Exception as exc:
        logger.warning(f"Could not scan rapidocr models dir {d}: {exc}")
        return ""


_CLS_MODEL_PATH = _find_bundled_model(_RAPIDOCR_MODELS, "cls")
_REC_MODEL_PATH = _find_bundled_model(_RAPIDOCR_MODELS, "rec")

# All declared in config.py (so the admin dashboard can set them); the only
# rule kept here is that the wide-box fallback never outranks the box threshold.
_BOX_THRESHOLD      = _cfg.PPOCR_BOX_THRESHOLD
_WIDE_BOX_THRESHOLD = min(_BOX_THRESHOLD, _cfg.PPOCR_WIDE_BOX_THRESHOLD)
_WIDE_MIN_ASPECT    = _cfg.PPOCR_WIDE_MIN_ASPECT
_WIDE_MIN_AREA      = _cfg.PPOCR_WIDE_MIN_AREA
_WIDE_MIN_Y         = _cfg.PPOCR_WIDE_MIN_Y
_SCAN_TOP           = _cfg.TEXTLESS_SCAN_TOP

# Scans run as a cascade.  The detector's cost is dominated by its full-resolution
# head, so every image is first scanned at _CASCADE_SCALE (~2x cheaper).  A text
# verdict there stands; a clear one is re-checked at native size only when the
# small pass still boxed something covering _CASCADE_RESCAN_AREA of the image —
# thin or condensed titles that fade at 0.65x still leave a box that size.
# About a quarter of scans take the second pass.  Measured on ~2,900 cached
# posters, eye-checking every verdict that changed: 4 wrong against 25 for the
# old single native-size pass, whose extra errors were missed stylised titles
# and scene text (signs, shirts, chalkboards) that the small pass never boxes.
_CASCADE_SCALE = 0.65
_CASCADE_RESCAN_AREA = 0.005

# Sizing is driven by the cores this process may actually use, not the host's
# core count — see config.effective_cpus().  Sessions divide the thread budget
# between them, and ONNX throughput degrades sharply once the total exceeds the
# real budget, so the split has to come out of a correct figure.
_MODEL_SESSIONS = max(1, min(EFFECTIVE_CPUS, _CONCURRENCY))
# No arbitrary ceiling: scaling was still near-linear at 4 threads on 4 cores
# (3.2x vs one thread) with no sign of saturation, so a 16-core host should get
# 16.  The floor of 1 matters more than any cap — oversubscription is far more
# expensive than undersubscription (on a 2-CPU budget: 135 ms at 2 threads,
# 299 ms at 6, 409 ms at 8).
_ORT_THREADS = max(1, EFFECTIVE_CPUS // _MODEL_SESSIONS)
DETECT_RES_SIG = (
    f"ppocrv5m-r8-cs{int(round(_CASCADE_SCALE * 100))}"
    f"-ca{int(round(_CASCADE_RESCAN_AREA * 10000))}"
    f"-c{int(round(_BOX_THRESHOLD * 100))}"
    f"-wc{int(round(_WIDE_BOX_THRESHOLD * 100))}"
    f"-wa{int(round(_WIDE_MIN_ASPECT * 10))}"
    f"-wr{int(round(_WIDE_MIN_AREA * 10000))}"
    f"-wy{int(round(_WIDE_MIN_Y * 100))}"
    f"-t{int(round(_SCAN_TOP * 100))}"
)

_ocr_pool = None
_ocr_sessions = []
_model_lock = threading.Lock()
_load_failed = False
_load_error = None


def text_detection_available() -> bool:
    """True when the PP-OCR runtime is importable."""
    return _HAS_RAPIDOCR


def text_detection_status() -> str:
    """Compact runtime status suitable for startup and request logs."""
    if not _HAS_RAPIDOCR:
        return f"RapidOCR import failed ({_RAPIDOCR_IMPORT_ERROR})"
    if _ocr_pool is not None:
        return (
            f"ready ({DETECT_RES_SIG}, model={_MODEL_PATH}, "
            f"sessions={_MODEL_SESSIONS}, ort_threads={_ORT_THREADS})"
        )
    if _load_failed:
        return f"model load failed ({_load_error})"
    return f"not loaded (model={_MODEL_PATH})"


def _valid_model(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) < 1_000_000:
        return False
    if _cfg.PPOCR_SKIP_MODEL_HASH:
        return True
    digest = hashlib.sha256()
    with open(path, "rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == _MODEL_SHA256


def _new_ocr_session():
    params = {
        "Global.use_cls": False,
        "Global.use_rec": False,
        "Global.log_level": "error",
        "Det.model_path": _MODEL_PATH,
        # Read by the DB post-processor, which _run_detector borrows.
        "Det.box_thresh": 0.3,
        "EngineConfig.onnxruntime.intra_op_num_threads": _ORT_THREADS,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        # Disable the ORT CPU memory arena so freed tensor allocations are
        # returned to the OS rather than held at the inference high-water mark
        # indefinitely.  Slightly slower first inference; no steady-state cost.
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
    }
    # Point RapidOCR at the bundled read-only models so it doesn't try to write
    # to a writable alias path.  Omit the key entirely when the model wasn't
    # found (e.g. rapidocr renamed it in a minor release) — RapidOCR will use
    # its own default, which is safer than a guaranteed FileNotFoundError.
    if _CLS_MODEL_PATH:
        params["Cls.model_path"] = _CLS_MODEL_PATH
    if _REC_MODEL_PATH:
        params["Rec.model_path"] = _REC_MODEL_PATH
    return RapidOCR(params=params)


def _ensure_model():
    """Download and load the bounded PP-OCR session pool once."""
    global _ocr_pool, _ocr_sessions, _load_failed, _load_error
    if not _HAS_RAPIDOCR:
        if not _load_failed:
            _load_error = _RAPIDOCR_IMPORT_ERROR
            _load_failed = True
            logger.warning(f"PP-OCR runtime unavailable: {_RAPIDOCR_IMPORT_ERROR}")
        return None
    if _ocr_pool is not None or _load_failed:
        return _ocr_pool
    with _model_lock:
        if _ocr_pool is not None or _load_failed:
            return _ocr_pool
        try:
            if not _valid_model(_MODEL_PATH):
                logger.info(
                    "Downloading PP-OCRv5 Mobile model (one-time) "
                    f"to {_MODEL_PATH}"
                )
                os.makedirs(os.path.dirname(_MODEL_PATH) or ".", exist_ok=True)
                tmp = _MODEL_PATH + ".part"
                urllib.request.urlretrieve(_MODEL_URL, tmp)
                if not _valid_model(tmp):
                    raise ValueError("downloaded model failed SHA-256 validation")
                os.replace(tmp, _MODEL_PATH)

            sessions = [_new_ocr_session() for _ in range(_MODEL_SESSIONS)]
            pool = LifoQueue(maxsize=_MODEL_SESSIONS)
            for session in sessions:
                pool.put(session)
            _ocr_sessions = sessions
            _ocr_pool = pool
            logger.info(
                "PP-OCRv5 Mobile text detector ready: "
                f"signature={DETECT_RES_SIG}, model={_MODEL_PATH}, "
                f"rapidocr={importlib.metadata.version('rapidocr')}, "
                f"sessions={_MODEL_SESSIONS}, ort_threads={_ORT_THREADS}, "
                f"threshold={_BOX_THRESHOLD:.2f}, "
                f"wide_threshold={_WIDE_BOX_THRESHOLD:.2f}, "
                f"wide_aspect={_WIDE_MIN_ASPECT:.2f}, "
                f"wide_area={_WIDE_MIN_AREA:.4f}"
            )
        except Exception as exc:
            _load_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "PP-OCR model unavailable; text detection disabled: "
                f"{_load_error}"
            )
            _load_failed = True
    return _ocr_pool



@contextmanager
def _borrow_ocr():
    pool = _ensure_model()
    if pool is None:
        yield None
        return
    session = pool.get()
    try:
        yield session
    finally:
        pool.put(session)


def warm_model() -> bool:
    return _ensure_model() is not None


# RapidOCR's own limits: it shrinks anything over 2000 px before detecting, and
# pads rather than scans images this thin, which no poster or backdrop is.
_MAX_DETECT_SIDE = 2000
_MIN_DETECT_SIDE = 31
_MAX_DETECT_ASPECT = 8.0


def _run_detector(ocr, rgb: np.ndarray):
    """One PP-OCR detection pass over an RGB array, in that array's coordinates.

    Drives RapidOCR's detector session and DB post-processor directly rather
    than through ``ocr(...)``: that path normalises in float64, crops every box
    for a recogniser we don't run, and sorts the boxes without their scores —
    in most images each box came back paired with another box's score.
    """
    det = ocr.text_det
    height, width = rgb.shape[:2]
    # RapidOCR's max-side rule (TextDetector.get_preprocess): a 500x750 poster
    # runs at native size, rounded to the network's multiple of 32 (512x736).
    longest = max(height, width)
    limit = 960 if longest < 960 else 1500 if longest < 1500 else 2000
    ratio = min(1.0, limit / longest)
    net_h = max(32, int(round(int(height * ratio) / 32) * 32))
    net_w = max(32, int(round(int(width * ratio) / 32) * 32))
    net_in = cv2.resize(rgb, (net_w, net_h)).astype(np.float32)
    # (v / 255 - 0.5) / 0.5, in the BGR, CHW layout the model was trained on.
    net_in *= 2.0 / 255.0
    net_in -= 1.0
    net_in = np.ascontiguousarray(net_in[:, :, ::-1].transpose(2, 0, 1))[None]
    boxes, scores = det.postprocess_op(det.session(net_in), (height, width))
    if len(boxes) == 0:
        return [], []
    boxes = np.asarray(boxes, dtype=np.float32)
    # Top-to-bottom, then left-to-right, keeping each score with its box.
    order = np.lexsort((boxes[:, 0, 0], boxes[:, 0, 1]))
    return [boxes[i] for i in order], [float(scores[i]) for i in order]


def _detect(image, scale: float = 1.0):
    """Detect text boxes on ``image`` shrunk by ``scale``.

    Boxes come back in ``image``'s own coordinates whatever the scale, so the
    rules downstream see the same geometry from either cascade pass.
    """
    if _ensure_model() is None:
        return None, None, 0, 0
    width, height = image.size
    if not height or not width:
        return None, None, width, height
    scale = min(scale, _MAX_DETECT_SIDE / max(width, height))
    pil_image = image.convert("RGB")
    try:
        if scale < 1.0:
            small = pil_image.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.BILINEAR,
            )
            pil_image.close()
            pil_image = small
        rgb = np.asarray(pil_image)
        det_h, det_w = rgb.shape[:2]
        if (min(det_h, det_w) < _MIN_DETECT_SIDE
                or det_w / det_h > _MAX_DETECT_ASPECT):
            return [], [], width, height
        with _borrow_ocr() as ocr:
            boxes, scores = _run_detector(ocr, rgb)
        del rgb
    finally:
        pil_image.close()
    if (det_w, det_h) != (width, height):
        to_source = np.array([width / det_w, height / det_h], dtype=np.float32)
        boxes = [box * to_source for box in boxes]
    return boxes, scores, width, height


def _normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _title_terms(value: str) -> list[str]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return [
        term for term in re.findall(r"[a-z0-9]+", value.lower())
        if len(term) >= 4
    ]


def _text_matches_title(candidate: str, title: str) -> bool:
    candidate = _normalise_text(candidate)
    expected = _normalise_text(title)
    if len(candidate) < 4 or len(expected) < 4:
        return False
    if candidate in expected or expected in candidate:
        return True
    if len(candidate) < 6:
        return False
    if len(expected) >= 6 and SequenceMatcher(None, candidate, expected).ratio() >= 0.82:
        return True
    return any(
        SequenceMatcher(None, candidate, term).ratio() >= 0.82
        for term in _title_terms(title)
        if len(term) >= 6
    )


def _recognised_alpha_lengths(texts: list[str]) -> list[int]:
    return [
        len(re.sub(r"[^a-z]", "", text.lower()))
        for text in texts
    ]


def _recognised_title_match(
    image,
    title: str | list[str] | tuple[str, ...],
    boxes,
    scores,
) -> tuple[bool, list[str], int]:
    titles = [title] if isinstance(title, str) else list(title)
    titles = [value for value in titles if value]
    expected_titles = [_normalise_text(value) for value in titles]
    if not any(len(value) >= 4 for value in expected_titles):
        return False, [], 0

    source = image.convert("RGB")
    try:
        width, height = source.size
        image_area = max(1, width * height)
        texts = []
        centred_lines = 0
        for box, score in zip(boxes, scores):
            box = np.asarray(box, dtype=np.float32)
            box_width = float(box[:, 0].max() - box[:, 0].min())
            box_height = float(box[:, 1].max() - box[:, 1].min())
            aspect = box_width / max(1.0, box_height)
            area_ratio = (box_width * box_height) / image_area
            centre_x = float(box[:, 0].mean()) / max(1, width)
            title_candidate = aspect >= 1.5 and area_ratio >= _WIDE_MIN_AREA
            centred_candidate = (
                aspect >= _WIDE_MIN_ASPECT
                and area_ratio >= 0.0015
                and 0.25 <= centre_x <= 0.75
            )
            if (
                float(score) < _WIDE_BOX_THRESHOLD
                or not (title_candidate or centred_candidate)
            ):
                continue

            pad = max(3, int(box_height * 0.2))
            left   = max(0,     int(box[:, 0].min()) - pad)
            top    = max(0,     int(box[:, 1].min()) - pad)
            right  = min(width, int(box[:, 0].max()) + pad)
            bottom = min(height, int(box[:, 1].max()) + pad)
            pil_crop = source.crop((left, top, right, bottom))
            try:
                crop = np.asarray(pil_crop)
                with _borrow_ocr() as ocr:
                    result = ocr(crop, use_det=False, use_cls=False, use_rec=True)
            finally:
                pil_crop.close()
                del crop
            recognised = [] if result.txts is None else [
                str(text) for text in result.txts
            ]
            del result
            texts.extend(recognised)
            if centred_candidate and any(
                len(re.sub(r"[^a-z]", "", text.lower())) >= 5
                for text in recognised
            ):
                centred_lines += 1
            if title_candidate and any(
                _text_matches_title(text, alias)
                for text in recognised
                for alias in titles
            ):
                return True, texts, centred_lines
            if float(score) >= 0.80 and area_ratio >= 0.10:
                for text in recognised:
                    candidate = _normalise_text(text)
                    if any(
                        len(candidate) >= 6
                        and len(expected) >= 6
                        and SequenceMatcher(None, candidate, expected).ratio() >= 0.70
                        for expected in expected_titles
                    ):
                        return True, texts, centred_lines
        return False, texts, centred_lines
    finally:
        source.close()


def _qualifying_boxes(
    boxes,
    scores,
    width: int,
    height: int,
    conf: float,
    scan_top: float,
):
    cutoff = height * scan_top
    image_area = max(1, width * height)
    hits = []
    for box, score in zip(boxes, scores):
        box = np.asarray(box, dtype=np.float32)
        score = float(score)
        center_y = float(box[:, 1].mean())
        if center_y < cutoff:
            continue

        box_width = float(box[:, 0].max() - box[:, 0].min())
        box_height = float(box[:, 1].max() - box[:, 1].min())
        aspect = box_width / max(1.0, box_height)
        area_ratio = (box_width * box_height) / image_area
        is_wide_title = (
            score >= _WIDE_BOX_THRESHOLD
            and aspect >= _WIDE_MIN_ASPECT
            and area_ratio >= _WIDE_MIN_AREA
            and center_y / max(1, height) >= _WIDE_MIN_Y
        )
        if is_wide_title:
            hits.append((box, score, is_wide_title, aspect, area_ratio))
    return hits


def poster_has_burned_in_text(
    image,
    *,
    conf: float = _BOX_THRESHOLD,
    lower_region: float = _SCAN_TOP,
    title: str | list[str] | tuple[str, ...] | None = None,
    source: str = "poster",
    debug: bool = False,
) -> bool | None:
    """Return True/False for a completed scan, or None when unavailable."""
    try:
        if source not in ("poster", "backdrop"):
            raise ValueError(f"unknown text-detection source: {source}")
        boxes, scores, width, height = _detect(image, _CASCADE_SCALE)
        if boxes is None:
            return None
        scan = f"{_CASCADE_SCALE:g}x"
        verdict = _verdict(
            image, boxes, scores, width, height,
            conf=conf, lower_region=lower_region, title=title, source=source,
        )
        if not verdict[0] and _worth_full_scan(boxes, width, height):
            full = _detect(image)
            if full[0] is not None:
                boxes, scores, width, height = full
                scan = "1x"
                verdict = _verdict(
                    image, boxes, scores, width, height,
                    conf=conf, lower_region=lower_region, title=title,
                    source=source,
                )
        detected, hits, recognised, centred_lines = verdict
        if debug:
            candidates = []
            image_area = max(1, width * height)
            for box, score in zip(boxes, scores):
                box = np.asarray(box, dtype=np.float32)
                box_width = float(box[:, 0].max() - box[:, 0].min())
                box_height = float(box[:, 1].max() - box[:, 1].min())
                candidates.append(
                    f"{float(score):.3f}/a{box_width / max(1.0, box_height):.2f}"
                    f"/r{(box_width * box_height) / image_area:.4f}"
                    f"/y{float(box[:, 1].mean()) / max(1, height):.2f}"
                )
            best = max((score for _box, score, *_rest in hits), default=0.0)
            logger.info(
                f"text_detect (PP-OCRv5 Mobile, {scan}): boxes={len(hits)}, "
                f"best={best:.3f}, threshold={conf:.3f}, "
                f"source={source}, centred_lines={centred_lines}, "
                f"candidates=[{', '.join(candidates[:20])}], "
                f"recognised={recognised[:10]} -> {'TEXT' if detected else 'clear'}"
            )
        return detected
    except Exception as exc:
        logger.warning(f"text_detect error; scan unavailable: {exc}")
        return None


def _worth_full_scan(boxes, width: int, height: int) -> bool:
    """True when a clear small-scale pass still boxed something title-sized."""
    image_area = max(1, width * height)
    for box in boxes:
        box = np.asarray(box, dtype=np.float32)
        box_width = float(box[:, 0].max() - box[:, 0].min())
        box_height = float(box[:, 1].max() - box[:, 1].min())
        if box_width * box_height / image_area >= _CASCADE_RESCAN_AREA:
            return True
    return False


def _verdict(
    image,
    boxes,
    scores,
    width: int,
    height: int,
    *,
    conf: float,
    lower_region: float,
    title: str | list[str] | tuple[str, ...] | None,
    source: str,
):
    """Apply the text rules to one pass's boxes.

    Returns (detected, qualifying boxes, recognised text, centred lines).
    """
    hits = _qualifying_boxes(boxes, scores, width, height, conf, lower_region)
    recognised = []
    should_recognise = bool(title) and any(
        float(score) >= _WIDE_BOX_THRESHOLD
        for score in scores
    )
    detected = False
    centred_lines = 0
    if should_recognise:
        detected, recognised, centred_lines = _recognised_title_match(
            image, title, boxes, scores
        )
    alpha_lengths = _recognised_alpha_lengths(recognised)
    # Two centred lines are enough when OCR also sees substantial copy;
    # short two-line logos remain below these character thresholds.
    if (
        not detected
        and source == "poster"
        and centred_lines >= 2
        and sum(alpha_lengths) >= 30
        and max(alpha_lengths, default=0) >= 16
    ):
        detected = True
    # Recognition is primary because PP-OCR can confidently box broad scene
    # textures. Preserve a narrow escape hatch for unreadable poster titles.
    if not detected and source == "poster":
        has_readable_text = max(alpha_lengths, default=0) >= 6
        for box, score, _is_wide, aspect, area_ratio in hits:
            box = np.asarray(box, dtype=np.float32)
            left_margin = float(box[:, 0].min()) / max(1, width)
            right_margin = 1.0 - float(box[:, 0].max()) / max(1, width)
            centre_x = float(box[:, 0].mean()) / max(1, width)
            full_width_title = (
                has_readable_text
                and aspect >= 3.0
                and area_ratio >= 0.10
                and 0.25 <= centre_x <= 0.75
            )
            if (
                score >= conf
                and area_ratio >= 0.03
                and (
                    (
                        left_margin >= 0.05
                        and right_margin >= 0.05
                    )
                    or full_width_title
                )
            ):
                detected = True
                break
    return detected, hits, recognised, centred_lines


def text_column_profile(image, conf: float = _BOX_THRESHOLD):
    """Return a normalised horizontal text-density profile, or None."""
    try:
        boxes, scores, width, height = _detect(image)
        if boxes is None or width <= 0:
            return None
        profile = np.zeros(width, dtype=np.float32)
        hits = _qualifying_boxes(
            boxes, scores, width, height, conf, _SCAN_TOP
        )
        for box, score, _is_wide, _aspect, _area_ratio in hits:
            left = max(0, min(width - 1, int(np.floor(box[:, 0].min()))))
            right = max(left + 1, min(width, int(np.ceil(box[:, 0].max()))))
            profile[left:right] += score
        maximum = float(profile.max())
        if maximum > 0:
            profile /= maximum
        return profile
    except Exception as exc:
        logger.warning(f"text_column_profile error: {exc}")
        return None


if __name__ == "__main__":
    import sys
    from PIL import Image

    logging.basicConfig(level=logging.INFO)
    for path in sys.argv[1:]:
        try:
            result = poster_has_burned_in_text(Image.open(path), debug=True)
            print(f"{path}: {'HAS TEXT' if result else 'clear'}")
        except Exception as exc:
            print(f"{path}: error {exc}")
