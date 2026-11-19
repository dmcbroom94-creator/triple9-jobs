#!/usr/bin/env bash
# Local-only integration tests. No GitHub remotes, real credentials, or tokens.
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1 GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1
unset GH_TOKEN GITHUB_TOKEN GIT_ASKPASS SSH_ASKPASS RACE_REPO RACE_MARKER
export GIT_ALLOW_PROTOCOL=file
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [ -f "$script_dir/runner.py" ]; then
  source_runner="$script_dir/runner.py"       # installed as automation/tests.sh
elif [ -f "$script_dir/../runner.py" ]; then
  source_runner="$script_dir/../runner.py"    # development pipeline directory
else
  echo 'Missing companion runner.py.' >&2; exit 1
fi
publisher="$script_dir/publish_candidate.sh"
[ -f "$publisher" ] || { echo 'Missing publisher.' >&2; exit 1; }
source_taxonomy="$(dirname -- "$source_runner")/taxonomy.py"
[ -f "$source_taxonomy" ] || { echo 'Missing companion taxonomy.py.' >&2; exit 1; }
REAL_GIT=$(command -v git); export REAL_GIT
sandbox=$(mktemp -d "${TMPDIR:-/tmp}/triple9-pipeline-tests.XXXXXXXX")
trap 'rm -rf -- "$sandbox"' EXIT
mkdir -p "$sandbox/home" "$sandbox/bin"
export HOME="$sandbox/home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export AUDIT="$sandbox/git-audit.log"
: > "$AUDIT"
# Record commands and reject force flags/refspecs. Git's protocol allowlist
# independently prevents HTTP(S), SSH, and other network transports.
cat > "$sandbox/bin/git" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "$AUDIT"; printf '\n' >> "$AUDIT"
is_push=0
for arg in "$@"; do [ "$arg" = push ] && is_push=1; done
if [ "$is_push" = 1 ]; then
  for arg in "$@"; do
    case "$arg" in -f|--force|--force=*|--force-with-lease*|--force-if-includes|+*)
      echo 'Force push/refspec forbidden in tests.' >&2; exit 97;; esac
  done
  if [ -n "${RACE_REPO:-}" ] && [ ! -e "$RACE_MARKER" ]; then
    : > "$RACE_MARKER"
    "$REAL_GIT" -C "$RACE_REPO" push --quiet origin HEAD:refs/heads/main >/dev/null 2>&1
  fi
fi
exec "$REAL_GIT" "$@"
WRAPPER
chmod +x "$sandbox/bin/git"
export PATH="$sandbox/bin:$PATH"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }
commit_local() {
  git -C "$1" -c user.name='Local Fixture' -c user.email='fixture@example.invalid' \
    -c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q -m "$2"
}
make_fixture() {
  d="$sandbox/$1"
  mkdir -p "$d"
  git init --bare -q "$d/origin.git"
  git clone -q "$d/origin.git" "$d/seed" 2>/dev/null
  git -C "$d/seed" checkout -qb main
  mkdir -p "$d/seed/automation/collectors" "$d/seed/.github/workflows"
  cp "$source_runner" "$d/seed/automation/runner.py"
  cp "$source_taxonomy" "$d/seed/automation/taxonomy.py"
  cp "$publisher" "$d/seed/automation/publish_candidate.sh"
  printf '# fixture collector\n' > "$d/seed/automation/collectors/fixture.py"
  printf 'name: fixture\n' > "$d/seed/.github/workflows/daily-jobs.yml"
  printf 'initial site\n' > "$d/seed/site.txt"
  printf 'ignored.tmp\n' > "$d/seed/.gitignore"
  cat > "$d/seed/jobs.json" <<'JSON'
{"updated":"01 Jan 2025","jobs":[],"sourceHealth":{"attemptedAt":"2025-01-01T00:00:00Z","sources":[{"adapter":"btp","id":"btp","status":"success","success":true,"verifiedJobs":0,"rejectedJobs":0}]}}
JSON
  git -C "$d/seed" add .
  commit_local "$d/seed" 'fixture baseline'
  git -C "$d/seed" push -q origin HEAD:refs/heads/main
  git --git-dir="$d/origin.git" symbolic-ref HEAD refs/heads/main
  git clone -q "$d/origin.git" "$d/run"
  git clone -q "$d/origin.git" "$d/other"
  base=$(git -C "$d/run" rev-parse HEAD)
  blob=$(git -C "$d/run" rev-parse HEAD:jobs.json)
  # actions/checkout uses the event SHA, which leaves HEAD detached.
  git -C "$d/run" checkout -q --detach "$base"
  cp "$d/run/jobs.json" "$d/candidate.json"
}
change_candidate() {
  python3 -B - "$d/candidate.json" "${1:-success}" <<'PY'
import json, sys
p, mode = sys.argv[1:]
with open(p, encoding='utf-8') as f: feed = json.load(f)
feed['fixtureChange'] = 'candidate'
s = feed['sourceHealth']['sources'][0]
if mode == 'partial':
    s.update(status='partial', success=False, verifiedJobs=1, rejectedJobs=0)
elif mode == 'failed':
    s.update(status='failed', success=False, verifiedJobs=0, rejectedJobs=0)
with open(p, 'w', encoding='utf-8') as f: json.dump(feed, f); f.write('\n')
PY
}
publish_ok() {
  (cd "$d/run" && GITHUB_STEP_SUMMARY="$d/summary" bash automation/publish_candidate.sh "$d/candidate.json" "$base" "$blob") > "$d/output" 2>&1 || fail "publisher unexpectedly rejected $d"
}
publish_blocked() {
  if (cd "$d/run" && GITHUB_STEP_SUMMARY="$d/summary" bash automation/publish_candidate.sh "$d/candidate.json" "$base" "$blob") > "$d/output" 2>&1; then
    fail "publisher unexpectedly accepted $d"
  fi
}
remote_head() { git --git-dir="$d/origin.git" rev-parse refs/heads/main; }
assert_feed_only_commit() {
  [ "$(git --git-dir="$d/origin.git" diff-tree --no-commit-id --name-only -r "$(remote_head)")" = jobs.json ] || fail 'commit touched a file other than jobs.json'
  [ "$(git --git-dir="$d/origin.git" log -1 --format=%an refs/heads/main)" = 'github-actions[bot]' ] || fail 'bot identity missing'
}

make_fixture success
change_candidate
publish_ok
[ "$(remote_head)" != "$base" ] || fail 'successful publication did not move main'
assert_feed_only_commit
pass 'successful publication uses a bot commit containing only jobs.json'

make_fixture unrelated
change_candidate
printf 'concurrent site change\n' > "$d/other/site.txt"
git -C "$d/other" add site.txt; commit_local "$d/other" 'site edit'
git -C "$d/other" push -q origin HEAD:refs/heads/main
site_commit=$(remote_head)
publish_ok
assert_feed_only_commit
[ "$(git --git-dir="$d/origin.git" rev-parse 'refs/heads/main^')" = "$site_commit" ] || fail 'concurrent site commit was not preserved as parent'
[ "$(git --git-dir="$d/origin.git" show refs/heads/main:site.txt)" = 'concurrent site change' ] || fail 'site change was lost'
pass 'concurrent unrelated site change is preserved by fast-forward'

make_fixture feed-race
change_candidate
printf ' ' >> "$d/other/jobs.json"
git -C "$d/other" add jobs.json; commit_local "$d/other" 'concurrent feed edit'
git -C "$d/other" push -q origin HEAD:refs/heads/main
expected=$(remote_head); publish_blocked
[ "$(remote_head)" = "$expected" ] || fail 'feed race modified remote'
pass 'concurrent feed-content change blocks publication'

make_fixture code-race
change_candidate
printf '# changed during collection\n' >> "$d/other/automation/collectors/fixture.py"
git -C "$d/other" add automation/collectors/fixture.py; commit_local "$d/other" 'concurrent code edit'
git -C "$d/other" push -q origin HEAD:refs/heads/main
expected=$(remote_head); publish_blocked
[ "$(remote_head)" = "$expected" ] || fail 'code race modified remote'
pass 'concurrent automation code change blocks publication'

make_fixture workflow-race
change_candidate
printf '# changed during collection\n' >> "$d/other/.github/workflows/daily-jobs.yml"
git -C "$d/other" add .github/workflows/daily-jobs.yml; commit_local "$d/other" 'concurrent workflow edit'
git -C "$d/other" push -q origin HEAD:refs/heads/main
expected=$(remote_head); publish_blocked
[ "$(remote_head)" = "$expected" ] || fail 'workflow race modified remote'
pass 'concurrent workflow change blocks publication'

make_fixture dirty
change_candidate
printf 'untracked\n' > "$d/run/extra.txt"
publish_blocked
[ "$(remote_head)" = "$base" ] || fail 'untracked change was not blocked'
rm "$d/run/extra.txt"
printf 'ignored\n' > "$d/run/ignored.tmp"
publish_blocked
[ "$(remote_head)" = "$base" ] || fail 'ignored change was not blocked'
rm "$d/run/ignored.tmp"
printf 'tracked\n' >> "$d/run/site.txt"
publish_blocked
[ "$(remote_head)" = "$base" ] || fail 'tracked modification was not blocked'
git -C "$d/run" add site.txt
publish_blocked
[ "$(remote_head)" = "$base" ] || fail 'staged modification was not blocked'
pass 'tracked, staged, untracked, and ignored dirty work is rejected'

make_fixture noop
pushes_before=$(grep -c 'push --quiet origin HEAD:refs/heads/main' "$AUDIT" || true)
publish_ok
pushes_after=$(grep -c 'push --quiet origin HEAD:refs/heads/main' "$AUDIT" || true)
[ "$(remote_head)" = "$base" ] || fail 'unchanged candidate produced a commit'
[ "$pushes_before" = "$pushes_after" ] || fail 'unchanged candidate attempted a push'
grep -Fq 'No feed change' "$d/output" || fail 'missing no-op message'
pass 'identical candidate creates no commit and makes no push'

make_fixture partial
change_candidate partial
publish_ok
assert_feed_only_commit
grep -Fq 'Partial/incomplete source health' "$d/summary" || fail 'partial health not reported'
pass 'validated partial results can publish with explicit incomplete-health summary'

make_fixture all-failed
change_candidate failed
publish_blocked
[ "$(remote_head)" = "$base" ] || fail 'all-source failure was published'
pass 'all-source failure is blocked'

make_fixture invalid
printf '{"jobs":' > "$d/candidate.json"
publish_blocked
[ "$(remote_head)" = "$base" ] || fail 'invalid JSON was published'
pass 'invalid JSON is rejected before publication'

make_fixture push-race
change_candidate
printf 'late concurrent update\n' > "$d/other/site.txt"
git -C "$d/other" add site.txt; commit_local "$d/other" 'late site edit'
late=$(git -C "$d/other" rev-parse HEAD)
if (cd "$d/run" && RACE_REPO="$d/other" RACE_MARKER="$d/marker" bash automation/publish_candidate.sh "$d/candidate.json" "$base" "$blob") > "$d/output" 2>&1; then
  fail 'push race was unexpectedly accepted'
fi
[ -f "$d/marker" ] || fail 'push race was not injected'
[ "$(remote_head)" = "$late" ] || fail 'concurrent push was overwritten'
pass 'push race fails safely without overwriting the remote'

# The wrapper rejects all force push forms, and each fixture uses only a local
# path with the file protocol allowlist. A normal publication was observed.
grep -Fq 'push --quiet origin HEAD:refs/heads/main' "$AUDIT" || fail 'normal publisher push not observed'
pass 'no force push; all test remotes local and only file transport allowed'
printf 'All local pipeline integration tests passed.\n'
