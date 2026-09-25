#!/usr/bin/env python3
"""
Local, offline OHIF DICOM viewer for the MVP POC (http://localhost:3000/local).

Needs the viewer files in ./ohif-local/viewer  (extract ohif-local-viewer-part1.zip and part2.zip
into this folder). Files you open are read inside your browser; nothing is uploaded anywhere.

    py serve_ohif.py                 # Windows
    python3 serve_ohif.py            # Mac / Linux
    options: --port 3001   --no-browser
The start scripts launch this automatically next to the de-identification app.
"""
import argparse, http.server, os, sys, threading, webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANDIDATES = [HERE / "ohif-local" / "viewer", HERE / "ohif" / "viewer"]


def viewer_dir():
    return next((c for c in CANDIDATES if (c / "index.html").exists()), None)


def main():
    ap = argparse.ArgumentParser(description="Run the OHIF viewer locally")
    ap.add_argument("--port", type=int, default=int(os.environ.get("OHIF_PORT", "3000")))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    root = viewer_dir()
    if root is None:
        sys.exit("OHIF viewer files not found. Extract ohif-local-viewer-part1.zip and part2.zip into this folder "
                 "so that ohif-local\\viewer\\index.html exists.")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(root), **kw)

        def end_headers(self):
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
            super().end_headers()

        def send_head(self):
            if not os.path.exists(self.translate_path(self.path.split("?")[0])):
                self.path = "/index.html"          # single-page app routes like /local, /viewer
            return super().send_head()

        def log_message(self, *args):
            pass

    Handler.extensions_map.update({".js": "application/javascript", ".mjs": "application/javascript",
                                   ".wasm": "application/wasm"})
    url = f"http://localhost:{a.port}/local"
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:
        sys.exit(f"Port {a.port} is busy - OHIF may already be running at {url}, or use --port 3001")
    print(f"  OHIF viewer running at {url}  (local only; Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
