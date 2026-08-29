"""The anti-zombie guard (PLAN.md Wave 0 / "The anti-zombie guard").

Wave 0 intentionally leaves every non-schema file in src/ and eval/ as a
`raise NotImplementedError` stub with a real docstring — that is by design,
so that seven Wave 1 lanes can code against frozen interfaces in parallel
without blocking on a full implementation existing yet.

This test is what stops that from becoming permanent. It is marked
`xfail` during Wave 1 (stubs are expected to exist) and must be flipped to
a hard failure at the start of Wave 2 (PLAN.md Wave 2.1: "Flip the
NotImplementedError guard to hard-fail") — at that point, any remaining
`NotImplementedError` in src/ or eval/ is a real integration gap, not
an expected placeholder, and CI must catch it (FR9.4).

Do not delete this test when flipping it — remove the `xfail` marker (and
this comment block explaining why it was there) instead, so the guard's
history stays visible in git blame.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["src", "eval"]


def _find_notimplementederror_hits() -> list[str]:
    hits = []
    for dirname in SCAN_DIRS:
        base = REPO_ROOT / dirname
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "NotImplementedError" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(REPO_ROOT)))
    return sorted(hits)


@pytest.mark.xfail(
    reason=(
        "Wave 1 lanes intentionally leave NotImplementedError stubs in place "
        "while building against the frozen Wave 0 contracts. Flip to a hard "
        "failure (remove this xfail marker) at the start of Wave 2, once "
        "real implementations replace every stub (PLAN.md Wave 2.1, FR9.4)."
    ),
    strict=False,
)
def test_no_unimplemented_stubs_remain():
    """Fails CI (once xfail is removed in Wave 2) if any lane left a
    NotImplementedError behind in src/ or eval/.
    """
    hits = _find_notimplementederror_hits()
    assert not hits, f"Unimplemented stubs remain: {hits}"
