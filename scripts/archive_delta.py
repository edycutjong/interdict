#!/usr/bin/env python3
"""Poll OFAC and archive every publication, keyed by content hash.

WHY THIS EXISTS
---------------
Interdict's headline claim is unattended operation: something polls OFAC on a timer and the agent
acts without anyone watching. This script is that something. It runs on a local launchd timer
(ops/com.interdict.ofac-archiver.plist), not on Google Cloud Scheduler -- no billing account was
available for Google Cloud compute, so the loop closes locally instead. `trigger=SCHEDULER` in the
run records means THIS timer, never Google Cloud Scheduler.

Its first job is that no OFAC publication is ever *lost*: every delta and every full-list snapshot
is captured, hashed, and timestamped, so the sequence is complete and replayable rather than
starting from whenever the archiver happened to be installed.

Content-hash keying means re-polling is free and idempotent: an unchanged publication is recognised
and not re-stored, so this can run every 6 hours indefinitely without duplicating anything.

AND IT CLOSES THE LOOP
----------------------
Archiving alone is not the claim this project makes. The claim is that when Treasury publishes,
the book is re-screened without anybody asking -- and for most of the build that was not true:
this script archived, and a human then ran scripts/run_rescreen.py. The poll and the work were
two halves that never touched, which made "OFAC delta lands -> full-book re-screen" a description
of an intention rather than of the system. Chaining them was Cloud Scheduler's job in the intended
deployment and Cloud Scheduler was never deployed, so the gap sat there behind a true-sounding
sentence.

So the content hash now decides. A publication whose hash is one we have already seen changes
nothing -- that is the overwhelmingly common case and it must stay free. A publication we have
never seen is Treasury changing the sanctions list, which is precisely the event the whole system
exists to react to, so it starts a re-screen with trigger=SCHEDULER, under a lock, without a human.

Set INTERDICT_RESCREEN_ON_NEW=1 to arm it (the launchd plist in ops/ does). Set it to `dry` to log
the exact command and spawn nothing, which is how it is tested without touching a live book.

USAGE
    python3 scripts/archive_delta.py                     # poll once
    python3 scripts/archive_delta.py --dir data/archive  # custom archive root
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import NamedTuple

# A poll that dies is worse than no poll, because the log looks like it ran. This script is
# invoked by a launchd shim whose interpreter is configured outside the repo, and when that
# pointed at Apple's /usr/bin/python3 (3.9.6) every run died on `datetime.UTC`, which is 3.11+.
# Five days of publications were lost that way. Fail here, naming the cause, rather than in the
# middle of main() with a traceback that reads like a code bug.
#
# UP036 is suppressed below: ruff reads `target-version = "py311"` and calls this block dead. It
# is dead for every caller ruff can see, and it was precisely the caller it cannot see that broke.
# The same ruff pass that modernised `dt.timezone.utc` into `dt.UTC` is what made an out-of-repo
# 3.9 interpreter fatal. The check stays.
if sys.version_info < (3, 11):  # noqa: UP036
    raise SystemExit(
        f"archive_delta.py needs Python 3.11+; got {sys.version.split()[0]} at {sys.executable}. "
        "Point the caller (launchd plist ProgramArguments, cron, CI) at a 3.11 interpreter."
    )

SOURCES = {
    # name              url                                                                     ext
    "delta": ("https://sanctionslistservice.ofac.treas.gov/changes/latest", "xml"),
    "sdn": ("https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML", "xml"),
}
UA = "interdict-archiver/1.0 (hackathon project; contact via repo)"
TIMEOUT = 180


def fetch(url: str) -> bytes:
    """Fetch following redirects. OFAC 302s to a presigned S3 URL that expires in 1h --
    never cache the redirect target, only the source URL."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # follows redirects by default
        return resp.read()


def already_have(index: dict, name: str, digest: str) -> bool:
    return any(e["sha256"] == digest for e in index.get(name, []))


class Outcome(NamedTuple):
    """What the trigger did, in a form the heartbeat can carry.

    A status string alone was not enough, and that is not a style point. For eleven days this
    function returned "re-screen FAILED, exit 1" into a gitignored log, twice, on the only two
    occasions it had anything to do -- and nothing downstream could tell that apart from
    "nothing new", because both were just text. `attempted` and `ok` are what let
    archive_status.py fail the gate instead of a human noticing.

    attempted -- a child process was actually started, or we tried and could not.
    ok        -- nothing is known to be broken. True for the skipped cases: not armed, nothing
                 new, and already-running are all correct behaviour, not failures.
    """

    status: str
    attempted: bool
    ok: bool


def trigger_rescreen(new_sources: set[str], root: Path) -> Outcome:
    """Start a re-screen because Treasury published something we have not seen.

    Returns an Outcome for the poll log and the heartbeat. Never raises: archiving is the
    guarantee this script makes, and a re-screen that cannot start must not cost us the
    publication.

    The lock is the reason this is safe on a six-hourly timer. A full-book re-screen against
    free-tier Gemini takes tens of minutes, so a second publication -- or a manual run -- can
    easily land while one is still going. Two concurrent re-screens would both write decisions
    for the same counterparties. flock is held for the child's whole life by holding the fd
    open in the parent, and a stale lock cannot outlive the process because the kernel drops it.
    """
    mode = os.environ.get("INTERDICT_RESCREEN_ON_NEW", "").lower()
    if mode not in {"1", "true", "yes", "dry"}:
        return Outcome("not armed (set INTERDICT_RESCREEN_ON_NEW=1)", attempted=False, ok=True)
    if not new_sources:
        return Outcome("nothing new", attempted=False, ok=True)

    cmd = [sys.executable, str(Path(__file__).with_name("run_rescreen.py")), "--trigger", "SCHEDULER"]
    if mode == "dry":
        return Outcome("DRY RUN, would spawn: " + " ".join(cmd), attempted=False, ok=True)

    lock_path = root / "rescreen.lock"
    try:
        # 0o600, not 0o644: nothing but this process needs to read the lock, and a
        # world-readable file in the archive directory is the kind of default that is
        # only ever noticed by a scanner. CodeQL py/overly-permissive-file, high.
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        return Outcome(f"could not open {lock_path}: {exc}", attempted=True, ok=False)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        return Outcome("a re-screen is already running; not starting a second", attempted=False, ok=True)

    try:
        proc = subprocess.Popen(cmd, cwd=Path(__file__).resolve().parents[1],
                                stdout=sys.stdout, stderr=sys.stderr)
    except OSError as exc:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        return Outcome(f"failed to spawn: {exc}", attempted=True, ok=False)

    rc = proc.wait()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    if rc == 0:
        return Outcome(f"re-screen finished, exit {rc}", attempted=True, ok=True)
    return Outcome(f"re-screen FAILED, exit {rc}", attempted=True, ok=False)


def build_heartbeat(prev: dict, *, now: str, invoker: str, new_count: int,
                    failures: int, failed: set[str]) -> dict:
    """Build the heartbeat for this poll, carrying forward what must outlive it.

    Split out of main() to be testable. It is pure -- no clock, no environment, no disk --
    because the bug it now guards against is a state transition between two polls, and a
    function that reached for the real clock could not express "the next poll, six hours
    later, found nothing to do".

    THE CARRY-FORWARD, and why it is the third act of the same bug. This heartbeat is written
    BEFORE the re-screen runs, deliberately: a re-screen takes tens of minutes and the gate
    must not call the archiver stale while it works. The outcome is merged in afterwards --
    but only `if attempted`, because "nothing new" must never look like a fault. Those two
    correct decisions compose into a wrong one. A failed re-screen survived only until the
    next poll with nothing to do, and then this rebuild dropped it on the floor.

    That is not hypothetical. On 2026-08-28T22:35Z Treasury published, the armed trigger
    fired, and the child died on a missing Gemini API key. At 2026-08-29T04:41Z the next
    scheduled poll found nothing new and erased the record, and `make archive-status` printed
    OK over an unresolved failure -- the third time this loop has been broken and green at the
    same time, after a wrong interpreter and a wrong venv.

    So the record is sticky. It clears when an attempt SUCCEEDS, not when time passes and not
    when a later poll finds nothing to do: a quiet poll is not evidence that the action leg
    was repaired, and reading it as such is the exact silence this gate exists to break.
    Absent stays absent -- a heartbeat with no `rescreen` key still means "never attempted",
    which remains the honest reading of nothing having happened yet.
    """
    streaks = prev.get("consecutive_failures", {})
    heartbeat = {
        "utc": now,
        "invoked_by": invoker,
        "last_scheduled_utc": now if invoker == "launchd" else prev.get("last_scheduled_utc"),
        "new": new_count,
        "failures": failures,
        "sources": sorted(SOURCES),
        "consecutive_failures": {
            name: (streaks.get(name, 0) + 1 if name in failed else 0) for name in SOURCES
        },
    }
    if "rescreen" in prev:
        heartbeat["rescreen"] = prev["rescreen"]
    return heartbeat


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a full disk cannot leave a half-written manifest.

    `Path.write_text` truncates first. A disk that fills mid-write -- one of the failure modes
    this archiver is supposed to survive -- would destroy index.json, which is the record of
    everything ever captured and is the file data/PROVENANCE.md points at.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=Path("data/archive"))
    args = ap.parse_args()

    root: Path = args.dir
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / "index.json"
    index: dict = json.loads(index_path.read_text()) if index_path.exists() else {}

    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    new_count = 0
    failures = 0
    failed: set[str] = set()
    new_sources: set[str] = set()

    for name, (url, ext) in SOURCES.items():
        try:
            body = fetch(url)
        except Exception as exc:  # a transient OFAC/network failure must never kill the poll
            print(f"  {name:6} FETCH FAILED: {exc}", file=sys.stderr)
            failures += 1
            failed.add(name)
            continue

        digest = hashlib.sha256(body).hexdigest()
        if already_have(index, name, digest):
            print(f"  {name:6} unchanged ({digest[:12]}, {len(body):,} bytes)")
            continue

        out = root / f"{name}-{now.replace(':', '').replace('-', '')}-{digest[:12]}.{ext}"
        out.write_bytes(body)
        index.setdefault(name, []).append(
            {"sha256": digest, "bytes": len(body), "fetched_utc": now, "file": out.name, "url": url}
        )
        new_count += 1
        new_sources.add(name)
        print(f"  {name:6} NEW -> {out.name} ({len(body):,} bytes)")

    write_atomic(index_path, json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(f"{now}  new={new_count}  failures={failures}  archive={root}")

    # The heartbeat, and the reason it is separate from index.json. index.json only changes
    # when OFAC publishes something new, which is irregular -- days can pass between entries
    # while the archiver is perfectly healthy, so its timestamps cannot answer "is this thing
    # still running". This file is written on EVERY poll and answers exactly that, which is
    # what `make archive-status` reads. Gitignored, like the log: it is operational state, not
    # a deliverable, and committing it would dirty the tree every six hours.
    #
    # It records WHO invoked this, and carries forward the last scheduled poll separately.
    # Without that, running this script by hand resets the freshness clock for twelve hours,
    # so the gate cannot tell "the timer is alive" from "a human ran it once" -- which is the
    # only distinction it was built to make. The launchd plist sets INTERDICT_ARCHIVER_INVOKER.
    prev = {}
    hb_path = root / "last-poll.json"
    if hb_path.exists():
        try:
            prev = json.loads(hb_path.read_text())
        except (OSError, ValueError):
            prev = {}
    invoker = os.environ.get("INTERDICT_ARCHIVER_INVOKER", "manual")
    heartbeat = build_heartbeat(
        prev, now=now, invoker=invoker, new_count=new_count, failures=failures, failed=failed
    )
    write_atomic(hb_path, json.dumps(heartbeat, indent=2, sort_keys=True) + "\n")

    # The loop closes here, after the heartbeat is on disk. Order matters: the re-screen can run
    # for tens of minutes, and if the heartbeat were written afterwards `make archive-status`
    # would call the archiver stale for the whole time it was doing exactly what it should.
    outcome = trigger_rescreen(new_sources, root)
    print(f"  rescreen: {outcome.status}")

    # Second heartbeat write, and it has to be second. The first one above is deliberately
    # written BEFORE the re-screen so a run that legitimately takes tens of minutes is not
    # called stale while it works. But that ordering is also why a failed trigger left no
    # trace anywhere a check could read: by the time the outcome existed, the heartbeat was
    # already on disk. So the outcome is merged in afterwards, keeping every freshness field
    # exactly as written above.
    #
    # Only ATTEMPTED runs move this. "not armed", "nothing new" and "already running" are
    # correct behaviour and must not look like a fault -- a gate that cries wolf on the
    # common path gets ignored, which is how the original failure survived eleven days.
    if outcome.attempted:
        try:
            hb = json.loads(hb_path.read_text())
        except (OSError, ValueError):
            hb = {}
        prev_rescreen = prev.get("rescreen") or {}
        hb["rescreen"] = {
            "utc": now,
            "status": outcome.status,
            "ok": outcome.ok,
            "consecutive_failures": 0 if outcome.ok else int(prev_rescreen.get("consecutive_failures", 0)) + 1,
        }
        write_atomic(hb_path, json.dumps(hb, indent=2, sort_keys=True) + "\n")

    # Non-zero only if everything failed -- a partial poll is still a successful poll, and one
    # transient reset should not mark the scheduled job failed. A source that keeps failing is
    # caught by the streak above rather than by this exit code, which cannot distinguish
    # "OFAC blipped once" from "this endpoint has been gone for a week".
    return 1 if failures == len(SOURCES) else 0


if __name__ == "__main__":
    raise SystemExit(main())
