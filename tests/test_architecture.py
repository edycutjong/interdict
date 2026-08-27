"""Import-boundary tests -- the separation of concerns, asserted instead of assumed.

Every other test in this suite checks what a module *does*. This one checks what a module
is *allowed to reach*, because the strongest safety property in this system is not a
behaviour at all -- it is an absence.

`adjudicator.py` is the only module that talks to a language model. It does not import
`money` and it does not import `db`, so the model cannot hold a disbursement, cannot clear
a counterparty, and cannot write to the hash-chained ledger. That is not a policy, a prompt
instruction or a code review convention -- all three of which fail quietly. It is a fact
about the import graph, and it is the reason a hallucinated verdict is survivable.

The problem with a fact about the import graph is that nothing announces when it stops
being true. Adding `from .money import release` to `adjudicator.py` leaves all 343 other
tests green while deleting the guarantee the design is built on. So the graph is asserted
here, statically, with `ast` -- no module is imported to check it, because importing to
test importability is circular and because a module that fails to import should fail its
own tests, not this file.

Read the rules below as the architecture diagram in executable form. If one of them starts
failing, either the change is wrong or the diagram is out of date; both are worth stopping
for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "interdict"

# The decision path. Everything here is a deliberate edge, not an accident of convenience.
#
# adjudicator  -> matcher                           the model. Reaches no money, no db.
# oracle       -> (nothing)                         the guard. Reaches nothing at all.
# cloud        -> (nothing)                         the mirror. Never a source of truth.
# money        -> businessdays, db                  the ledger-facing side.
# orchestrator -> adjudicator, db, matcher, money   the only composer of the three.


def _internal_imports(path: Path) -> set[str]:
    """Names inside the `interdict` package that `path` imports, directly.

    Handles the three spellings that occur in this codebase and the two that do not yet
    but would silently defeat the check if they ever did: `from .x import y`,
    `from . import x`, `from interdict.x import y`, and `import interdict.x`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:            # from .matcher import Match
                found.add(node.module.split(".")[0])
            elif node.level or node.module == "interdict":
                # `from . import matcher` and `from interdict import matcher`. In both
                # spellings the imported names ARE the modules, so they are read off
                # `names` rather than off `module`.
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif node.module and node.module.startswith("interdict."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):            # import interdict.matcher
            for alias in node.names:
                if alias.name.startswith("interdict."):
                    found.add(alias.name.split(".")[1])

    return found - {"__init__"}


def _graph() -> dict[str, set[str]]:
    return {
        path.stem: _internal_imports(path)
        for path in sorted(PACKAGE.glob("*.py"))
        if path.stem != "__init__"
    }


def _reachable(start: str, graph: dict[str, set[str]]) -> set[str]:
    """Everything `start` can reach, transitively.

    Transitive rather than direct on purpose: `adjudicator -> helper -> money` hides the
    forbidden edge one hop deeper, and would pass a check that only read direct imports.
    """
    seen: set[str] = set()
    stack = list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node in seen or node not in graph:
            continue
        seen.add(node)
        stack.extend(graph[node])
    return seen


def test_the_module_graph_is_acyclic_and_every_edge_resolves():
    """A cycle here would mean two modules own the same decision. None may exist."""
    graph = _graph()
    for module, imports in graph.items():
        unknown = imports - graph.keys()
        assert not unknown, f"{module} imports {unknown}, which is not in the package"
        assert module not in _reachable(module, graph), f"{module} is part of an import cycle"


@pytest.mark.parametrize("forbidden", ["money", "db"])
def test_the_adjudicator_cannot_reach_the_money_or_the_ledger(forbidden):
    """The model's blast radius, asserted.

    This is the test that matters. A language model that cannot import the module that
    moves money cannot move money, however wrong its verdict is. If this fails, the
    project's central safety claim -- "a model verdict is not a decision" -- is no longer
    structurally true, and the README says something the code does not.
    """
    graph = _graph()
    reachable = _reachable("adjudicator", graph)
    assert forbidden not in reachable, (
        f"adjudicator can now reach {forbidden!r} via {sorted(reachable)}. "
        "The model must not be able to hold, clear or record anything."
    )


@pytest.mark.parametrize("forbidden", ["money", "db"])
def test_the_matcher_cannot_reach_the_money_or_the_ledger(forbidden):
    """Screening proposes; it never disposes. Same argument, one layer down."""
    assert forbidden not in _reachable("matcher", _graph())


def test_the_oracle_is_fully_isolated():
    """The independent check has to be independent.

    yente is the second opinion on every verdict. An oracle that imported our matcher, our
    thresholds or our normalisation would be agreeing with itself through a longer path,
    and the comparison in the README would be measuring nothing.
    """
    assert _internal_imports(PACKAGE / "oracle.py") == set()


def test_the_cloud_mirror_imports_nothing_from_the_package():
    """Firestore is a mirror and never a source of truth.

    Keeping `cloud` a leaf is what makes that sentence enforceable: a module that imports
    nothing cannot be consulted by the decision path, only written to by it.
    """
    assert _internal_imports(PACKAGE / "cloud.py") == set()


def test_only_the_composer_reaches_the_money():
    """Exactly one module is allowed to route between the three agents.

    `orchestrator` is that module. Concentrating the authority in one place is what makes
    the routing auditable at all -- if any module could reach `money`, "who released this?"
    would have no single answer.
    """
    graph = _graph()
    holders = {m for m in graph if "money" in _reachable(m, graph)}
    assert holders == {"orchestrator", "rescreen"}, (
        f"modules reaching money changed: {sorted(holders)}. "
        "rescreen is the entrypoint and reaches it only through orchestrator."
    )
