'use strict';
const $ = id => document.getElementById(id);
const view = $('view'), ov = $('overlay'), vctx = view.getContext('2d'), octx = ov.getContext('2d');
const mask = document.createElement('canvas'), mctx = mask.getContext('2d');   // full-res selection (alpha)
let state = 'empty';           // empty | crop | page
let projId = null, srcImg = null, crop = null, tool = 'area';
let undo = [], redo = [], cur = null;   // PNG blobs of page states

const status = (m, err) => { $('status').textContent = m || ''; $('status').className = err ? 'err' : ''; };
const ASPECT = () => ({portrait: 8.5 / 11, landscape: 11 / 8.5, square: 1})[$('orient').value];

for (const k of ['detail', 'simplify', 'interior', 'weight', 'inner', 'brush'])
  $(k).addEventListener('input', () => $('o_' + k).textContent = $(k).value);

async function api(action, fields, files) {
  const fd = new FormData();
  fd.append('action', action);
  if (projId) fd.append('id', projId);
  for (const [k, v] of Object.entries(fields || {})) fd.append(k, v);
  for (const [k, v] of Object.entries(files || {})) fd.append(k, v, k + '.png');
  const r = await fetch('api.php', {method: 'POST', body: fd});
  if ((r.headers.get('content-type') || '').includes('image/png')) return r.blob();
  const j = await r.json().catch(() => ({error: 'Server error'}));
  if (j.error) throw new Error(j.error);
  return j;
}
const blobToImg = b => new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = URL.createObjectURL(b); });
const canvasBlob = c => new Promise(r => c.toBlob(r, 'image/png'));
const busy = (on, msg) => { document.querySelectorAll('#panel button').forEach(b => b.disabled = on || (b.id === 'generate' && !srcImg)); status(on ? msg : ''); };

// ---------- loading a picture
async function loadFile(f) {
  if (!f || !f.type.startsWith('image/')) return status('That is not an image.', true);
  busy(true, 'Uploading…');
  try {
    const j = await api('upload', {}, {image: f});
    projId = j.id;
    const r = await fetch('api.php?action=file&name=source&id=' + projId);
    srcImg = await blobToImg(await r.blob());
    showCrop();
  } catch (e) { status(e.message, true); }
  busy(false);
}
$('drop').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', e => loadFile(e.target.files[0]));
$('drop').addEventListener('dragover', e => { e.preventDefault(); $('drop').classList.add('over'); });
$('drop').addEventListener('dragleave', () => $('drop').classList.remove('over'));
$('drop').addEventListener('drop', e => { e.preventDefault(); $('drop').classList.remove('over'); loadFile(e.dataTransfer.files[0]); });
addEventListener('paste', e => { for (const it of e.clipboardData.items) if (it.type.startsWith('image/')) loadFile(it.getAsFile()); });

// ---------- crop step: aspect-locked rectangle on the source
function sizeCanvases(w, h) { for (const c of [view, ov]) { c.width = w; c.height = h; } }
function defaultCrop() {
  const W = srcImg.width, H = srcImg.height, a = ASPECT();
  let w = Math.min(W, H * a), h = w / a;
  crop = {x: (W - w) / 2, y: (H - h) / 2, w, h};
}
function showCrop() {
  state = 'crop';
  $('editSec').hidden = true; $('cropHint').hidden = false; $('generate').disabled = false;
  sizeCanvases(srcImg.width, srcImg.height);
  defaultCrop(); drawCrop();
}
function drawCrop() {
  vctx.drawImage(srcImg, 0, 0);
  octx.clearRect(0, 0, ov.width, ov.height);
  octx.fillStyle = 'rgba(0,0,0,.55)';
  octx.fillRect(0, 0, ov.width, ov.height);
  octx.clearRect(crop.x, crop.y, crop.w, crop.h);
  const lw = Math.max(2, ov.width / 300);
  octx.strokeStyle = '#fff'; octx.lineWidth = lw; octx.strokeRect(crop.x, crop.y, crop.w, crop.h);
  octx.fillStyle = '#2a7de1';
  const hs = lw * 5; octx.fillRect(crop.x + crop.w - hs, crop.y + crop.h - hs, hs * 2, hs * 2);
}
$('orient').addEventListener('change', () => { if (state === 'crop') { defaultCrop(); drawCrop(); } });

function pos(e) {
  const r = ov.getBoundingClientRect();
  return {x: (e.clientX - r.left) * ov.width / r.width, y: (e.clientY - r.top) * ov.height / r.height};
}
let drag = null, last = null;
ov.addEventListener('pointerdown', e => {
  if (state === 'empty') return;
  ov.setPointerCapture(e.pointerId);
  const p = pos(e);
  if (state === 'crop') {
    const hs = ov.width / 300 * 10;
    if (Math.abs(p.x - (crop.x + crop.w)) < hs * 2 && Math.abs(p.y - (crop.y + crop.h)) < hs * 2) drag = {t: 'size'};
    else if (p.x > crop.x && p.x < crop.x + crop.w && p.y > crop.y && p.y < crop.y + crop.h) drag = {t: 'move', dx: p.x - crop.x, dy: p.y - crop.y};
    return;
  }
  drag = {t: tool}; last = p; paint(p, p);
});
ov.addEventListener('pointermove', e => {
  if (!drag) return;
  const p = pos(e);
  if (state === 'crop') {
    const W = srcImg.width, H = srcImg.height, a = ASPECT();
    if (drag.t === 'move') {
      crop.x = Math.max(0, Math.min(W - crop.w, p.x - drag.dx));
      crop.y = Math.max(0, Math.min(H - crop.h, p.y - drag.dy));
    } else {
      let w = Math.max(40, p.x - crop.x);
      w = Math.min(w, W - crop.x, (H - crop.y) * a);
      crop.w = w; crop.h = w / a;
    }
    drawCrop();
  } else { paint(last, p); last = p; }
});
const endDrag = async () => {
  if (!drag) return;
  const t = drag.t; drag = null;
  if (state === 'page' && (t === 'pen' || t === 'eraser')) await commit();
};
ov.addEventListener('pointerup', endDrag); ov.addEventListener('pointercancel', endDrag);

// ---------- page editing
function brushPx() { return $('brush').value * view.width / 1700; }   // brush scales with page size
function stroke(ctx, a, b, color, size, comp) {
  ctx.save();
  ctx.globalCompositeOperation = comp || 'source-over';
  ctx.strokeStyle = color; ctx.lineWidth = size; ctx.lineCap = ctx.lineJoin = 'round';
  ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x + .01, b.y + .01); ctx.stroke(); ctx.restore();
}
function paint(a, b) {
  const s = brushPx();
  if (tool === 'pen') stroke(vctx, a, b, '#000', s * .25);
  else if (tool === 'eraser') stroke(vctx, a, b, '#fff', s);
  else if (tool === 'area') { stroke(mctx, a, b, '#f00', s); drawMask(); }
  else if (tool === 'unarea') { stroke(mctx, a, b, '#000', s, 'destination-out'); drawMask(); }
}
function drawMask() {
  octx.clearRect(0, 0, ov.width, ov.height);
  octx.globalAlpha = .4; octx.drawImage(mask, 0, 0); octx.globalAlpha = 1;
}
$('tools').addEventListener('click', e => {
  const t = e.target.dataset.tool; if (!t) return;
  tool = t; document.querySelectorAll('#tools button').forEach(b => b.classList.toggle('on', b === e.target));
});
$('selAll').onclick = () => { mctx.fillStyle = '#f00'; mctx.fillRect(0, 0, mask.width, mask.height); drawMask(); };
$('selNone').onclick = () => { mctx.clearRect(0, 0, mask.width, mask.height); drawMask(); };

async function setPage(blob, keepUndo) {
  const img = await blobToImg(blob);
  if (state !== 'page' || view.width !== img.width) {
    sizeCanvases(img.width, img.height); mask.width = img.width; mask.height = img.height;
    undo = []; redo = [];
  }
  vctx.fillStyle = '#fff'; vctx.fillRect(0, 0, view.width, view.height); vctx.drawImage(img, 0, 0);
  state = 'page'; cur = blob;
  $('editSec').hidden = false; $('cropHint').hidden = true;
  mctx.clearRect(0, 0, mask.width, mask.height); drawMask();
  updUndo();
}
async function commit() {                     // call after a change drawn on `view`
  undo.push(cur); if (undo.length > 20) undo.shift();
  redo = []; cur = await canvasBlob(view); updUndo();
}
function updUndo() { $('undo').disabled = !undo.length; $('redo').disabled = !redo.length; }
async function restore(blob) {
  const img = await blobToImg(blob);
  vctx.fillStyle = '#fff'; vctx.fillRect(0, 0, view.width, view.height); vctx.drawImage(img, 0, 0); cur = blob;
}
$('undo').onclick = async () => { if (!undo.length) return; redo.push(cur); await restore(undo.pop()); updUndo(); };
$('redo').onclick = async () => { if (!redo.length) return; undo.push(cur); await restore(redo.pop()); updUndo(); };
addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'z' && state === 'page') { e.preventDefault(); (e.shiftKey ? $('redo') : $('undo')).click(); }
});

const settings = () => ({mode: $('mode').value, style: $('style').value, bg: $('bg').value, detail: $('detail').value, simplify: $('simplify').value,
  interior: $('interior').value, weight: $('weight').value, inner: $('inner').value});

$('generate').onclick = async () => {
  if (!srcImg) return;
  busy(true, 'Tracing… real photos can take several seconds');
  try {
    const f = {...settings(), orient: $('orient').value, dpi: $('dpi').value};
    if (state === 'crop') f.rect = [crop.x / srcImg.width, crop.y / srcImg.height, crop.w / srcImg.width, crop.h / srcImg.height].map(v => v.toFixed(5)).join(',');
    else f.rect = lastRect || '';
    lastRect = f.rect;
    const was = state;
    state = 'empty';                       // force fresh canvas size / history
    await setPage(await api('generate', f));
    if ($('style').value === 'outline' && inkFraction() < 0.002) status('Page came out nearly blank. Try more Detail, less Simplify, or more Interior detail.', true);
  } catch (e) { status(e.message, true); if (!cur) showCrop(); }
  busy(false);
};
let lastRect = '';
function inkFraction() {   // share of dark pixels, sampled
  const t = document.createElement('canvas'); t.width = 200; t.height = Math.round(200 * view.height / view.width);
  const c = t.getContext('2d'); c.drawImage(view, 0, 0, t.width, t.height);
  const d = c.getImageData(0, 0, t.width, t.height).data; let n = 0;
  for (let i = 0; i < d.length; i += 4) if (d[i] < 160) n++;
  return n / (t.width * t.height);
}

async function areaOp(action, extra, msg) {
  const m = await canvasBlob(mask);
  const ctxData = mctx.getImageData(0, 0, mask.width, mask.height).data;
  let any = false; for (let i = 3; i < ctxData.length; i += 4) if (ctxData[i]) { any = true; break; }
  if (!any) return status('Select an area first (red brush).', true);
  busy(true, msg);
  try {
    const out = await api(action, {...settings(), ...extra}, {page: await canvasBlob(view), mask: m});
    undo.push(cur); if (undo.length > 20) undo.shift(); redo = [];
    const keep = undo; await setPage(out); undo = keep; updUndo();
  } catch (e) { status(e.message, true); }
  busy(false);
}
$('retrace').onclick = () => areaOp('retrace', {}, 'Re-tracing area…');
$('cleanup').onclick = () => areaOp('cleanup', {}, 'Cleaning specks…');
$('ai').onclick = () => areaOp('ai', {prompt: $('prompt').value}, 'Asking the AI… (up to a minute)');
$('blank').onclick = async () => {
  vctx.save(); vctx.globalCompositeOperation = 'destination-out'; vctx.drawImage(mask, 0, 0); vctx.restore();
  // destination-out would make it transparent; paint white behind
  const t = document.createElement('canvas'); t.width = view.width; t.height = view.height;
  const c = t.getContext('2d'); c.fillStyle = '#fff'; c.fillRect(0, 0, t.width, t.height); c.drawImage(view, 0, 0);
  vctx.drawImage(t, 0, 0);
  await commit(); mctx.clearRect(0, 0, mask.width, mask.height); drawMask();
};
$('back').onclick = () => { if (srcImg) { cur = null; showCrop(); } };
$('save').onclick = () => { const a = document.createElement('a'); a.href = URL.createObjectURL(cur); a.download = 'coloring-page.png'; a.click(); };
$('print').onclick = () => print();
