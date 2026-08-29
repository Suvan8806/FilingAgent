"""FilingAgent source package.

Loads `.env` at package import so that module-level configuration reads --
`src/llm.py`'s PROVIDER/MODEL constants, `src/store.py`'s CHROMA_DIR,
`src/facts.py`'s FACTS_DB -- observe the developer's local settings.

This has to happen in `__init__.py` rather than a config module that those
files import: several of them resolve `os.environ` at import time, so by the
time any sibling module's body runs, it is already too late.

`override=False` is deliberate. A real environment variable always beats the
file, which keeps `monkeypatch.setenv` authoritative in tests and keeps CI --
where no `.env` exists and this call is a silent no-op -- reproducible.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
