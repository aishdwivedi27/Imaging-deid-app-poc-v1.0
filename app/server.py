"""
Local web app (127.0.0.1 only) around the de-identification engine.

    python -m app.server            -> http://localhost:8765

Endpoints
  GET  /                         UI
  GET  /api/status               settings + OCR/NER availability + current job
  POST /api/settings             update settings (output folder, OCR, NER, date mode)
  POST /api/upload               multipart files (+ relative paths) -> staged -> processed -> staging deleted
  POST /api/process-folder       {"path": "..."} process a folder on this computer in place
  GET  /api/jobs/{id}/events     Server-Sent Events: live progress
  POST /api/jobs/{id}/cancel
  GET  /api/records              all records (from deidentified_records.json)
  GET  /api/records/{rid}/preview | /report | /json
  GET  /api/download/json | csv
  POST /api/open-output          open the output folder in Explorer / Finder
"""
import asyncio, json, os, platform, queue, shutil, subprocess, sys, threading, uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import engine, ocr_mask, text_ner

app = FastAPI(title="Imaging De-identification Station")
STATIC = Path(__file__).parent / "static"
SETTINGS = engine.load_settings()
JOBS: dict[str, engine.Job] = {}
RUN_LOCK = threading.Lock()          # one job at a time; others wait their turn
CAPS = {"ocr": None, "ner": None}


def out_dir():
    return engine._abs(SETTINGS["output_dir"])


def _caps():
    if CAPS["ocr"] is None:
        CAPS["ocr"] = ocr_mask.available()
    if CAPS["ner"] is None:
        CAPS["ner"] = text_ner.available()
    return CAPS


def _start(job: engine.Job):
    JOBS[job.id] = job

    def worker():
        with RUN_LOCK:
            try:
                job.run()
            except Exception as e:
                job.state = "failed"
                job.emit(type="failed", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job.id}


def _ohif():
    import socket
    port = int(os.environ.get("OHIF_PORT", "3000"))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return f"http://localhost:{port}/local"
    except OSError:
        return None


@app.get("/api/status")
def status():
    caps = _caps()
    running = next((j.id for j in JOBS.values() if j.state in ("queued", "running")), None)
    return {"settings": SETTINGS, "output_dir_abs": str(out_dir()), "ocr_available": caps["ocr"],
            "ner_available": caps["ner"], "ner_status": text_ner.status(), "running_job": running,
            "platform": platform.system(), "ohif_url": _ohif()}


@app.post("/api/settings")
async def update_settings(req: Request):
    body = await req.json()
    for k in ("output_dir", "date_mode", "ocr_enabled", "ner_enabled"):
        if k in body:
            SETTINGS[k] = body[k]
    if SETTINGS["date_mode"] not in ("shift", "year_only", "remove"):
        raise HTTPException(400, "date_mode must be shift, year_only or remove")
    engine.save_settings(SETTINGS)
    return status()


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), paths: list[str] = Form(default=[])):
    staging = out_dir().parent / f".staging_{uuid.uuid4().hex[:8]}"   # outside the output folder
    staging.mkdir(parents=True)
    for i, f in enumerate(files):
        rel = paths[i] if i < len(paths) and paths[i] else f.filename
        rel = Path(*[p for p in Path(rel.replace("\\", "/")).parts if p not in ("..", "/", "")])
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            shutil.copyfileobj(f.file, fh)
    job = engine.Job(staging, dict(SETTINGS), cleanup_input=True)
    return _start(job)


@app.post("/api/process-folder")
async def process_folder(req: Request):
    body = await req.json()
    p = Path(os.path.expanduser(body.get("path", "").strip().strip('"')))
    if not p.is_dir():
        raise HTTPException(400, f"Folder not found: {p}")
    if out_dir() in p.resolve().parents or p.resolve() == out_dir().resolve():
        raise HTTPException(400, "Input folder cannot be inside the output folder")
    return _start(engine.Job(p, dict(SETTINGS)))


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    q = queue.Queue()
    with job.lock:
        backlog = list(job.events)
        job.listeners.append(q)

    async def stream():
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev)}\n\n"
            if job.state in ("finished", "failed"):
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.to_thread(q.get, True, 15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
                if ev["type"] in ("finished", "failed", "cancelled"):
                    return
        finally:
            with job.lock:
                if q in job.listeners:
                    job.listeners.remove(q)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    job.cancel.set()
    return {"ok": True}


@app.get("/api/records")
def records():
    return engine.RecordStore(out_dir()).list()


def _record_dir(rid):
    if not rid.isalnum():
        raise HTTPException(400, "bad id")
    for d in (out_dir() / rid, out_dir() / "_needs_review" / rid):
        if d.is_dir():
            return d
    raise HTTPException(404, "record not found")


@app.get("/api/records/{rid}/preview")
def preview(rid: str):
    p = _record_dir(rid) / "preview.png"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/records/{rid}/report", response_class=PlainTextResponse)
def report(rid: str):
    p = _record_dir(rid) / "report.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@app.get("/api/records/{rid}/json")
def record_json(rid: str):
    return JSONResponse(json.loads((_record_dir(rid) / "record.json").read_text()))


@app.get("/api/download/{kind}")
def download(kind: str):
    name = {"json": "deidentified_records.json", "csv": "deidentified_records.csv"}.get(kind)
    p = out_dir() / (name or "")
    if not name or not p.exists():
        raise HTTPException(404, "nothing processed yet")
    return FileResponse(p, filename=name)


@app.post("/api/open-output")
def open_output():
    d = out_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(d)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
    except Exception as e:
        return {"ok": False, "path": str(d), "error": str(e)}
    return {"ok": True, "path": str(d)}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


def main():
    import uvicorn, webbrowser
    port = int(os.environ.get("DEID_PORT", "8765"))
    print(f"\n  De-identification Station running at http://localhost:{port}  (local only; Ctrl+C to stop)\n")
    if os.environ.get("DEID_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
