"""Efficient canonical-English translation harness.

This is the token-lean version of the dogfood translation loop:

- scan Japanese-containing chunks deterministically;
- send only one chunk, its type, and compact constraints to the LLM;
- apply the returned replacement with deterministic file patching;
- verify with local checks instead of asking the LLM to inspect the repository.

The module is importable so tests can cover the scanner and patcher without
calling an LLM. For a real run, use the Codex translator backend.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import time
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from loop_agent import (
    ActOutcome,
    DBProgressLog,
    MaxIterations,
    NoProgress,
    TokenBudget,
    VerifyOutcome,
    WorkItem,
    WorkListDrained,
    WorkListGather,
    run_loop,
)
from loop_agent.adapters import CodexAct


JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
DEFAULT_TARGETS = (
    "README.md",
    "CHANGELOG.md",
    "report.md",
    "docs",
    "src",
    "tests",
    "examples",
    "scripts",
    ".github",
)
SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", "dist", "build", ".loopagent-redo"}


@dataclass(frozen=True)
class TranslationChunk:
    id: str
    path: str
    kind: str
    old_text: str
    text_for_translation: str
    start_line: int
    end_line: int
    start_col: int = 0
    end_col: int = 0

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchResult:
    item_id: str
    path: str
    status: str
    detail: str
    replacement: str = ""


@dataclass(frozen=True)
class BatchPatchResult:
    item_id: str
    path: str
    status: str
    detail: str
    results: list[dict[str, Any]]


class TranslationError(RuntimeError):
    def __init__(self, message: str, *, tokens: int = 0) -> None:
        super().__init__(message)
        self.tokens = tokens


class MockTranslator:
    """Deterministic translator for tests and dry local demonstrations."""

    def __init__(self, replacements: Mapping[str, str]) -> None:
        self.replacements = dict(replacements)

    def __call__(self, chunk: TranslationChunk) -> tuple[str, int]:
        if chunk.id not in self.replacements:
            raise KeyError(f"missing mock replacement for {chunk.id}")
        return self.replacements[chunk.id], 0


class CodexChunkTranslator:
    """Translate one already-scoped chunk through CodexAct."""

    def __init__(self, *, model: str, effort: str, timeout: float) -> None:
        self._act = CodexAct(model=model, effort=effort, timeout=timeout)

    def __call__(self, chunk: TranslationChunk) -> tuple[str, int]:
        outcome = self._act({"prompt": build_translation_prompt(chunk)})
        tokens = outcome.tokens
        observation = outcome.observation
        if getattr(observation, "failed", False):
            error = getattr(observation, "error", "") or "codex act failed"
            raise TranslationError(str(error), tokens=tokens)
        raw = getattr(observation, "text", str(observation))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslationError(
                f"translator did not return JSON: {raw[:200]}", tokens=tokens
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("replacement"), str):
            raise TranslationError(
                "translator JSON must be an object with string field 'replacement'",
                tokens=tokens,
            )
        return payload["replacement"], tokens

    def translate_batch(self, chunks: Sequence[TranslationChunk]) -> tuple[dict[str, str], int]:
        outcome = self._act({"prompt": build_batch_translation_prompt(chunks)})
        tokens = outcome.tokens
        observation = outcome.observation
        if getattr(observation, "failed", False):
            error = getattr(observation, "error", "") or "codex act failed"
            raise TranslationError(str(error), tokens=tokens)
        raw = getattr(observation, "text", str(observation))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslationError(
                f"translator did not return JSON: {raw[:200]}", tokens=tokens
            ) from exc
        replacements = payload.get("replacements") if isinstance(payload, dict) else None
        if not isinstance(replacements, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in replacements.items()
        ):
            raise TranslationError(
                "translator JSON must be an object with object field 'replacements'",
                tokens=tokens,
            )
        return dict(replacements), tokens


def contains_japanese(text: str) -> bool:
    return bool(JAPANESE_RE.search(text))


def iter_target_files(root: Path, targets: Sequence[str]) -> Iterable[Path]:
    for raw in targets:
        target = root / raw
        if target.is_file():
            if should_scan_file(target):
                yield target
            continue
        if not target.is_dir():
            continue
        for path in target.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if should_scan_file(path):
                yield path


def should_scan_file(path: Path) -> bool:
    if not path.is_file():
        return False
    parts = set(path.parts)
    if "docs" in parts and "ja" in parts:
        return False
    if path.name.endswith(".ja.md"):
        return False
    return path.suffix in {".md", ".py", ".yml", ".yaml", ".toml", ".txt", ".ps1"}


def scan_japanese_chunks(root: Path, targets: Sequence[str] = DEFAULT_TARGETS) -> list[TranslationChunk]:
    chunks: list[TranslationChunk] = []
    for path in sorted(set(iter_target_files(root, targets))):
        rel = path.relative_to(root).as_posix()
        text = read_text_preserving_newlines(path)
        if not contains_japanese(text):
            continue
        if path.suffix == ".py":
            chunks.extend(scan_python_chunks(rel, text))
        else:
            chunks.extend(scan_text_chunks(rel, text))
    return chunks


def scan_text_chunks(relpath: str, text: str) -> list[TranslationChunk]:
    lines = text.splitlines(keepends=True)
    chunks: list[TranslationChunk] = []
    in_fence = False
    start: int | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal start, buf
        if start is None:
            return
        old = "".join(buf)
        chunks.append(
            TranslationChunk(
                id=f"{relpath}:{start}",
                path=relpath,
                kind="text",
                old_text=old,
                text_for_translation=old,
                start_line=start,
                end_line=start + len(buf) - 1,
                start_col=0,
                end_col=len(buf[-1]) if buf else 0,
            )
        )
        start = None
        buf = []

    for index, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            in_fence = not in_fence
            continue
        if not in_fence and contains_japanese(line):
            if start is None:
                start = index
            buf.append(line)
        else:
            flush()
    flush()
    return chunks


def scan_python_chunks(relpath: str, source: str) -> list[TranslationChunk]:
    chunks: list[TranslationChunk] = []
    doc_spans = _docstring_spans(source)
    for start, end, start_col, end_col, old_text, value in doc_spans:
        if contains_japanese(value):
            chunks.append(
                TranslationChunk(
                    id=f"{relpath}:{start}:docstring",
                    path=relpath,
                    kind="python-docstring",
                    old_text=old_text,
                    text_for_translation=value,
                    start_line=start,
                    end_line=end,
                    start_col=start_col,
                    end_col=end_col,
                )
            )

    reader = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(reader):
        if tok.type != tokenize.COMMENT or not contains_japanese(tok.string):
            continue
        chunks.append(
            TranslationChunk(
                id=f"{relpath}:{tok.start[0]}:comment",
                path=relpath,
                kind="python-comment",
                old_text=tok.string,
                text_for_translation=tok.string,
                start_line=tok.start[0],
                end_line=tok.end[0],
                start_col=tok.start[1],
                end_col=tok.end[1],
            )
        )
    return sorted(chunks, key=lambda chunk: (chunk.start_line, chunk.kind))


def _docstring_spans(source: str) -> list[tuple[int, int, int, int, str, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[int, int, int, int, str, str]] = []
    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        segment = ast.get_source_segment(source, first)
        if segment is None:
            continue
        start_col = _byte_col_to_char_col(lines[first.lineno - 1], first.col_offset)
        if first.end_lineno is None or first.end_col_offset is None:
            end_line = first.lineno
            end_col = start_col + len(segment)
        else:
            end_line = first.end_lineno
            end_col = _byte_col_to_char_col(lines[end_line - 1], first.end_col_offset)
        spans.append(
            (
                first.lineno,
                end_line,
                start_col,
                end_col,
                segment,
                first.value.value,
            )
        )
    return spans


def _byte_col_to_char_col(line: str, byte_col: int) -> int:
    prefix = line.encode("utf-8")[:byte_col]
    return len(prefix.decode("utf-8", errors="ignore"))


def build_translation_prompt(chunk: TranslationChunk) -> str:
    return json.dumps(
        {
            "task": "Translate this single chunk to canonical English.",
            "constraints": [
                "Return only JSON.",
                "Use exactly this shape: {\"replacement\": \"...\"}.",
                "Do not inspect the repository.",
                "For python-comment, replacement must include the leading #.",
                "For python-docstring, replacement is the docstring text only, not quotes.",
                "Preserve Markdown links and code spans unless the Japanese text inside them must change.",
            ],
            "chunk": chunk.to_payload(),
        },
        ensure_ascii=False,
    )


def build_batch_translation_prompt(chunks: Sequence[TranslationChunk]) -> str:
    return json.dumps(
        {
            "task": "Translate these already-scoped chunks to canonical English.",
            "constraints": [
                "Return only JSON.",
                "Use exactly this shape: {\"replacements\": {\"chunk-id\": \"...\"}}.",
                "Return one replacement for every chunk id and no extra ids.",
                "Do not inspect the repository.",
                "For python-comment, replacement must include the leading #.",
                "For python-docstring, replacement is the docstring text only, not quotes.",
                "Preserve Markdown links and code spans unless the Japanese text inside them must change.",
            ],
            "chunks": [chunk.to_payload() for chunk in chunks],
        },
        ensure_ascii=False,
    )


def apply_translation(root: Path, chunk: TranslationChunk, replacement: str) -> PatchResult:
    if contains_japanese(replacement):
        return PatchResult(chunk.id, chunk.path, "failed", "replacement still contains Japanese")
    path = root / chunk.path
    current = read_text_preserving_newlines(path)
    old = chunk.old_text
    new_text = replacement
    if chunk.kind == "python-docstring":
        new_text = _python_string_literal(replacement)
    updated = replace_chunk_span(current, chunk, new_text)
    if updated is None:
        return PatchResult(chunk.id, chunk.path, "failed", "original chunk no longer matches")
    if path.suffix == ".py":
        try:
            ast.parse(updated)
        except SyntaxError as exc:
            return PatchResult(chunk.id, chunk.path, "failed", f"patched Python is invalid: {exc}")
    path.write_text(updated, encoding="utf-8", newline="")
    return PatchResult(chunk.id, chunk.path, "done", "patched", replacement)


def apply_translation_batch(
    root: Path, item_id: str, chunks: Sequence[TranslationChunk], replacements: Mapping[str, str]
) -> BatchPatchResult:
    results: list[PatchResult] = []
    for chunk in sorted(chunks, key=lambda c: (c.path, c.start_line, c.start_col), reverse=True):
        if chunk.id not in replacements:
            results.append(PatchResult(chunk.id, chunk.path, "failed", "missing replacement"))
            continue
        results.append(apply_translation(root, chunk, replacements[chunk.id]))
    ordered = list(reversed(results))
    failed = [result for result in ordered if result.status != "done"]
    path = chunks[0].path if chunks else ""
    if failed:
        return BatchPatchResult(
            item_id=item_id,
            path=path,
            status="failed",
            detail="; ".join(f"{result.item_id}: {result.detail}" for result in failed),
            results=[asdict(result) for result in ordered],
        )
    return BatchPatchResult(
        item_id=item_id,
        path=path,
        status="done",
        detail=f"patched {len(ordered)} chunks",
        results=[asdict(result) for result in ordered],
    )


def read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _python_string_literal(value: str) -> str:
    return repr(value)


def replace_chunk_span(source: str, chunk: TranslationChunk, replacement: str) -> str | None:
    lines = source.splitlines(keepends=True)
    if chunk.start_line < 1 or chunk.end_line > len(lines) or chunk.start_line > chunk.end_line:
        return None
    start_index = chunk.start_line - 1
    end_index = chunk.end_line - 1
    if start_index == end_index:
        line = lines[start_index]
        if line[chunk.start_col : chunk.end_col] != chunk.old_text:
            return None
        lines[start_index] = line[: chunk.start_col] + replacement + line[chunk.end_col :]
        return "".join(lines)

    selected = "".join(
        [
            lines[start_index][chunk.start_col :],
            *lines[start_index + 1 : end_index],
            lines[end_index][: chunk.end_col],
        ]
    )
    if selected != chunk.old_text:
        return None
    lines[start_index : end_index + 1] = [
        lines[start_index][: chunk.start_col] + replacement + lines[end_index][chunk.end_col :]
    ]
    return "".join(lines)


def result_from_observation(observation: Any) -> PatchResult | BatchPatchResult | None:
    if isinstance(observation, PatchResult):
        return observation
    if isinstance(observation, BatchPatchResult):
        return observation
    if isinstance(observation, Mapping):
        if "results" in observation:
            try:
                raw_results = observation["results"]
                if not isinstance(raw_results, list):
                    return None
                return BatchPatchResult(
                    item_id=str(observation["item_id"]),
                    path=str(observation["path"]),
                    status=str(observation["status"]),
                    detail=str(observation.get("detail", "")),
                    results=[dict(item) for item in raw_results if isinstance(item, Mapping)],
                )
            except KeyError:
                return None
        try:
            return PatchResult(
                item_id=str(observation["item_id"]),
                path=str(observation["path"]),
                status=str(observation["status"]),
                detail=str(observation.get("detail", "")),
                replacement=str(observation.get("replacement", "")),
            )
        except KeyError:
            return None
    return None


def done_when(item: WorkItem, record: Any) -> bool:
    result = result_from_observation(record.observation)
    return result is not None and result.item_id == item.id and result.status == "done"


def item_of(record: Any) -> str | None:
    result = result_from_observation(record.observation)
    return result.item_id if result is not None else None


def build_context(item: WorkItem, attempt: int, _state: Any) -> dict[str, Any]:
    payload = dict(item.payload or {})
    payload["attempt"] = attempt + 1
    return payload


def _chunks_from_context(context: Mapping[str, Any]) -> tuple[str, list[TranslationChunk], bool]:
    if "chunks" in context:
        chunks = [TranslationChunk(**chunk) for chunk in context["chunks"]]
        return str(context.get("batch_id") or "+".join(chunk.id for chunk in chunks)), chunks, True
    chunk = TranslationChunk(**{k: v for k, v in context.items() if k != "attempt"})
    return chunk.id, [chunk], False


def make_act(root: Path, translator: Callable[[TranslationChunk], tuple[str, int]]) -> Callable[[Any], ActOutcome]:
    def act(context: Any) -> ActOutcome:
        item_id, chunks, is_batch = _chunks_from_context(context)
        try:
            if is_batch:
                translate_batch = getattr(translator, "translate_batch", None)
                if translate_batch is None:
                    replacements: dict[str, str] = {}
                    tokens = 0
                    for chunk in chunks:
                        replacement, used = translator(chunk)
                        replacements[chunk.id] = replacement
                        tokens += used
                else:
                    replacements, tokens = translate_batch(chunks)
                result = apply_translation_batch(root, item_id, chunks, replacements)
            else:
                replacement, tokens = translator(chunks[0])
                result = apply_translation(root, chunks[0], replacement)
        except TranslationError as exc:
            result = BatchPatchResult(
                item_id,
                chunks[0].path if chunks else "",
                "failed",
                f"{type(exc).__name__}: {exc}",
                [],
            )
            tokens = exc.tokens
        except Exception as exc:
            result = BatchPatchResult(
                item_id,
                chunks[0].path if chunks else "",
                "failed",
                f"{type(exc).__name__}: {exc}",
                [],
            )
            tokens = 0
        return ActOutcome(observation=asdict(result), tokens=tokens)

    return act


def verify_patch(outcome: ActOutcome) -> VerifyOutcome:
    result = result_from_observation(outcome.observation)
    if result is None:
        return VerifyOutcome(goal_met=False, detail="act did not return a patch result")
    return VerifyOutcome(
        goal_met=False,
        detail=json.dumps(asdict(result), ensure_ascii=False, sort_keys=True),
    )


def run_efficient_translation(
    root: Path,
    *,
    targets: Sequence[str],
    translator: Callable[[TranslationChunk], tuple[str, int]],
    db_path: Path,
    run_id: str,
    events_path: Path,
    manifest_path: Path,
    max_attempts_per_item: int,
    token_budget: int,
    batch_size: int = 1,
) -> Any:
    with DBProgressLog(db_path, run_id) as progress:
        chunks = load_or_create_manifest(
            root,
            targets,
            manifest_path,
            resume=bool(progress.state.history),
        )
        work = WorkListGather(
            build_work_items(chunks, batch_size=batch_size),
            strategy="fewest_attempts",
            max_attempts_per_item=max_attempts_per_item,
            done_when=done_when,
            item_of=item_of,
            build_ctx=build_context,
        )

        def on_step(record: Any, state: Any) -> None:
            progress.on_step(record, state)
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "kind": "efficient_translation_step",
                            "run_id": run_id,
                            "iteration": record.iteration,
                            "detail": record.detail,
                            "tokens_used": state.tokens_used,
                            "time": time.time(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        result = run_loop(
            gather=work,
            act=make_act(root, translator),
            verify=verify_patch,
            on_step=on_step,
            initial_state=progress.state,
            conditions=[
                WorkListDrained(work),
                MaxIterations(max(1, len(chunks) * max_attempts_per_item)),
                TokenBudget(token_budget),
                NoProgress(window=3, repeat=3, key=lambda record: record.detail),
            ],
        )
        progress.record_result(result)
        return result


def load_or_create_manifest(
    root: Path,
    targets: Sequence[str],
    manifest_path: Path,
    *,
    resume: bool,
) -> list[TranslationChunk]:
    if manifest_path.exists():
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("translation manifest must be a JSON list")
        return [TranslationChunk(**item) for item in raw]
    if resume:
        raise ValueError(
            f"cannot resume without stable translation manifest: {manifest_path}"
        )
    chunks = scan_japanese_chunks(root, targets)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps([chunk.to_payload() for chunk in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return chunks


def build_work_items(chunks: Sequence[TranslationChunk], *, batch_size: int) -> list[WorkItem]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    items: list[WorkItem] = []
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        if batch_size == 1 or chunk.kind != "text":
            items.append(WorkItem(id=chunk.id, payload=chunk.to_payload()))
            index += 1
            continue
        batch = [chunk]
        index += 1
        while (
            index < len(chunks)
            and len(batch) < batch_size
            and chunks[index].kind == "text"
            and chunks[index].path == chunk.path
        ):
            batch.append(chunks[index])
            index += 1
        batch_id = "batch:" + "+".join(item.id for item in batch)
        items.append(
            WorkItem(
                id=batch_id,
                payload={
                    "batch_id": batch_id,
                    "path": chunk.path,
                    "kind": "batch",
                    "chunks": [item.to_payload() for item in batch],
                },
            )
        )
    return items


def _load_mock_translator(path: Path) -> MockTranslator:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise SystemExit("mock translation file must be a JSON object of item_id -> replacement")
    return MockTranslator(data)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--target", action="append", dest="targets", help="target path; repeatable")
    parser.add_argument("--db", default="loop-state.db")
    parser.add_argument("--run-id", default="efficient-canonical-english-translation")
    parser.add_argument("--events", default="efficient-translation-events.jsonl")
    parser.add_argument("--manifest", default="efficient-translation-manifest.json")
    parser.add_argument("--max-attempts-per-item", type=int, default=2)
    parser.add_argument("--token-budget", type=int, default=2_000_000)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock-translations", help="JSON object of item_id -> replacement")
    parser.add_argument("--codex", action="store_true", help="use CodexAct for chunk translation")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--effort", default="low")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    targets = tuple(args.targets or DEFAULT_TARGETS)
    chunks = scan_japanese_chunks(root, targets)
    if args.dry_run:
        # Keep dry-run output shell-redirect safe on Windows and POSIX. The
        # manifest writer uses UTF-8 directly, but stdout may pass through a
        # terminal or shell redirection with a different encoding.
        print(json.dumps([chunk.to_payload() for chunk in chunks], ensure_ascii=True, indent=2))
        return 0
    if args.mock_translations:
        translator: Callable[[TranslationChunk], tuple[str, int]] = _load_mock_translator(
            Path(args.mock_translations)
        )
    elif args.codex:
        translator = CodexChunkTranslator(model=args.model, effort=args.effort, timeout=300)
    else:
        raise SystemExit("choose --dry-run, --mock-translations, or --codex")

    result = run_efficient_translation(
        root,
        targets=targets,
        translator=translator,
        db_path=Path(args.db),
        run_id=args.run_id,
        events_path=Path(args.events),
        manifest_path=Path(args.manifest),
        max_attempts_per_item=args.max_attempts_per_item,
        token_budget=args.token_budget,
        batch_size=args.batch_size,
    )
    print(result.status, result.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
