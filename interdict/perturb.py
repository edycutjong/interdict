"""Deterministic name perturbation -- the anti-circularity harness.

WHY THIS EXISTS. Screening the sentinel book against the SDN list scores top-1 = 1.000,
and that number is worth almost nothing: the sentinels are names copied verbatim out of
the very publication being searched, so finding them is a string-equality test wearing a
costume. Round-1 audit called this hit-circularity, and reporting it as a quality result
would be exactly the self-grading the whole oracle design exists to prevent.

Real sanctions evasion does not hand you the spelling on the list. It hands you a
transliteration, a reordering, a dropped particle, a typo'd passport transcription. So
the honest question is: *given a name that is not on the list character-for-character,
do we still find the right person?* That is what this module manufactures and what
`make challenge` screens.

DETERMINISM. There is no RNG anywhere here. Which perturbation a name receives is
derived from the SHA-256 of the name itself, so the challenge set is byte-identical on
any machine, in any order, on any day -- reproducible by a judge, and stable enough that
an agreement number means something across runs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .normalize import normalize

# Real transliteration families. These are not invented: they are the spelling variants
# that appear across OFAC's own aka lists for the same individuals, which is why
# screening systems are expected to survive them.
_TRANSLITERATIONS: list[tuple[str, str]] = [
    ("MUHAMMAD", "MOHAMMED"), ("MUHAMMAD", "MOHAMED"), ("MUHAMMAD", "MUHAMED"),
    ("ABDUL", "ABD AL"), ("ABDUL", "ABDEL"), ("ABD AL", "ABDUL"),
    ("HUSSEIN", "HUSAYN"), ("HUSSEIN", "HUSSAIN"),
    ("YUSUF", "YOUSEF"), ("YUSUF", "JOSEPH"),
    ("IBRAHIM", "IBRAHEEM"), ("KHALID", "KHALED"),
    ("OMAR", "UMAR"), ("USAMA", "OSAMA"), ("HASAN", "HASSAN"),
    ("SHAYKH", "SHEIKH"), ("AHMAD", "AHMED"), ("MAHMUD", "MAHMOUD"),
    ("ALI", "ALY"), ("SAID", "SAEED"), ("JAMAL", "GAMAL"),
    ("AL ", "EL "), ("ZAYDAN", "ZAIDAN"), ("RASHID", "RACHID"),
]

# Latin-script neighbours: how a name gets mangled in transcription, not on a keyboard.
_CONFUSABLE = {"I": "Y", "Y": "I", "K": "C", "C": "K", "S": "Z", "Z": "S",
               "F": "PH", "U": "OU", "EE": "I", "OO": "U"}


@dataclass(frozen=True)
class Perturbation:
    original: str
    perturbed: str
    kind: str
    sdn_uid: str

    @property
    def changed(self) -> bool:
        return normalize(self.original) != normalize(self.perturbed)


def _digest(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")


def _transliterate(name: str, seed: int) -> tuple[str, str]:
    upper = normalize(name)
    applicable = [(a, b) for a, b in _TRANSLITERATIONS if a in upper]
    if not applicable:
        return name, "none"
    src, dst = applicable[seed % len(applicable)]
    return upper.replace(src, dst, 1), "transliteration"


def _reorder(name: str, seed: int) -> tuple[str, str]:
    parts = normalize(name).split()
    if len(parts) < 2:
        return name, "none"
    # Rotate rather than shuffle: deterministic, and it models the surname-first /
    # surname-last convention clash that actually causes screening misses.
    k = 1 + (seed % (len(parts) - 1))
    return " ".join(parts[k:] + parts[:k]), "reorder"


def _drop_particle(name: str, seed: int) -> tuple[str, str]:
    parts = normalize(name).split()
    particles = [i for i, p in enumerate(parts) if p in {"AL", "EL", "BIN", "BEN", "IBN", "ABD"}]
    if not particles or len(parts) <= 2:
        return name, "none"
    idx = particles[seed % len(particles)]
    return " ".join(parts[:idx] + parts[idx + 1:]), "drop_particle"


def _typo(name: str, seed: int) -> tuple[str, str]:
    upper = normalize(name)
    letters = [i for i, ch in enumerate(upper) if ch.isalpha()]
    if len(letters) < 4:
        return name, "none"
    # Avoid the first character: a first-letter typo defeats prefix blocking for
    # everyone, and would measure the blocking key rather than the scorer.
    pos = letters[1 + (seed % (len(letters) - 1))]
    ch = upper[pos]
    repl = _CONFUSABLE.get(ch)
    if not repl:
        # Character transposition -- the most common transcription error there is.
        if pos + 1 < len(upper):
            return upper[:pos] + upper[pos + 1] + upper[pos] + upper[pos + 2:], "transpose"
        return name, "none"
    return upper[:pos] + repl + upper[pos + 1:], "confusable"


def _truncate_middle(name: str, seed: int) -> tuple[str, str]:
    """Drop a middle name -- extremely common in payment instructions."""
    parts = normalize(name).split()
    if len(parts) < 3:
        return name, "none"
    idx = 1 + (seed % (len(parts) - 2))
    return " ".join(parts[:idx] + parts[idx + 1:]), "drop_middle"


_STRATEGIES = (_transliterate, _reorder, _drop_particle, _typo, _truncate_middle)


def perturb(name: str, sdn_uid: str = "", strategy: str | None = None) -> Perturbation:
    """Produce one deterministic variant of `name`.

    Falls through the strategy list until one actually changes the name, so a name with
    no particles still yields a real variant rather than a silent no-op that would
    inflate the score.
    """
    seed = _digest(name)

    if strategy:
        fn = {"transliteration": _transliterate, "reorder": _reorder,
              "drop_particle": _drop_particle, "typo": _typo,
              "drop_middle": _truncate_middle}[strategy]
        out, kind = fn(name, seed)
        return Perturbation(name, out, kind, sdn_uid)

    start = seed % len(_STRATEGIES)
    for offset in range(len(_STRATEGIES)):
        out, kind = _STRATEGIES[(start + offset) % len(_STRATEGIES)](name, seed)
        if kind != "none" and normalize(out) != normalize(name):
            return Perturbation(name, out, kind, sdn_uid)

    return Perturbation(name, name, "unperturbable", sdn_uid)
