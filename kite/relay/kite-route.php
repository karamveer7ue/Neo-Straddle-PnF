<?php
/*
 * kite-route.php - relays Kite Connect order requests from the P&F app, so that
 * Kite sees them coming from this server's IP (register that IP as the static IP
 * in the Kite developer console; Kite checks it on order endpoints only).
 *
 * Upload this file to your web host (e.g. public_html/kite-route.php, served at
 * https://your-domain.example/kite-route.php) and set RELAY_SECRET below to the same value as
 * KITE_RELAY_SECRET in the app's .env (or run `.venv/bin/python -m kite.order_relay php`,
 * which writes a copy with the secret filled in). See kite/README.md.
 *
 * What it accepts, and nothing else:
 *   POST|PUT|DELETE ?path=/orders/<variety>[/<order_id>]   place, modify, cancel an order
 *   GET ?path=whoami                                       this server's outgoing IPs
 * Every request must be signed by the app with the shared secret:
 *   X-Relay-Timestamp: unix seconds (accepted within 60 s of this server's clock)
 *   X-Relay-Signature: hex HMAC-SHA256 of
 *     "<timestamp>\n<METHOD>\n<path>\n<authorization>\n<content-type>\n<x-kite-version>\n<body>"
 * so nobody else can send orders from your IP, nothing that is forwarded can be
 * changed, and a captured request cannot be sent again: a signature is accepted
 * once (the hashes of recent ones are kept in SEEN_FILE, outside the web root).
 * The Kite access token travels in the request (Authorization) and is never
 * stored or logged here.
 *
 * Kite's reply (status code and body) is passed back unchanged. The relay's own
 * errors use Kite's error format, so the app handles them like Kite's:
 *   403 PermissionException - refused here; nothing was sent to Kite
 *   502 NetworkException    - Kite could not be reached or did not answer; the
 *                             order may or may not have been placed
 */

declare(strict_types=1);

// A PHP notice or deprecation printed into a reply would corrupt Kite's JSON: log only.
ini_set('display_errors', '0');
ini_set('log_errors', '1');

const RELAY_SECRET = 'PASTE-KITE_RELAY_SECRET-FROM-.env-HERE';
const KITE_API = 'https://api.kite.trade';
const MAX_CLOCK_SKEW = 60;  // seconds
// The IP version used to reach Kite: 4 or 6. A host with both (Hostinger has both) would
// otherwise pick one by itself, and Kite refuses orders from the address not registered.
// `python -m kite.order_relay check` shows the address of this version: register that one.
const KITE_IP_VERSION = 4;
const ORDER_PATH = '#^/orders/(regular|amo|co|iceberg|auction)(/[A-Za-z0-9]+)?$#';
// Hashes of the signatures already used, so a captured order request cannot be sent twice.
// Kept next to the web root's folder (not served); falls back to the system temp folder.
const SEEN_FILE = '.kite-route-seen.json';

function reply(int $status, string $body, string $contentType = 'application/json'): void
{
    http_response_code($status);
    header('Content-Type: ' . $contentType);
    header('Cache-Control: no-store');
    echo $body;
    exit;
}

function fail(int $status, string $errorType, string $message): void
{
    reply($status, json_encode([
        'status' => 'error',
        'error_type' => $errorType,
        'message' => 'Order relay: ' . $message,
        'data' => null,
    ]));
}

/** Request headers, lower-cased (works on Apache, LiteSpeed and PHP-FPM alike). */
function request_headers(): array
{
    $headers = [];
    foreach ($_SERVER as $key => $value) {
        if (strncmp($key, 'HTTP_', 5) === 0) {
            $headers[strtolower(str_replace('_', '-', substr($key, 5)))] = (string) $value;
        }
    }
    foreach (['CONTENT_TYPE' => 'content-type', 'CONTENT_LENGTH' => 'content-length'] as $key => $name) {
        if (isset($_SERVER[$key])) {
            $headers[$name] = (string) $_SERVER[$key];
        }
    }
    if (!isset($headers['authorization']) && isset($_SERVER['REDIRECT_HTTP_AUTHORIZATION'])) {
        $headers['authorization'] = (string) $_SERVER['REDIRECT_HTTP_AUTHORIZATION'];
    }
    return $headers;
}

/** Where the used signatures are kept: the first writable of the places below. */
function seen_file(): string
{
    $override = getenv('KITE_ROUTE_SEEN_FILE');  // the app's tests only
    if ($override) {
        return $override;
    }
    foreach ([dirname(__DIR__), sys_get_temp_dir()] as $dir) {
        if (is_dir($dir) && is_writable($dir)) {
            return $dir . '/' . SEEN_FILE;
        }
    }
    fail(403, 'PermissionException', 'nowhere to record used requests (make the folder above this file writable).');
}

/** True the first time a signature is seen within its validity window; false for a repeat. */
function first_use(string $signature): bool
{
    $fh = @fopen(seen_file(), 'c+');
    if ($fh === false || !flock($fh, LOCK_EX)) {
        fail(403, 'PermissionException', 'could not record this request (is the folder above this file writable?).');
    }
    $seen = json_decode((string) stream_get_contents($fh), true);
    $seen = is_array($seen) ? $seen : [];
    $now = time();
    // A signature is valid for at most 2 x MAX_CLOCK_SKEW (a timestamp up to that far ahead).
    $seen = array_filter($seen, fn ($t) => is_int($t) && $now - $t <= 2 * MAX_CLOCK_SKEW);
    $key = hash('sha256', $signature);
    $fresh = !isset($seen[$key]);
    if ($fresh) {
        $seen[$key] = $now;
        ftruncate($fh, 0);
        rewind($fh);
        fwrite($fh, (string) json_encode($seen));
        fflush($fh);
    }
    flock($fh, LOCK_UN);
    fclose($fh);
    return $fresh;
}

function ip_resolve(int $version): int
{
    return $version === 6 ? CURL_IPRESOLVE_V6 : CURL_IPRESOLVE_V4;
}

/** This server's outgoing address of one IP version ('' if it has none). */
function outgoing_ip(int $version): string
{
    $ch = curl_init($version === 6 ? 'https://api6.ipify.org' : 'https://api.ipify.org');
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 10,
        CURLOPT_IPRESOLVE => ip_resolve($version),
    ]);
    $ip = curl_exec($ch);
    $ok = $ip !== false && curl_getinfo($ch, CURLINFO_HTTP_CODE) === 200;
    return $ok ? trim((string) $ip) : '';
}

$secret = getenv('KITE_RELAY_SECRET') ?: RELAY_SECRET;
if (strlen($secret) < 32 || strpos($secret, 'PASTE-') === 0) {
    fail(500, 'GeneralException', 'not set up: put KITE_RELAY_SECRET from .env into RELAY_SECRET in kite-route.php.');
}

$method = strtoupper($_SERVER['REQUEST_METHOD'] ?? '');
$target = isset($_GET['path']) && is_string($_GET['path']) ? $_GET['path'] : '';
$body = (string) file_get_contents('php://input');
$headers = request_headers();
$auth = $headers['x-kite-authorization'] ?? ($headers['authorization'] ?? '');
$requestType = $headers['content-type'] ?? '';
$kiteVersion = $headers['x-kite-version'] ?? '';

// Every request is signed, including every header that is forwarded: check the clock
// first, then the signature.
$timestamp = $headers['x-relay-timestamp'] ?? '';
$signature = strtolower($headers['x-relay-signature'] ?? '');
if (!ctype_digit($timestamp) || abs(time() - (int) $timestamp) > MAX_CLOCK_SKEW) {
    fail(403, 'PermissionException', 'missing or stale timestamp (are both clocks right?).');
}
$signed = implode("\n", [$timestamp, $method, $target, $auth, $requestType, $kiteVersion, $body]);
$expected = hash_hmac('sha256', $signed, $secret);
if (!hash_equals($expected, $signature)) {
    fail(403, 'PermissionException', 'bad signature (does RELAY_SECRET match KITE_RELAY_SECRET in .env, '
        . 'and is this the kite-route.php written by this version of the app?).');
}

if ($method === 'GET' && $target === 'whoami') {
    $other = KITE_IP_VERSION === 6 ? 4 : 6;
    reply(200, json_encode(['status' => 'success', 'data' => [
        'ip' => outgoing_ip(KITE_IP_VERSION),  // the address Kite sees
        'version' => KITE_IP_VERSION,
        'otherIp' => outgoing_ip($other),  // not used for Kite
        'time' => time(),
    ]]));
}

if (!in_array($method, ['POST', 'PUT', 'DELETE'], true)) {
    fail(405, 'PermissionException', 'only order placement, modification and cancellation are relayed.');
}
$path = (string) parse_url($target, PHP_URL_PATH);
if (!preg_match(ORDER_PATH, $path) || strpos($target, '//') !== false) {
    fail(403, 'PermissionException', 'only Kite order endpoints are relayed.');
}

if (!preg_match('/^token [A-Za-z0-9]+:[A-Za-z0-9]+$/', $auth)) {
    fail(403, 'PermissionException', 'missing Kite authorization.');
}
if (!first_use($signature)) {
    fail(403, 'PermissionException', 'this request was already sent once; a repeat is refused.');
}

$upstream = getenv('KITE_ROUTE_UPSTREAM') ?: KITE_API;  // the env: the app's tests only, not in the upload copy
$forward = [
    'Authorization: ' . $auth,
    'X-Kite-Version: ' . ($kiteVersion !== '' ? $kiteVersion : '3'),
    'User-Agent: pnf-kite-route/1',
];
if ($requestType !== '') {
    $forward[] = 'Content-Type: ' . $requestType;
}

$contentType = 'application/json';
$ch = curl_init($upstream . $target);
curl_setopt_array($ch, [
    CURLOPT_CUSTOMREQUEST => $method,
    CURLOPT_HTTPHEADER => $forward,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_FOLLOWLOCATION => false,
    CURLOPT_CONNECTTIMEOUT => 5,
    CURLOPT_TIMEOUT => 10,
    CURLOPT_SSL_VERIFYPEER => true,
    CURLOPT_SSL_VERIFYHOST => 2,
    CURLOPT_IPRESOLVE => ip_resolve(KITE_IP_VERSION),
    CURLOPT_HEADERFUNCTION => function ($ch, string $line) use (&$contentType): int {
        if (stripos($line, 'content-type:') === 0) {
            $contentType = trim(substr($line, 13));
        }
        return strlen($line);
    },
]);
if ($method !== 'DELETE' || $body !== '') {
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
}
$response = curl_exec($ch);
$status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
$error = curl_error($ch);

if ($response === false || $status === 0) {
    // It may have reached Kite before failing: the app treats this as "outcome unknown".
    fail(502, 'NetworkException', 'no reply from Kite (' . $error . ').');
}
reply($status, (string) $response, $contentType);
