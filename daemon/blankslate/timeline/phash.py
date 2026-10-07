"""Perceptual hash (pHash) for change-triggered capture.

Hand-rolled 64-bit DCT pHash over numpy — `imagehash` is deliberately not a
dependency (security doc section 9). The hash exists only in RAM; frames are
discarded after OCR (D9: no thumbnails anywhere, ever).
"""

from __future__ import annotations

import numpy as np
from PIL import Image

_HASH_SIZE = 8
_HIGHFREQ_FACTOR = 4


def _dct_matrix(n: int) -> np.ndarray:
    """Orthonormal DCT-II basis matrix (n x n)."""
    k = np.arange(n, dtype=np.float64)
    x = np.arange(n, dtype=np.float64)
    mat = np.cos(np.pi * np.outer(k, (2.0 * x + 1.0)) / (2.0 * n))
    mat[0, :] *= 1.0 / np.sqrt(2.0)
    return mat * np.sqrt(2.0 / n)


_DCT_CACHE: dict[int, np.ndarray] = {}


def phash64(img: Image.Image) -> int:
    """64-bit perceptual hash of a PIL image (any mode/size)."""
    size = _HASH_SIZE * _HIGHFREQ_FACTOR
    gray = img.convert("L").resize((size, size), Image.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)

    d = _DCT_CACHE.get(size)
    if d is None:
        d = _DCT_CACHE[size] = _dct_matrix(size)

    dct = d @ pixels @ d.T
    low = dct[:_HASH_SIZE, :_HASH_SIZE]
    med = float(np.median(low.flat[1:]))  # ignore the DC term
    bits = 0
    for i, v in enumerate(low.flat):
        if v > med:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return (a ^ b).bit_count()
