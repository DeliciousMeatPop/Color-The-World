#!/usr/bin/env python3
"""One-step launcher: makes a .venv, installs deps, starts PHP's dev server."""
import os, shutil, subprocess, sys, venv, webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
PORT = os.environ.get("CTW_PORT", "8080")
win = os.name == "nt"
py = os.path.join(".venv", "Scripts" if win else "bin", "python.exe" if win else "python")

if not os.path.exists(py):
    print("Creating .venv ...")
    venv.create(".venv", with_pip=True)
print("Checking Python packages ...")
subprocess.check_call([py, "-m", "pip", "install", "-q", "-r", "requirements.txt"])
if not shutil.which("php"):
    sys.exit("PHP not found. Install PHP 8+ and make sure `php` is on your PATH.")

os.makedirs(os.path.join("storage", "projects"), exist_ok=True)
url = "http://127.0.0.1:%s/" % PORT
print("Open", url, "(Ctrl+C to stop)")
try:
    webbrowser.open(url)
except Exception:
    pass
subprocess.call(["php", "-d", "upload_max_filesize=40M", "-d", "post_max_size=60M",
                 "-d", "max_execution_time=300", "-S", "127.0.0.1:" + PORT, "-t", "public"])
