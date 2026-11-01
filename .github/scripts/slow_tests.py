"""Is the `tests` workflow getting slow? Used by .github/workflows/slow-tests.yml (docs/ci.md).

Reads the last successful pushes to main, drops docs-only runs (under a minute), and compares the median of
the newest RUNS to LIMIT_MIN. Prints a Markdown report and writes `slow=true|false` and `median=<min>` to
$GITHUB_OUTPUT when it is set. Needs GH_TOKEN and GITHUB_REPOSITORY (gh CLI).
"""

import json
import os
import statistics
import subprocess
from datetime import datetime

WORKFLOW = "test.yml"
RUNS = 5
LIMIT_MIN = 5.0
DOCS_ONLY_MIN = 1.0


def gh(path):
    return json.loads(subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True).stdout)


def minutes(run):
    t = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (t(run["updated_at"]) - t(run["run_started_at"])).total_seconds() / 60


def slowest_steps(run_id, n=5):
    steps = []
    for job in gh(f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}/jobs")["jobs"]:
        for s in job["steps"]:
            if s.get("conclusion") == "success" and s.get("started_at") and s.get("completed_at"):
                steps.append((minutes({"run_started_at": s["started_at"], "updated_at": s["completed_at"]}), s["name"]))
    return sorted(steps, reverse=True)[:n]


def full_runs(runs):
    """Durations of the runs that tested code, newest first."""
    return [(r, minutes(r)) for r in runs if minutes(r) >= DOCS_ONLY_MIN][:RUNS]


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    runs = gh(f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?branch=main&event=push&status=success&per_page=40")
    recent = full_runs(runs["workflow_runs"])
    median = statistics.median(m for _, m in recent) if recent else 0.0
    slow = len(recent) == RUNS and median > LIMIT_MIN
    lines = [
        f"Median of the last {len(recent)} full `tests` runs on main: **{median:.1f} min** (limit {LIMIT_MIN:g}).",
        "",
        "| Run | Commit | Minutes |",
        "|---|---|---|",
        *(f"| [{r['id']}]({r['html_url']}) | {r['head_sha'][:7]} | {m:.1f} |" for r, m in recent),
    ]
    if recent:
        newest = recent[0][0]
        lines += ["", f"Slowest steps in the newest run ({newest['id']}):", ""]
        lines += [f"- {name}: {m * 60:.0f} s" for m, name in slowest_steps(newest["id"])]
    print("\n".join(lines))
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"slow={'true' if slow else 'false'}\nmedian={median:.1f}\n")


if __name__ == "__main__":
    main()
