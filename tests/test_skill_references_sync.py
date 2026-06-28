"""The bundled skill references must stay in sync with their docs/ sources.

The 8 verbatim reference files under
``src/loop_agent/skills/loop-agent/references/`` are derived from ``docs/`` by
``scripts/sync_skill_references.py``. This test is the local mirror of the
``sync-skill-references`` CI check: if a contributor edits a bundled doc without
re-running the sync, ``check()`` reports the drift and this test fails with the
exact fix command. SKILL.md / design-philosophy.md / examples/ are hand-authored
and deliberately out of scope.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_skill_references.py"


def _load_sync() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_skill_references", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_references_are_in_sync_with_docs() -> None:
    sync = _load_sync()
    drifted = sync.check()
    assert drifted == [], (
        "bundled skill references drifted from docs/; run "
        "`python scripts/sync_skill_references.py` and commit: "
        + ", ".join(flat for flat, _, _ in drifted)
    )


def test_every_bundle_maps_to_existing_doc_and_reference() -> None:
    sync = _load_sync()
    for docs_relpath, flat in sync.BUNDLES:
        assert (sync.REPO_ROOT / docs_relpath).is_file(), docs_relpath
        assert (sync.REPO_ROOT / sync.REF_SUBDIR / flat).is_file(), flat
