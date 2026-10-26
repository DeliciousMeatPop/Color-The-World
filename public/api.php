<?php
// Thin HTTP layer: validates input, calls engine/ctw.py, returns PNG or JSON.
$ROOT    = dirname(__DIR__);
$STORAGE = $ROOT . DIRECTORY_SEPARATOR . 'storage' . DIRECTORY_SEPARATOR . 'projects';
$ENGINE  = $ROOT . DIRECTORY_SEPARATOR . 'engine' . DIRECTORY_SEPARATOR . 'ctw.py';
const MAX_AGE = 7 * 86400;

function json_out($data, $code = 200) {
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode($data);
    exit;
}

function python_cmd() {
    global $ROOT;
    if (getenv('CTW_PYTHON')) return getenv('CTW_PYTHON');
    $win = PHP_OS_FAMILY === 'Windows';
    $venv = $ROOT . '/.venv/' . ($win ? 'Scripts/python.exe' : 'bin/python');
    if (is_file($venv)) return $venv;
    return $win ? 'python' : 'python3';
}

function run_engine(array $args) {
    global $ENGINE;
    $cmd = array_merge([python_cmd(), '-I', $ENGINE], $args);
    $p = proc_open($cmd, [1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (!is_resource($p)) json_out(['error' => 'Could not start Python.'], 500);
    $stdout = stream_get_contents($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    fclose($pipes[1]); fclose($pipes[2]);
    proc_close($p);
    $lines = array_filter(explode("\n", trim($stdout)));
    $res = json_decode(end($lines) ?: '', true);
    if (!is_array($res)) json_out(['error' => 'Engine error: ' . substr($stderr ?: $stdout, -400)], 500);
    if (isset($res['error'])) json_out(['error' => $res['error']], 400);
    return $res;
}

function project_dir() {
    global $STORAGE;
    $id = $_POST['id'] ?? $_GET['id'] ?? '';
    if (!preg_match('/^[a-f0-9]{16}$/', $id) || !is_dir("$STORAGE/$id")) json_out(['error' => 'Unknown project, upload again.'], 404);
    return "$STORAGE/$id";
}

function int_param($k, $def, $lo, $hi) {
    return (string) max($lo, min($hi, (int) ($_POST[$k] ?? $def)));
}

function trace_args() {
    $mode = ($_POST['mode'] ?? 'photo') === 'graphic' ? 'graphic' : 'photo';
    return ['--mode', $mode,
        '--detail', int_param('detail', 5, 1, 10),
        '--weight', int_param('weight', 3, 1, 10),
        '--simplify', int_param('simplify', 4, 0, 10)];
}

function save_upload($field, $dest) {
    if (!isset($_FILES[$field]) || $_FILES[$field]['error'] !== UPLOAD_ERR_OK) json_out(['error' => "Missing or failed upload: $field"], 400);
    if (!move_uploaded_file($_FILES[$field]['tmp_name'], $dest)) json_out(['error' => 'Could not store upload.'], 500);
}

function send_png($path) {
    header('Content-Type: image/png');
    header('Cache-Control: no-store');
    readfile($path);
    exit;
}

function cleanup_old() {
    global $STORAGE;
    foreach (glob("$STORAGE/*", GLOB_ONLYDIR) ?: [] as $d) {
        if (filemtime($d) < time() - MAX_AGE) {
            foreach (glob("$d/*") ?: [] as $f) @unlink($f);
            @rmdir($d);
        }
    }
}

$action = $_POST['action'] ?? $_GET['action'] ?? '';

if ($action === 'file') {                       // serve source/base for the browser
    $dir = project_dir();
    $name = $_GET['name'] ?? '';
    if (!in_array($name, ['source', 'base'], true) || !is_file("$dir/$name.png")) json_out(['error' => 'Not found'], 404);
    send_png("$dir/$name.png");
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') json_out(['error' => 'POST required'], 405);

if ($action === 'upload') {
    if (!is_dir($STORAGE)) mkdir($STORAGE, 0775, true);
    cleanup_old();
    $id = bin2hex(random_bytes(8));
    $dir = "$STORAGE/$id";
    mkdir($dir, 0775, true);
    save_upload('image', "$dir/upload.bin");
    $res = run_engine(['ingest', '--proj', $dir, '--src', "$dir/upload.bin"]);
    @unlink("$dir/upload.bin");
    json_out(['id' => $id] + $res);
}

$dir = project_dir();
$out = "$dir/page.png";

switch ($action) {
    case 'generate':
        $orient = in_array($_POST['orient'] ?? '', ['portrait', 'landscape', 'square'], true) ? $_POST['orient'] : 'portrait';
        $dpi = in_array((int) ($_POST['dpi'] ?? 200), [150, 200, 300], true) ? (string) (int) $_POST['dpi'] : '200';
        $rect = '';
        if (!empty($_POST['rect']) && preg_match('/^[0-9.]+(,[0-9.]+){3}$/', $_POST['rect'])) $rect = $_POST['rect'];
        $args = array_merge(['generate', '--proj', $dir, '--out', $out, '--orient', $orient, '--dpi', $dpi], trace_args());
        if ($rect !== '') array_push($args, '--rect', $rect);
        run_engine($args);
        send_png($out);

    case 'retrace': case 'cleanup': case 'ai':
        save_upload('page', "$dir/in_page.png");
        save_upload('mask', "$dir/in_mask.png");
        $args = array_merge([$action, '--proj', $dir, '--out', $out, '--page', "$dir/in_page.png", '--mask', "$dir/in_mask.png"], trace_args());
        if ($action === 'ai') array_push($args, '--prompt', substr((string) ($_POST['prompt'] ?? ''), 0, 500));
        run_engine($args);
        send_png($out);

    default:
        json_out(['error' => 'Unknown action'], 400);
}
