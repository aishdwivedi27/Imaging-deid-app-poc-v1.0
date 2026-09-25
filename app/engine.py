"""
Record-by-record de-identification engine.

    input folder (any layout: DICOM with/without extension, .txt/.docx reports)
        -> index files by study (headers only)
        -> for each study, one at a time:
             de-identify images (allowlist + coded IDs + date shift + OCR masking of burned-in text)
             match + redact the report (rules + header identifiers + Presidio NER)
             QA the record
             write   <output>/<RECORD_ID>/  images + report.txt + preview.png + record.json
                     (QA failures go to <output>/_needs_review/<RECORD_ID>/ instead)
             update  <output>/deidentified_records.json  and  <output>/deidentified_records.csv
        -> events are emitted after every step so the UI can show live progress

CLI:  python -m app.engine --input INBOX --output OUT
"""
import argparse, csv, datetime as dt, hashlib, json, os, re, shutil, sys, threading
from collections import defaultdict
from pathlib import Path

import numpy as np
import pydicom
from PIL import Image

from . import core, ocr_mask, text_ner

APP_DIR = Path(__file__).resolve().parent.parent
REPORT_EXTS = {".txt", ".docx"}
NON_IMAGE_SOP = {"1.2.840.10008.5.1.4.1.1.104.1": "encapsulated PDF"}
SCREEN_SAVE_HINT = ("SCREEN SAVE", "DOSE")
ALLOWED = core.KEEP_TAGS | set(core.DATE_TAGS) | set(core.UID_TAGS) | {
    "PatientID", "PatientName", "AccessionNumber", "StudyID", "PatientAge",
    "PatientIdentityRemoved", "DeidentificationMethod", "BurnedInAnnotation", "PlanarConfiguration"}
RESIDUAL = [(n, rx) for n, rx in core.REGEX_RULES if n in {"EMAIL", "PHONE", "ID12", "PAN", "ABHA"}]
CSV_FIELDS = ["record_id", "patient_id", "modality", "body_part", "study_description", "study_date",
              "patient_sex", "patient_age", "manufacturer", "model", "n_series", "n_images",
              "images_excluded", "excluded_reasons", "ocr_regions_masked", "report_present",
              "report_redactions", "qa_status", "qa_findings", "folder", "processed_at", "job_id"]


# ------------------------------------------------------------------------------------------ settings
DEFAULTS = {
    "output_dir": "deidentified_output",
    "secure_dir": "secure",
    "date_mode": "shift",               # shift | year_only | remove
    "ocr_enabled": True,
    "ocr_modalities": ["CR", "DX", "RG", "US", "MG", "XA", "RF", "SC", "OT", "ES", "XC"],
    "ner_enabled": True,
    "age_cap_years": 90,
}


def load_settings():
    p = APP_DIR / "config.json"
    s = dict(DEFAULTS)
    if p.exists():
        s.update(json.loads(p.read_text()))
    return s


def save_settings(s):
    (APP_DIR / "config.json").write_text(json.dumps(s, indent=2))


def _abs(p):
    p = Path(os.path.expanduser(str(p)))
    return p if p.is_absolute() else (APP_DIR / p)


# ------------------------------------------------------------------------------------------ store
class RecordStore:
    """deidentified_records.json (array) + .csv at the output root, updated after every record."""

    def __init__(self, out: Path):
        self.out = out
        self.json_path = out / "deidentified_records.json"
        self.csv_path = out / "deidentified_records.csv"
        self.records = {}
        if self.json_path.exists():
            try:
                for r in json.loads(self.json_path.read_text()):
                    self.records[r["record_id"]] = r
            except Exception:
                pass

    def upsert(self, rec):
        self.records[rec["record_id"]] = rec
        rows = sorted(self.records.values(), key=lambda r: r["processed_at"])
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2))
        os.replace(tmp, self.json_path)
        tmp = self.csv_path.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, self.csv_path)

    def list(self):
        return sorted(self.records.values(), key=lambda r: r["processed_at"], reverse=True)


class Ledger:
    """Source-file hash -> record. Kept in the secure folder (source paths can contain names)."""

    def __init__(self, secure: Path):
        self.path = secure / "ledger.csv"
        self.seen = {}
        if self.path.exists():
            with open(self.path, newline="", encoding="utf-8") as f:
                self.seen = {r["sha256"]: r for r in csv.DictReader(f)}

    def add(self, sha, source, result, record_id):
        new = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["sha256", "source", "result", "record_id", "at"])
            if new:
                w.writeheader()
            row = {"sha256": sha, "source": source, "result": result, "record_id": record_id,
                   "at": dt.datetime.now().isoformat(timespec="seconds")}
            w.writerow(row)
        self.seen[sha] = row


# ------------------------------------------------------------------------------------------ helpers
def _preview(ds_path, png_path, size=512):
    ds = pydicom.dcmread(ds_path)
    a = ds.pixel_array
    if int(ds.get("NumberOfFrames", 1) or 1) > 1:
        a = a[len(a) // 2]
    if a.ndim == 3:
        a = a.mean(axis=2)
    try:
        from pydicom.pixels import apply_modality_lut, apply_voi_lut
        if "WindowCenter" in ds and a.ndim == 2:
            a = apply_voi_lut(apply_modality_lut(a, ds), ds)
    except Exception:
        pass
    a = a.astype(np.float32)
    lo, hi = np.percentile(a, 1), np.percentile(a, 99)
    a = np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    if str(ds.get("PhotometricInterpretation", "")) == "MONOCHROME1":
        a = 255 - a
    im = Image.fromarray(a.astype(np.uint8))
    im.thumbnail((size, size))
    im.save(png_path)


def _qa_record(folder, identifiers, report_text):
    findings = []
    idents = sorted((i for i in identifiers if len(i) >= 3), key=len, reverse=True)
    id_rx = re.compile("|".join(re.escape(i) for i in idents), re.I) if idents else None
    for f in sorted(folder.glob("*.dcm")):
        ds = pydicom.dcmread(f, stop_before_pixels=True)
        extra = [el.keyword or str(el.tag) for el in ds if el.keyword not in ALLOWED or el.tag.is_private or el.VR == "SQ"]
        if extra:
            findings.append(f"A1 {f.name}: unexpected tags {extra}")
        if str(ds.get("PatientIdentityRemoved", "")) != "YES":
            findings.append(f"A3 {f.name}: identity flag missing")
        for kw in ("PixelSpacing", "ImagerPixelSpacing"):
            if kw in ds and not all(float(v) > 0 for v in ds[kw].value):
                findings.append(f"A6 {f.name}: invalid {kw}")
        if id_rx:
            for el in ds:
                if el.VR in {"PN", "LO", "SH", "ST", "LT", "UT", "CS", "DA", "UI", "AS", "DS", "IS"}:
                    if id_rx.search(str(el.value)):
                        findings.append(f"A2 {f.name}: {el.keyword} contains an original identifier")
    if report_text is not None:
        if id_rx and id_rx.search(report_text):
            findings.append("A2 report.txt: original identifier present")
        for name, rx in RESIDUAL:
            if rx.search(report_text):
                findings.append(f"A4 report.txt: residual {name} pattern")
    return findings


def _match_report(study, reports, studies_by_dir, studies_by_pid):
    for r in reports:
        if r in study["_used_reports"]:
            continue
        stem = r.stem
        if study["accession"] and study["accession"] in stem:
            return r
        if r.parent in study["dirs"] and len(studies_by_dir[r.parent]) == 1:
            return r
        if study["pid"] and study["pid"] in stem and len(studies_by_pid[study["pid"]]) == 1:
            return r
    return None


# ------------------------------------------------------------------------------------------ job
class Job:
    def __init__(self, input_dir, settings, job_id=None, cleanup_input=False):
        self.input_dir = Path(input_dir)
        self.s = settings
        self.id = job_id or dt.datetime.now().strftime("J%Y%m%d-%H%M%S")
        self.cleanup_input = cleanup_input
        self.cancel = threading.Event()
        self.events = []
        self.listeners = []
        self.lock = threading.Lock()
        self.state = "queued"

    # event fan-out ---------------------------------------------------------------------------
    def emit(self, **ev):
        ev.setdefault("job_id", self.id)
        ev.setdefault("at", dt.datetime.now().isoformat(timespec="seconds"))
        with self.lock:
            self.events.append(ev)
            for q in list(self.listeners):
                q.put(ev)

    # main loop -------------------------------------------------------------------------------
    def run(self):
        s = self.s
        out = _abs(s["output_dir"]); secure = _abs(s["secure_dir"])
        out.mkdir(parents=True, exist_ok=True); secure.mkdir(parents=True, exist_ok=True)
        key = core.load_or_create_key(secure / "secret.key")
        store, ledger = RecordStore(out), Ledger(secure)
        cfg = {"date_mode": s["date_mode"], "age_cap_years": s["age_cap_years"]}
        use_ocr = s["ocr_enabled"] and ocr_mask.available()
        use_ner = s["ner_enabled"] and text_ner.available()
        self.state = "running"
        self.emit(type="started", ocr=use_ocr, ner=use_ner,
                  ocr_note=None if use_ocr or not s["ocr_enabled"] else "OCR not installed: risky images will be excluded",
                  ner_note=None if use_ner or not s["ner_enabled"] else f"NER {text_ner.status()}")

        # 1. index ---------------------------------------------------------------------------
        files = [p for p in self.input_dir.rglob("*") if p.is_file() and not p.name.startswith(".")]
        reports = [p for p in files if p.suffix.lower() in REPORT_EXTS]
        studies, skipped = {}, defaultdict(int)
        for p in files:
            if p.suffix.lower() in REPORT_EXTS:
                continue
            sha = core.sha256_file(p)
            if sha in ledger.seen:
                skipped["already processed"] += 1
                continue
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True)
            except Exception:
                skipped["not DICOM"] += 1
                ledger.add(sha, str(p.relative_to(self.input_dir)), "not_dicom", "")
                continue
            suid = str(ds.get("StudyInstanceUID", "") or p.parent)
            st = studies.setdefault(suid, {"suid": suid, "pid": str(ds.get("PatientID", "") or ""),
                                           "accession": str(ds.get("AccessionNumber", "") or ""),
                                           "files": [], "dirs": set(), "_used_reports": set()})
            st["files"].append((p, sha))
            st["dirs"].add(p.parent)
        studies_by_dir, studies_by_pid = defaultdict(set), defaultdict(set)
        for st in studies.values():
            for d in st["dirs"]:
                studies_by_dir[d].add(st["suid"])
            studies_by_pid[st["pid"]].add(st["suid"])
        self.emit(type="indexed", files=len(files), studies=len(studies), reports=len(reports),
                  skipped=dict(skipped))

        # 2. record loop ----------------------------------------------------------------------
        done, totals, used_reports = 0, defaultdict(int), set()
        for st in studies.values():
            if self.cancel.is_set():
                self.emit(type="cancelled", done=done)
                break
            try:
                rec = self._process_study(st, key, cfg, out, store, ledger, reports, used_reports,
                                          studies_by_dir, studies_by_pid, use_ocr, use_ner)
            except Exception as e:  # never stop the loop for one bad study
                self.emit(type="record_error", error=f"{type(e).__name__}: {e}")
                totals["errors"] += 1
                continue
            done += 1
            totals["images"] += rec["n_images"]
            totals["ocr_regions"] += rec["ocr_regions_masked"]
            totals["redactions"] += rec["report_redactions"]
            totals["qa_fail"] += rec["qa_status"] != "pass"
            self.emit(type="record", record=rec, done=done, total=len(studies))

        unmatched = [r for r in reports if r not in used_reports]
        self.state = "finished"
        self.emit(type="finished", done=done, total=len(studies), totals=dict(totals),
                  unmatched_reports=len(unmatched), output_dir=str(out))
        if self.cleanup_input:
            shutil.rmtree(self.input_dir, ignore_errors=True)

    # one study -------------------------------------------------------------------------------
    def _process_study(self, st, key, cfg, out, store, ledger, reports, used_reports,
                       studies_by_dir, studies_by_pid, use_ocr, use_ner):
        pid = st["pid"] or "UNKNOWN"
        pat = "P" + core.h(key, "patient", pid)[:12].upper()
        rid = "S" + core.h(key, "study", st["suid"])[:12].upper()
        shift = int(core.h(key, "shift", pid)[:8], 16) % 365 + 1
        self.emit(type="record_start", record_id=rid, images=len(st["files"]))

        work = out / f".work_{rid}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        existing = out / rid if (out / rid).exists() else (out / "_needs_review" / rid if (out / "_needs_review" / rid).exists() else None)
        if existing:                                   # same study re-sent with more images -> merge
            for f in existing.iterdir():
                shutil.copy2(f, work / f.name)

        identifiers, meta, excluded = set(), {}, defaultdict(int)
        ocr_regions, ocr_unverified, written, mods = 0, 0, [], set()
        for p, sha in st["files"]:
            ds = pydicom.dcmread(p)
            identifiers |= core.known_identifiers(ds)
            identifiers |= {pid, st["suid"], str(ds.get("SOPInstanceUID", ""))} - {"", "UNKNOWN"}
            mod = str(ds.get("Modality", "OT")).upper()
            sop = str(ds.get("SOPClassUID", ""))
            itype = " ".join(str(x).upper() for x in (ds.get("ImageType", []) or []))
            reason = None
            if "PixelData" not in ds:
                reason = NON_IMAGE_SOP.get(sop, "no image data")
            elif any(k in itype for k in SCREEN_SAVE_HINT) or (sop.startswith("1.2.840.10008.5.1.4.1.1.7") and mod == "CT"):
                reason = "screen capture / dose report"
            risky = mod in self.s["ocr_modalities"] or str(ds.get("BurnedInAnnotation", "")).upper() == "YES" \
                or sop.startswith("1.2.840.10008.5.1.4.1.1.7")
            if not reason and risky and not use_ocr and (mod in {"US", "SC", "OT"} or str(ds.get("BurnedInAnnotation", "")).upper() == "YES"):
                reason = "may contain burned-in text (OCR off)"
            if reason:
                excluded[reason] += 1
                ledger.add(sha, str(p.relative_to(self.input_dir)), f"excluded: {reason}", rid)
                continue
            if risky and use_ocr:
                r = ocr_mask.mask_dataset(ds)
                ocr_regions += r["regions"]
                ocr_unverified += 0 if r["verified"] else 1
            o = core.deid_dataset(ds, key, cfg, pat, rid, shift)
            if "BurnedInAnnotation" in ds:
                o.BurnedInAnnotation = ds.BurnedInAnnotation
            series = int(float(str(ds.get("SeriesNumber", "0") or "0")))
            inst = str(ds.get("InstanceNumber", "") or sha[:8])
            name = f"{mod}_{series:03d}_{int(inst):05d}.dcm" if inst.isdigit() else f"{mod}_{series:03d}_{inst}.dcm"
            o.save_as(work / name, enforce_file_format=True)
            written.append(name)
            ledger.add(sha, str(p.relative_to(self.input_dir)), "ok", rid)
            if not meta:
                meta = {k: str(o.get(k, "")) for k in ("Modality", "BodyPartExamined", "StudyDescription", "StudyDate",
                                                        "PatientSex", "PatientAge", "Manufacturer", "ManufacturerModelName")}
            mods.add(mod)

        # report ------------------------------------------------------------------------------
        report_text, red_counts = None, {}
        rp = _match_report(st, [r for r in reports if r not in used_reports], studies_by_dir, studies_by_pid)
        if rp is not None:
            report_text, red_counts = core.redact_report(core.read_report(rp), identifiers)
            red_counts = {k: v for k, v in red_counts.items() if v}
            if use_ner:
                report_text, ner_counts = text_ner.redact(report_text)
                red_counts.update(ner_counts)
            (work / "report.txt").write_text(report_text, encoding="utf-8")
            used_reports.add(rp)
            ledger.add(core.sha256_file(rp), str(rp.relative_to(self.input_dir)), "report ok", rid)
        elif (work / "report.txt").exists():
            report_text = (work / "report.txt").read_text(encoding="utf-8")

        # preview + QA ------------------------------------------------------------------------
        dcms = sorted(work.glob("*.dcm"))
        if dcms:
            try:
                _preview(dcms[len(dcms) // 2], work / "preview.png")
            except Exception:
                pass
        findings = _qa_record(work, identifiers, report_text)
        if ocr_unverified:
            findings.append(f"A7 {ocr_unverified} image(s): text still detected after masking")
        if not dcms:
            findings.append("No releasable images in this record")
        status = "pass" if not findings else "review"

        dest = out / rid if status == "pass" else out / "_needs_review" / rid
        for old in (out / rid, out / "_needs_review" / rid):
            if old.exists():
                shutil.rmtree(old)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(work, dest)

        prev = store.records.get(rid, {})
        rec = {
            "record_id": rid, "patient_id": pat,
            "modality": "|".join(sorted(mods | set(filter(None, prev.get("modality", "").split("|"))))) or prev.get("modality", ""),
            "body_part": meta.get("BodyPartExamined", prev.get("body_part", "")),
            "study_description": meta.get("StudyDescription", prev.get("study_description", "")),
            "study_date": meta.get("StudyDate", prev.get("study_date", "")),
            "patient_sex": meta.get("PatientSex", prev.get("patient_sex", "")),
            "patient_age": meta.get("PatientAge", prev.get("patient_age", "")),
            "manufacturer": meta.get("Manufacturer", prev.get("manufacturer", "")),
            "model": meta.get("ManufacturerModelName", prev.get("model", "")),
            "n_series": len({f.name.split("_")[1] for f in dcms}),
            "n_images": len(dcms),
            "images": [f.name for f in dcms],
            "images_excluded": sum(excluded.values()) + prev.get("images_excluded", 0),
            "excluded_reasons": "; ".join(f"{k} x{v}" for k, v in excluded.items()),
            "ocr_regions_masked": ocr_regions + prev.get("ocr_regions_masked", 0),
            "report_present": (dest / "report.txt").exists(),
            "report_redactions": sum(red_counts.values()) or prev.get("report_redactions", 0),
            "report_redaction_breakdown": red_counts or prev.get("report_redaction_breakdown", {}),
            "qa_status": status, "qa_findings": " | ".join(findings),
            "folder": str(dest.relative_to(out)).replace("\\", "/"),
            "processed_at": dt.datetime.now().isoformat(timespec="seconds"), "job_id": self.id,
        }
        (dest / "record.json").write_text(json.dumps(rec, indent=2))
        store.upsert(rec)
        return rec


def main():
    ap = argparse.ArgumentParser(description="De-identify a folder record by record")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output")
    a = ap.parse_args()
    s = load_settings()
    if a.output:
        s["output_dir"] = a.output
    job = Job(a.input, s)
    job.listeners.append(type("P", (), {"put": staticmethod(lambda e: print(json.dumps(
        {k: v for k, v in e.items() if k != "record"} | ({"record": e["record"]["record_id"], "qa": e["record"]["qa_status"]} if "record" in e else {}))))})())
    job.run()


if __name__ == "__main__":
    main()
