"""
genre_backgrounds.py — procedural atmospheric poster backgrounds.

Generates a 500x750 themed background per genre (a starfield/nebula for Sci-Fi,
blood drips for Horror, dusty sunset for Western, …) using only PIL + numpy, so
there are no external art assets to ship and the set is fully regenerable.

Run as a script to (re)write the PNGs into static/genre_bg/minimal/:

    python genre_backgrounds.py

At runtime main.py loads those PNGs (cached in memory) as the base for no-art
fallback posters, then composites the title / sash on top as usual.
Drop a hand-made 500x750 PNG into static/genre_bg/minimal/<Genre>.png to override any
genre's procedural art — the loader prefers an existing file.

Backgrounds are kept fairly dark and weighted toward the upper two-thirds so the
title band (lower-middle) and the bottom score strip stay legible after the
poster's own top/bottom gradients are applied.
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 500, 750


# ---------------------------------------------------------------------------
# Primitives — all return float HxWx3 in [0, 255] (additive-friendly)
# ---------------------------------------------------------------------------

def _canvas() -> np.ndarray:
    return np.zeros((H, W, 3), dtype=np.float32)


def _vgrad(top: tuple, bottom: tuple) -> np.ndarray:
    """Vertical linear gradient from top→bottom colour."""
    t = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None, None]
    top_a = np.array(top, dtype=np.float32)[None, None, :]
    bot_a = np.array(bottom, dtype=np.float32)[None, None, :]
    return (top_a * (1.0 - t) + bot_a * t) * np.ones((H, W, 1), dtype=np.float32)


def _radial(cx: float, cy: float, color: tuple, radius: float,
            intensity: float = 1.0) -> np.ndarray:
    """Additive radial glow centred at (cx, cy) in pixel coords."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / radius
    fall = np.clip(1.0 - d, 0.0, 1.0) ** 2
    return fall[:, :, None] * np.array(color, dtype=np.float32)[None, None, :] * intensity


def _value_noise(seed: int, scale: int) -> np.ndarray:
    """Smooth value noise in [0,1] — small random field upscaled with bicubic."""
    rng = np.random.default_rng(seed)
    sw, sh = max(2, W // scale), max(2, H // scale)
    small = (rng.random((sh, sw)) * 255).astype(np.uint8)
    img = Image.fromarray(small).resize((W, H), Image.Resampling.BICUBIC)
    return np.asarray(img, dtype=np.float32) / 255.0


def _fbm(seed: int, scales=(60, 30, 15)) -> np.ndarray:
    """Fractal noise — sum of octaves, normalised to [0,1]."""
    acc = np.zeros((H, W), dtype=np.float32)
    amp = 1.0
    for i, s in enumerate(scales):
        acc += amp * _value_noise(seed + i * 17, s)
        amp *= 0.5
    acc -= acc.min()
    acc /= (acc.max() + 1e-6)
    return acc


def _starfield(seed: int, count: int, max_radius: float = 1.6,
               color: tuple = (255, 255, 255)) -> np.ndarray:
    """Sparse bright points with soft halos."""
    rng = np.random.default_rng(seed)
    layer = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        x = rng.integers(0, W)
        y = rng.integers(0, H)
        r = rng.random() * max_radius + 0.4
        b = int(120 + rng.random() * 135)
        d.ellipse([x - r, y - r, x + r, y + r], fill=b)
    arr = np.asarray(layer.filter(ImageFilter.GaussianBlur(0.5)), dtype=np.float32) / 255.0
    return arr[:, :, None] * np.array(color, dtype=np.float32)[None, None, :]


def _particles(seed: int, count: int, color: tuple, size=(2, 7),
               y_bias: float = 0.5, glow: float = 1.0) -> np.ndarray:
    """Soft bokeh / ember particles, optionally biased toward the top/bottom."""
    rng = np.random.default_rng(seed)
    layer = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        x = rng.integers(0, W)
        # y_bias 0 → top-heavy, 1 → bottom-heavy
        y = int((rng.random() ** (1.0 / (0.3 + y_bias))) * H)
        r = rng.random() * (size[1] - size[0]) + size[0]
        b = int(60 + rng.random() * 160)
        d.ellipse([x - r, y - r, x + r, y + r], fill=b)
    blurred = layer.filter(ImageFilter.GaussianBlur(3))
    arr = np.asarray(blurred, dtype=np.float32) / 255.0
    return arr[:, :, None] * np.array(color, dtype=np.float32)[None, None, :] * glow


def _drips(seed: int, color: tuple, count: int = 14) -> np.ndarray:
    """Vertical drip streaks hanging from the top edge (blood, paint…)."""
    rng = np.random.default_rng(seed)
    layer = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        x = rng.integers(0, W)
        length = int(rng.random() * H * 0.5 + H * 0.1)
        width = rng.random() * 6 + 2
        d.line([x, 0, x, length], fill=int(120 + rng.random() * 120), width=int(width))
        # bead at the tip
        r = width * 0.9
        d.ellipse([x - r, length - r, x + r, length + r], fill=int(150 + rng.random() * 100))
    arr = np.asarray(layer.filter(ImageFilter.GaussianBlur(1.5)), dtype=np.float32) / 255.0
    return arr[:, :, None] * np.array(color, dtype=np.float32)[None, None, :]


def _beams(seed: int, color: tuple, count: int = 5, angle: float = 20.0) -> np.ndarray:
    """Diagonal light beams (stage lights / noir blinds)."""
    rng = np.random.default_rng(seed)
    layer = Image.new("L", (W * 2, H * 2), 0)
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        x = rng.integers(-W // 2, int(W * 1.5))
        bw = int(rng.random() * 50 + 20)
        d.polygon([(x, 0), (x + bw, 0), (x + bw - H, H * 2), (x - H, H * 2)],
                  fill=int(40 + rng.random() * 80))
    layer = layer.rotate(angle, center=(W, H)).crop((W // 2, H // 2, W // 2 + W, H // 2 + H))
    arr = np.asarray(layer.filter(ImageFilter.GaussianBlur(8)), dtype=np.float32) / 255.0
    return arr[:, :, None] * np.array(color, dtype=np.float32)[None, None, :]


def _vignette(strength: float = 0.55) -> np.ndarray:
    """Multiplicative darkening toward the edges (returns HxWx1 multiplier)."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = W / 2, H / 2
    d = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    v = 1.0 - strength * np.clip(d - 0.4, 0, 1)
    return v[:, :, None]


def _grain(seed: int, amount: float = 8.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    g = (rng.random((H, W)) - 0.5) * 2.0 * amount
    return g[:, :, None]


def _finish(arr: np.ndarray, seed: int, vignette: float = 0.5,
            grain: float = 7.0) -> Image.Image:
    arr = arr * _vignette(vignette)
    arr = arr + _grain(seed, grain)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    rgba = np.dstack([arr, np.full((H, W), 255, dtype=np.uint8)])
    return Image.fromarray(rgba)


# ---------------------------------------------------------------------------
# Per-look composers
# ---------------------------------------------------------------------------

def _space(seed: int) -> Image.Image:
    base = _vgrad((6, 7, 18), (2, 2, 8))
    neb = _fbm(seed, (90, 45, 22))
    neb2 = _fbm(seed + 5, (70, 35))
    base += (neb[:, :, None] ** 2) * np.array([40, 18, 70], np.float32) * 1.4   # purple
    base += (neb2[:, :, None] ** 2) * np.array([10, 45, 70], np.float32) * 1.1  # teal
    base += _radial(W * 0.6, H * 0.35, (30, 20, 60), 360, 0.7)
    base += _starfield(seed + 1, 380, 1.7)
    base += _starfield(seed + 2, 40, 2.6, (200, 220, 255))
    return _finish(base, seed, vignette=0.55, grain=5)


def _blood(seed: int) -> Image.Image:
    base = _vgrad((26, 4, 4), (6, 1, 1))
    base += _radial(W * 0.5, H * 0.28, (70, 6, 6), 420, 0.8)
    base += _drips(seed, (120, 8, 8), count=18)
    base += _fbm(seed + 3, (50, 25))[:, :, None] * np.array([18, 2, 2], np.float32)
    # shadowy mass rising from the bottom
    base *= 1.0 - 0.5 * np.clip((np.linspace(0, 1, H)[:, None, None] - 0.55) / 0.45, 0, 1)
    return _finish(base, seed, vignette=0.65, grain=9)


def _noir(seed: int, cool: bool = False) -> Image.Image:
    top = (24, 26, 32) if cool else (30, 28, 26)
    base = _vgrad(top, (5, 5, 7))
    beam_col = (60, 70, 95) if cool else (90, 85, 70)
    base += _beams(seed, beam_col, count=6, angle=18)
    base += _radial(W * 0.35, H * 0.3, beam_col, 300, 0.4)
    return _finish(base, seed, vignette=0.62, grain=8)


def _embers(seed: int) -> Image.Image:
    base = _vgrad((30, 14, 6), (6, 3, 2))
    base += _radial(W * 0.5, H * 0.7, (90, 35, 8), 460, 0.7)
    base += _beams(seed, (120, 50, 15), count=4, angle=-25)
    base += _particles(seed + 2, 70, (255, 140, 40), size=(1, 4), y_bias=0.8, glow=1.2)
    return _finish(base, seed, vignette=0.6, grain=8)


def _mist(seed: int, warm: bool = False) -> Image.Image:
    top = (34, 22, 44) if not warm else (40, 30, 36)
    bot = (10, 14, 26) if not warm else (16, 14, 18)
    base = _vgrad(top, bot)
    fog = _fbm(seed, (120, 60, 30))
    fog_col = np.array([60, 50, 90], np.float32) if not warm else np.array([70, 55, 60], np.float32)
    base += fog[:, :, None] * fog_col * 1.2
    base += _particles(seed + 4, 60, (180, 170, 220), size=(2, 6), y_bias=0.4, glow=0.7)
    base += _radial(W * 0.5, H * 0.3, (60, 50, 90), 380, 0.4)
    return _finish(base, seed, vignette=0.5, grain=5)


def _bokeh(seed: int, palette: list) -> Image.Image:
    base = _vgrad(tuple(palette[0]), tuple(palette[1]))
    rng = np.random.default_rng(seed)
    for i in range(3):
        col = palette[2 + (i % (len(palette) - 2))]
        base += _particles(seed + i * 9, 22, tuple(col), size=(8, 26),
                           y_bias=0.5, glow=0.55)
    return _finish(base, seed, vignette=0.45, grain=4)


def _cinematic(seed: int) -> Image.Image:
    base = _vgrad((18, 30, 36), (8, 8, 10))
    base += _radial(W * 0.3, H * 0.35, (20, 50, 60), 360, 0.5)   # teal
    base += _radial(W * 0.75, H * 0.7, (70, 40, 15), 340, 0.5)   # orange
    base += _fbm(seed, (90, 45))[:, :, None] * np.array([12, 16, 18], np.float32)
    return _finish(base, seed, vignette=0.6, grain=7)


def _sepia(seed: int, cool: bool = False) -> Image.Image:
    top = (46, 40, 30) if not cool else (34, 36, 38)
    base = _vgrad(top, (12, 10, 8))
    paper = _fbm(seed, (40, 20, 10))
    base += paper[:, :, None] * (np.array([22, 18, 12], np.float32)
                                 if not cool else np.array([14, 16, 18], np.float32))
    base += _radial(W * 0.5, H * 0.32, (50, 42, 28), 380, 0.4)
    return _finish(base, seed, vignette=0.6, grain=10)


def _smoke(seed: int) -> Image.Image:
    base = _vgrad((26, 26, 22), (8, 9, 8))
    smk = _fbm(seed, (110, 55, 28))
    base += smk[:, :, None] * np.array([30, 30, 26], np.float32) * 1.3
    base += _particles(seed + 3, 40, (200, 120, 50), size=(1, 3), y_bias=0.85, glow=0.9)
    base *= 0.92
    return _finish(base, seed, vignette=0.62, grain=9)


def _sunset(seed: int) -> Image.Image:
    base = _vgrad((70, 38, 18), (18, 10, 12))
    base += _radial(W * 0.5, H * 0.42, (140, 70, 25), 520, 0.8)   # low sun
    base += _radial(W * 0.5, H * 0.42, (200, 130, 60), 200, 0.7)
    base += _fbm(seed, (120, 60))[:, :, None] * np.array([20, 12, 6], np.float32)
    # dusty haze toward bottom
    base += (np.linspace(0, 1, H)[:, None, None]) * np.array([10, 6, 3], np.float32)
    return _finish(base, seed, vignette=0.5, grain=8)


def _beams_color(seed: int) -> Image.Image:
    base = _vgrad((14, 10, 22), (4, 3, 8))
    for i, col in enumerate([(120, 30, 90), (30, 60, 130), (30, 120, 90)]):
        base += _beams(seed + i * 4, col, count=2, angle=-30 + i * 25)
    base += _particles(seed + 7, 30, (180, 160, 220), size=(4, 12), y_bias=0.6, glow=0.6)
    base += _radial(W * 0.5, H * 0.75, (60, 30, 90), 360, 0.5)
    return _finish(base, seed, vignette=0.55, grain=6)


def _deep(seed: int) -> Image.Image:
    base = _vgrad((20, 22, 30), (6, 6, 10))
    base += _radial(W * 0.5, H * 0.32, (28, 30, 44), 380, 0.5)
    base += _fbm(seed, (90, 45))[:, :, None] * np.array([10, 11, 16], np.float32)
    return _finish(base, seed, vignette=0.55, grain=6)


# ---------------------------------------------------------------------------
# Genre → look mapping (genre names must match config.GENRE_MAP values)
# ---------------------------------------------------------------------------

_LOOKS = {
    "Sci-Fi":      lambda s: _space(s),
    "Horror":      lambda s: _blood(s),
    "Thriller":    lambda s: _noir(s, cool=False),
    "Mystery":     lambda s: _noir(s, cool=True),
    "Action":      lambda s: _embers(s),
    "Fantasy":     lambda s: _mist(s, warm=False),
    "Adventure":   lambda s: _mist(s, warm=True),
    "Comedy":      lambda s: _bokeh(s, [(40, 30, 20), (12, 10, 14),
                                        (255, 180, 60), (90, 160, 200), (230, 90, 110)]),
    "Animation":   lambda s: _bokeh(s, [(30, 24, 44), (10, 10, 16),
                                        (120, 90, 230), (60, 180, 200), (240, 130, 70)]),
    "Family":      lambda s: _bokeh(s, [(28, 32, 44), (10, 12, 16),
                                        (110, 170, 210), (240, 170, 90), (130, 200, 150)]),
    "Drama":       lambda s: _cinematic(s),
    "Romance":     lambda s: _bokeh(s, [(46, 22, 30), (14, 8, 12),
                                        (230, 90, 120), (250, 160, 130), (180, 70, 110)]),
    "History":     lambda s: _sepia(s, cool=False),
    "Documentary": lambda s: _sepia(s, cool=True),
    "War":         lambda s: _smoke(s),
    "Western":     lambda s: _sunset(s),
    "Music":       lambda s: _beams_color(s),
    "Crime":       lambda s: _noir(s, cool=False),
    "News":        lambda s: _noir(s, cool=True),
    "Talk":        lambda s: _beams_color(s),
    "Kids":        lambda s: _bokeh(s, [(30, 34, 50), (10, 12, 18),
                                        (250, 90, 90), (250, 210, 70), (90, 200, 230)]),
    "Reality":     lambda s: _bokeh(s, [(40, 26, 30), (14, 10, 12),
                                        (240, 110, 90), (250, 190, 90), (220, 80, 140)]),
    "Soap":        lambda s: _bokeh(s, [(44, 24, 32), (14, 8, 12),
                                        (230, 100, 130), (250, 170, 140), (200, 80, 120)]),
}
_DEFAULT_LOOK = _deep


def generate(genre: str) -> Image.Image:
    """Generate (deterministically) the background for one genre name."""
    seed = abs(hash(genre)) % (2 ** 31)
    look = _LOOKS.get(genre, _DEFAULT_LOOK)
    return look(seed)


def generate_all(out_dir: str) -> int:
    """Write a PNG per mapped genre plus a 'default' to out_dir.  Returns count."""
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for genre in list(_LOOKS) + ["default"]:
        img = _DEFAULT_LOOK(42) if genre == "default" else generate(genre)
        img.convert("RGB").save(os.path.join(out_dir, f"{genre}.png"))
        n += 1
    return n


if __name__ == "__main__":
    _here = os.path.dirname(os.path.abspath(__file__))
    # Minimal/procedural set lives under genre_bg/minimal/; the photorealistic
    # set (genre_bg/photoreal/) is hand-supplied, not generated here.
    _out = os.path.join(_here, "static", "genre_bg", "minimal")
    count = generate_all(_out)
    print(f"Generated {count} genre backgrounds in {_out}")
