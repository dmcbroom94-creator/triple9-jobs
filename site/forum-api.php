<?php
/* Triple 9 Careers community forum API.
   No accounts. Posts are stored in an SQLite file OUTSIDE the public web folder.
   For spam control only, a one-way hash of the poster's IP address (with a salt that changes every day)
   is kept for 24 hours and then deleted. Nothing else about the poster is stored. */
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Robots-Tag: noindex');

const MOD_HASH = '$2y$10$18NktdBGqAFYTPeROrpQw.AoC6B6Q8hWV.aiXFjAQE4jESlAp5roq';
const CATS = ['general','transfers','leaving','applications','pay','wellbeing','training','recruits'];
const MAX_TITLE = 120; const MAX_BODY = 3000; const MAX_NAME = 40;
const POST_GAP = 20; const POSTS_PER_DAY = 20; const REPORTS_TO_HIDE = 3;

function out($data, int $code = 200): void { http_response_code($code); echo json_encode($data, JSON_UNESCAPED_UNICODE); exit; }
function fail(string $msg, int $code = 400): void { out(['ok' => false, 'error' => $msg], $code); }

$dir = dirname(__DIR__) . '/t9-forum-data';
if (!is_dir($dir) && !@mkdir($dir, 0700, true)) fail('The forum storage folder could not be created on the server.', 500);
if (!class_exists('PDO') || !in_array('sqlite', PDO::getAvailableDrivers(), true)) fail('This server does not have SQLite enabled for PHP.', 500);
try {
  $db = new PDO('sqlite:' . $dir . '/forum.sqlite', null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]);
  $db->exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;');
  $db->exec("CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY, cat TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, name TEXT NOT NULL, created INTEGER NOT NULL, last_at INTEGER NOT NULL, replies INTEGER NOT NULL DEFAULT 0, reports INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0, staff INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS replies (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE, body TEXT NOT NULL, name TEXT NOT NULL, created INTEGER NOT NULL, reports INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, staff INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id INTEGER NOT NULL, reason TEXT NOT NULL, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS rl (h TEXT NOT NULL, at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_threads ON threads(hidden, pinned, last_at);
    CREATE INDEX IF NOT EXISTS ix_replies ON replies(thread_id, created);
    CREATE INDEX IF NOT EXISTS ix_rl ON rl(h, at);");
} catch (Throwable $e) { fail('The forum database could not be opened.', 500); }

$now = time();
$db->prepare('DELETE FROM rl WHERE at < ?')->execute([$now - 86400]);

/* First run: a server secret for the daily salt, and starter threads from the site team. */
$secret = $db->query("SELECT v FROM meta WHERE k='secret'")->fetchColumn();
if (!$secret) {
  $secret = bin2hex(random_bytes(32));
  $db->prepare("INSERT INTO meta (k, v) VALUES ('secret', ?)")->execute([$secret]);
  $seed = [
    ['general', 'Welcome to the Triple 9 community – please read', "This is a space for anyone in, joining or leaving the emergency and uniformed services to ask questions and share experience.\n\nThe rules are simple:\n• Be respectful. No abuse, harassment, discrimination or threats.\n• Stay anonymous. Don't post your full name, warrant/collar number, address or anything that identifies you or anyone else.\n• Nothing operational. Never discuss live cases, incidents, intelligence, victims, detainees, patients or prisoners.\n• No adverts, spam or links.\n• Nothing illegal.\n\nUse the Report button on anything that breaks the rules and a moderator will review it. If you're struggling, please see our Support page or call 999 in an emergency.", 1],
    ['general', 'Introduce yourself – what service are you in (or leaving)?', "Say hello! Which service are you in, how long have you served, and what brings you here? (Keep it anonymous – no names or numbers.)", 0],
    ['transfers', 'What do you wish you’d known before transferring forces?', "If you've transferred, what surprised you? Vetting times, losing your seniority, the interview, the move itself – share what would help someone thinking about it.", 0],
    ['leaving', 'Left the job? What did you go on to do?', "For anyone who has already left: what job did you move into, how did you find it, and what would you tell someone thinking about leaving?", 0],
  ];
  $ins = $db->prepare('INSERT INTO threads (cat, title, body, name, created, last_at, pinned, staff) VALUES (?,?,?,?,?,?,?,1)');
  foreach ($seed as $i => $s) $ins->execute([$s[0], $s[1], $s[2], 'Triple 9 Careers', $now - $i, $now - $i, $s[3]]);
}

function client_hash(string $secret): string {
  $ip = $_SERVER['REMOTE_ADDR'] ?? '0';
  return hash('sha256', $secret . '|' . gmdate('Y-m-d') . '|' . $ip);
}
function clean(string $s, int $max): string {
  $s = str_replace(["\r\n", "\r"], "\n", $s);
  $s = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $s) ?? '';
  $s = preg_replace("/\n{3,}/", "\n\n", $s) ?? '';
  return trim(mb_substr(trim($s), 0, $max));
}
function blocked(string $s): ?string {
  if (preg_match('~(https?://|www\.|\b[a-z0-9-]+\.(com|co\.uk|uk|net|org|io|ly|me|xyz|ru)\b)~i', $s)) return 'Links and web addresses aren\'t allowed in posts.';
  if (preg_match('~\b[\w.+-]+@[\w-]+\.[\w.]+\b~', $s)) return 'Please don\'t post email addresses – keep yourself anonymous.';
  if (preg_match('~(\+44|\b0)\s?\d{3,4}\s?\d{3}\s?\d{3,4}\b~', $s)) return 'Please don\'t post phone numbers – keep yourself anonymous.';
  $bad = ['nigger','nigga','paki','faggot','tranny','kike','spastic','retard','chink','wog','coon','raghead'];
  foreach ($bad as $w) if (preg_match('~\b' . $w . 's?\b~i', $s)) return 'Your post contains language that isn\'t allowed here.';
  return null;
}
function rate_check(PDO $db, string $h, int $now): void {
  $q = $db->prepare('SELECT MAX(at) AS last, COUNT(*) AS n FROM rl WHERE h = ?'); $q->execute([$h]); $r = $q->fetch();
  if ($r && $r['last'] && $now - (int)$r['last'] < POST_GAP) fail('You\'re posting very quickly – please wait a few seconds and try again.', 429);
  if ($r && (int)$r['n'] >= POSTS_PER_DAY) fail('You\'ve reached today\'s posting limit. Please come back tomorrow.', 429);
}
function is_mod(?string $key): bool { return $key !== null && $key !== '' && password_verify($key, MOD_HASH); }

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$in = $method === 'POST' ? (json_decode(file_get_contents('php://input') ?: '[]', true) ?: []) : $_GET;
$a = (string)($in['a'] ?? '');

if ($a === 'threads') {
  $cat = (string)($in['cat'] ?? ''); $page = max(0, (int)($in['page'] ?? 0));
  $sql = 'SELECT id, cat, title, name, created, last_at, replies, pinned, staff, substr(body,1,180) AS snippet FROM threads WHERE hidden = 0';
  $args = [];
  if (in_array($cat, CATS, true)) { $sql .= ' AND cat = ?'; $args[] = $cat; }
  $sql .= ' ORDER BY pinned DESC, last_at DESC LIMIT 31 OFFSET ' . ($page * 30);
  $q = $db->prepare($sql); $q->execute($args); $rows = $q->fetchAll();
  out(['ok' => true, 'threads' => array_slice($rows, 0, 30), 'more' => count($rows) > 30]);
}
if ($a === 'thread') {
  $id = (int)($in['id'] ?? 0);
  $q = $db->prepare('SELECT id, cat, title, body, name, created, last_at, replies, pinned, staff FROM threads WHERE id = ? AND hidden = 0'); $q->execute([$id]);
  $t = $q->fetch(); if (!$t) fail('This discussion has been removed or doesn\'t exist.', 404);
  $r = $db->prepare('SELECT id, body, name, created, staff FROM replies WHERE thread_id = ? AND hidden = 0 ORDER BY created ASC LIMIT 500'); $r->execute([$id]);
  out(['ok' => true, 'thread' => $t, 'replies' => $r->fetchAll()]);
}
if ($method !== 'POST') fail('Unknown request.');

if ($a === 'new_thread' || $a === 'reply') {
  if (!empty($in['website'])) out(['ok' => true]);           /* honeypot: bots fill this */
  if ((int)($in['t'] ?? 0) < 4000) fail('Please take a moment to write your post before sending.');
  if (empty($in['agree'])) fail('Please tick to confirm your post follows the community rules.');
  $name = clean((string)($in['name'] ?? ''), MAX_NAME); if ($name === '') $name = 'Anonymous';
  if (preg_match('~triple\s*9|moderator|admin~i', $name)) $name = 'Anonymous';
  $body = clean((string)($in['body'] ?? ''), MAX_BODY);
  if (mb_strlen($body) < 10) fail('Please write a little more (at least 10 characters).');
  $h = client_hash($secret); rate_check($db, $h, $now);
  if ($a === 'new_thread') {
    $cat = (string)($in['cat'] ?? ''); if (!in_array($cat, CATS, true)) fail('Please choose a topic.');
    $title = clean((string)($in['title'] ?? ''), MAX_TITLE); if (mb_strlen($title) < 5) fail('Please give your discussion a title.');
    foreach ([$title, $body, $name] as $s) if ($m = blocked($s)) fail($m);
    $db->prepare('INSERT INTO threads (cat, title, body, name, created, last_at) VALUES (?,?,?,?,?,?)')->execute([$cat, $title, $body, $name, $now, $now]);
    $id = (int)$db->lastInsertId();
  } else {
    $id = (int)($in['thread_id'] ?? 0);
    $q = $db->prepare('SELECT id FROM threads WHERE id = ? AND hidden = 0'); $q->execute([$id]); if (!$q->fetch()) fail('This discussion is no longer available.', 404);
    foreach ([$body, $name] as $s) if ($m = blocked($s)) fail($m);
    $db->prepare('INSERT INTO replies (thread_id, body, name, created) VALUES (?,?,?,?)')->execute([$id, $body, $name, $now]);
    $db->prepare('UPDATE threads SET replies = replies + 1, last_at = ? WHERE id = ?')->execute([$now, $id]);
  }
  $db->prepare('INSERT INTO rl (h, at) VALUES (?, ?)')->execute([$h, $now]);
  out(['ok' => true, 'id' => $id]);
}
if ($a === 'report') {
  $kind = (string)($in['kind'] ?? ''); $id = (int)($in['id'] ?? 0); $reason = clean((string)($in['reason'] ?? ''), 300);
  if (!in_array($kind, ['thread','reply'], true) || $id < 1) fail('Unknown post.');
  $table = $kind === 'thread' ? 'threads' : 'replies';
  $db->prepare("INSERT INTO reports (kind, item_id, reason, created) VALUES (?,?,?,?)")->execute([$kind, $id, $reason !== '' ? $reason : 'No reason given', $now]);
  $db->prepare("UPDATE $table SET reports = reports + 1, hidden = CASE WHEN staff = 0 AND reports + 1 >= " . REPORTS_TO_HIDE . " THEN 1 ELSE hidden END WHERE id = ?")->execute([$id]);
  out(['ok' => true]);
}
if ($a === 'mod_list' || $a === 'mod') {
  if (!is_mod((string)($in['key'] ?? ''))) { usleep(800000); fail('Wrong moderator key.', 403); }
  if ($a === 'mod_list') {
    $t = $db->query('SELECT id, cat, title, body, name, created, reports, hidden FROM threads WHERE reports > 0 OR hidden = 1 ORDER BY hidden DESC, reports DESC, created DESC LIMIT 200')->fetchAll();
    $r = $db->query('SELECT r.id, r.thread_id, t.title, r.body, r.name, r.created, r.reports, r.hidden FROM replies r JOIN threads t ON t.id = r.thread_id WHERE r.reports > 0 OR r.hidden = 1 ORDER BY r.hidden DESC, r.reports DESC, r.created DESC LIMIT 200')->fetchAll();
    $latest = $db->query('SELECT id, cat, title, name, created, replies, hidden FROM threads ORDER BY created DESC LIMIT 50')->fetchAll();
    $why = $db->query('SELECT kind, item_id, reason, created FROM reports ORDER BY created DESC LIMIT 300')->fetchAll();
    out(['ok' => true, 'threads' => $t, 'replies' => $r, 'latest' => $latest, 'reports' => $why]);
  }
  $kind = (string)($in['kind'] ?? ''); $id = (int)($in['id'] ?? 0); $act = (string)($in['action'] ?? '');
  $table = $kind === 'thread' ? 'threads' : ($kind === 'reply' ? 'replies' : '');
  if ($table === '' || $id < 1) fail('Unknown post.');
  if ($act === 'hide') $db->prepare("UPDATE $table SET hidden = 1 WHERE id = ?")->execute([$id]);
  elseif ($act === 'restore') $db->prepare("UPDATE $table SET hidden = 0, reports = 0 WHERE id = ?")->execute([$id]);
  elseif ($act === 'pin' && $table === 'threads') $db->prepare('UPDATE threads SET pinned = 1 - pinned WHERE id = ?')->execute([$id]);
  elseif ($act === 'delete') {
    if ($table === 'replies') {
      $q = $db->prepare('SELECT thread_id FROM replies WHERE id = ?'); $q->execute([$id]); $tid = $q->fetchColumn();
      $db->prepare('DELETE FROM replies WHERE id = ?')->execute([$id]);
      if ($tid) $db->prepare('UPDATE threads SET replies = (SELECT COUNT(*) FROM replies WHERE thread_id = ?) WHERE id = ?')->execute([$tid, $tid]);
    } else $db->prepare('DELETE FROM threads WHERE id = ?')->execute([$id]);
    $db->prepare('DELETE FROM reports WHERE kind = ? AND item_id = ?')->execute([$kind, $id]);
  } else fail('Unknown action.');
  out(['ok' => true]);
}
fail('Unknown request.');
