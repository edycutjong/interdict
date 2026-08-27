"""Version-integrity tests.

The version exists in four places that can silently drift apart: the package, the
changelog, package.json, and the git tag the release workflow writes. These tests assert
the first three agree and are well-formed, so a release that forgets one fails in CI
rather than shipping a tag nobody can read.

The git tag itself is deliberately not asserted here -- a fresh clone at an untagged
commit is a legitimate state, and a test that fails on it would fail for every
contributor before their first release.
"""

import json
import pathlib
import re

import interdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"

# The official SemVer 2.0.0 pattern, minus the named groups.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def test_version_is_valid_semver():
    assert SEMVER.match(interdict.__version__), (
        f"__version__ is {interdict.__version__!r}, which is not valid SemVer 2.0.0"
    )


def test_version_is_at_least_one_point_oh():
    major = int(interdict.__version__.split(".")[0])
    assert major >= 1, "the first tagged release is 1.0.0; the version must not go backwards"


def test_changelog_documents_the_current_version():
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = f"## [{interdict.__version__}]"
    assert heading in text, (
        f"CHANGELOG.md has no {heading} section. The release workflow writes one; "
        f"if you bumped __version__ by hand, add the section by hand too."
    )


def test_changelog_versions_are_ordered_newest_first():
    text = CHANGELOG.read_text(encoding="utf-8")
    found = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.MULTILINE)
    assert found, "CHANGELOG.md lists no released versions"
    keyed = [tuple(int(p) for p in v.split(".")) for v in found]
    assert keyed == sorted(keyed, reverse=True), (
        f"CHANGELOG.md versions are out of order: {found}"
    )


def test_changelog_has_no_duplicate_versions():
    text = CHANGELOG.read_text(encoding="utf-8")
    found = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.MULTILINE)
    assert len(found) == len(set(found)), f"CHANGELOG.md repeats a version: {found}"


def test_release_workflow_anchor_is_present():
    """The workflow inserts new sections at a literal marker. If the marker is renamed
    or deleted, every future release silently appends nothing -- catch it here."""
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "<!-- release-workflow inserts new sections directly below this line -->" in text


def test_package_json_version_matches_the_package():
    """package.json is the browser toolchain, not the product -- and it still has to agree.

    Two version numbers in one repository that disagree is a question a reader has to stop
    and resolve, and the one they check first is not always the one we remembered to bump.
    The release workflow rewrites this file; this test is what makes forgetting it loud.
    """
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == interdict.__version__, (
        f"package.json is {pkg['version']!r} but __version__ is {interdict.__version__!r}"
    )


def test_package_lock_agrees_with_package_json():
    """npm ci refuses a tree whose lockfile and manifest disagree.

    The release workflow bumps both. If only one moved, the e2e and Lighthouse jobs would
    fail on the release commit itself -- after the tag was already pushed. Fail here first.
    """
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == pkg["version"], "package-lock.json root version drifted"
    assert lock["packages"][""]["version"] == pkg["version"], (
        'package-lock.json packages[""] version drifted'
    )
