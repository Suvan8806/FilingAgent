"""FilingAgent evaluation harness package.

Mirrors the `.env` load in `src/__init__.py`. Importing `eval.judge` does not
execute `src/__init__.py`, and `eval/judge.py` resolves JUDGE_MODEL and the
provider base URLs at import time -- so the harness needs its own load to see
local settings when it is driven directly (`python -m eval.run_eval`).

`load_dotenv` is idempotent and `override=False`, so running both loaders in
one process is harmless and never clobbers a real environment variable.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
