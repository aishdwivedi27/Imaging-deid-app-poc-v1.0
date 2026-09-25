"""
Core de-identification rules shared by the engine: tag allowlist, keyed pseudonyms, date shift,
report redaction patterns. (Originally the PoC batch pipeline; the batch runner now lives in engine.py.)
"""
import argparse, csv, datetime as dt, hashlib, hmac, json, os, random, re, secrets, shutil, sys
from collections import defaultdict
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import generate_uid, PYDICOM_IMPLEMENTATION_UID

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------------------------------
# Tag policy
# --------------------------------------------------------------------------------------------------
KEEP_TAGS = {
    # image pixel / display
    "SamplesPerPixel", "PhotometricInterpretation", "Rows", "Columns", "BitsAllocated", "BitsStored",
    "HighBit", "PixelRepresentation", "PlanarConfiguration", "NumberOfFrames", "PixelData",
    "RescaleIntercept", "RescaleSlope", "RescaleType", "WindowCenter", "WindowWidth", "VOILUTFunction",
    "PixelSpacing", "ImagerPixelSpacing", "PixelAspectRatio", "LossyImageCompression",
    "LossyImageCompressionRatio", "LossyImageCompressionMethod", "PresentationLUTShape",
    # geometry
    "ImagePositionPatient", "ImageOrientationPatient", "SliceThickness", "SpacingBetweenSlices",
    "SliceLocation", "PatientPosition", "ViewPosition", "Laterality", "ImageLaterality",
    # acquisition / equipment (useful for AI buyers, not identifying)
    "Modality", "BodyPartExamined", "KVP", "ExposureTime", "XRayTubeCurrent", "Exposure",
    "ConvolutionKernel", "ContrastBolusAgent", "Manufacturer", "ManufacturerModelName",
    "ImageType", "SOPClassUID", "SeriesNumber", "InstanceNumber", "AcquisitionNumber",
    "StudyDescription", "SeriesDescription", "ProtocolName", "BurnedInAnnotation",
    # demographics kept at low granularity
    "PatientSex", "PatientAge", "PatientSize", "PatientWeight",
}
DATE_TAGS = ["StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"]
UID_TAGS = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "FrameOfReferenceUID"]
# free-text tags we keep but still scan for leaked identifiers
SCAN_TEXT_TAGS = ["StudyDescription", "SeriesDescription", "ProtocolName", "ContrastBolusAgent"]

# --------------------------------------------------------------------------------------------------
# Report redaction rules (India-oriented)
# --------------------------------------------------------------------------------------------------
LABEL_LINE = re.compile(
    r"(?im)^(\s*(?:patient\s*name|pt\.?\s*name|name|uhid|mrn|reg(?:istration)?\.?\s*no\.?|patient\s*id|"
    r"ip\s*no\.?|op\s*no\.?|lab\s*no\.?|accession(?:\s*no\.?)?|ref(?:erred)?\.?\s*by|referring\s*(?:doctor|physician)|"
    r"address|mobile|phone|contact(?:\s*no\.?)?|email|aadhaar|abha(?:\s*(?:id|no\.?))?|d\.?o\.?b\.?|date\s*of\s*birth)"
    r"\s*[:\-]\s*)(.+)$")
REGEX_RULES = [
    ("EMAIL",   re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE",   re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")),
    ("ID12",    re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")),       # Aadhaar-like
    ("ABHA",    re.compile(r"(?<!\d)\d{2}-\d{4}-\d{4}-\d{4}(?!\d)")),
    ("PAN",     re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("REGNO",   re.compile(r"\b(?:Reg(?:istration)?|IP|OP|Lab)\.?\s*(?:No\.?|Number)\s*[:#-]?\s*(?=[A-Z0-9/\-]*\d)[A-Z0-9][A-Z0-9/\-]{3,}", re.I)),
    ("MRN",     re.compile(r"\b(?:UHID|MRN|ABHA)\s*(?:No\.?|ID)?\s*[:#-]?\s*(?=\S*\d)[A-Z0-9][A-Z0-9/\-]{3,}", re.I)),
    ("DATE",    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{2,4}\b", re.I)),
    ("PIN",     re.compile(r"(?<!\d)[1-9]\d{5}(?!\d)")),
    ("NAME",    re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Miss|Smt|Shri|Sri|Kumari|Master|Baby)\.?\s+[A-Z][a-zA-Z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-zA-Z]+){0,2}")),
]

# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------
def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    for k in ("input_dir", "output_dir", "secret_key_file"):
        p = Path(cfg[k])
        cfg[k] = p if p.is_absolute() else (Path(path).parent / p)
    return cfg

def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(key.hex())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"[key] new secret key created at {path} - back it up securely; never ship it with data")
    return key

def h(key: bytes, kind: str, value: str) -> str:
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

def pseudo_uid(key, original):
    # 2.25.<decimal of 120-bit int> -> valid DICOM UID, < 64 chars
    return "2.25." + str(int(h(key, "uid", original)[:30], 16))

def sha256_file(p: Path) -> str:
    d = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            d.update(chunk)
    return d.hexdigest()

def parse_da(s):
    try:
        return dt.datetime.strptime(str(s)[:8], "%Y%m%d").date()
    except Exception:
        return None

def age_from(ds):
    age = str(ds.get("PatientAge", "") or "")
    if re.fullmatch(r"\d{3}[DWMY]", age):
        return age
    dob, sd = parse_da(ds.get("PatientBirthDate", "")), parse_da(ds.get("StudyDate", ""))
    if dob and sd:
        yrs = sd.year - dob.year - ((sd.month, sd.day) < (dob.month, dob.day))
        return f"{max(yrs, 0):03d}Y"
    return ""

def known_identifiers(ds):
    """Exact strings from the original header that must never appear in outputs."""
    vals = set()
    for tag in ("PatientID", "AccessionNumber", "OtherPatientIDs", "InstitutionName",
                "InstitutionAddress", "PatientAddress", "PatientTelephoneNumbers"):
        v = str(ds.get(tag, "") or "").strip()
        if len(v) >= 3:
            vals.add(v)
    for tag in ("PatientName", "ReferringPhysicianName", "PerformingPhysicianName",
                "OperatorsName", "NameOfPhysiciansReadingStudy"):
        v = ds.get(tag, None)
        if v is None:
            continue
        for part in re.split(r"[\^\s,]+", str(v)):
            if len(part) >= 3 and not part.lower() in {"dr", "mr", "mrs", "smt", "shri"}:
                vals.add(part)
    dob = parse_da(ds.get("PatientBirthDate", ""))
    if dob:
        vals.update({dob.strftime("%d/%m/%Y"), dob.strftime("%d-%m-%Y"), dob.strftime("%Y%m%d"),
                     dob.strftime("%d.%m.%Y")})
    return vals

def is_quarantine(ds, cfg):
    mod = str(ds.get("Modality", "")).upper()
    if mod in cfg["quarantine_modalities"]:
        return f"modality {mod}"
    if str(ds.get("BurnedInAnnotation", "")).upper() == "YES":
        return "BurnedInAnnotation=YES"
    sop = str(ds.get("SOPClassUID", ""))
    if sop.startswith("1.2.840.10008.5.1.4.1.1.7"):          # secondary capture family
        return "secondary capture (e.g. CT dose screen)"
    if sop == "1.2.840.10008.5.1.4.1.1.104.1":               # encapsulated PDF
        return "encapsulated PDF"
    itype = [str(x).upper() for x in (ds.get("ImageType", []) or [])]
    if "SECONDARY" in itype and "SCREEN SAVE" in " ".join(itype):
        return "screen save"
    return None

# --------------------------------------------------------------------------------------------------
# DICOM de-identification
# --------------------------------------------------------------------------------------------------
def deid_dataset(ds, key, cfg, pat_pseudo, study_pseudo, shift_days):
    out = Dataset()
    for kw in KEEP_TAGS:
        if kw in ds:
            out[kw] = ds[kw]
    # scrub identifiers typed into free-text descriptions (e.g. "CHEST PA - RAMESH")
    idents = sorted((i for i in known_identifiers(ds) if len(i) >= 3), key=len, reverse=True)
    for kw in SCAN_TEXT_TAGS:
        if kw in out and idents:
            val = str(out[kw].value)
            for i in idents:
                val = re.sub(re.escape(i), "[REDACTED]", val, flags=re.I)
            el = out[kw]
            out[kw] = pydicom.DataElement(el.tag, el.VR, val[:64])   # new element; source stays untouched
    # viewer compatibility: a zero/invalid pixel spacing (seen in real exports) makes web viewers such as
    # OHIF render a black image, so drop it rather than pass it on
    for kw in ("PixelSpacing", "ImagerPixelSpacing"):
        if kw in out:
            try:
                ok = all(float(v) > 0 for v in out[kw].value)
            except Exception:
                ok = False
            if not ok:
                del out[kw]
    # identity
    out.PatientID = pat_pseudo
    out.PatientName = pat_pseudo
    out.AccessionNumber = study_pseudo
    out.StudyID = study_pseudo[-8:]
    age = age_from(ds)
    if age:
        if age.endswith("Y") and int(age[:3]) >= cfg["age_cap_years"]:
            age = f"{cfg['age_cap_years']:03d}Y"
        out.PatientAge = age
    # UIDs
    for kw in UID_TAGS:
        if kw in ds:
            setattr(out, kw, pseudo_uid(key, str(ds.get(kw))))
    # dates
    for kw in DATE_TAGS:
        d = parse_da(ds.get(kw, ""))
        if not d or cfg["date_mode"] == "remove":
            continue
        if cfg["date_mode"] == "year_only":
            setattr(out, kw, f"{d.year}0101")
        else:
            setattr(out, kw, (d - dt.timedelta(days=shift_days)).strftime("%Y%m%d"))
    # de-identification flags (DICOM PS3.15)
    out.PatientIdentityRemoved = "YES"
    out.DeidentificationMethod = "PoC allowlist; HMAC pseudonyms; UIDs remapped; dates " + cfg["date_mode"]
    # file meta
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ds.get("SOPClassUID", "1.2.840.10008.5.1.4.1.1.7")
    meta.MediaStorageSOPInstanceUID = out.get("SOPInstanceUID", generate_uid())
    meta.TransferSyntaxUID = ds.file_meta.TransferSyntaxUID
    meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    out.file_meta = meta
    return out

# --------------------------------------------------------------------------------------------------
# Report de-identification
# --------------------------------------------------------------------------------------------------
def read_report(p: Path) -> str:
    if p.suffix.lower() == ".docx":
        import docx
        return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)
    return p.read_text(encoding="utf-8", errors="replace")

def redact_report(text, identifiers):
    counts = defaultdict(int)
    def lab(m):
        counts["LABELLED_FIELD"] += 1
        return m.group(1) + "[REDACTED]"
    text = LABEL_LINE.sub(lab, text)
    for ident in sorted(identifiers, key=len, reverse=True):
        pat = re.compile(re.escape(ident), re.I)
        text, n = pat.subn("[REDACTED]", text)
        counts["KNOWN_ID"] += n
    for name, rx in REGEX_RULES:
        text, n = rx.subn(f"[{name}]", text)
        counts[name] += n
    return text, dict(counts)

def find_reports(input_dir, exts):
    return [p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts]

# --------------------------------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------------------------------
