"""Is the Cloudflare Worker failing? Used by .github/workflows/worker-errors.yml (docs/ci.md#worker-errors).

Reads the `tastr` Worker's error log lines since the previous successful check (Workers Observability API), groups
them by request (a Python traceback arrives as one log line per traceback line), drops known harmless noise, and
counts failed requests. It trips when FAILED_LIMIT or more requests failed, or when the Durable Object was reset
(out of memory or CPU), which fails every request in flight.

Prints a Markdown report and writes `tripped=true|false` and `failed=<n>` to $GITHUB_OUTPUT when it is set. The
report can go into a public issue (and parts of it show on the public run page), so emails, upload names and link
tokens are masked.

GitHub runs scheduled workflows late or drops them when it is busy (on 2026-11-05 the 30-minute schedule ran about
every 6 to 8 hours), so a fixed one-hour window missed most of the time. Each check therefore reads back to the start
of the previous successful check, at least WINDOW_MIN and at most MAX_WINDOW_MIN minutes. That needs GH_TOKEN,
GITHUB_REPOSITORY and GITHUB_RUN_ID (set in Actions); without them, or with no earlier run, it reads WINDOW_MIN.

Needs CF_LOGS_TOKEN: a Cloudflare API token with only Account > Workers Observability > Read.
`--minutes N` reads a fixed window instead (Claude uses it to investigate).
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

ACCOUNT = "1c793218966f05bb509e1e76f4ba0b67"  # not a secret (worker/wrangler.jsonc)
SERVICE = "tastr"
WINDOW_MIN = 60
MAX_WINDOW_MIN = 12 * 60  # after a longer gap, the oldest errors are left out rather than reading days of logs
WORKFLOW = "worker-errors.yml"
FAILED_LIMIT = 5
PAGE = 2000  # the API's most events per query; no offset, so long windows are read an hour at a time

# A ZipFile from the runtime's start-up snapshot closing a gone file descriptor at garbage collection: printed,
# never raised (TAS-98). Requests that log only this did not fail.
NOISE = [re.compile(r"Exception ignored while calling deallocator <function ZipFile\.__del__"),
         re.compile(r"OSError: \[Errno \d+\] Bad file descriptor"),
         re.compile(r'File "/lib/python\d+\.zip/zipfile/'),
         re.compile(r"^\s*Traceback \(most recent call last\):\s*$")]
RESET = re.compile(r"exceeded its (memory|CPU time) limit and was reset")
EXCEPTION = re.compile(r"^[\s|+]*([\w.]+(?:Error|Exception)\b.*|.*exceeded its \w+ (?:time )?limit.*|.*couldn't .*)$")


def query(token: str, start_ms: int, end_ms: int) -> list[dict]:
    body = {"queryId": "worker-errors", "timeframe": {"from": start_ms, "to": end_ms}, "view": "events",
            "limit": PAGE, "parameters": {"filters": [
                {"key": "$metadata.service", "operation": "eq", "type": "string", "value": SERVICE},
                {"key": "$metadata.level", "operation": "eq", "type": "string", "value": "error"}]}}
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/observability/telemetry/query",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "tastr-worker-errors"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - Cloudflare's API
            data = json.load(r)
    except urllib.error.HTTPError as e:  # Cloudflare's answer says why (bad token, missing permission)
        raise RuntimeError(f"Cloudflare answered HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
    if not data.get("success", True) or "result" not in data:
        raise RuntimeError(f"Cloudflare refused the query: {str(data)[:300]}")
    return data["result"]["events"]["events"]


def fetch(token: str, minutes: int, now_ms: int | None = None) -> tuple[list[dict], bool]:
    """Error events of the last `minutes`, and whether any hour hit the page limit (some are missing then)."""
    end = now_ms or int(time.time() * 1000)
    start, out, full = end - minutes * 60_000, [], False
    while start < end:
        stop = min(start + 3_600_000, end)
        page = query(token, start, stop)
        full |= len(page) >= PAGE
        out += page
        start = stop
    return out, full


def window(previous_start_ms: int | None, now_ms: int) -> int:
    """Minutes to read: back to the previous check's start, within WINDOW_MIN..MAX_WINDOW_MIN."""
    if previous_start_ms is None:
        return WINDOW_MIN
    return max(WINDOW_MIN, min(MAX_WINDOW_MIN, math.ceil((now_ms - previous_start_ms) / 60_000)))


def previous_start_ms() -> int | None:
    """When the newest successful check before this one started, from the GitHub API; None if unknown."""
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if not (repo and run_id and os.environ.get("GH_TOKEN")):
        return None
    try:
        out = subprocess.run(["gh", "api", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?status=success&per_page=10"],
                             check=True, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for run in json.loads(out).get("workflow_runs", []):
        if str(run.get("id")) != run_id and run.get("run_started_at"):
            return calendar.timegm(time.strptime(run["run_started_at"], "%Y-%m-%dT%H:%M:%SZ")) * 1000
    return None


def mask(text: str) -> str:
    """What may go in a public issue: no emails, upload names, link tokens or query strings."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
    text = re.sub(r"(uploads|stems)/[0-9a-f]{4,}/[^'\"\s)]+", r"\1/<id>/<file>", text)
    text = re.sub(r"/pub/[^/\s]+", "/pub/<token>", text)
    return re.sub(r"\?[^\s'\"]*", "", text)


def message(e: dict) -> str:
    m = e.get("$metadata", {})
    return str(m.get("message") or m.get("error") or "")


def is_noise(line: str) -> bool:
    return not line.strip() or any(p.search(line) for p in NOISE)


def group(events: list[dict]) -> dict:
    """Requests that failed, grouped by what went wrong: {signature: [{when, trigger, text}]}, plus the
    resets and the requests that logged only noise."""
    by_request = defaultdict(list)
    for e in events:
        by_request[e.get("$metadata", {}).get("requestId") or id(e)].append(e)
    groups, noise_only = defaultdict(list), 0
    for es in by_request.values():
        es.sort(key=lambda e: e.get("timestamp", 0))
        lines = [ln for e in es for ln in message(e).split("\n")]
        real = [ln for ln in lines if not is_noise(ln)]
        if not real:
            noise_only += 1
            continue
        found = list(dict.fromkeys(m.group(1).strip() for ln in real if (m := EXCEPTION.match(ln))))
        sig = re.sub(r"\d+", "N", mask(" | ".join(found) or real[0].strip()))[:240]
        groups[sig].append({"when": es[0].get("timestamp", 0), "trigger": mask(es[0]["$metadata"].get("trigger", "")),
                            "text": mask("\n".join(real))})
    resets = sum(len(v) for k, v in groups.items() if RESET.search(k))
    return {"groups": dict(groups), "resets": resets, "noise_only": noise_only}


def report(result: dict, minutes: int, full: bool) -> tuple[str, int, bool]:
    groups = result["groups"]
    failed = sum(len(v) for v in groups.values())
    tripped = failed >= FAILED_LIMIT or result["resets"] > 0
    stamp = lambda ms: time.strftime("%H:%M", time.gmtime(ms / 1000))  # noqa: E731
    lines = [f"**{failed} failed request(s)** on the `{SERVICE}` Worker in the last {minutes} min "
             f"(limit {FAILED_LIMIT}), {result['resets']} of them in a Durable Object reset. "
             f"{result['noise_only']} request(s) logged only the known harmless ZipFile noise (TAS-98).", ""]
    if full:
        lines += ["Some hour hit the API's 2,000-event page, so there may be more than shown.", ""]
    if groups:
        lines += ["| Requests | First–last (UTC) | Endpoints | Error |", "|---|---|---|---|"]
        for sig, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            ends = ", ".join(sorted({i["trigger"].split(" https")[0][:40] for i in items})[:3])
            when = f"{stamp(min(i['when'] for i in items))}–{stamp(max(i['when'] for i in items))}"
            lines.append(f"| {len(items)} | {when} | {ends} | `{sig.replace('|', '/')}` |")
        lines += ["", "One example of each, newest last:", ""]
        for sig, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            sample = max(items, key=lambda i: i["when"])["text"]
            lines += ["<details><summary>" + sig[:100].replace("<", "&lt;") + "</summary>", "", "```",
                      sample[-3000:], "```", "</details>", ""]
    return "\n".join(lines), failed, tripped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, help="a fixed window (default: since the previous successful check)")
    args = ap.parse_args()
    now = int(time.time() * 1000)
    minutes = args.minutes or window(previous_start_ms(), now)
    try:
        events, full = fetch(os.environ["CF_LOGS_TOKEN"], minutes, now)
    except RuntimeError as e:  # fail the run, so a broken token shows as a red run instead of silence
        sys.exit(f"Couldn't read the Worker's logs: {e}")
    text, failed, tripped = report(group(events), minutes, full)
    print(text)
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"tripped={'true' if tripped else 'false'}\nfailed={failed}\nminutes={minutes}\n")


if __name__ == "__main__":
    main()
