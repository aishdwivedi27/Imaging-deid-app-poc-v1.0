"""
End-to-end checks (no pytest needed):  python tests/test_end_to_end.py

 1. builds the sample inbox (fake patient details on real public test X-rays, burned-in text included)
 2. runs the engine into a temporary output folder
 3. checks folder structure, JSON/CSV rows, that no original identifier survives anywhere
    (DICOM headers, reports, JSON, CSV, and OCR of every released image)
 4. proves QA catches leaks by planting identifiers in a copy of a record
"""
import csv, json, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pydicom  # noqa: E402
from app import engine, ocr_mask  # noqa: E402

PHI = ["Sharma", "Ramesh", "UHID-778812", "Iyer", "Lakshmi", "UHID-903311", "Patel", "Ananya", "UHID-440021",
       "98220 45671", "9444012345", "4821 7730 1192", "Kulkarni", "Deshpande", "Sunrise Diagnostics", "Menon"]
ok = True


def check(cond, msg):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    ok &= bool(cond)


subprocess.run([sys.executable, str(ROOT / "make_sample_data.py")], check=True, capture_output=True)
tmp = Path(tempfile.mkdtemp())
s = engine.load_settings() | {"output_dir": str(tmp / "out"), "secure_dir": str(tmp / "secure")}
events = []
job = engine.Job(ROOT / "sample_inbox", s)
job.listeners.append(type("Q", (), {"put": staticmethod(events.append)})())
job.run()
out = tmp / "out"

print("Structure")
recs = json.loads((out / "deidentified_records.json").read_text())
rows = list(csv.DictReader(open(out / "deidentified_records.csv", encoding="utf-8")))
check(len(recs) == 4 and len(rows) == 4, f"4 records in JSON and CSV (got {len(recs)}, {len(rows)})")
for r in recs:
    d = out / r["folder"]
    check(d.is_dir() and (d / "record.json").exists() and list(d.glob("*.dcm")), f"{r['record_id']}: folder with images + record.json")
check(sum(r["report_present"] for r in recs) == 3, "3 reports matched and redacted")
check(any(e["type"] == "record" for e in events) and events[-1]["type"] == "finished", "live events emitted per record")

print("No identifiers left")
blob = (out / "deidentified_records.json").read_text() + (out / "deidentified_records.csv").read_text()
for f in out.rglob("*"):
    if f.suffix == ".txt" or f.name == "record.json":
        blob += f.read_text()
    if f.suffix == ".dcm":
        blob += " ".join(str(el.value) for el in pydicom.dcmread(f, stop_before_pixels=True) if el.VR not in ("OB", "OW"))
low = blob.lower()
leaks = [p for p in PHI if p.lower() in low]
check(not leaks, f"no original identifiers in headers / reports / JSON / CSV {leaks or ''}")
if ocr_mask.available():
    left = {f.parent.name: ocr_mask.text_remaining(pydicom.dcmread(f)) for f in out.rglob("CR_*.dcm")}
    left |= {f.parent.name: ocr_mask.text_remaining(pydicom.dcmread(f)) for f in out.rglob("US_*.dcm")}
    check(all(v == 0 for v in left.values()), f"no burned-in text detected on released X-ray / US images {left}")

print("QA catches planted leaks")
rec = next(r for r in recs if r["report_present"])
bad = tmp / "tampered"
shutil.copytree(out / rec["folder"], bad)
f = next(bad.glob("*.dcm"))
ds = pydicom.dcmread(f); ds.StudyDescription = "XR CHEST SHARMA"; ds.add_new(0x00291010, "LO", "Sharma"); ds.save_as(f)
(bad / "report.txt").write_text((bad / "report.txt").read_text() + "\ncall 9822045671")
findings = engine._qa_record(bad, {"Sharma"}, (bad / "report.txt").read_text())
codes = {x.split()[0] for x in findings}
check({"A1", "A2", "A4"} <= codes, f"QA flagged private tag, name and phone ({sorted(codes)})")

print("Vendor name in InstitutionName is not an identifier (dental CBCT)")
from pydicom.dataset import Dataset  # noqa: E402
from app import core  # noqa: E402
v = Dataset(); v.PatientName = "DEMO^PATIENT"; v.InstitutionName = "VATECH Co., Ltd."
v.Manufacturer = "VATECH Co., Ltd."; v.ManufacturerModelName = "PHT-75CHS"
ids = core.known_identifiers(v)
check("VATECH Co., Ltd." not in ids and {"DEMO", "PATIENT"} <= ids, "vendor-as-institution skipped, patient name still checked")
v.InstitutionName = "Sunrise Diagnostics"
check("Sunrise Diagnostics" in core.known_identifiers(v), "a real institution name is still an identifier")

if ocr_mask.available():
    print("Burned-in text: small text, neighbouring words, markers")
    import numpy as np  # noqa: E402
    from PIL import Image, ImageDraw, ImageFont  # noqa: E402
    from pydicom.dataset import FileMetaDataset  # noqa: E402
    from pydicom.uid import ExplicitVRLittleEndian  # noqa: E402

    def _xray(texts, W=2800, H=2300):
        yy, xx = np.mgrid[0:H, 0:W]
        base = 800 + 1500 * np.exp(-(((xx - W / 2) / 900) ** 2 + ((yy - H / 2) / 800) ** 2))
        im = Image.new("L", (W, H)); d = ImageDraw.Draw(im)
        for x, y, size, t in texts:
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", size)
            except OSError:
                font = ImageFont.truetype("arial.ttf", size)
            d.text((x, y), t, font=font, fill=255)
        m = np.asarray(im) > 0
        a = np.where(m, 4095, base).astype(np.uint16)
        x = Dataset(); x.file_meta = FileMetaDataset(); x.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        x.Modality = "CR"; x.Rows, x.Columns = a.shape; x.SamplesPerPixel = 1; x.PhotometricInterpretation = "MONOCHROME2"
        x.BitsAllocated = 16; x.BitsStored = 12; x.HighBit = 11; x.PixelRepresentation = 0; x.PixelData = a.tobytes()
        return x, a, m

    x, a, m = _xray([(40, 2250, 11, "PATEL ANANYA 9839001122")])
    r = ocr_mask.mask_dataset(x)
    check(r["regions"] >= 1 and (x.pixel_array[m] != a[m]).all(), "11-pixel name on a 2800-pixel X-ray is masked")
    x, a, m = _xray([(1100, 1100, 40, "SHARMA RAMESH")])
    r = ocr_mask.mask_dataset(x)
    check((x.pixel_array[m] != a[m]).all() and r["interior"] >= 1, "whole name over anatomy masked and flagged for review")
    x, a, m = _xray([(2500, 1900, 70, "R")])
    r = ocr_mask.mask_dataset(x)
    check(r["regions"] == 0, "a lone positional marker is kept")

shutil.rmtree(tmp, ignore_errors=True)
print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
