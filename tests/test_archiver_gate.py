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


def _run(archive: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--archive", str(archive)],
        capture_output=True, text=True, check=False,
    )


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
