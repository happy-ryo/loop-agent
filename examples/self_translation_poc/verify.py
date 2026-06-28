#!/usr/bin/env python3
"""Three-stage ground-truth verifier for the self-translation PoC (Issue #37).

A translated file is considered *done* only when all three stages pass, in
order of increasing cost:

1. ``parses_ok``         -- the file is still valid Python (``ast.parse``). A
   botched edit that breaks syntax fails here cheaply, before pytest is run.
2. ``japanese_cleared``  -- no Japanese remains in the *translation targets*:
   comments (``# ...``) and docstrings. Non-docstring string literals are
   explicitly out of scope (user-facing messages may stay in Japanese), so they
   are ignored -- otherwise the goal could never be reached.
3. ``tests_pass``        -- the module's own test file passes (``pytest``). This
   is the ground truth that the translation did not change behaviour.

The verifier is the non-gameable signal that drives loop termination: an LLM may
*claim* a file is translated, but only ``ast.parse`` + a Japanese scan + the
real pytest exit code decide ``goal_met``.

This module has no LLM dependency and is import-safe, so it is unit-tested
directly in ``tests/test_self_translation_poc.py``.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "loop_agent"
TESTS_ROOT = REPO_ROOT / "tests"

# Hiragana, katakana, half-width katakana, and CJK unified ideographs. This is
# the "is there Japanese here" signal; it intentionally also matches kanji that
# Chinese shares, which is fine -- this codebase's prose is Japanese.
_JAPANESE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def has_japanese(text: str) -> bool:
    """True if ``text`` contains any Japanese (kana or CJK) character."""
    return bool(_JAPANESE.search(text))


@dataclass(frozen=True)
class JapaneseHit:
    """One leftover Japanese fragment in a translation target."""

    kind: str  # "comment" | "docstring"
    line: int
    excerpt: str


def _docstring_node_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    """Line ranges (1-based, inclusive) of every docstring expression node.

    A docstring is the first statement of a module / class / function when it is
    a bare string constant. We record the *string node's* own line span so that
    comment scanning can tell a docstring's lines apart from ordinary comments.
    """
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            start = first.value.lineno
            end = getattr(first.value, "end_lineno", start)
            ranges.append((start, end))
    return ranges


def japanese_hits(path: Path) -> list[JapaneseHit]:
    """Find Japanese remaining in comments and docstrings (the translation targets).

    Ordinary string literals are *ignored* -- they are out of scope for the
    translation and may legitimately keep Japanese (e.g. user-facing messages),
    so flagging them would make ``japanese_cleared`` unreachable.
    """
    source = path.read_text(encoding="utf-8")
    hits: list[JapaneseHit] = []

    tree = ast.parse(source)

    # Docstrings: walk AST, read each docstring's text directly.
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc and has_japanese(doc):
            first = node.body[0]  # type: ignore[attr-defined]
            line = getattr(first.value, "lineno", getattr(node, "lineno", 0))
            excerpt = next(
                (ln.strip() for ln in doc.splitlines() if has_japanese(ln)), doc[:60]
            )
            hits.append(JapaneseHit("docstring", line, excerpt[:80]))

    # Comments: tokenize and check COMMENT tokens.
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT and has_japanese(tok.string):
                hits.append(
                    JapaneseHit("comment", tok.start[0], tok.string.strip()[:80])
                )
    except tokenize.TokenError:
        # A truncated file can fail tokenization even when ast.parse succeeded
        # on a repaired form; treat the docstring scan as authoritative.
        pass

    hits.sort(key=lambda h: h.line)
    return hits


# Map each translatable source file to the test module that exercises it.
_TEST_MODULE = {
    "waker.py": "test_waker.py",
    "convergence.py": "test_convergence.py",
    "observe.py": "test_observe.py",
    "events.py": "test_events.py",
    "memory.py": "test_memory.py",
    "evaluator.py": "test_evaluator.py",
    "adapters/claude_code.py": "test_adapters_claude_code.py",
    "reflexion_store.py": "test_reflexion_store.py",
    "transport.py": "test_transport.py",
    "gate.py": "test_gate.py",
}


def test_module_for(path: Path) -> Optional[Path]:
    """Return the pytest file covering ``path`` (under ``src/loop_agent``), if any."""
    try:
        rel = path.resolve().relative_to(SRC_ROOT).as_posix()
    except ValueError:
        return None
    name = _TEST_MODULE.get(rel)
    return TESTS_ROOT / name if name else None


def run_module_tests(path: Path, *, timeout: float = 300.0) -> tuple[bool, str]:
    """Run the module's pytest file; return ``(passed, detail)``.

    A subprocess re-imports the (just edited) module, so a translation that broke
    code surfaces here as a non-zero exit. Files with no mapped test pass
    vacuously (the global suite run at the end of the PoC is the backstop).
    """
    test_file = test_module_for(path)
    if test_file is None:
        return True, "no mapped test module"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode == 0, f"pytest {test_file.name} rc={proc.returncode}"


@dataclass
class VerifyReport:
    """Outcome of the three-stage check on one file."""

    path: str
    parses_ok: bool
    japanese_cleared: bool
    tests_pass: bool
    hits: list[JapaneseHit] = field(default_factory=list)
    detail: str = ""

    @property
    def done(self) -> bool:
        """All three stages passed -- the file is fully translated and green."""
        return self.parses_ok and self.japanese_cleared and self.tests_pass

    def summary(self) -> str:
        flags = (
            f"parse={'ok' if self.parses_ok else 'FAIL'} "
            f"jp={'clear' if self.japanese_cleared else f'{len(self.hits)} left'} "
            f"tests={'pass' if self.tests_pass else 'FAIL'}"
        )
        return f"{Path(self.path).name}: {flags}" + (f" ({self.detail})" if self.detail else "")


def verify_file(path: Path, *, run_tests: bool = True) -> VerifyReport:
    """Run the three-stage verification on ``path``.

    Stage 1 (parse) gates everything: an unparseable file cannot be scanned or
    tested. Tests run whenever the file parses (even if Japanese remains), so a
    translation that broke behaviour is detected as a distinct failure mode from
    "Japanese still present".
    """
    path = Path(path)
    source = path.read_text(encoding="utf-8")

    try:
        ast.parse(source)
        parses_ok = True
        parse_detail = ""
    except SyntaxError as exc:
        return VerifyReport(
            path=str(path),
            parses_ok=False,
            japanese_cleared=False,
            tests_pass=False,
            detail=f"SyntaxError: {exc.msg} (line {exc.lineno})",
        )

    hits = japanese_hits(path)
    japanese_cleared = not hits

    # When tests are not run (e.g. wiring tests), they do not gate "done".
    tests_pass = True
    test_detail = "tests skipped"
    if parses_ok and run_tests:
        tests_pass, test_detail = run_module_tests(path)

    detail = " | ".join(p for p in (parse_detail, test_detail) if p)
    if hits:
        first = hits[0]
        detail = (detail + " | " if detail else "") + (
            f"{len(hits)} JP left, first @L{first.line} ({first.kind})"
        )

    return VerifyReport(
        path=str(path),
        parses_ok=parses_ok,
        japanese_cleared=japanese_cleared,
        tests_pass=tests_pass,
        hits=hits,
        detail=detail,
    )
