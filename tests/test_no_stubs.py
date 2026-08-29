"""The anti-zombie guard (PLAN.md Wave 0 / "The anti-zombie guard").

Wave 0 intentionally left every non-schema file in src/ and eval/ as a
`raise NotImplementedError` stub with a real docstring — that was by design,
so that seven Wave 1 lanes could code against frozen interfaces in parallel
without blocking on a full implementation existing yet.

This test is what stops that from becoming permanent. It was marked `xfail`
during Wave 1; **as of Wave 2 it is a hard failure** (PLAN.md Wave 2.1:
"Flip the NotImplementedError guard to hard-fail"). Any remaining
unimplemented stub in src/ or eval/ is now a real integration gap, not an
expected placeholder, and CI must catch it (FR9.4, PLAN.md Wave 2 exit
criterion "Zero NotImplementedError in src/ or eval/").

Why this is AST-based and not a substring grep
----------------------------------------------
The Wave 0 sketch grepped for the literal string "NotImplementedError" in
every .py file. That over-matches: by the end of Wave 1 it flagged three
files that contain no stub at all —

- `src/api.py` — `except NotImplementedError:` — a *handler* for an arm that
  is not wired yet, i.e. the opposite of an unimplemented stub.
- `eval/run_eval.py` — the identifier appears twice in *docstring prose*
  explaining how arm dispatch degrades when an arm is missing.

A guard that cries wolf on correct code gets suppressed, and a suppressed
guard catches nothing. So detection is done by parsing each file with `ast`
and flagging only a genuine `raise NotImplementedError` / `raise
NotImplementedError(...)` **statement**. Exception handlers, type
annotations, string literals, docstrings, and comments cannot trigger it,
because none of them parse to an `ast.Raise` node.

Do not delete this test, and do not add per-file exemptions to it — a lane
that is not finished should show up here as a red test, which is the entire
point.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("src", "eval")

_TARGET_EXCEPTION = "NotImplementedError"


def _raises_not_implemented(exc: ast.expr | None) -> bool:
    """True only for `raise NotImplementedError` / `raise
    NotImplementedError(...)` (including a dotted spelling such as
    `builtins.NotImplementedError`). A bare `raise` (`exc is None`) re-raises
    whatever is already in flight and is not a stub.
    """
    if exc is None:
        return False
    if isinstance(exc, ast.Call):
        return _raises_not_implemented(exc.func)
    if isinstance(exc, ast.Name):
        return exc.id == _TARGET_EXCEPTION
    if isinstance(exc, ast.Attribute):
        return exc.attr == _TARGET_EXCEPTION
    return False


class _StubFinder(ast.NodeVisitor):
    """Collect every `raise NotImplementedError` statement in one module,
    tagged with the enclosing function/method path so the failure message
    points at what to implement, not just at a line number.
    """

    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []
        self._scope: list[str] = []

    def _visit_scoped(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_Raise(self, node: ast.Raise) -> None:
        if _raises_not_implemented(node.exc):
            scope = ".".join(self._scope) if self._scope else "<module>"
            self.hits.append((node.lineno, scope))
        self.generic_visit(node)


def find_unimplemented_stubs(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return `path:line (scope)` for every genuine unimplemented stub under
    the scanned directories, sorted for a stable failure message.
    """
    stubs: list[str] = []
    for dirname in SCAN_DIRS:
        base = repo_root / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            # Cheap pre-filter: a file that never mentions the name cannot
            # raise it, and parsing every module is the expensive part.
            if _TARGET_EXCEPTION not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            finder = _StubFinder()
            finder.visit(tree)
            relpath = path.relative_to(repo_root).as_posix()
            stubs.extend(f"{relpath}:{lineno} ({scope})" for lineno, scope in finder.hits)
    return sorted(stubs)


def test_no_unimplemented_stubs_remain():
    """Hard-fails CI if any lane left a `raise NotImplementedError` behind in
    src/ or eval/ (PLAN.md Wave 2.1, FR9.4).
    """
    stubs = find_unimplemented_stubs()
    assert not stubs, (
        "Unimplemented stubs remain (a lane is not finished; implement them, "
        f"do not exempt them): {stubs}"
    )


# --- the guard's own guard -------------------------------------------------
# A detector that silently stops detecting is worse than no detector. These
# assert both directions of the AST check against the exact false-positive
# shapes that motivated replacing the substring scan.

_TRUE_POSITIVES = [
    "def f():\n    raise NotImplementedError\n",
    "def f():\n    raise NotImplementedError()\n",
    'def f():\n    raise NotImplementedError("Lane E owns this")\n',
    "class C:\n    def m(self):\n        raise NotImplementedError\n",
    "async def f():\n    raise NotImplementedError\n",
    "import builtins\n\n\ndef f():\n    raise builtins.NotImplementedError\n",
]

_FALSE_POSITIVE_SHAPES = [
    # A *handler* — src/api.py:382's shape. Catching is not stubbing.
    "def f():\n    try:\n        go()\n    except NotImplementedError:\n        return None\n",
    "def f():\n    try:\n        go()\n    except (NotImplementedError, ValueError):\n        return None\n",
    # Docstring prose — eval/run_eval.py's shape.
    '"""Arms may still raise NotImplementedError during Wave 1."""\n',
    "def f():\n    '''Raises NotImplementedError if unwired.'''\n    return 1\n",
    # A comment.
    "def f():\n    # raise NotImplementedError once wired\n    return 1\n",
    # A bare string / identifier mention that never raises.
    'MESSAGE = "NotImplementedError"\n',
    "def f() -> None:\n    handled = NotImplementedError\n    return handled\n",
    # A bare re-raise inside a handler.
    "def f():\n    try:\n        go()\n    except NotImplementedError:\n        raise\n",
]


def _stub_lines(source: str) -> list[tuple[int, str]]:
    finder = _StubFinder()
    finder.visit(ast.parse(source))
    return finder.hits


def test_detector_flags_real_raise_statements():
    for source in _TRUE_POSITIVES:
        assert _stub_lines(source), f"detector missed a real stub:\n{source}"


def test_detector_ignores_catches_annotations_and_prose():
    for source in _FALSE_POSITIVE_SHAPES:
        assert not _stub_lines(source), f"detector false-positived on:\n{source}"
