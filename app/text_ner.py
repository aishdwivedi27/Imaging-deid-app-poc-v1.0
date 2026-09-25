"""
Second-pass name / place detection in report text using Microsoft Presidio + spaCy (en_core_web_sm).

Runs AFTER the rule-based redaction in core.py. Presidio's small model over-calls clinical phrases as
names (e.g. "Grade III OA"), so a span is only redacted if every word is a Title-case word and none of
them is a known clinical term / eponym. Tune CLINICAL_ALLOW for the centre's own report templates.
"""
import re

CLINICAL_ALLOW = {w.lower() for w in """
Findings Impression Impressions Opinion Advice Report Clinical History Indication Technique Comparison Conclusion
Right Left Bilateral Upper Lower Middle Mid Zone Zones Lobe Lobes Lung Lungs Chest Thorax Heart Cardiac Abdomen Pelvis
Normal Abnormal Mild Moderate Severe Grade Stage Type No Nil Not Seen Noted Suggest Suggested Advised Correlation
Fleischner Kellgren Lawrence Hounsfield Bosniak Salter Harris Weber Garden Schatzker Pott Colles Smith Barton
Monteggia Galeazzi Jones Bennett Rolando Segond Hill Sachs Bankart Kerley Fleischner Swyer James Kartagener
Mallory Weiss Crohn Hodgkin Wilms Ewing Paget Perthes Osgood Schlatter Baker Morton Hoffa Chilaiditi Rigler
PA AP Lateral Erect Supine Portable CT MRI USG Xray X Ray Plain Contrast Dr MD DNB DMRD MBBS Radiology Radiologist
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August
September October November December India
""".split()}

_engine = None
_status = "not loaded"


def available():
    global _status
    try:
        _load()
        return True
    except Exception as e:  # missing package or model
        _status = f"unavailable: {type(e).__name__}"
        return False


def status():
    return _status


def _load():
    global _engine, _status
    if _engine is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        nlp = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
        _engine = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
        _status = "ready"
    return _engine


def _plausible(span):
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", span)
    if not words:
        return False
    for w in words:
        if w.lower() in CLINICAL_ALLOW or not re.fullmatch(r"[A-Z][a-z][a-zA-Z'\-]*", w):
            return False
    return True


def redact(text, min_score=0.6):
    """Returns (text, counts). Only PERSON and LOCATION spans that pass the plausibility filter."""
    eng = _load()
    results = eng.analyze(text=text, language="en", entities=["PERSON", "LOCATION"])
    counts = {}
    for r in sorted(results, key=lambda r: r.start, reverse=True):
        span = text[r.start:r.end]
        if r.score < min_score or "[" in span or not _plausible(span):
            continue
        tag = "[NAME]" if r.entity_type == "PERSON" else "[LOCATION]"
        text = text[:r.start] + tag + text[r.end:]
        counts["NER_" + r.entity_type] = counts.get("NER_" + r.entity_type, 0) + 1
    return text, counts
