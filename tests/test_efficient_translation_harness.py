from __future__ import annotations

from pathlib import Path
import json

import scripts.efficient_translation_harness as harness


def test_scanner_finds_text_comments_and_docstrings(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "# " + "\u65e5\u672c\u8a9e" + "\n\nEnglish\n",
        encoding="utf-8",
    )
    (tmp_path / "module.py").write_text(
        '"""' + "\u65e5\u672c\u8a9e" + ' docs"""\n'
        "x = 1  # " + "\u30b3\u30e1\u30f3\u30c8" + "\n",
        encoding="utf-8",
    )

    chunks = harness.scan_japanese_chunks(tmp_path, ["docs", "module.py"])

    assert [chunk.kind for chunk in chunks] == [
        "text",
        "python-docstring",
        "python-comment",
    ]
    assert chunks[0].path == "docs/guide.md"
    assert chunks[1].text_for_translation.endswith(" docs")


def test_apply_translation_patches_markdown_and_python(tmp_path: Path):
    md = tmp_path / "README.md"
    py = tmp_path / "module.py"
    md.write_text("# " + "\u65e5\u672c\u8a9e" + "\n", encoding="utf-8")
    py.write_text('"""' + "\u65e5\u672c\u8a9e" + '"""\n', encoding="utf-8")
    chunks = harness.scan_japanese_chunks(tmp_path, ["README.md", "module.py"])
    md_chunk = next(chunk for chunk in chunks if chunk.path == "README.md")
    py_chunk = next(chunk for chunk in chunks if chunk.kind == "python-docstring")

    md_result = harness.apply_translation(tmp_path, md_chunk, "# Japanese\n")
    py_result = harness.apply_translation(tmp_path, py_chunk, "Japanese")

    assert md_result.status == "done"
    assert py_result.status == "done"
    assert not harness.contains_japanese(md.read_text(encoding="utf-8"))
    assert not harness.contains_japanese(py.read_text(encoding="utf-8"))


def test_loop_uses_work_items_and_mock_translator(tmp_path: Path):
    target = tmp_path / "README.md"
    target.write_text("# " + "\u65e5\u672c\u8a9e" + "\n", encoding="utf-8")
    chunks = harness.scan_japanese_chunks(tmp_path, ["README.md"])
    translator = harness.MockTranslator({chunks[0].id: "# Japanese\n"})

    result = harness.run_efficient_translation(
        tmp_path,
        targets=["README.md"],
        translator=translator,
        db_path=tmp_path / "state.db",
        run_id="test-efficient-translation",
        events_path=tmp_path / "events.jsonl",
        manifest_path=tmp_path / "manifest.json",
        max_attempts_per_item=2,
        token_budget=10_000,
    )

    assert result.status == "stopped"
    assert result.stop is not None
    assert result.stop.name == "work_list_drained"
    assert not harness.contains_japanese(target.read_text(encoding="utf-8"))
    assert (tmp_path / "events.jsonl").exists()


def test_loop_batches_three_markdown_chunks_into_one_translator_call(tmp_path: Path):
    target = tmp_path / "README.md"
    jp = "\u65e5\u672c\u8a9e"
    target.write_text(
        f"# {jp}\n\n"
        "English divider\n\n"
        f"- {jp} item\n\n"
        "Another divider\n\n"
        f"Final {jp} sentence.\n",
        encoding="utf-8",
    )
    chunks = harness.scan_japanese_chunks(tmp_path, ["README.md"])

    class BatchTranslator:
        calls = 0

        def translate_batch(self, offered):
            self.calls += 1
            return {
                offered[0].id: "# Japanese\n",
                offered[1].id: "- Japanese item\n",
                offered[2].id: "Final Japanese sentence.\n",
            }, 321

        def __call__(self, _chunk):
            raise AssertionError("single-chunk translator path should not run")

    translator = BatchTranslator()
    result = harness.run_efficient_translation(
        tmp_path,
        targets=["README.md"],
        translator=translator,
        db_path=tmp_path / "state.db",
        run_id="test-efficient-batched-translation",
        events_path=tmp_path / "events.jsonl",
        manifest_path=tmp_path / "manifest.json",
        max_attempts_per_item=1,
        token_budget=10_000,
        batch_size=3,
    )

    assert result.stop is not None
    assert result.stop.name == "work_list_drained"
    assert translator.calls == 1
    assert result.tokens_used == 321
    assert target.read_text(encoding="utf-8") == (
        "# Japanese\n\n"
        "English divider\n\n"
        "- Japanese item\n\n"
        "Another divider\n\n"
        "Final Japanese sentence.\n"
    )


def test_loop_batches_three_markdown_chunks_across_two_iterations(tmp_path: Path):
    target = tmp_path / "README.md"
    jp = "\u65e5\u672c\u8a9e"
    target.write_text(
        f"# {jp}\n\n"
        "English divider\n\n"
        f"- {jp} item\n\n"
        "Another divider\n\n"
        f"Final {jp} sentence.\n",
        encoding="utf-8",
    )
    chunks = harness.scan_japanese_chunks(tmp_path, ["README.md"])

    class BatchTranslator:
        calls: list[list[str]] = []

        def translate_batch(self, offered):
            self.calls.append([chunk.id for chunk in offered])
            replacements = {
                chunks[0].id: "# Japanese\n",
                chunks[1].id: "- Japanese item\n",
                chunks[2].id: "Final Japanese sentence.\n",
            }
            return {chunk.id: replacements[chunk.id] for chunk in offered}, 100 * len(offered)

        def __call__(self, _chunk):
            raise AssertionError("single-chunk translator path should not run")

    translator = BatchTranslator()
    result = harness.run_efficient_translation(
        tmp_path,
        targets=["README.md"],
        translator=translator,
        db_path=tmp_path / "state.db",
        run_id="test-efficient-batched-two-iterations",
        events_path=tmp_path / "events.jsonl",
        manifest_path=tmp_path / "manifest.json",
        max_attempts_per_item=1,
        token_budget=10_000,
        batch_size=2,
    )

    assert result.stop is not None
    assert result.stop.name == "work_list_drained"
    assert result.iterations == 2
    assert translator.calls == [[chunks[0].id, chunks[1].id], [chunks[2].id]]
    assert result.tokens_used == 300
    assert target.read_text(encoding="utf-8") == (
        "# Japanese\n\n"
        "English divider\n\n"
        "- Japanese item\n\n"
        "Another divider\n\n"
        "Final Japanese sentence.\n"
    )


def test_patch_uses_line_span_not_first_matching_text(tmp_path: Path):
    target = tmp_path / "README.md"
    jp = "\u65e5\u672c\u8a9e"
    target.write_text(f"{jp}\nEnglish\n{jp}\n", encoding="utf-8")
    chunks = harness.scan_japanese_chunks(tmp_path, ["README.md"])

    result = harness.apply_translation(tmp_path, chunks[1], "Japanese\n")

    assert result.status == "done"
    assert target.read_text(encoding="utf-8") == f"{jp}\nEnglish\nJapanese\n"


def test_comment_patch_does_not_touch_matching_string_literal(tmp_path: Path):
    target = tmp_path / "module.py"
    jp_comment = "# \u65e5\u672c\u8a9e"
    target.write_text(f'value = "{jp_comment}"\n{jp_comment}\n', encoding="utf-8")
    chunks = harness.scan_japanese_chunks(tmp_path, ["module.py"])

    result = harness.apply_translation(tmp_path, chunks[0], "# Japanese")

    assert result.status == "done"
    assert target.read_text(encoding="utf-8") == f'value = "{jp_comment}"\n# Japanese\n'


def test_nested_docstring_patch_preserves_valid_python(tmp_path: Path):
    target = tmp_path / "module.py"
    target.write_text(
        "def f():\n"
        "    \"\"\"\u65e5\u672c\u8a9e\n"
        "    docs\"\"\"\n"
        "    return 1\n",
        encoding="utf-8",
    )
    chunks = harness.scan_japanese_chunks(tmp_path, ["module.py"])

    result = harness.apply_translation(tmp_path, chunks[0], "Japanese\ndocs")

    assert result.status == "done"
    compiled = compile(target.read_text(encoding="utf-8"), str(target), "exec")
    namespace = {}
    exec(compiled, namespace)
    assert namespace["f"].__doc__ == "Japanese\ndocs"


def test_patch_preserves_crlf_newlines(tmp_path: Path):
    target = tmp_path / "README.md"
    target.write_text("English\r\n\u65e5\u672c\u8a9e\r\n", encoding="utf-8", newline="")
    chunks = harness.scan_japanese_chunks(tmp_path, ["README.md"])

    result = harness.apply_translation(tmp_path, chunks[0], "Japanese\r\n")

    assert result.status == "done"
    assert target.read_bytes() == b"English\r\nJapanese\r\n"


def test_translation_error_preserves_token_cost(tmp_path: Path):
    chunk = harness.TranslationChunk(
        id="README.md:1",
        path="README.md",
        kind="text",
        old_text="\u65e5\u672c\u8a9e\n",
        text_for_translation="\u65e5\u672c\u8a9e\n",
        start_line=1,
        end_line=1,
        end_col=4,
    )
    (tmp_path / "README.md").write_text(chunk.old_text, encoding="utf-8")

    def translator(_chunk):
        raise harness.TranslationError("bad json", tokens=123)

    outcome = harness.make_act(tmp_path, translator)(chunk.to_payload())

    assert outcome.tokens == 123
    assert outcome.observation["status"] == "failed"


def test_manifest_is_stable_for_resume(tmp_path: Path):
    target = tmp_path / "README.md"
    target.write_text("\u65e5\u672c\u8a9e\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    first = harness.load_or_create_manifest(tmp_path, ["README.md"], manifest, resume=False)
    target.write_text("Japanese\n", encoding="utf-8")

    resumed = harness.load_or_create_manifest(tmp_path, ["README.md"], manifest, resume=True)

    assert [chunk.id for chunk in resumed] == [chunk.id for chunk in first]


def test_dry_run_json_is_ascii_safe(tmp_path: Path, capsys):
    target = tmp_path / "README.md"
    target.write_text("# \u65e5\u672c\u8a9e\n", encoding="utf-8")

    exit_code = harness.main(["--root", str(tmp_path), "--dry-run", "--target", "README.md"])

    captured = capsys.readouterr().out
    assert exit_code == 0
    captured.encode("ascii")
    payload = json.loads(captured)
    assert payload[0]["old_text"].replace("\r\n", "\n") == "# \u65e5\u672c\u8a9e\n"
