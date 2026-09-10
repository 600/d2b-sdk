"""The GitHub Actions workflow ``d2b github-workflow`` prints.

Two jobs close the loop between a repository and a workbook without any
GitHub App or D2B-side integration — the CLI is the only moving part:

* ``push``: on a merge to ``main`` that touched synced files, ``d2b push``
  re-runs what changed and names the resulting version after the commit;
  the refreshed ``d2b.json`` (sync hashes) is committed back so the next
  developer pull/push starts from the right base. A REFUSED push (both
  sides changed — the conflict rule) lands the refusal report in the job
  summary instead of an anonymous red X.
* ``pull``: on a schedule (and by hand), ``d2b pull`` fetches what the
  workbook's agent changed and opens a pull request for review.

Secrets: ``D2B_API_KEY`` (a PAT with ``data:write``); variable
``D2B_BASE_URL``. The CLI version is pinned to the one that printed the
file — this workflow rewrites workbook state, so upgrades should be
deliberate. The pin names the *distribution* (``d2b-sdk``), which is not
the command (``d2b``) — hence ``uvx --from``.
"""


def _cli_version() -> str:
    try:
        from importlib.metadata import version

        return version("d2b-sdk")
    except Exception:  # pragma: no cover - unbuilt dev checkouts
        return "0"


_TEMPLATE = """\
name: d2b sync

on:
  push:
    branches: [main]
    paths: ["transforms/**", "sheets/**", "data/**", "d2b.json"]
  schedule:
    - cron: "0 * * * *"   # hourly: pick up what the D2B agent changed → PR
  workflow_dispatch:

# Deny by default: a job added later without its own `permissions` block
# would otherwise inherit the repository default, often read-write-all.
permissions: {}

# The push job rewrites d2b.json/data on main while the scheduled pull may
# be reading — never let the two overlap.
concurrency:
  group: d2b-sync-${{ github.ref }}
  cancel-in-progress: false

env:
  # Pinned to the CLI that generated this file (a workbook-rewriting
  # workflow should not float on :latest). Bump deliberately.
  D2B_VERSION: "__D2B_VERSION__"

jobs:
  push:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@0c5e2b8115b80b4c7c5ddf6ffdd634974642d182 # v5.4.1
      - name: Push changed transforms / sheets / data to the workbook
        id: d2b_push
        continue-on-error: true
        env:
          D2B_BASE_URL: ${{ vars.D2B_BASE_URL }}
          D2B_API_KEY: ${{ secrets.D2B_API_KEY }}
        run: |
          set -o pipefail
          uvx --from "d2b-sdk==${D2B_VERSION}" d2b push --commit "${GITHUB_SHA::7}" | tee push-report.json
      - name: Explain a refused push (conflict report → job summary)
        if: steps.d2b_push.outcome == 'failure'
        run: |
          {
            echo "## d2b push was refused"
            echo ""
            echo "Both the repository and the workbook changed the files below, so the"
            echo "conflict rule refused rather than guess. Either:"
            echo ""
            echo "- run \\`d2b pull\\`, merge locally, and push a new commit, or"
            echo "- re-run with \\`--force\\` ONLY when the repository is the source of truth."
            echo ""
            echo '```json'
            cat push-report.json 2>/dev/null || echo '{}'
            echo '```'
          } >> "$GITHUB_STEP_SUMMARY"
          exit 1
      # NOTE: commits straight to main. On a branch-protected repository
      # swap this step for peter-evans/create-pull-request — which also
      # needs `pull-requests: write` adding to this job's permissions.
      - name: Commit the refreshed sync manifest
        if: steps.d2b_push.outcome == 'success'
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          git config user.name "d2b-sync"
          git config user.email "d2b-sync@users.noreply.github.com"
          git add d2b.json data/ || true
          git diff --cached --quiet || git commit -m "d2b push: refresh sync manifest [skip ci]"
          # GitHub masks the token itself but not this derived encoding, and
          # `base64 -w0` is GNU-only — keep both portable.
          auth="$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '[:space:]')"
          echo "::add-mask::$auth"
          git -c "http.${GITHUB_SERVER_URL}/.extraheader=AUTHORIZATION: basic $auth" push origin "HEAD:${GITHUB_REF_NAME}"

  pull:
    if: github.event_name != 'push'
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@0c5e2b8115b80b4c7c5ddf6ffdd634974642d182 # v5.4.1
      - name: Pull what changed in the workbook
        env:
          D2B_BASE_URL: ${{ vars.D2B_BASE_URL }}
          D2B_API_KEY: ${{ secrets.D2B_API_KEY }}
        run: uvx --from "d2b-sdk==${D2B_VERSION}" d2b pull
      # Reuses one branch, so an open d2b/pull PR is UPDATED every hour —
      # review comments can end up on outdated diffs. Slow the cron (or
      # pause the schedule) while a pull PR is under review.
      - name: Open a pull request when something changed
        uses: peter-evans/create-pull-request@271a8d0340265f705b14b6d32b9829c1cb33d45e # v7.0.8
        with:
          branch: d2b/pull
          title: "d2b: changes from the workbook"
          commit-message: "d2b pull"
          body: |
            Transforms, sheets, charts or data changed in the D2B workbook since the last sync.
            Review the diff, then merge — the push job sends any edits back.
"""

GITHUB_WORKFLOW = _TEMPLATE.replace("__D2B_VERSION__", _cli_version())
