#!/usr/bin/env python3
"""
Build a small BEFORE / AFTER folder for showing a doctor in the OHIF viewer.

Run AFTER you have (1) created the sample inbox and (2) processed it in the app:
    python make_doctor_demo.py

Creates doctor_demo/ with pairs such as
    01_chest_xray_BEFORE_identified.dcm     (fake patient name in the header AND burned into the image)
    02_chest_xray_AFTER_deidentified.dcm    (coded IDs only; burned-in text blacked out by OCR)
Demo data only - never use this on real patient files.
"""
import json, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "sample_inbox"
OUT = ROOT / "deidentified_output"
DEMO = ROOT / "doctor_demo"

# (label, original file in sample_inbox, how to find the de-identified record, file inside the record)
PAIRS = [
    ("chest_xray", "CR_export_0923/IMG0001", lambda r: r["modality"] == "CR" and r["body_part"] == "CHEST", None),
    ("ultrasound", "US/us_0001.dcm",         lambda r: r["modality"] == "US", None),
    ("leg_xray",   "misc/leg.dcm",           lambda r: r["modality"] == "CR" and r["body_part"] == "LEG", None),
    ("ct_slice10", "CT/Study_A/CT0010.dcm",  lambda r: r["modality"] == "CT", "CT_003_00010.dcm"),
]


def main():
    if not (INBOX / ".sample_inbox_marker").exists() and not (INBOX / "CR_export_0923").exists():
        sys.exit("sample_inbox not found - run make_sample_data.py first.")
    recs_path = OUT / "deidentified_records.json"
    if not recs_path.exists():
        sys.exit("No de-identified output yet - process sample_inbox in the app first.")
    records = json.loads(recs_path.read_text())
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir()
    n = 0
    for label, before, match, after_name in PAIRS:
        rec = next((r for r in records if match(r)), None)
        src = INBOX / before
        if rec is None or not src.exists():
            print(f"skip {label}: not found")
            continue
        folder = OUT / rec["folder"]
        after = folder / after_name if after_name else next(iter(sorted(folder.glob("*.dcm"))), None)
        if after is None or not after.exists():
            print(f"skip {label}: de-identified image not found")
            continue
        n += 1
        shutil.copy2(src, DEMO / f"{2*n-1:02d}_{label}_BEFORE_identified.dcm")
        shutil.copy2(after, DEMO / f"{2*n:02d}_{label}_AFTER_deidentified.dcm")
        rep = folder / "report.txt"
        if rep.exists():
            shutil.copy2(rep, DEMO / f"{2*n:02d}_{label}_AFTER_report.txt")
        print(f"{label}: record {rec['record_id']}  ({rec.get('ocr_regions_masked', 0)} text region(s) masked)")
    print(f"\n{2*n} DICOM files written to {DEMO}")


if __name__ == "__main__":
    main()
