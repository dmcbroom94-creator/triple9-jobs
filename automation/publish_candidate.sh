#!/usr/bin/env bash
# Publish a staged candidate using only a normal fast-forward push.
# Usage: bash automation/publish_candidate.sh CANDIDATE BASE_COMMIT BASE_JOBS_BLOB
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1 GIT_TERMINAL_PROMPT=0

fail() { printf 'Publisher: %s\n' "$1" >&2; exit 1; }
trap 'printf "Publisher: stopped; inspect the generic checks and retry from a fresh run.\n" >&2' ERR
[ "$#" -eq 3 ] || fail 'expected candidate, baseline commit, and baseline feed blob'
candidate=$1
baseline=$2
baseline_blob=$3
[[ "$baseline" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || fail 'invalid baseline commit'
[[ "$baseline_blob" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || fail 'invalid baseline feed blob'
[ -f "$candidate" ] && [ ! -L "$candidate" ] || fail 'candidate must be a regular, non-symlink file'
root=$(git rev-parse --show-toplevel 2>/dev/null) || fail 'not in a Git worktree'
cd "$root"
root=$(pwd -P)
candidate_real=$(realpath -e -- "$candidate" 2>/dev/null) || fail 'cannot resolve candidate'
case "$candidate_real" in "$root"/*|"$root") fail 'candidate must be outside the worktree';; esac
[ "$(git rev-parse --verify HEAD 2>/dev/null)" = "$baseline" ] || fail 'checkout does not match the baseline'
[ "$(git cat-file -t "$baseline" 2>/dev/null)" = commit ] || fail 'baseline is not a commit'
[ "$(git rev-parse "$baseline:jobs.json" 2>/dev/null)" = "$baseline_blob" ] || fail 'baseline feed blob does not match'
[ "$(git cat-file -t "$baseline_blob" 2>/dev/null)" = blob ] || fail 'baseline feed is not a blob'
[ "$(git ls-tree "$baseline" -- jobs.json | awk '{print $1}')" = 100644 ] || fail 'feed must be a regular tracked file'
[ -z "$(git status --porcelain=v1 --untracked-files=all --ignored 2>/dev/null)" ] || fail 'worktree or index contains other work'

scratch=$(mktemp -d "${TMPDIR:-/tmp}/triple9-publish.XXXXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
cp -- "$candidate_real" "$scratch/candidate.json" || fail 'cannot snapshot candidate'

# Never print JSON data or exception text: input can contain sensitive URLs.
validate() {
  python3 -B - "$1" <<'PY'
import importlib.util, json, sys
from pathlib import Path

def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result

def reject_constant(_):
    raise ValueError('non-finite number')

try:
    path = Path(sys.argv[1])
    if not path.is_file() or path.stat().st_size > 100_000_000:
        raise ValueError('bad candidate')
    sys.path.insert(0, str(Path('automation').resolve()))
    spec = importlib.util.spec_from_file_location('feed_validator', 'automation/runner.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    with path.open(encoding='utf-8') as f:
        feed = json.load(f, object_pairs_hook=pairs, parse_constant=reject_constant)
    now = runner._clock(None)
    runner.validate_feed(feed, now)
    health = feed.get('sourceHealth')
    if not isinstance(health, dict):
        raise ValueError('missing health')
    runner.parse_timestamp(health.get('attemptedAt'), now)
    sources = health.get('sources')
    if not isinstance(sources, list) or not 0 < len(sources) <= runner.MAX_SOURCES:
        raise ValueError('bad health')
    success, verified = 0, 0
    for s in sources:
        if not isinstance(s, dict) or s.get('adapter') not in runner.ADAPTERS:
            raise ValueError('bad adapter')
        if not isinstance(s.get('id'), str) or len(s['id']) > 128:
            raise ValueError('bad source id')
        status = s.get('status')
        if status not in ('success', 'partial', 'failed') or type(s.get('success')) is not bool:
            raise ValueError('bad status')
        if s['success'] != (status == 'success'):
            raise ValueError('inconsistent status')
        for field in ('verifiedJobs', 'rejectedJobs'):
            if type(s.get(field)) is not int or not 0 <= s[field] <= runner.MAX_JOBS:
                raise ValueError('bad count')
        if (status == 'success' and s['rejectedJobs']) or (status == 'failed' and s['verifiedJobs']):
            raise ValueError('inconsistent count')
        success += status == 'success'
        verified += s['verifiedJobs']
    if not success and not verified:
        raise ValueError('all sources failed')
except Exception:
    print('Publisher: candidate validation or source-health check failed.', file=sys.stderr)
    sys.exit(1)
PY
}
validate "$scratch/candidate.json" || fail 'candidate rejected'

# Fetch without force, then demand that main is a descendant of the exact base.
git fetch --quiet --no-tags origin refs/heads/main:refs/remotes/origin/main >/dev/null 2>&1 || fail 'cannot safely fetch origin/main'
latest=$(git rev-parse --verify refs/remotes/origin/main 2>/dev/null) || fail 'origin/main missing'
git merge-base --is-ancestor "$baseline" "$latest" 2>/dev/null || fail 'main was rewritten; retry after review'
[ "$(git rev-parse "$latest:jobs.json" 2>/dev/null)" = "$baseline_blob" ] || fail 'feed changed during collection; retry'
[ "$(git ls-tree "$latest" -- jobs.json | awk '{print $1}')" = 100644 ] || fail 'feed type changed; retry'
for path in automation .github/workflows; do
  before=$(git rev-parse "$baseline:$path" 2>/dev/null) || fail 'baseline automation or workflows missing'
  after=$(git rev-parse "$latest:$path" 2>/dev/null) || fail 'current automation or workflows missing'
  [ "$before" = "$after" ] || fail 'automation or workflows changed during collection; retry'
done
[ -z "$(git status --porcelain=v1 --untracked-files=all --ignored 2>/dev/null)" ] || fail 'worktree became dirty'
git -c core.hooksPath=/dev/null merge --ff-only --quiet "$latest" >/dev/null 2>&1 || fail 'cannot fast-forward checkout'
[ "$(git rev-parse HEAD)" = "$latest" ] || fail 'checkout did not fast-forward'
[ -z "$(git status --porcelain=v1 --untracked-files=all --ignored 2>/dev/null)" ] || fail 'worktree became dirty'

if cmp -s -- "$scratch/candidate.json" jobs.json; then
  outcome='No feed change; no commit or push.'
else
  cp -- "$scratch/candidate.json" jobs.json || fail 'cannot stage candidate'
  validate jobs.json || fail 'copied candidate rejected'
  git add -- jobs.json >/dev/null 2>&1 || fail 'cannot add candidate'
  [ "$(git diff --cached --name-only)" = jobs.json ] || fail 'index contains unexpected files'
  [ -z "$(git diff --name-only)" ] || fail 'other tracked files changed'
  [ "$(git status --porcelain=v1 --untracked-files=all --ignored)" = 'M  jobs.json' ] || fail 'other work detected'
  git show :jobs.json > "$scratch/index.json" 2>/dev/null || fail 'cannot inspect staged feed'
  cmp -s -- "$scratch/candidate.json" "$scratch/index.json" || fail 'Git filters changed candidate bytes'
  validate "$scratch/index.json" || fail 'staged candidate rejected'
  git -c user.name='github-actions[bot]' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
    -c commit.gpgsign=false -c core.hooksPath=/dev/null commit --quiet -m 'Update Triple 9 jobs feed' >/dev/null 2>&1 || fail 'feed commit failed'
  [ "$(git diff-tree --no-commit-id --name-only -r HEAD)" = jobs.json ] || fail 'commit includes unexpected paths'
  remote_tip=$(git ls-remote --refs origin refs/heads/main 2>/dev/null | awk 'NR == 1 {print $1}') || fail 'cannot check remote tip'
  [ "$remote_tip" = "$latest" ] || fail 'remote moved after fetch; retry'
  # No force or lease: a concurrent normal update causes a non-fast-forward rejection.
  git push --quiet origin HEAD:refs/heads/main >/dev/null 2>&1 || fail 'push rejected; remote may have moved; retry'
  outcome='Feed published; bot commit contains only jobs.json.'
fi
printf 'Publisher: %s\n' "$outcome"
# Source IDs are printed only when restricted to a safe character set. Never print
# source errors, URLs, titles, raw JSON, or free-form text.
python3 -B - "$scratch/candidate.json" "$outcome" <<'PY' || true
import json, os, re, sys
with open(sys.argv[1], encoding='utf-8') as f:
    sources = json.load(f)['sourceHealth']['sources']
incomplete = any(s['status'] != 'success' for s in sources)
heading = 'Partial/incomplete source health' if incomplete else 'All recorded sources completed successfully'
lines = ['## Triple 9 daily feed', '', sys.argv[2], '', '**' + heading + '**', '']
for s in sources:
    ident = s['id'] if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', s['id']) else 'unidentified'
    lines.append('- ' + s['adapter'] + '/' + ident + ': ' + s['status'] +
                 '; verified=' + str(s['verifiedJobs']) + '; rejected=' + str(s['rejectedJobs']))
if incomplete:
    lines += ['', 'Partial results may include individually validated items; failed or partial scans do not establish complete coverage.']
text = '\n'.join(lines) + '\n'
print(text, end='')
if os.environ.get('GITHUB_STEP_SUMMARY'):
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write(text)
PY
