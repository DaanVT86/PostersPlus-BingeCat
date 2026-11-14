#pxscale.py
"""Pixel rounding for posters drawn above the 500-wide canvas.

Overlay geometry is specified as ratios of the canvas and then floored to whole
pixels (``int(width * 0.06)`` for a font size, ``max(4, int(h * 0.18))`` for a
bar).  At 500 wide that flooring is part of the design everyone has seen; at a
larger canvas the same expression floors a different fraction away, so each
element lands a slightly different size and position — up to ~9 px and ~9 %
measured in 500 units, varying by element and resolution.

The helpers here do the flooring in 500-wide units and scale the result back
up, so a larger poster is the 500 one enlarged.  At 500 they are exactly the
plain expressions they replace (``px(v) == int(v)``), so nothing drawn at the
default size changes.

The scale is set around a render by build_poster (``render_scale``).  It is a
ContextVar because renders run on thread-pool threads: each thread sees only
the scale its own render set, and the default outside a render is 1.
"""
from contextlib import contextmanager
from contextvars import ContextVar

REFERENCE_WIDTH = 500

_SCALE: ContextVar[float] = ContextVar("render_px_scale", default=1.0)

# v / k lands a hair under a whole number when the 500-wide value is exact
# (780 * 0.06 / 1.56 == 29.999999999999996), and flooring that loses a whole
# 500-wide pixel.  Nudged by far less than any real fraction.
_EPS = 1e-6


@contextmanager
def render_scale(canvas_width: int):
    """Draw at *canvas_width* with 500-wide rounding for the duration."""
    token = _SCALE.set(canvas_width / REFERENCE_WIDTH)
    try:
        yield
    finally:
        _SCALE.reset(token)


def scale() -> float:
    return _SCALE.get()


def px(v: float) -> float:
    """``int(v)`` as the 500-wide canvas would floor it, at this canvas's
    scale.  An int at 500; a float above it (fonts, text positions and
    ImageDraw shapes all take fractional values)."""
    k = _SCALE.get()
    if k == 1.0:
        return int(v)
    return int(v / k + (_EPS if v >= 0 else -_EPS)) * k


def pxr(v: float) -> float:
    """``round(v)`` in 500-wide units, scaled back."""
    k = _SCALE.get()
    if k == 1.0:
        return round(v)
    return round(v / k) * k


def pxc(v: float) -> float:
    """``math.ceil(v)`` in 500-wide units, scaled back."""
    import math
    k = _SCALE.get()
    if k == 1.0:
        return math.ceil(v)
    return math.ceil(v / k - _EPS) * k


def pxi(v: float) -> int:
    """``int(v)`` in 500-wide units, as a whole pixel at this canvas — for
    image sizes, crop boxes and paste offsets, which must be integers."""
    k = _SCALE.get()
    if k == 1.0:
        return int(v)
    return round(px(v))


def pxri(v: float) -> int:
    """``round(v)`` in 500-wide units, as a whole pixel at this canvas."""
    k = _SCALE.get()
    if k == 1.0:
        return round(v)
    return round(round(v / k) * k)


def fixed(n: float) -> float:
    """A size given in 500-wide pixels (a minimum, a fixed gap), at this
    canvas.  ``n`` itself at 500."""
    k = _SCALE.get()
    return n if k == 1.0 else n * k


def fixedi(n: float) -> int:
    """``fixed`` as a whole pixel."""
    k = _SCALE.get()
    return n if k == 1.0 else round(n * k)
