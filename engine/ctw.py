#!/usr/bin/env python3
"""Color The World engine: turns photos/graphics into coloring book pages.

Stateless CLI used by public/api.php. Every sub-command prints one JSON line on
stdout. Paths are always passed as arguments (relative or absolute).

  ingest    normalise an upload into <proj>/source.png
  generate  crop + trace the source into <proj>/base.png and an output page
  retrace   re-trace only the masked area of an existing page
  cleanup   remove specks / tiny regions inside the masked area
  ai        redo the masked area with an AI image-edit API (optional)
"""
import argparse
import base64
import io
import json
import os
import sys
import uuid
import urllib.request

import cv2
import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 120_000_000
MAX_SOURCE = 4096          # longest side kept from an upload
WORK_LONG_SIDE = 1400      # tracing is done at this size, then scaled up
PAGES = {                  # (w, h) in inches
    "portrait": (8.5, 11.0),
    "landscape": (11.0, 8.5),
    "square": (8.5, 8.5),
}


def out(**kw):
    print(json.dumps(kw))


def fail(msg):
    print(json.dumps({"error": msg}))
    sys.exit(1)


# ---------------------------------------------------------------- io helpers

def load_meta(proj):
    with open(os.path.join(proj, "meta.json")) as f:
        return json.load(f)


def save_meta(proj, meta):
    with open(os.path.join(proj, "meta.json"), "w") as f:
        json.dump(meta, f)


def read_mask(path, size):
    """Mask PNG from the browser: any pixel with alpha > 0 (or bright, if opaque) is 'selected'."""
    im = Image.open(path)
    if im.mode in ("RGBA", "LA"):
        m = np.array(im.getchannel("A"))
    else:
        m = np.array(im.convert("L"))
    m = (m > 8).astype(np.uint8) * 255
    if (m.shape[1], m.shape[0]) != tuple(size):
        m = cv2.resize(m, tuple(size), interpolation=cv2.INTER_NEAREST)
    return m


def read_page(path, size):
    g = np.array(Image.open(path).convert("L"))
    if (g.shape[1], g.shape[0]) != tuple(size):
        g = cv2.resize(g, tuple(size), interpolation=cv2.INTER_AREA)
    return g


def save_png(gray_or_bgr, path, dpi=None):
    if gray_or_bgr.ndim == 3:
        im = Image.fromarray(cv2.cvtColor(gray_or_bgr, cv2.COLOR_BGR2RGB))
    else:
        im = Image.fromarray(gray_or_bgr)
    kw = {"dpi": (dpi, dpi)} if dpi else {}
    im.save(path, "PNG", **kw)


# ----------------------------------------------------------------- tracing

def params_from(args, mode):
    """Map the friendly 1-10 sliders onto the numbers the pipeline needs."""
    detail = max(1, min(10, args.detail))
    simplify = max(0, min(10, args.simplify))
    return {
        "mode": mode,
        "bg": args.bg,
        "detail": detail,
        "simplify": simplify,
        "k": 3 + detail * 2,                         # colour regions
        "sr": 10 + (11 - detail) * 2.5,              # mean-shift colour radius
        "edge_hi": int(210 - detail * 14),           # canny high threshold
        "edge_min_len": max(8, int(60 - detail * 5)),  # drop short edge bits
        "min_area": int(simplify ** 2 * 25),         # merge regions below this
        "mode_sigma": 1.0 + simplify * 0.35,         # label-map smoothing
        "k_coarse": max(3, min(8, 2 + detail // 2)),  # big shapes for the bold outlines
        "coarse_sigma": 2.0 + simplify * 0.5,
        "coarse_area": max(300, int(simplify ** 2 * 100)),
        # how far (work px) fine detail may sit from a bold outline; 10 = unlimited
        "band": None if args.interior >= 10 else 6 + args.interior * 10,
    }


def find_foreground(bgr):
    """Subject mask (255) when the picture has a plain backdrop around its edge, else None.

    Flood-fills from the border over pixels close to the border colour, so a black
    (or white / flat colour) background is removed but dark detail inside the subject isn't.
    """
    h, w = bgr.shape[:2]
    lab = cv2.cvtColor(cv2.GaussianBlur(bgr, (0, 0), 1.5), cv2.COLOR_BGR2LAB).astype(np.float32)
    edge = np.concatenate([lab[:3].reshape(-1, 3), lab[-3:].reshape(-1, 3),
                           lab[:, :3].reshape(-1, 3), lab[:, -3:].reshape(-1, 3)])
    bgc = np.median(edge, axis=0)
    if np.percentile(np.linalg.norm(edge - bgc, axis=1), 90) > 8:
        return None                                   # busy edge: not a plain backdrop
    near = (np.linalg.norm(lab - bgc, axis=2) < 9).astype(np.uint8)
    n, cc, stats, _ = cv2.connectedComponentsWithStats(near, connectivity=4)
    bg = np.zeros(n, bool)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        touches = x == 0 or y == 0 or x + bw == w or y + bh == h
        if touches or area > 0.015 * h * w:           # edge-connected, or a big enclosed gap
            bg[i] = True
    fg = (~bg[cc]).astype(np.uint8) * 255
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    fg = drop_small_components(fg, int(0.002 * h * w))     # stray specks outside the subject
    fg = 255 - drop_small_components(255 - fg, int(0.0005 * h * w))  # pin-holes inside it
    if fg.mean() < 255 * 0.03 or fg.mean() > 255 * 0.97:
        return None
    return fg


def kmeans_labels(lab, k, seed=7, sel=None):
    h, w = lab.shape[:2]
    flat = lab.reshape(-1, 3).astype(np.float32)
    rng = np.random.default_rng(seed)
    pool = np.flatnonzero(sel.reshape(-1)) if sel is not None and sel.any() else np.arange(len(flat))
    idx = rng.choice(pool, size=min(len(pool), 120_000), replace=False)
    cv2.setRNGSeed(seed)
    _, _, centers = cv2.kmeans(
        flat[idx], k, None,
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5),
        2, cv2.KMEANS_PP_CENTERS)
    best = np.full(len(flat), np.inf, np.float32)
    lbl = np.zeros(len(flat), np.uint8)
    for i, c in enumerate(centers):
        d = ((flat - c) ** 2).sum(1)
        m = d < best
        best[m] = d[m]
        lbl[m] = i
    return lbl.reshape(h, w)


def smooth_labels(lbl, k, sigma):
    """Mode filter: each pixel takes the label most common around it."""
    if sigma < 0.5:
        return lbl
    best = np.full(lbl.shape, -1.0, np.float32)
    res = np.zeros_like(lbl)
    for i in range(k):
        m = (lbl == i).astype(np.float32)
        if not m.any():
            continue
        s = cv2.GaussianBlur(m, (0, 0), sigma)
        upd = s > best
        best[upd] = s[upd]
        res[upd] = i
    return res


def merge_small_regions(lbl, min_area):
    """Give tiny connected regions the label of their most common neighbour."""
    if min_area <= 0:
        return lbl
    lbl = lbl.copy()
    kern = np.ones((3, 3), np.uint8)
    for _ in range(2):
        changed = False
        for v in np.unique(lbl):
            m = (lbl == v).astype(np.uint8)
            n, cc, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
            small = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] < min_area]
            for i in small[:4000]:
                x, y, w, h, _a = stats[i]
                x0, y0 = max(0, x - 2), max(0, y - 2)
                sub_cc = cc[y0:y + h + 2, x0:x + w + 2] == i
                ring = cv2.dilate(sub_cc.astype(np.uint8), kern, iterations=2).astype(bool) & ~sub_cc
                nb = lbl[y0:y + h + 2, x0:x + w + 2][ring]
                if nb.size:
                    lbl[y0:y + h + 2, x0:x + w + 2][sub_cc] = np.bincount(nb).argmax()
                    changed = True
        if not changed:
            break
    return lbl


def drop_small_components(binary, min_len):
    """Remove connected line bits whose pixel count is below min_len."""
    n, cc, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    keep = np.zeros(n, np.uint8)
    keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= min_len).astype(np.uint8)
    return keep[cc] * 255


def trace_lines(bgr, p, fg=None):
    """Return (coarse, fine) uint8 line maps, 255 on a line, at bgr's size."""
    graphic = p["mode"] == "graphic"
    if graphic:
        base = cv2.bilateralFilter(bgr, 5, 25, 5)
    else:
        base = cv2.pyrMeanShiftFiltering(bgr, 6, p["sr"], 1)
        base = cv2.GaussianBlur(base, (0, 0), 0.5 + p["simplify"] * 0.55)   # wipe out fur / grain
    lab = cv2.cvtColor(base, cv2.COLOR_BGR2LAB)

    k = p["k"] if not graphic else max(2, p["k"] // 2)
    lbl = kmeans_labels(lab, k, sel=fg)
    lbl = smooth_labels(lbl, k, 0.8 if graphic else p["mode_sigma"])
    if fg is not None:
        lbl[fg == 0] = k                                  # one flat 'background' label: no lines in it
    lbl = merge_small_regions(lbl, p["min_area"] // (4 if graphic else 1))

    # fine detail: region borders + internal edges
    fine = np.zeros(lbl.shape, np.uint8)
    fine[:, 1:] |= (lbl[:, 1:] != lbl[:, :-1]).astype(np.uint8)
    fine[1:, :] |= (lbl[1:, :] != lbl[:-1, :]).astype(np.uint8)
    fine *= 255
    gray = cv2.cvtColor(base if graphic else bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (0, 0), (1.2 + p["simplify"] * 0.4) if not graphic else 0.8)
    hi = p["edge_hi"] if not graphic else max(60, p["edge_hi"] - 40)
    edges = cv2.Canny(gray, hi * 0.45, hi)
    if fg is not None:
        edges[cv2.erode(fg, np.ones((7, 7), np.uint8)) == 0] = 0   # no edges in/near the backdrop
    fine = cv2.bitwise_or(fine, drop_small_components(edges, p["edge_min_len"]))

    # bold structure: borders of a few big, heavily simplified shapes
    kc = p["k_coarse"]
    lc = kmeans_labels(lab, kc, sel=fg)
    lc = smooth_labels(lc, kc, p["coarse_sigma"])
    if fg is not None:
        lc[fg == 0] = kc
    lc = merge_small_regions(lc, p["coarse_area"])
    coarse = np.zeros(lc.shape, np.uint8)
    coarse[:, 1:] |= (lc[:, 1:] != lc[:, :-1]).astype(np.uint8)
    coarse[1:, :] |= (lc[1:, :] != lc[:-1, :]).astype(np.uint8)
    coarse = drop_small_components(coarse * 255, 30)

    # keep fine detail only near the bold outlines; the middle of shapes stays open
    if p["band"] is not None:
        dist = cv2.distanceTransform((coarse == 0).astype(np.uint8), cv2.DIST_L2, 3)
        fine = np.where(dist <= p["band"], fine, 0).astype(np.uint8)
    fine = drop_small_components(fine, max(6, p["edge_min_len"] // 2))
    return coarse, fine


def upscale_lines(lines, size, thick):
    big = cv2.resize(lines, size, interpolation=cv2.INTER_LINEAR)
    big = (big > 60).astype(np.uint8) * 255
    if thick > 1:
        big = cv2.dilate(big, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (thick, thick)))
    return big


def lineart_page(base_bgr, dpi, p, weight):
    """Keep the drawing's own ink strokes: blur away hatching/texture, threshold, clean up."""
    px = dpi / 200.0
    g = cv2.cvtColor(base_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    bg = cv2.GaussianBlur(g, (0, 0), 40 * px) + 1.0
    g = np.clip(g / bg * 235, 0, 255)                       # even out paper tone / shadows
    g = cv2.GaussianBlur(g, (0, 0), (0.6 + p["simplify"] * 0.55) * px)   # hatching -> light gray
    thr = 70 + p["detail"] * 12                              # more detail keeps lighter strokes
    ink = (g < thr).astype(np.uint8) * 255
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    ink = drop_small_components(ink, max(8, int(p["simplify"] ** 2 * 25 * px * px)))
    holes = drop_small_components(255 - ink, max(8, int(p["simplify"] ** 2 * 12 * px * px)))
    ink = 255 - holes                                        # fill pin-holes inside strokes
    t = max(1, int(round((weight - 2) * dpi / 100.0)))       # weight 2 = keep original stroke width
    if t > 1:
        ink = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (t, t)))
    ink = cv2.GaussianBlur(ink, (0, 0), 0.8)
    ink = np.clip((ink.astype(np.float32) - 40) * 255 / 175, 0, 255).astype(np.uint8)
    return 255 - ink


def trace_to_page(base_bgr, page_size, dpi, p, weight, inner):
    """Trace at working size, scale up, draw bold outlines + thinner detail (255 = paper)."""
    if p["mode"] == "lineart":
        return lineart_page(base_bgr, dpi, p, weight)
    pw, ph = page_size
    scale = min(1.0, WORK_LONG_SIDE / max(pw, ph))
    ww, wh = max(2, int(pw * scale)), max(2, int(ph * scale))
    small = cv2.resize(base_bgr, (ww, wh), interpolation=cv2.INTER_AREA)
    fg = find_foreground(small) if p["bg"] == "auto" else None
    coarse, fine = trace_lines(small, p, fg)

    px = dpi / 100.0
    big = cv2.bitwise_or(upscale_lines(coarse, (pw, ph), max(1, int(round(weight * px)))),
                         upscale_lines(fine, (pw, ph), max(1, int(round(inner * px)))))
    big = cv2.GaussianBlur(big, (0, 0), 0.8)          # soft edge for clean printing
    big = np.clip((big.astype(np.float32) - 40) * 255 / 175, 0, 255).astype(np.uint8)
    return 255 - big


def tone_page(base_bgr, style):
    """No lines: just remove colour (gray) or reduce it to a few flat tones."""
    g = cv2.cvtColor(base_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(2.0, (8, 8)).apply(g)
    if style == "tones":
        g = cv2.GaussianBlur(g, (0, 0), 3)
        g = (np.round(g / 255.0 * 3) * 85).astype(np.uint8)   # 4 flat grays
    return g


# ------------------------------------------------------------ sub-commands

def cmd_ingest(a):
    os.makedirs(a.proj, exist_ok=True)
    try:
        im = Image.open(a.src)
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        im = im.convert("RGB")
    except Exception as e:  # not an image / corrupt
        fail("Could not read that image: %s" % e)
    if max(im.size) > MAX_SOURCE:
        im.thumbnail((MAX_SOURCE, MAX_SOURCE), Image.LANCZOS)
    im.save(os.path.join(a.proj, "source.png"), "PNG")
    save_meta(a.proj, {"src_w": im.size[0], "src_h": im.size[1]})
    out(w=im.size[0], h=im.size[1])


def crop_to_aspect(img, rect, aspect):
    """rect = x,y,w,h as fractions of the image; result is cropped to `aspect` (w/h)."""
    H, W = img.shape[:2]
    if rect:
        x, y, w, h = rect
        x0, y0 = int(round(x * W)), int(round(y * H))
        x1, y1 = int(round((x + w) * W)), int(round((y + h) * H))
        x0, y0 = max(0, min(W - 2, x0)), max(0, min(H - 2, y0))
        x1, y1 = max(x0 + 2, min(W, x1)), max(y0 + 2, min(H, y1))
        img = img[y0:y1, x0:x1]
        H, W = img.shape[:2]
    cur = W / H
    if cur > aspect:       # too wide
        nw = int(round(H * aspect))
        x0 = (W - nw) // 2
        img = img[:, x0:x0 + nw]
    elif cur < aspect:     # too tall
        nh = int(round(W / aspect))
        y0 = (H - nh) // 2
        img = img[y0:y0 + nh, :]
    return img


def cmd_generate(a):
    src = cv2.imread(os.path.join(a.proj, "source.png"), cv2.IMREAD_COLOR)
    if src is None:
        fail("Source image missing; upload again.")
    iw, ih = PAGES[a.orient]
    dpi = a.dpi
    page_size = (int(round(iw * dpi)), int(round(ih * dpi)))
    rect = [float(v) for v in a.rect.split(",")] if a.rect else None
    crop = crop_to_aspect(src, rect, iw / ih)
    interp = cv2.INTER_AREA if crop.shape[1] > page_size[0] else cv2.INTER_CUBIC
    base = cv2.resize(crop, page_size, interpolation=interp)
    save_png(base, os.path.join(a.proj, "base.png"))

    p = params_from(a, a.mode)
    if a.style == "outline":
        page = trace_to_page(base, page_size, dpi, p, a.weight, a.inner)
    else:
        page = tone_page(base, a.style)
    save_png(page, a.out, dpi)
    meta = load_meta(a.proj)
    meta.update(w=page_size[0], h=page_size[1], dpi=dpi, orient=a.orient)
    save_meta(a.proj, meta)
    ink = float((page < 128).mean())
    out(w=page_size[0], h=page_size[1], ink=round(ink, 4))


def load_base_and_page(a):
    meta = load_meta(a.proj)
    size = (meta["w"], meta["h"])
    base = cv2.imread(os.path.join(a.proj, "base.png"), cv2.IMREAD_COLOR)
    if base is None:
        fail("Generate a page first.")
    page = read_page(a.page, size)
    mask = read_mask(a.mask, size)
    if not mask.any():
        fail("Paint or select an area first.")
    return meta, size, base, page, mask


def blend(page, new, mask, grow=3):
    """Replace page with new inside the mask, feathering the seam slightly."""
    m = cv2.dilate(mask, np.ones((grow, grow), np.uint8)) if grow else mask
    f = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), 1.5)
    res = page.astype(np.float32) * (1 - f) + new.astype(np.float32) * f
    return np.clip(res, 0, 255).astype(np.uint8)


def cmd_retrace(a):
    meta, size, base, page, mask = load_base_and_page(a)
    p = params_from(a, a.mode)
    # trace only a padded box around the mask so retracing a small spot is fast
    ys, xs = np.where(mask > 0)
    pad = 40
    x0, x1 = max(0, xs.min() - pad), min(size[0], xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(size[1], ys.max() + pad)
    crop = base[y0:y1, x0:x1]
    sub = trace_to_page(crop, (x1 - x0, y1 - y0), meta["dpi"], p, a.weight, a.inner) \
        if crop.shape[0] > 8 and crop.shape[1] > 8 else None
    if sub is None:
        fail("Selected area is too small.")
    new = np.full(page.shape, 255, np.uint8)
    new[y0:y1, x0:x1] = sub
    save_png(blend(page, new, mask), a.out, meta["dpi"])
    out(ok=True)


def cmd_cleanup(a):
    meta, size, base, page, mask = load_base_and_page(a)
    ink = (page < 128).astype(np.uint8) * 255
    min_px = max(1, int(a.simplify ** 2 * 12))
    kept = drop_small_components(ink, min_px)
    ink = np.where(mask > 0, kept, ink)
    new = 255 - cv2.GaussianBlur(ink, (0, 0), 0.8)
    new = np.where(mask > 0, new, page).astype(np.uint8)
    save_png(new, a.out, meta["dpi"])
    out(ok=True)


# ---------------------------------------------------------------- AI edit

AI_SIZES = {"portrait": (1024, 1536), "landscape": (1536, 1024), "square": (1024, 1024)}
AI_PROMPT = (
    "Redraw the transparent area of this image as a clean coloring book page: "
    "pure black outlines on pure white, medium-weight uniform lines, closed shapes "
    "that are easy to color in, no shading, no gray, no filled black areas, "
    "no text. Match the line style of the surrounding drawing. "
)


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode())
    for k, (name, data, ctype) in files.items():
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: %s\r\n\r\n" % (boundary, k, name, ctype)).encode())
        body.write(data)
        body.write(b"\r\n")
    body.write(("--%s--\r\n" % boundary).encode())
    return body.getvalue(), "multipart/form-data; boundary=" + boundary


def get_config():
    cfg = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
    cfg["openai_api_key"] = os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key", "")
    cfg["openai_base"] = os.environ.get("CTW_OPENAI_BASE") or cfg.get("openai_base", "https://api.openai.com/v1")
    cfg["openai_model"] = cfg.get("openai_model", "gpt-image-1")
    return cfg


def png_bytes(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def cmd_ai(a):
    cfg = get_config()
    if not cfg["openai_api_key"]:
        fail("AI is not set up. Put your key in config.json (see config.sample.json) or set OPENAI_API_KEY.")
    meta, size, base, page, mask = load_base_and_page(a)
    W, H = size
    aw, ah = AI_SIZES[meta["orient"]]

    # context image: line art outside the mask, original photo inside it
    ctx = cv2.cvtColor(page, cv2.COLOR_GRAY2BGR)
    sel = mask > 0
    ctx[sel] = base[sel]
    ctx_im = Image.fromarray(cv2.cvtColor(ctx, cv2.COLOR_BGR2RGB)).resize((aw, ah), Image.LANCZOS)

    # fit into the model's size; transparent = the area to edit
    rgba = ctx_im.convert("RGBA")
    m_small = cv2.resize(mask, (aw, ah), interpolation=cv2.INTER_NEAREST)
    m_small = cv2.dilate(m_small, np.ones((9, 9), np.uint8))
    alpha = np.where(m_small > 0, 0, 255).astype(np.uint8)
    rgba.putalpha(Image.fromarray(alpha))

    prompt = AI_PROMPT + (a.prompt or "")
    body, ctype = multipart(
        {"model": cfg["openai_model"], "prompt": prompt, "size": "%dx%d" % (aw, ah), "n": "1"},
        {"image": ("image.png", png_bytes(rgba), "image/png"),
         "mask": ("mask.png", png_bytes(rgba), "image/png")})
    req = urllib.request.Request(cfg["openai_base"].rstrip("/") + "/images/edits", data=body, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("Authorization", "Bearer " + cfg["openai_api_key"])
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"]["message"]
        except Exception:
            msg = "HTTP %d" % e.code
        fail("AI request failed: " + msg)
    except Exception as e:
        fail("AI request failed: %s" % e)

    item = data["data"][0]
    if "b64_json" in item:
        raw = base64.b64decode(item["b64_json"])
    else:
        raw = urllib.request.urlopen(item["url"], timeout=60).read()
    res = Image.open(io.BytesIO(raw)).convert("L").resize((W, H), Image.LANCZOS)
    g = np.array(res)
    # force strict black & white, then soften like the rest of the page
    bw = (g < 140).astype(np.uint8) * 255
    new = 255 - cv2.GaussianBlur(bw, (0, 0), 0.8)
    save_png(blend(page, new, mask), a.out, meta["dpi"])
    out(ok=True)


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, gen=False):
        p.add_argument("--proj", required=True)
        p.add_argument("--out")
        p.add_argument("--mode", default="photo", choices=["photo", "graphic", "lineart"])
        p.add_argument("--detail", type=int, default=5)
        p.add_argument("--weight", type=int, default=5)
        p.add_argument("--inner", type=int, default=2)
        p.add_argument("--interior", type=int, default=3)
        p.add_argument("--bg", default="auto", choices=["auto", "keep"])
        p.add_argument("--style", default="outline", choices=["outline", "gray", "tones"])
        p.add_argument("--simplify", type=int, default=4)

    p = sub.add_parser("ingest"); p.add_argument("--proj", required=True); p.add_argument("--src", required=True)
    p = sub.add_parser("generate"); common(p)
    p.add_argument("--orient", default="portrait", choices=list(PAGES))
    p.add_argument("--dpi", type=int, default=200, choices=[150, 200, 300])
    p.add_argument("--rect", default="")
    for name in ("retrace", "cleanup", "ai"):
        p = sub.add_parser(name); common(p)
        p.add_argument("--page", required=True)
        p.add_argument("--mask", required=True)
        p.add_argument("--prompt", default="")
    a = ap.parse_args()
    try:
        {"ingest": cmd_ingest, "generate": cmd_generate, "retrace": cmd_retrace,
         "cleanup": cmd_cleanup, "ai": cmd_ai}[a.cmd](a)
    except SystemExit:
        raise
    except Exception as e:
        fail("%s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
