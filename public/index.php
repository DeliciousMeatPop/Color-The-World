<?php
$aiReady = getenv('OPENAI_API_KEY') || (is_file(__DIR__ . '/../config.json') && strpos(file_get_contents(__DIR__ . '/../config.json'), '"openai_api_key": ""') === false);
?><!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Color The World</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header><h1>Color The World</h1><span class="sub">photos &amp; graphics &rarr; coloring book pages</span></header>
<main>
<aside id="panel">
  <section>
    <h2>1. Picture</h2>
    <label class="drop" id="drop">Click, drop, or paste an image<input type="file" id="file" accept="image/*" hidden></label>
  </section>

  <section>
    <h2>2. Page</h2>
    <label>Shape
      <select id="orient"><option value="portrait">Portrait 8.5×11</option><option value="landscape">Landscape 11×8.5</option><option value="square">Square 8.5×8.5</option></select></label>
    <label>Resolution
      <select id="dpi"><option value="150">Draft 150 dpi</option><option value="200" selected>Standard 200 dpi</option><option value="300">Print 300 dpi</option></select></label>
    <label>Type
      <select id="mode"><option value="photo">Real photo (heavy cleanup)</option><option value="graphic">Graphic / drawing</option></select></label>
    <label>Detail <output id="o_detail">5</output><input type="range" id="detail" min="1" max="10" value="5"></label>
    <label>Simplify <output id="o_simplify">4</output><input type="range" id="simplify" min="0" max="10" value="4"></label>
    <label>Line weight <output id="o_weight">3</output><input type="range" id="weight" min="1" max="10" value="3"></label>
    <button id="generate" class="primary" disabled>Make coloring page</button>
    <p class="hint" id="cropHint">Drag the box on the picture to choose just the part you want.</p>
  </section>

  <section id="editSec" hidden>
    <h2>3. Edit</h2>
    <div class="tools" id="tools">
      <button data-tool="area" class="on" title="Paint the area you want to change">Select area</button>
      <button data-tool="unarea" title="Remove from selection">Deselect</button>
      <button data-tool="pen" title="Draw black lines">Pen</button>
      <button data-tool="eraser" title="Erase lines">Eraser</button>
    </div>
    <label>Brush size <output id="o_brush">30</output><input type="range" id="brush" min="3" max="150" value="30"></label>
    <div class="row">
      <button id="selAll">Select all</button><button id="selNone">Clear selection</button>
    </div>
    <p class="hint">Selected area (red) actions. Detail / Simplify / Line weight sliders above apply to Re-trace.</p>
    <button id="retrace">Re-trace area</button>
    <button id="cleanup">Remove specks in area</button>
    <button id="blank">Blank out area</button>
    <input type="text" id="prompt" placeholder="Optional: what should be drawn here?" maxlength="300">
    <button id="ai" <?= $aiReady ? '' : 'title="Add your key to config.json to enable"' ?>>AI redo area<?= $aiReady ? '' : ' (no key)' ?></button>
    <div class="row"><button id="undo">Undo</button><button id="redo">Redo</button></div>
    <div class="row"><button id="save" class="primary">Download PNG</button><button id="print">Print</button></div>
    <button id="back">Change crop / start over with settings</button>
  </section>
  <p id="status" role="status"></p>
</aside>
<div id="stage"><div id="wrap">
  <canvas id="view"></canvas><canvas id="overlay"></canvas>
</div></div>
</main>
<script src="app.js"></script>
</body>
</html>
