"""
Burned-in text masking with RapidOCR (pip-only, CPU, ONNX models bundled - no system installs).

Strategy
  * Render the frame to 8-bit and run OCR at FULL resolution (no downscaling), so small text on large
    X-rays and mammograms is found. Both the masking pass and the verification pass run at full size.
  * Mask every box the text DETECTOR finds, whatever the recognition confidence. Recognition confidence
    is used for one thing only: a positional marker (R, L, PA, AP, LAT, ERECT, SUPINE ...) or a lone
    character (a mirrored lead marker) is kept when read confidently. Unreadable or low-confidence
    text is always masked.
  * Widen each box along its text line, so a neighbouring word the detector missed (e.g. a surname over
    bright anatomy) is covered too.
  * Text found inside the image area (not in the border strips where labels normally sit) is reported,
    so the record is held for a human to look at the pixels.
  * Multi-frame (ultrasound cine): OCR first / middle / last frame, mask the union on every frame
    (overlays are static).
  * Fill each box with the background value (black on screen), then re-OCR to verify.
"""
import re
import numpy as np

KEEP_TOKENS = {"R", "L", "RT", "LT", "PA", "AP", "LAT", "LATERAL", "ERECT", "SUPINE", "PRONE", "UPRIGHT",
               "INSP", "EXP", "PORTABLE", "DECUB", "OBL", "OBLIQUE", "LAO", "RAO", "LPO", "RPO", "H", "F", "A", "P"}
KEEP_MIN_SCORE = 0.8      # a marker is kept only when read at least this confidently
BORDER_FRACTION = 0.12    # outer 12% on each side counts as the label border; the rest is image area
MAX_SIDE = 10000          # RapidOCR shrinks images above 2000 px by default; lift that limit
_ocr = None


def available():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _engine():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR(max_side_len=MAX_SIDE, text_score=0.0)
    return _ocr


def _to8(frame, mono1):
    f = frame.astype(np.float32)
    if f.ndim == 3:                       # RGB
        f = f.mean(axis=2)
    lo, hi = np.percentile(f, 0.5), np.percentile(f, 99.8)
    f = np.clip((f - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    if mono1:
        f = 255 - f
    return f.astype(np.uint8)


def _detect(frame8):
    """Boxes (x0, y0, x1, y1) of all detected text at full resolution, minus confidently read markers."""
    res, _ = _engine()(np.stack([frame8] * 3, axis=-1), text_score=0.0)
    boxes = []
    for box, text, score in res or []:
        token = re.sub(r"[^A-Z0-9]", "", str(text).upper())
        # keep confidently read positional markers; a lone character is treated as a marker too,
        # because lead markers are often mirrored ("R" read as "B") and one glyph cannot identify anyone
        if (token in KEEP_TOKENS or len(token) == 1) and float(score or 0) >= KEEP_MIN_SCORE:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        boxes.append((int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1))
    return boxes


def _widen(box, W, H, pad):
    """Extend a box along its text line (its own width plus two line-heights each side)."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    ext = bw + 2 * bh
    vpad = max(pad, int(0.25 * bh))
    return max(0, x0 - ext), max(0, y0 - vpad), min(W, x1 + ext), min(H, y1 + vpad)


def _is_interior(box, W, H):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    mx, my = W * BORDER_FRACTION, H * BORDER_FRACTION
    return mx < cx < W - mx and my < cy < H - my


def mask_dataset(ds, pad=6):
    """Mask burned-in text in-place. Returns dict(regions, verified, interior). Never stores the text itself."""
    arr = ds.pixel_array
    nframes = int(ds.get("NumberOfFrames", 1) or 1)
    spp = int(ds.get("SamplesPerPixel", 1))
    mono1 = str(ds.get("PhotometricInterpretation", "")) == "MONOCHROME1"
    frames = arr if nframes > 1 else arr[None, ...]
    sample_idx = sorted({0, len(frames) // 2, len(frames) - 1})

    boxes = []
    for i in sample_idx:
        boxes += _detect(_to8(frames[i], mono1))
    if not boxes:
        return {"regions": 0, "verified": True, "interior": 0}

    arr = np.array(frames, copy=True)
    if spp == 1:
        fill = arr.max() if mono1 else arr.min()
    else:
        fill = 0
    H, W = arr.shape[1], arr.shape[2]
    interior = sum(_is_interior(b, W, H) for b in boxes)
    for b in boxes:
        x0, y0, x1, y1 = _widen(b, W, H, pad)
        arr[:, y0:y1, x0:x1, ...] = fill

    # verify on the sampled frames, at full resolution
    remaining = sum(len(_detect(_to8(arr[i], mono1))) for i in sample_idx)

    out = arr if nframes > 1 else arr[0]
    ds.PixelData = np.ascontiguousarray(out).tobytes()
    from pydicom.uid import ExplicitVRLittleEndian
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    if spp == 3:
        ds.PhotometricInterpretation = "RGB"   # pydicom converts YBR to RGB when decoding
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = out.dtype.itemsize * 8
    if remaining == 0:
        ds.BurnedInAnnotation = "NO"
    return {"regions": len(boxes), "verified": remaining == 0, "interior": interior}


def text_remaining(ds):
    """Count OCR detections (excluding confidently read positional markers) on a written dataset - used by QA."""
    arr = ds.pixel_array
    nframes = int(ds.get("NumberOfFrames", 1) or 1)
    mono1 = str(ds.get("PhotometricInterpretation", "")) == "MONOCHROME1"
    frames = arr if nframes > 1 else arr[None, ...]
    return sum(len(_detect(_to8(frames[i], mono1))) for i in sorted({0, len(frames) // 2, len(frames) - 1}))
