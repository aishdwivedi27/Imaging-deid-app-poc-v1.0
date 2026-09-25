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

shutil.rmtree(tmp, ignore_errors=True)
print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
