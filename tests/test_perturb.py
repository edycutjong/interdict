"""Perturbation-harness tests.

The whole value of this harness is that it is reproducible by someone who does not
trust us, so determinism is tested harder than the perturbations themselves.
"""

from interdict.normalize import blocking_keys, normalize
from interdict.perturb import _CONFUSABLE, perturb


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


# ---------------------------------------------------------------------------
# The determinism claim, pinned
# ---------------------------------------------------------------------------

# Byte-for-byte expected output of the fall-through cascade. The README promises the
# challenge set is identical on any machine on any day; this table is what that promise
# means concretely. Any change to a strategy, to the strategy order, or to the seeding
# breaks it -- which is the point: the number a judge reproduces must be the same number.
GOLDEN = [
    ("Ibrahim Al Rashid", "AL RASHID IBRAHIM", "reorder"),
    ("Muhammad Hussein Ali", "ALI MUHAMMAD HUSSEIN", "reorder"),
    ("Abu Abbas", "ABBAS ABU", "reorder"),
    ("Shining Path", "PATH SHINING", "reorder"),
    ("Acme Trading Limited", "ACME TRADIN GLIMITED", "transpose"),
    ("Muhammad Zaydan", "MOHAMMED ZAYDAN", "transliteration"),
    ("Peter Jones", "JONES PETER", "reorder"),
    ("Abd Al Rahman Al Tikriti", "ABD AL RAHMAN TIKRITI", "drop_particle"),
]


def test_the_challenge_set_is_byte_identical_to_its_published_values():
    for name, expected, kind in GOLDEN:
        p = perturb(name, sdn_uid="X")
        assert p.perturbed == expected, f"{name!r} drifted: {p.perturbed!r}"
        assert p.kind == kind


def test_different_names_receive_different_perturbations():
    """A harness that maps many names onto one variant would measure nothing."""
    outputs = {perturb(name).perturbed for name, _, _ in GOLDEN}
    assert len(outputs) == len(GOLDEN)


def test_the_sdn_uid_is_carried_through_untouched():
    """The variant is worthless as evidence if it loses the record it came from."""
    p = perturb("Ibrahim Al Rashid", sdn_uid="12345")
    assert p.sdn_uid == "12345" and p.original == "Ibrahim Al Rashid"


# ---------------------------------------------------------------------------
# Each strategy: when it fires, and when it honestly declines
# ---------------------------------------------------------------------------
# A strategy that cannot apply must report kind "none" and hand the name back
# unmodified. Returning a fabricated or silently-unchanged variant is the failure that
# matters here: the first would test a spelling nobody uses, the second would enter the
# challenge set as a free hit and inflate recall.


def test_transliteration_declines_a_name_with_no_alias_family():
    p = perturb("Peter Jones", strategy="transliteration")
    assert p.kind == "none"
    assert p.perturbed == "Peter Jones" and not p.changed


def test_transliteration_uses_a_real_ofac_alias_family():
    p = perturb("Muhammad Zaydan", strategy="transliteration")
    assert p.kind == "transliteration"
    assert p.perturbed == "MOHAMMED ZAYDAN"


def test_reorder_declines_a_single_token_name():
    """There is no surname-first/surname-last clash to model with one token."""
    p = perturb("Rashid", strategy="reorder")
    assert p.kind == "none"
    assert p.perturbed == "Rashid" and not p.changed


def test_drop_particle_removes_the_particle_and_keeps_every_other_token_in_order():
    p = perturb("Ibrahim Al Rashid Hussein", strategy="drop_particle")
    assert p.kind == "drop_particle"
    assert p.perturbed == "IBRAHIM RASHID HUSSEIN"


def test_drop_particle_removes_exactly_one_of_several_particles():
    """ABD and AL are both particles; dropping both would be a different person."""
    p = perturb("Abd Al Rahman Khalid", strategy="drop_particle")
    assert p.kind == "drop_particle"
    dropped = [t for t in normalize(p.original).split() if t not in p.perturbed.split()]
    assert len(p.perturbed.split()) == 3
    assert dropped in (["ABD"], ["AL"])


def test_drop_particle_declines_a_name_with_no_particle():
    p = perturb("Peter Jones", strategy="drop_particle")
    assert p.kind == "none" and not p.changed


def test_typo_declines_a_name_too_short_to_damage_safely():
    """Under four letters the only edit left is the first one, which is forbidden."""
    p = perturb("Li", strategy="typo")
    assert p.kind == "none"
    assert p.perturbed == "Li" and not p.changed


def test_typo_declines_when_the_chosen_position_is_the_final_character():
    """There is no neighbour to swap with past the end, so it must not fake one."""
    p = perturb("Muhammad Zaydan", strategy="typo")
    assert p.kind == "none"
    assert p.perturbed == "Muhammad Zaydan" and not p.changed


def test_typo_substitutes_a_transcription_confusable_from_the_published_table():
    p = perturb("Peter Jones", strategy="typo")
    assert p.kind == "confusable"
    original = normalize(p.original)
    # strict=True on purpose: a confusable is a SUBSTITUTION, so the two strings must
    # be the same length. Without it a length change would be silently truncated away
    # and this test would still pass while the perturbation had become something else.
    diffs = [
        i
        for i, (a, b) in enumerate(zip(original, p.perturbed, strict=True))
        if a != b
    ]
    assert len(diffs) == 1, f"confusable must edit one site, got {diffs}"
    pos = diffs[0]
    repl = _CONFUSABLE[original[pos]]
    assert p.perturbed == original[:pos] + repl + original[pos + 1:]


def test_drop_middle_declines_a_name_with_no_middle_name():
    p = perturb("Peter Jones", strategy="drop_middle")
    assert p.kind == "none"
    assert p.perturbed == "Peter Jones" and not p.changed


def test_a_name_no_strategy_can_touch_is_labelled_rather_than_silently_passed_through():
    """The honesty valve: an unperturbed name must be excludable from the scoring set.

    "LI" has no alias family, one token, no particle, no middle name and too few letters
    to typo. Every strategy declines. If that fell through as a normal Perturbation the
    screener would score a verbatim string-equality hit and the challenge number would
    quietly become the circular one this module exists to replace.
    """
    p = perturb("Li", sdn_uid="9")
    assert p.kind == "unperturbable"
    assert p.perturbed == "Li" and not p.changed
    assert p.sdn_uid == "9"


def test_every_kind_the_cascade_emits_is_a_real_change():
    """The cascade's contract: it only stops on a strategy that actually changed something."""
    for name in ("Ibrahim Al Rashid", "Muhammad Zaydan", "Peter Jones",
                 "Acme Trading Limited", "Abd Al Rahman Al Tikriti", "Li"):
        p = perturb(name)
        assert p.changed == (p.kind != "unperturbable"), f"{name!r} -> {p.kind}"
