#!/usr/bin/env python3
"""Create a messy, fully SYNTHETIC inbox (fake patients, fake PHI) to demo the pipeline.
No real patient data is used anywhere in this PoC."""
import datetime as dt, shutil, sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

ROOT = Path(__file__).resolve().parent / "sample_inbox"
CR, CT, US, SC = ("1.2.840.10008.5.1.4.1.1.1", "1.2.840.10008.5.1.4.1.1.2",
                  "1.2.840.10008.5.1.4.1.1.6.1", "1.2.840.10008.5.1.4.1.1.7")

def make(path, sop, mod, pat, study_uid, series_uid, series_no, inst_no, arr, **extra):
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID, ds.SOPInstanceUID = sop, ds.file_meta.MediaStorageSOPInstanceUID
    ds.Modality = mod
    ds.PatientName, ds.PatientID, ds.PatientBirthDate, ds.PatientSex = pat["name"], pat["id"], pat["dob"], pat["sex"]
    ds.PatientAddress, ds.PatientTelephoneNumbers = pat["addr"], pat["phone"]
    ds.InstitutionName, ds.InstitutionAddress = "Sunrise Diagnostics Pvt Ltd", "12 MG Road, Pune 411001"
    ds.ReferringPhysicianName = pat["ref"]
    ds.OperatorsName = "Tech^Sunil"
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = study_uid, series_uid, generate_uid()
    ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = pat["study_date"]
    ds.StudyTime = "101500"
    ds.AccessionNumber = pat["acc"]
    ds.SeriesNumber, ds.InstanceNumber = series_no, inst_no
    ds.Manufacturer, ds.ManufacturerModelName = "DemoVendor", "DemoModel 5000"
    ds.Rows, ds.Columns = arr.shape
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.PixelData = arr.astype(np.uint16).tobytes()
    # private vendor tag carrying the patient's name (common in real exports)
    blk = ds.private_block(0x0029, "DEMO VENDOR", create=True)
    blk.add_new(0x10, "LO", pat["name"].replace("^", " "))
    for k, v in extra.items():
        setattr(ds, k, v)
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(path, enforce_file_format=True)

SAMPLES = Path(__file__).resolve().parent / "samples"

def stamp_real(src, path, pat, study_uid, series_no, **extra):
    """Take a real (already anonymised) radiograph and stamp FAKE patient details on it."""
    ds = pydicom.dcmread(src)
    ds.PatientName, ds.PatientID, ds.PatientBirthDate, ds.PatientSex = pat["name"], pat["id"], pat["dob"], pat["sex"]
    ds.PatientAddress, ds.PatientTelephoneNumbers = pat["addr"], pat["phone"]
    ds.InstitutionName, ds.InstitutionAddress = "Sunrise Diagnostics Pvt Ltd", "12 MG Road, Pune 411001"
    ds.ReferringPhysicianName, ds.OperatorsName = pat["ref"], "Tech^Sunil"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study_uid, generate_uid()
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = pat["study_date"]
    ds.AccessionNumber, ds.SeriesNumber, ds.InstanceNumber = pat["acc"], series_no, 1
    if "PixelSpacing" in ds and not all(float(v) > 0 for v in ds.PixelSpacing):
        del ds.PixelSpacing   # source sample has 0\\0 spacing, which renders black in OHIF
    for kw in ("PatientAge",):
        if kw in ds:
            del ds[kw]
    blk = ds.private_block(0x0029, "DEMO VENDOR", create=True)
    blk.add_new(0x10, "LO", pat["name"].replace("^", " "))
    for k, v in extra.items():
        setattr(ds, k, v)
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(path, enforce_file_format=True)

def burn(path, lines, corner="tl"):
    """Burn text into the pixels, as many CR / US machines do (patient name printed on the image)."""
    from PIL import Image, ImageDraw, ImageFont
    ds = pydicom.dcmread(path)
    arr = ds.pixel_array.astype(np.int32)
    h, w = arr.shape[-2:]
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    size = max(14, w // 55)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size) \
        if Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf").exists() else ImageFont.load_default()
    for i, t in enumerate(lines):
        d.text((int(w * 0.03), int(h * 0.03) + i * int(size * 1.4)), t, fill=255, font=font)
    m = np.asarray(mask) > 128
    bright = arr.min() if str(ds.PhotometricInterpretation) == "MONOCHROME1" else arr.max()
    arr[m] = bright
    ds.PixelData = arr.astype(ds.pixel_array.dtype).tobytes()
    ds.save_as(path, enforce_file_format=True)

def phantom(n, blob=True):
    y, x = np.mgrid[:n, :n]
    img = 800 + 600 * np.exp(-(((x - n / 2) / (n / 3)) ** 2 + ((y - n / 2) / (n / 2.5)) ** 2))
    if blob:
        img += 900 * np.exp(-(((x - n * .35) / 12) ** 2 + ((y - n * .4) / 12) ** 2))
    return np.clip(img + np.random.default_rng(0).normal(0, 20, img.shape), 0, 4095)

def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    p1 = dict(name="Sharma^Ramesh^Kumar", id="UHID-778812", dob="19680412", sex="M", acc="ACC-10231",
              addr="Flat 4B, Shanti Nagar, Pune 411014", phone="+91 98220 45671", ref="Dr^Meena^Kulkarni", study_date="20260901")
    p2 = dict(name="Iyer^Lakshmi", id="UHID-903311", dob="19810930", sex="F", acc="ACC-55678",
              addr="22 Anna Salai, Chennai 600002", phone="9444012345", ref="Dr^Arjun^Rao", study_date="20260905")
    p3 = dict(name="Patel^Ananya", id="UHID-440021", dob="20070315", sex="F", acc="ACC-60990",
              addr="Civil Lines, Lucknow 226001", phone="9839001122", ref="Dr^Neha^Gupta", study_date="20260910")

    # 1) REAL chest X-ray (PA), file without extension, patient's name typed into the description
    stamp_real(SAMPLES / "chest_pa_RG1.dcm", ROOT / "CR_export_0923" / "IMG0001", p1, generate_uid(), 2,
               StudyDescription="XR CHEST PA - RAMESH SHARMA", ViewPosition="PA")
    burn(ROOT / "CR_export_0923" / "IMG0001", ["SHARMA RAMESH KUMAR  58Y/M", "UHID-778812  01/09/2026", "SUNRISE DIAGNOSTICS PUNE"])
    (ROOT / "CR_export_0923" / "report.txt").write_text(
        "SUNRISE DIAGNOSTICS PVT LTD, PUNE\n"
        "Patient Name: Mr. Ramesh Kumar Sharma\nUHID: UHID-778812\nAge/Sex: 58Y/M\n"
        "Ref By: Dr. Meena Kulkarni\nDate: 01/09/2026\nMobile: +91 98220 45671\n\n"
        "X-RAY CHEST PA (FICTIONAL DEMO REPORT)\n\nFINDINGS:\nMultiple well-defined rounded opacities of varying size in both lungs,\n"
        "predominantly mid and lower zones. Cardiac silhouette within normal limits. Costophrenic angles clear.\n\n"
        "IMPRESSION:\nBilateral multiple pulmonary nodules - suggest CT chest correlation.\n\n"
        "Dr. Anil Deshpande, MD Radiology (Reg. No. MMC-2011/04/1234)\n")

    # 2) CT chest, 20 slices + a CT dose screen (secondary capture) that must be quarantined
    s, se = generate_uid(), generate_uid()
    for i in range(1, 21):
        make(ROOT / "CT" / "Study_A" / f"CT{i:04d}.dcm", CT, "CT", p2, s, se, 3, i, phantom(128, blob=(8 <= i <= 12)),
             BodyPartExamined="CHEST", StudyDescription="CT CHEST PLAIN", SliceThickness=5.0,
             PixelSpacing=[0.7, 0.7], ImagePositionPatient=[0, 0, i * 5.0], ImageOrientationPatient=[1, 0, 0, 0, 1, 0],
             RescaleIntercept=-1024, RescaleSlope=1, KVP=120, ConvolutionKernel="B30f")
    make(ROOT / "CT" / "Study_A" / "DOSE_SUMMARY.dcm", SC, "CT", p2, s, generate_uid(), 999, 1, phantom(128, False),
         ImageType=["DERIVED", "SECONDARY", "SCREEN SAVE"])
    import docx
    d = docx.Document()
    for line in ["Patient Name : Mrs. Lakshmi Iyer", "Patient ID : UHID-903311", "Age / Sex : 44 Y / F",
                 "Referred by : Dr. Arjun Rao", "Study date: 5 Sep 2026", "Contact no: 9444012345", "",
                 "CT THORAX (PLAIN)", "", "Findings: A 9 mm solid nodule in the left lower lobe (series 3, image 10).",
                 "No mediastinal lymphadenopathy. No pleural effusion.", "",
                 "Impression: Solitary pulmonary nodule, Fleischner follow-up advised.", "", "Dr. Kavita Menon, DNB"]:
        d.add_paragraph(line)
    (ROOT / "reports").mkdir(parents=True, exist_ok=True)
    d.save(ROOT / "reports" / "ACC-55678_report.docx")

    # 3) Ultrasound with burned-in annotation -> quarantine
    make(ROOT / "US" / "us_0001.dcm", US, "US", p3, generate_uid(), generate_uid(), 1, 1, phantom(512, False),
         BurnedInAnnotation="YES", BodyPartExamined="ABDOMEN")
    burn(ROOT / "US" / "us_0001.dcm", ["PATEL ANANYA  UHID-440021", "10/09/2026  ABDOMEN"])

    # 4) REAL right leg X-ray with a documented finding (non-ossifying fibroma, distal tibia)
    stamp_real(SAMPLES / "tibia_RG3.dcm", ROOT / "misc" / "leg.dcm", p3, generate_uid(), 1,
               StudyDescription="XR RIGHT LEG AP - ANANYA PATEL", BodyPartExamined="LEG", ViewPosition="AP")
    (ROOT / "misc" / "ACC-60990.txt").write_text(
        "Name: Ananya Patel   Reg No: UHID-440021\nAadhaar: 4821 7730 1192\nemail: ananya.p@example.com\n"
        "Address: Civil Lines, Lucknow 226001\n\nX-RAY RIGHT LEG (TIBIA/FIBULA) AP (DEMO REPORT)\n"
        "Findings: Well-defined, eccentric, lobulated lytic lesion with a thin sclerotic margin in the distal\n"
        "tibial metaphysis. No periosteal reaction, cortical breach or soft-tissue component. Ankle joint normal.\n"
        "Impression: Appearances consistent with non-ossifying fibroma of the distal tibia.\n")

    # 5) junk that real exports contain
    (ROOT / "CR_export_0923" / "Thumbs.db").write_bytes(b"\x00junk")
    (ROOT / "DICOMDIR_notes.csv").write_text("exported,by,modality\n")
    print(f"synthetic inbox created at {ROOT}")

if __name__ == "__main__":
    main()
