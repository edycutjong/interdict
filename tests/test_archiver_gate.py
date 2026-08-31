"""The archiver liveness gate, including the leg it used to be blind to.

`archive_status.py` exists because an unattended job with no liveness check is not
unattended, it is unobserved -- its own docstring says so, after a five-day outage that
nothing noticed. It then spent eleven days proving the point a second time in the other
direction: it checked that the POLL was alive and never checked that the poll ever DID
anything. The archiver ran 37 times, the gate said OK 37 times, and both re-screens it
actually attempted died on `ModuleNotFoundError: psycopg` because the launchd plist pointed
at an interpreter with no project dependencies.

These tests pin the behaviour that closes that hole: an attempted re-screen that failed
fails the gate, and the common cases that are *not* failures do not. The second half matters
as much as the first -- a gate that cries wolf on "nothing new" gets ignored, and being
ignored is how the original bug survived.

The gate is run as a subprocess rather than imported, because its contract is the exit code.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "archive_status.py"


def _heartbeat(tmp_path: Path, **overrides) -> Path:
    """A heartbeat that passes every check, so a test only varies the thing it is about."""
    archive = tmp_path / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC) - timedelta(minutes=5)
    hb = {
        "utc": now.isoformat(),
        "invoked_by": "launchd",
        "last_scheduled_utc": now.isoformat(),
        "new": 0,
        "failures": 0,
        "sources": ["delta", "sdn"],
        "consecutive_failures": {"delta": 0, "sdn": 0},
    }
    hb.update(overrides)
    (archive / "last-poll.json").write_text(json.dumps(hb, indent=2), encoding="utf-8")
    return archive


def _run(archive: Path, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Heartbeat tests pass --no-check-deps: they are about the heartbeat, and must not
    pass or fail depending on whether docker happens to be up on the machine running them."""
    return subprocess.run(
        [sys.executable, str(GATE), "--archive", str(archive), "--no-check-deps", *extra],
        capture_output=True, text=True, check=False,
        env={**os.environ, **(env or {})},
    )


def _free_port() -> int:
    """A port with nothing on it. Bind, read the number back, close -- so the probe under
    test gets a refused connection rather than a timeout against something real."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_healthy_archiver_passes(tmp_path):
    """The baseline. If this fails, every other assertion here is meaningless."""
    assert _run(_heartbeat(tmp_path)).returncode == 0


def test_a_failed_rescreen_fails_the_gate(tmp_path):
    """THE regression. This exact heartbeat is what 2026-08-26 would have written."""
    archive = _heartbeat(tmp_path, rescreen={
        "utc": datetime.now(UTC).isoformat(),
        "status": "re-screen FAILED, exit 1",
        "ok": False,
        "consecutive_failures": 1,
    })
    res = _run(archive)
    assert res.returncode != 0, "a failed re-screen must not pass the liveness gate"
    assert "re-screen" in (res.stdout + res.stderr).lower()

    # ...and it must still fail six hours later. Failing the gate is worth nothing if the
    # next quiet poll erases the evidence, which is exactly what happened on 2026-08-29:
    # the 04:41 poll found nothing new, rebuilt the heartbeat without the `rescreen` key,
    # and this gate went green over a re-screen that had died on a missing API key.
    # Asserted here rather than in its own test because it is the same regression -- the
    # gate catching a failure and the failure surviving to be caught are one guarantee.
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import archive_delta
    finally:
        sys.path.pop(0)

    prev = json.loads((archive / "last-poll.json").read_text())
    quiet = archive_delta.build_heartbeat(
        prev, now=datetime.now(UTC).isoformat(), invoker="launchd",
        new_count=0, failures=0, failed=set(),
    )
    assert quiet["rescreen"] == prev["rescreen"], (
        "a poll with nothing to do erased the failed re-screen -- the gate is blind again"
    )
    (archive / "last-poll.json").write_text(json.dumps(quiet, indent=2), encoding="utf-8")
    assert _run(archive).returncode != 0, "the failure must outlive the poll that follows it"


def test_a_successful_rescreen_passes(tmp_path):
    archive = _heartbeat(tmp_path, rescreen={
        "utc": datetime.now(UTC).isoformat(),
        "status": "re-screen finished, exit 0",
        "ok": True,
        "consecutive_failures": 0,
    })
    assert _run(archive).returncode == 0


def test_no_rescreen_key_still_passes(tmp_path):
    """Absence is not failure here, and this asymmetry is deliberate.

    A heartbeat with no `rescreen` key is one written before a re-screen was ever attempted --
    the state of every poll where OFAC published nothing, which is most of them. Treating that
    as a fault would fail the gate permanently on a perfectly healthy archiver.

    Note this is the opposite call from `consecutive_failures`, where a missing key IS fatal.
    That one is written on every single poll, so its absence means a partial or stale writer;
    this one is written only when there was something to do.
    """
    assert _run(_heartbeat(tmp_path)).returncode == 0


@pytest.fixture
def archive_delta():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import archive_delta as mod
        return mod
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize(
    "armed,new_sources,why",
    [
        ("", {"delta"}, "not armed -- the trigger is opt-in and off is not a fault"),
        ("1", set(), "armed but OFAC published nothing, which is most polls"),
        ("dry", {"delta"}, "dry run logs the command and spawns nothing"),
    ],
)
def test_the_skipped_cases_never_look_like_a_failed_attempt(
    archive_delta, monkeypatch, tmp_path, armed, new_sources, why
):
    """Calls the real trigger, so this cannot drift from the function it describes.

    Each of these is correct behaviour. If any were recorded as `attempted`, the gate would
    fail on a healthy archiver every six hours -- and a gate that cries wolf on the common
    path gets ignored, which is precisely how the original failure survived eleven days.
    No subprocess is ever spawned on these paths, which is why this is safe to run in CI.
    """
    monkeypatch.setenv("INTERDICT_RESCREEN_ON_NEW", armed)
    outcome = archive_delta.trigger_rescreen(new_sources, tmp_path)
    assert outcome.attempted is False, why
    assert outcome.ok is True, why


def test_a_real_attempt_that_fails_is_marked_attempted_and_not_ok(
    archive_delta, monkeypatch, tmp_path
):
    """The other half: a spawn that cannot happen must be attempted=True, ok=False.

    Without this pairing the gate is unreachable -- `attempted` is what gets the outcome
    written into the heartbeat at all.
    """
    monkeypatch.setenv("INTERDICT_RESCREEN_ON_NEW", "1")

    def boom(*a, **k):
        raise OSError("no such interpreter")

    monkeypatch.setattr(archive_delta.subprocess, "Popen", boom)
    outcome = archive_delta.trigger_rescreen({"delta"}, tmp_path)
    assert outcome.attempted is True
    assert outcome.ok is False
    assert "failed to spawn" in outcome.status


# --- The dependency probe (outage #4, 2026-08-31) ---------------------------------------
#
# On 08-31 the whole docker stack was found stopped and this gate printed OK. Archiving is
# pure stdlib, so the polls kept arriving flawlessly while the ledger the re-screen writes to
# was not listening. The checks above could not see it: they prove the poll is alive and that
# no re-screen has *already* failed, and the next re-screen would have died at connect()
# before producing the failed attempt they watch for. These pin the layer that closes that.


def _run_with_deps(archive: Path, **env) -> subprocess.CompletedProcess:
    """The probe ON -- the real `make archive-status` path, which has no --no-check-deps."""
    return subprocess.run(
        [sys.executable, str(GATE), "--archive", str(archive)],
        capture_output=True, text=True, check=False,
        env={**os.environ, **env},
    )


def test_an_unreachable_ledger_fails_the_gate(tmp_path):
    """THE regression for 08-31. A dead ledger means the next re-screen cannot open a run."""
    result = _run_with_deps(
        _heartbeat(tmp_path),
        INTERDICT_DSN=f"postgresql://u:p@localhost:{_free_port()}/interdict",
    )
    assert result.returncode != 0, "a stopped ledger must not pass the liveness gate"
    assert "ledger is unreachable" in result.stderr


def test_an_unreachable_oracle_only_warns(tmp_path):
    """yente down is NOT a failure: run_rescreen.py:210 degrades to an ungraded oracle column
    and the run still completes. A gate that fails here cries wolf, and a gate that cries wolf
    is how the original five-day outage survived."""
    with socket.socket() as pg:
        pg.bind(("127.0.0.1", 0))
        pg.listen(1)  # a real listener, so only the oracle leg is down
        result = _run_with_deps(
            _heartbeat(tmp_path),
            INTERDICT_DSN=f"postgresql://u:p@localhost:{pg.getsockname()[1]}/interdict",
            YENTE_URL=f"http://localhost:{_free_port()}",
        )
    assert result.returncode == 0, "yente down must not fail the gate"
    assert "WARN oracle unreachable" in result.stdout


def test_the_probe_can_be_switched_off(tmp_path):
    """--no-check-deps is what lets the heartbeat tests run on a machine with no docker.
    If this regresses, every other test in this file starts depending on the host."""
    result = _run(_heartbeat(tmp_path),
                  env={"INTERDICT_DSN": f"postgresql://u:p@localhost:{_free_port()}/x"})
    assert result.returncode == 0
    assert "unreachable" not in result.stdout
