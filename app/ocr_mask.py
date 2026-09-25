"""
Burned-in text masking with RapidOCR (pip-only, CPU, ONNX models bundled - no system installs).

Strategy
  * Render the frame to 8-bit, run OCR (downscaled for speed), map boxes back to full resolution.
  * Multi-frame (ultrasound cine): OCR first / middle / last frame, mask the union on every frame
    (overlays are static).
  * Keep short positional markers that AI buyers need (R, L, PA, AP, LAT, ERECT, SUPINE ...).
  * Fill each box with the background value (black on screen), then re-OCR to verify.
"""
import re
import numpy as np

KEEP_TOKENS = {"R", "L", "RT", "LT", "PA", "AP", "LAT", "LATERAL", "ERECT", "SUPINE", "PRONE", "UPRIGHT",
               "INSP", "EXP", "PORTABLE", "DECUB", "OBL", "OBLIQUE", "LAO", "RAO", "LPO", "RPO", "H", "F", "A", "P"}
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
        _ocr = RapidOCR()
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


def _detect(frame8, min_score=0.5, max_side=1600):
    h, w = frame8.shape
    scale = min(1.0, max_side / max(h, w))
    img = frame8
    if scale < 1.0:
        from PIL import Image
        img = np.asarray(Image.fromarray(frame8).resize((int(w * scale), int(h * scale))))
    res, _ = _engine()(np.stack([img] * 3, axis=-1))
    boxes = []
    for box, text, score in res or []:
        if float(score) < min_score:
            continue
        token = re.sub(r"[^A-Z0-9]", "", str(text).upper())
        if not token or token in KEEP_TOKENS:
            continue
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        boxes.append((int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1))
    return boxes


def mask_dataset(ds, pad=6):
    """Mask burned-in text in-place. Returns dict(regions, verified). Never stores the text itself."""
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
        return {"regions": 0, "verified": True}

    arr = np.array(frames, copy=True)
    if spp == 1:
        fill = arr.max() if mono1 else arr.min()
    else:
        fill = 0
    H, W = arr.shape[1], arr.shape[2]
    for x0, y0, x1, y1 in boxes:
        arr[:, max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad), ...] = fill

    # verify on the sampled frames
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
    return {"regions": len(boxes), "verified": remaining == 0}


def text_remaining(ds):
    """Count OCR detections (excluding positional markers) on a written dataset - used by QA."""
    arr = ds.pixel_array
    nframes = int(ds.get("NumberOfFrames", 1) or 1)
    mono1 = str(ds.get("PhotometricInterpretation", "")) == "MONOCHROME1"
    frame = arr[len(arr) // 2] if nframes > 1 else arr
    return len(_detect(_to8(frame, mono1)))
