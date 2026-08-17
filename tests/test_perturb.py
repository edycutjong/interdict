"""Perturbation-harness tests.

The whole value of this harness is that it is reproducible by someone who does not
trust us, so determinism is tested harder than the perturbations themselves.
"""

from interdict.normalize import blocking_keys, normalize
from interdict.perturb import perturb


def test_perturbation_is_deterministic():
    a, b = perturb("Ibrahim Al Rashid"), perturb("Ibrahim Al Rashid")
    assert a.perturbed == b.perturbed and a.kind == b.kind


def test_perturbation_does_not_depend_on_call_order():
    first = perturb("Abu Abbas").perturbed
    for other in ("Muhammad Zaydan", "Shining Path", "Acme Trading"):
        perturb(other)
    assert perturb("Abu Abbas").perturbed == first


def test_perturbation_actually_changes_the_name():
    for name in ("Ibrahim Al Rashid", "Muhammad Hussein Ali",
                 "Acme Trading Limited", "Shining Path"):
        p = perturb(name)
        assert p.changed, f"{name!r} was not perturbed ({p.kind})"


def test_forced_strategies_produce_their_kind():
    p = perturb("Muhammad Hussein Ali", strategy="transliteration")
    assert p.kind == "transliteration"
    assert normalize(p.perturbed) != normalize(p.original)


def test_reorder_preserves_token_multiset():
    p = perturb("Ibrahim Hussein Rashid", strategy="reorder")
    assert sorted(p.perturbed.split()) == sorted(normalize(p.original).split())


def test_drop_middle_removes_exactly_one_token():
    p = perturb("Muhammad Hussein Ali Rashid", strategy="drop_middle")
    assert len(p.perturbed.split()) == len(normalize(p.original).split()) - 1


def test_typo_keeps_first_character():
    """A first-letter change would measure the blocking key, not the scorer."""
    p = perturb("Ibrahim Rashid", strategy="typo")
    if p.changed:
        assert p.perturbed[0] == normalize(p.original)[0]


# ---------------------------------------------------------------------------
# The blocking families added after the perturbed challenge run
# ---------------------------------------------------------------------------

def test_transposition_still_blocks_together():
    """INETRNACIONAL vs INTERNACIONAL -- no shared 4-char prefix.

    This was 4 of the 7 misses in the first perturbed run; the sorted-character key
    exists specifically to make them collide.
    """
    assert blocking_keys("G G INTERNACIONAL S A S") & blocking_keys("G G INETRNACIONAL S A S")


def test_single_substitution_still_blocks_together():
    assert blocking_keys("XZAKT INC") & blocking_keys("XZACT INC")


def test_single_deletion_still_blocks_together():
    assert blocking_keys("PANIA AG") & blocking_keys("PANA AG")


def test_unrelated_names_do_not_share_blocking_keys():
    """The generous keys must not collapse into matching everything with everything."""
    assert not (blocking_keys("Jennifer Marie Thompson") & blocking_keys("Shining Path"))


def test_blocking_key_count_stays_bounded():
    """Deletion neighbourhoods are O(len) per token; keep the index from exploding."""
    keys = blocking_keys("Muhammad Hussein Abd Al Rahman Al Tikriti")
    assert len(keys) < 120
