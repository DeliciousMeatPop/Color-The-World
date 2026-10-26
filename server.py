#!/usr/bin/env python3
"""Pure-Python stand-in for public/api.php so the tool runs with no PHP installed.
Same endpoints as api.php; serves public/ and calls engine/ctw.py."""
import json, os, re, secrets, subprocess, sys, time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
STORAGE = os.path.join(ROOT, "storage", "projects")
ENGINE = os.path.join(ROOT, "engine", "ctw.py")
MAX_AGE = 7 * 86400
MAX_BODY = 60 * 1024 * 1024
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png"}


def clamp(v, d, lo, hi):
    try:
        return str(max(lo, min(hi, int(v))))
    except (TypeError, ValueError):
        return str(d)


def cleanup_old():
    for n in os.listdir(STORAGE):
        d = os.path.join(STORAGE, n)
        if os.path.isdir(d) and os.path.getmtime(d) < time.time() - MAX_AGE:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)


def run_engine(args):
    p = subprocess.run([sys.executable, "-I", ENGINE] + args, capture_output=True, text=True)
    lines = [l for l in p.stdout.strip().splitlines() if l]
    try:
        res = json.loads(lines[-1])
    except Exception:
        return 500, {"error": "Engine error: " + (p.stderr or p.stdout)[-400:]}
    return (400, res) if "error" in res else (200, res)


class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def project(self, pid):
        if not re.fullmatch(r"[a-f0-9]{16}", pid or "") or not os.path.isdir(os.path.join(STORAGE, pid)):
            return None
        return os.path.join(STORAGE, pid)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api.php":
            d = self.project(q.get("id"))
            if q.get("action") != "file" or not d or q.get("name") not in ("source", "base"):
                return self.send(404, {"error": "Not found"})
            return self.send(200, open(os.path.join(d, q["name"] + ".png"), "rb").read(), "image/png")
        page = u.path in ("/", "/index.php")
        path = os.path.join(PUBLIC, "index.php") if page else os.path.normpath(os.path.join(PUBLIC, u.path.lstrip("/")))
        ext = ".html" if page else os.path.splitext(path)[1]
        if not path.startswith(PUBLIC + os.sep) or ext not in TYPES or not os.path.isfile(path):
            return self.send(404, {"error": "Not found"})
        data = open(path, "rb").read()
        if page:   # render the little bit of PHP in index.php
            html = data.decode("utf-8")
            html = html[html.index("<!doctype"):]
            key = os.environ.get("OPENAI_API_KEY") or os.path.isfile(os.path.join(ROOT, "config.json"))
            tags = iter(["", "" if key else " (no key)"])   # only <?= ?> tags left: title attr, label
            html = re.sub(r"<\?=.*?\?>", lambda m: next(tags), html)
            data = html.encode("utf-8")
        self.send(200, data, TYPES[ext] + "; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self.send(413, {"error": "File too large."})
        raw = self.rfile.read(n)
        msg = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
        f, files = {}, {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                files[name] = part.get_payload(decode=True)
            else:
                f[name] = part.get_content()
        a = f.get("action", "")
        os.makedirs(STORAGE, exist_ok=True)
        if a == "upload":
            if "image" not in files:
                return self.send(400, {"error": "Missing upload: image"})
            cleanup_old()
            pid = secrets.token_hex(8)
            d = os.path.join(STORAGE, pid)
            os.makedirs(d)
            up = os.path.join(d, "upload.bin")
            open(up, "wb").write(files["image"])
            code, res = run_engine(["ingest", "--proj", d, "--src", up])
            os.remove(up)
            return self.send(code, {"id": pid, **res} if code == 200 else res)
        d = self.project(f.get("id"))
        if not d:
            return self.send(404, {"error": "Unknown project, upload again."})
        out = os.path.join(d, "page.png")
        t = ["--mode", "graphic" if f.get("mode") == "graphic" else "photo",
             "--detail", clamp(f.get("detail"), 5, 1, 10), "--weight", clamp(f.get("weight"), 3, 1, 10),
             "--simplify", clamp(f.get("simplify"), 4, 0, 10)]
        if a == "generate":
            orient = f.get("orient") if f.get("orient") in ("portrait", "landscape", "square") else "portrait"
            dpi = f.get("dpi") if f.get("dpi") in ("150", "200", "300") else "200"
            args = ["generate", "--proj", d, "--out", out, "--orient", orient, "--dpi", dpi] + t
            if re.fullmatch(r"[0-9.]+(,[0-9.]+){3}", f.get("rect", "")):
                args += ["--rect", f["rect"]]
        elif a in ("retrace", "cleanup", "ai"):
            if "page" not in files or "mask" not in files:
                return self.send(400, {"error": "Missing page or mask."})
            open(os.path.join(d, "in_page.png"), "wb").write(files["page"])
            open(os.path.join(d, "in_mask.png"), "wb").write(files["mask"])
            args = [a, "--proj", d, "--out", out, "--page", os.path.join(d, "in_page.png"),
                    "--mask", os.path.join(d, "in_mask.png")] + t
            if a == "ai":
                args += ["--prompt", f.get("prompt", "")[:500]]
        else:
            return self.send(400, {"error": "Unknown action"})
        code, res = run_engine(args)
        if code != 200:
            return self.send(code, res)
        self.send(200, open(out, "rb").read(), "image/png")

    def log_message(self, *a):
        pass


def serve(port):
    print("Serving http://127.0.0.1:%d/ (Ctrl+C to stop)" % port)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    serve(int(os.environ.get("CTW_PORT", "8080")))
