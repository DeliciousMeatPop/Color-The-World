# Color The World

Turn photos and graphics into printable black-and-white coloring book pages.

1. Drop in a picture, drag the box to pick the part you want (locked to the page shape).
2. **Make coloring page**: local OpenCV tracing. Bold outlines come from a few big simplified shapes; thinner inner lines are kept only near those outlines, so the middle of shapes stays open (**Interior detail** 0-10). A plain/black backdrop is detected automatically and removed (one bold silhouette, no lines in the background). **Type** has a Line art mode for existing ink drawings. **Style** can also just remove color (gray / flat tones) with no lines.
3. Paint a red **selection** over anything you want to change, then:
   - **Re-trace area**: redo just that spot using the current Detail / Simplify / Line weight sliders
   - **Remove specks**, **Blank out**, or **AI redo area** (optional, needs a key)
   - Pen / Eraser for hand fixes, Undo/Redo, Download PNG, Print.

## Run

Needs Python 3. PHP is optional: without it `run.py` serves the app with the built-in `server.py` (same features). Force that with `python run.py --python`.

    run.bat        (Windows)      .\run.ps1      ./run.sh      python run.py

It makes `.venv`, installs `requirements.txt`, and serves http://127.0.0.1:8080 (set `CTW_PORT` to change).
Everything uses relative paths, so the folder can be moved between machines.

## AI (optional)

Copy `config.sample.json` to `config.json` and add `openai_api_key` (or set `OPENAI_API_KEY`).
Only the selected area is sent, as an image-edit request; the result is forced to pure black/white and blended back in.

## Hosting with nginx + PHP-FPM

See `nginx.sample.conf`. Point `root` at `public/` only; `storage/` (temporary projects, auto-deleted after 7 days), `engine/` and `config.json` stay outside the web root. Set `CTW_PYTHON` if PHP-FPM should use a specific interpreter, otherwise `.venv` is used when present.

## Layout

    engine/ctw.py   Python engine (ingest, generate, retrace, cleanup, ai)
    public/         index.php, api.php, app.js, style.css
