# Daily vacancy refresh

The `Triple 9 daily jobs` GitHub Actions workflow is configured for **05:10 Europe/London**, every day. GitHub may start runs late or disable schedules after a long period of repository inactivity. The live homepage and jobs pages load the repository-root `jobs.json` directly; the separate cPanel task publishes changes under `site/`.

## Current coverage

The readers currently cover a selected set of vacancies from:

- Avon and Somerset Police's Oleeo board.
- British Transport Police's vacancies board.
- NHS Jobs' employer feeds E0059, C9335, C9202, C9242, C9247 and C9308, restricted to selected emergency/ambulance and custody medical roles.
- Ministry of Justice prison, probation and other selected justice roles.

These are not comprehensive scans of every role at these organisations. The other employers already represented in `jobs.json` are **not** automatically refreshed by these readers. Additional source integrations need independent review and testing before they can be added. Some employer pages may fail or change. Existing listings from unverified sources are retained, and may be out of date; visitors should check the official employer advert.

The owner confirmed that the intended sources may be republished. `NHS_JOBS_PERMISSION_CONFIRMED=1` is a non-secret assertion of that permission, set only in the collection step; remove it if the permission is withdrawn. Ministry of Justice content covered by the Open Government Licence is credited on the jobs pages; some government content may have separate rights.

## Safety and maintenance

Each source adapter verifies individual vacancies and records the actual check timestamp. A partial or failed source does not establish that its other roles are closed. The merge keeps unrelated jobs, protects curated filters, validates official URLs, and prevents unexpectedly large removals. A valid individually verified item from a partial source can still be added, but is reported as partial. The feed's `updated` field means the **feed** was updated, not that every listing was rechecked. `sourceHealth` records the last attempt by source.

Closing dates are enforced on every run: a listing whose closing date has passed (UK date) is dropped from the published feed automatically, instead of relying on the source to remove it. Listings with no closing date (for example "Open – see force website") are kept, and the run log records how many were kept (`openEnded`) so they can be reviewed periodically. The live site also hides expired listings itself and can sort the feed by closing date, so a stale snapshot never shows a closed vacancy.

Collection and validation happen in an isolated staging file. The publisher checks for concurrent changes to the feed or automation, makes a commit containing only `jobs.json`, and uses a non-force push. If all sources fail with no validated vacancies, or validation or publication fails, the last published feed remains in place. GitHub notification delivery depends on the repository owner's notification settings; there is no separate missed-run monitor.

View or disable the workflow under **GitHub → Actions → Triple 9 daily jobs**. A manual run defaults to a dry run; select `publish` only to make it write the feed. Each run's summary shows source-level results; a partial result is not full coverage. The workflow uses a short-lived `GITHUB_TOKEN`, not a stored personal access token.

Local offline checks:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/automation" python3 -m unittest discover -s automation/tests -p 'test_*.py'
bash automation/tests.sh
```

The readers are intentionally bounded and honour their recorded source access policies and robots rules. Check source terms, health, selectors and permissions when maintaining them. When a source's web layout changes, repair and test its adapter rather than silently treating inaccessible jobs as closed.
