# De-identification Station

This is a local web app for imaging centres. You drop in X-ray, CT or ultrasound DICOM files and their reports, or point it at a folder on the computer. It works through the input one study at a time and produces a de-identified, AI-ready dataset:

```
deidentified_output/
├─ S7DB2DCB7C0A0/                 one folder per de-identified record (study)
│  ├─ CR_002_00001.dcm            de-identified image(s)
│  ├─ report.txt                  redacted report
│  ├─ preview.png                 thumbnail for review
│  └─ record.json                 this record's metadata
├─ _needs_review/<RECORD>/        records that failed QA are held back here
├─ deidentified_records.json      all records, updated after every record
└─ deidentified_records.csv       same, as a spreadsheet
secure/                           NEVER share: secret.key (coded-ID key) + ledger.csv (source file log)
```

The app listens on `127.0.0.1` only. Files never leave the computer, and there is no cloud service or PACS connection.

## Install and run

You need **Python 3.10, 3.11 or 3.12**. Python 3.13 and 3.14 are too new: the OCR package (`rapidocr-onnxruntime`) has no builds for them yet. The start scripts pick 3.12/3.11/3.10 automatically and rebuild a `.venv` made with the wrong version. On Windows, install 3.12 alongside any newer Python with `winget install -e --id Python.Python.3.12`. The first run installs about 1 GB of packages into a local `.venv`.

**Windows (PowerShell)**
```powershell
cd path\to\imaging-deid-app
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
```

**Mac / Linux (bash)**
```bash
cd path/to/imaging-deid-app
./start_mac_linux.sh
```

Your browser opens **http://localhost:8765**. Press Ctrl+C in the terminal to stop the app.

Manual setup, if you prefer it:

| Step | bash | PowerShell |
|---|---|---|
| venv | `python3.12 -m venv .venv && source .venv/bin/activate` | `py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1` |
| packages | `pip install -r requirements.txt` | `pip install -r requirements.txt` |
| NER model | `python -m spacy download en_core_web_sm` | `python -m spacy download en_core_web_sm` |
| run | `python -m app.server` | `python -m app.server` |
| other port | `DEID_PORT=9000 python -m app.server` | `$env:DEID_PORT=9000; python -m app.server` |
| CLI, no UI | `python -m app.engine --input /data/inbox` | `python -m app.engine --input D:\data\inbox` |
| tests | `python tests/test_end_to_end.py` | `python tests\test_end_to_end.py` |
| demo data | `python make_sample_data.py` → `sample_inbox/` | `python make_sample_data.py` |

## OHIF viewer (built in)
Use it to show the images before and after de-identification.

1. Extract `ohif-local-viewer-part1.zip` **and** `part2.zip` into this folder. You should end up with `ohif-local\viewer\index.html`.
2. Start the app as usual. The start scripts also launch OHIF on **http://localhost:3000/local** and stop it when you press Ctrl+C.
3. In the app, the top-right pill **Open OHIF viewer ↗** opens it in a new tab. Drag DICOM files onto it, click the study, then **Basic Viewer**. The **Tag Browser** (under "more tools" in the toolbar) shows the header.

To run OHIF on its own:

| | bash | PowerShell |
|---|---|---|
| start | `python3 serve_ohif.py` | `py serve_ohif.py` |
| other port | `python3 serve_ohif.py --port 3001` | `py serve_ohif.py --port 3001` |

**Doctor demo pairs:** after processing `sample_inbox`, run `python make_doctor_demo.py` (PowerShell: `.\.venv\Scripts\python.exe make_doctor_demo.py`). It writes numbered BEFORE/AFTER files to `doctor_demo\`.

## Using it
1. **Drop** files or whole folders onto the drop zone, or use **Browse files** / **Browse folder**. Dropped files are copied to a temporary staging folder, processed, and the staging copy is then deleted.
2. **Or** type a folder path (for example `D:\Exports\2026-09`) and click **Run**. The folder is read in place and nothing is copied.
3. Watch each record appear live. Click a row to see the preview, the redacted report and what was removed.
4. Use **Open output folder**, **CSV** or **JSON** to get the results.

Re-running the same input is safe. Files already processed are recognised by their SHA-256 hash and skipped. If new images arrive for a study that is already processed, they are merged into that study's folder.

## What happens to each record
| Step | How |
|---|---|
| Group | Files are indexed by DICOM StudyInstanceUID. One study becomes one record. |
| Header | **Allowlist**: 56 technical and clinical fields are kept. Everything else is dropped, including private vendor fields and sequences. |
| IDs | Patient and study IDs become keyed hashes (HMAC-SHA256, using the key in `secure/`). The same patient always gets the same code. |
| Dates | Shifted back by 1–365 days per patient (the default), or reduced to year only, or removed. Age is kept and capped at 90. |
| Burned-in text | **RapidOCR** reads CR, DX, US, MG, SC and similar images and blacks out any text it finds. Short positioning markers (R, L, PA, AP, LAT…) are kept. The image is then OCR'd again to check nothing is left. CT dose screens and screenshots are excluded. |
| Report | Matched to its study by accession number in the file name, by sharing the study's folder, or by patient ID. Redaction uses the patient's own identifiers from the header, labelled fields, and Indian patterns (mobile, Aadhaar-like numbers, ABHA, PAN, PIN code, dates, titled names, registration numbers). **Presidio** NER then catches remaining names and places. |
| QA | A1 tag allowlist · A2 no original identifier anywhere · A3 identity-removed flag · A4 no phone/ID patterns in the report · A6 valid pixel spacing · A7 no text left after masking. Any failure puts the record in `_needs_review/`. |

## Settings (`config.json`, also editable in the UI)
`output_dir`, `secure_dir`, `date_mode` (`shift` / `year_only` / `remove`), `ocr_enabled`, `ocr_modalities`, `ner_enabled`, `age_cap_years`.

## Tuning for a centre
- **Report templates:** add the centre's own field labels to `LABEL_LINE` in `app/core.py`.
- **Name detection:** add eponyms or clinical words that must not be redacted to `CLINICAL_ALLOW` in `app/text_ner.py`.
- **OCR:** add positional markers that must stay visible to `KEEP_TOKENS` in `app/ocr_mask.py`. Mirrored or unusual markers can get masked, which is the conservative choice.

## Known limits
- PDF reports aren't read yet. Export them as .txt/.docx, or add `pdfplumber` in `core.read_report`.
- Head CT or MR 3D reconstructions can show faces. Exclude them, or add defacing, before licensing.
- The small spaCy model misses some names and over-flags some clinical phrases (the filter removes most of these). Keep human review of a sample before each release.
- The upload path suits batches up to a few GB. For bigger exports, use the folder-path mode.

## Files
```
app/core.py        tag allowlist, coded IDs, date shift, report rules
app/ocr_mask.py    burned-in text masking (RapidOCR)
app/text_ner.py    Presidio name / place detection with a clinical-term filter
app/engine.py      record-by-record loop, output writer, QA, JSON/CSV store
app/server.py      FastAPI server, live progress (Server-Sent Events)
app/static/        UI (plain HTML/CSS/JS, no build step, no external calls)
make_sample_data.py  demo inbox: real public NEMA test X-rays + fake Indian patient details + burned-in text
tests/test_end_to_end.py
```
