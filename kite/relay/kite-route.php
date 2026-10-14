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
 *   GET ?path=whoami                                       this server's outgoing IP
 * Every request must be signed by the app with the shared secret:
 *   X-Relay-Timestamp: unix seconds (accepted within 60 s of this server's clock)
 *   X-Relay-Signature: hex HMAC-SHA256 of "<timestamp>\n<METHOD>\n<path>\n<body>"
 * so nobody else can send orders from your IP, and a captured request cannot be
 * replayed later to place the same order again. The Kite access token travels in
 * the request (Authorization) and is never stored or logged here.
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
const ORDER_PATH = '#^/orders/(regular|amo|co|iceberg|auction)(/[A-Za-z0-9]+)?$#';

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

function outgoing_ip(): string
{
    $ch = curl_init('https://api.ipify.org');
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 10]);
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

// Every request is signed: check the clock first, then the signature.
$timestamp = $headers['x-relay-timestamp'] ?? '';
$signature = strtolower($headers['x-relay-signature'] ?? '');
if (!ctype_digit($timestamp) || abs(time() - (int) $timestamp) > MAX_CLOCK_SKEW) {
    fail(403, 'PermissionException', 'missing or stale timestamp (are both clocks right?).');
}
$expected = hash_hmac('sha256', $timestamp . "\n" . $method . "\n" . $target . "\n" . $body, $secret);
if (!hash_equals($expected, $signature)) {
    fail(403, 'PermissionException', 'bad signature (does RELAY_SECRET match KITE_RELAY_SECRET in .env?).');
}

if ($method === 'GET' && $target === 'whoami') {
    reply(200, json_encode(['status' => 'success', 'data' => ['ip' => outgoing_ip(), 'time' => time()]]));
}

if (!in_array($method, ['POST', 'PUT', 'DELETE'], true)) {
    fail(405, 'PermissionException', 'only order placement, modification and cancellation are relayed.');
}
$path = (string) parse_url($target, PHP_URL_PATH);
if (!preg_match(ORDER_PATH, $path) || strpos($target, '//') !== false) {
    fail(403, 'PermissionException', 'only Kite order endpoints are relayed.');
}

$auth = $headers['x-kite-authorization'] ?? ($headers['authorization'] ?? '');
if (!preg_match('/^token [A-Za-z0-9]+:[A-Za-z0-9]+$/', $auth)) {
    fail(403, 'PermissionException', 'missing Kite authorization.');
}

$upstream = getenv('KITE_ROUTE_UPSTREAM') ?: KITE_API;  // overridden only by the app's tests
$forward = [
    'Authorization: ' . $auth,
    'X-Kite-Version: ' . ($headers['x-kite-version'] ?? '3'),
    'User-Agent: pnf-kite-route/1',
];
if (isset($headers['content-type'])) {
    $forward[] = 'Content-Type: ' . $headers['content-type'];
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
