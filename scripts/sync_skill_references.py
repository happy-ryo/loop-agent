#!/usr/bin/env python3
"""Regenerate the bundled skill references from docs/ (Issue #73).

The reference-bundled coding-agent skill at
``src/loop_agent/skills/loop-agent/references/`` carries a verbatim copy of
selected ``docs/`` pages for load-on-demand use. This script is the *single*
deterministic transform that derives those 8 reference files from their
``docs/`` sources:

- it prepends a provenance header note, and
- it rewrites links so the bundle is self-contained: a link to another bundled
  page becomes a flat in-bundle filename, and a link to any other repo file
  becomes an absolute GitHub URL.

The body is otherwise kept byte-for-byte. SKILL.md, references/design-philosophy.md,
and references/examples/ are hand-authored and intentionally NOT managed here
(Issue #73: "SKILL.md is hand-written, references are auto-derived").

Usage::

    python scripts/sync_skill_references.py            # --apply (default): rewrite
    python scripts/sync_skill_references.py --check     # CI: fail on drift

CI runs ``--check`` (no commit-back): if a reference drifts from its docs source
it prints a diff and exits non-zero, telling the developer to run the apply mode
and commit. All user-facing status strings stay ASCII so the script does not
crash under a cp932 console on Windows; only the (UTF-8 reconfigured) diff body
carries the docs' Japanese text.
"""

from __future__ import annotations

import argparse
import difflib
import posixpath
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REF_SUBDIR = "src/loop_agent/skills/loop-agent/references"
GITHUB_BLOB = "https://github.com/happy-ryo/loop-agent/blob/main"
GITHUB_TREE = "https://github.com/happy-ryo/loop-agent/tree/main"

# (docs path relative to repo root, flat reference filename in the bundle).
# These 8 are verbatim bundles; everything else in the bundle is hand-authored.
BUNDLES: list[tuple[str, str]] = [
    ("docs/seams.md", "seams.md"),
    ("docs/adapters/writing-an-adapter.md", "writing-an-adapter.md"),
    ("docs/persistence-and-resume.md", "persistence-and-resume.md"),
    ("docs/safety.md", "safety.md"),
    ("docs/reflexion-when-to-use.md", "reflexion-when-to-use.md"),
    ("docs/async.md", "async.md"),
    ("docs/transport.md", "transport.md"),
    ("docs/errors.md", "errors.md"),
]
# Map a bundled doc's repo-relative path to its flat in-bundle filename.
_BUNDLED_FLAT = {docs: flat for docs, flat in BUNDLES}

# Inline markdown link target: the "(...)" in "[text](target)". The docs use no
# "](" inside code blocks (verified), so a global substitution is safe here.
_LINK = re.compile(r"\]\(([^)]+)\)")


def _rewrite_target(raw: str, doc_dir: str) -> str:
    """Rewrite one link target for a doc whose dir (repo-relative) is ``doc_dir``."""
    target, sep, frag = raw.partition("#")
    anchor = sep + frag
    if target == "":
        # Pure in-page anchor like "(#section)": leave untouched.
        return raw
    if target.lower().startswith(("http://", "https://", "mailto:")):
        return raw
    is_dir = target.endswith("/")
    resolved = posixpath.normpath(posixpath.join(doc_dir, target))
    if resolved in _BUNDLED_FLAT:
        # Link to another bundled page -> flat in-bundle filename.
        return _BUNDLED_FLAT[resolved] + anchor
    # Link to a non-bundled repo path -> absolute GitHub URL (tree for a
    # directory link, blob for a file).
    base = GITHUB_TREE if is_dir else GITHUB_BLOB
    suffix = "/" if is_dir else ""
    return f"{base}/{resolved}{suffix}" + anchor


def _rewrite_links(body: str, doc_dir: str) -> str:
    return _LINK.sub(lambda m: "](" + _rewrite_target(m.group(1), doc_dir) + ")", body)


def _header(docs_relpath: str) -> str:
    return (
        f"> このファイルは `{docs_relpath}` の load-on-demand 用バンドルコピーです。"
        f"正典はリポジトリの `{docs_relpath}` を参照してください。\n\n"
    )


def build_reference(docs_relpath: str) -> str:
    """Build the bundled reference text for one docs page (header + rewritten body)."""
    body = (REPO_ROOT / docs_relpath).read_text(encoding="utf-8")
    return _header(docs_relpath) + _rewrite_links(body, posixpath.dirname(docs_relpath))


def _ref_path(flat: str) -> Path:
    return REPO_ROOT / REF_SUBDIR / flat


def apply() -> list[str]:
    """Write each reference; return the filenames that actually changed."""
    changed: list[str] = []
    for docs_relpath, flat in BUNDLES:
        content = build_reference(docs_relpath)
        path = _ref_path(flat)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        # Compare line-wise so a CRLF/LF checkout difference is not a spurious
        # rewrite; only touch the file when the meaningful content differs.
        if current is None or current.splitlines() != content.splitlines():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
            changed.append(flat)
    return changed


def check() -> list[tuple[str, str, str]]:
    """Return (flat, expected, actual) for each reference that drifts from docs."""
    drifted: list[tuple[str, str, str]] = []
    for docs_relpath, flat in BUNDLES:
        expected = build_reference(docs_relpath)
        path = _ref_path(flat)
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if expected.splitlines() != actual.splitlines():
            drifted.append((flat, expected, actual))
    return drifted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync_skill_references",
        description="Regenerate the bundled skill references from docs/ (Issue 73).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any reference differs from docs/",
    )
    args = parser.parse_args(argv)

    # Diffs echo the docs' Japanese text; force UTF-8 so --check does not crash
    # on a cp932 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable
            pass

    if args.check:
        drifted = check()
        if not drifted:
            print(f"skill references in sync ({len(BUNDLES)} files).")
            return 0
        print("skill references are OUT OF SYNC with docs/:", file=sys.stderr)
        for flat, expected, actual in drifted:
            print(f"  - {flat}", file=sys.stderr)
            for line in difflib.unified_diff(
                actual.splitlines(),
                expected.splitlines(),
                fromfile=f"{flat} (on disk)",
                tofile=f"{flat} (regenerated from docs)",
                lineterm="",
            ):
                print(line, file=sys.stderr)
        print(
            "\nrun: python scripts/sync_skill_references.py   (then commit the result)",
            file=sys.stderr,
        )
        return 1

    changed = apply()
    if changed:
        print(f"updated {len(changed)} reference(s): {', '.join(changed)}")
    else:
        print(f"skill references already in sync ({len(BUNDLES)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
