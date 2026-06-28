"""Tests for the ``loop-agent install-skills`` subcommand (Issue #73).

The reference-bundled coding-agent skill ships *inside* the package at
``loop_agent/skills/loop-agent/``; ``install-skills`` copies it into a
``.claude/skills/`` directory a coding agent will discover. These tests pin that

- the bundled skill is present in the installed package,
- the copy is faithful (every file, byte-for-byte) to the bundle,
- the default / ``--user`` / ``--target`` destinations resolve correctly, and
- re-running is idempotent (converges, never errors).

They drive the public :func:`loop_agent.cli.main` and never touch the real
home / project directory (everything is redirected under ``tmp_path``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loop_agent.cli import _bundled_skill_dir, main


def _tree(root: Path) -> dict[str, bytes]:
    """Map every file under ``root`` to its bytes, keyed by POSIX relpath."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_bundled_skill_dir_is_present_and_well_formed() -> None:
    src = _bundled_skill_dir()
    assert src.is_dir(), f"bundled skill missing at {src}"
    assert (src / "SKILL.md").is_file()
    assert (src / "references" / "design-philosophy.md").is_file()
    assert (src / "references" / "seams.md").is_file()
    assert (src / "references" / "examples" / "translation.md").is_file()


def test_install_to_target_copies_the_whole_bundle(tmp_path: Path) -> None:
    dest = tmp_path / "myskill"
    rc = main(["install-skills", "--target", str(dest)])
    assert rc == 0
    # Faithful copy: identical relative-path set and identical bytes.
    assert _tree(dest) == _tree(_bundled_skill_dir())


def test_install_is_idempotent(tmp_path: Path) -> None:
    dest = tmp_path / "myskill"
    assert main(["install-skills", "--target", str(dest)]) == 0
    first = _tree(dest)
    # Second run must not error (dirs already exist) and must converge to the
    # same tree, not duplicate or diverge.
    assert main(["install-skills", "--target", str(dest)]) == 0
    assert _tree(dest) == first


def test_reinstall_removes_stale_files(tmp_path: Path) -> None:
    # A file left by an older version (renamed/removed upstream) must not survive
    # a reinstall: the install converges to exactly the bundled tree.
    dest = tmp_path / "myskill"
    assert main(["install-skills", "--target", str(dest)]) == 0
    stale = dest / "references" / "OLD-renamed-reference.md"
    stale.write_text("stale", encoding="utf-8")
    assert main(["install-skills", "--target", str(dest)]) == 0
    assert not stale.exists()
    assert _tree(dest) == _tree(_bundled_skill_dir())


def test_refuses_unrelated_nonempty_target(tmp_path: Path) -> None:
    # A non-empty dir with no SKILL.md is not a skill install; refuse to wipe it.
    dest = tmp_path / "notaskill"
    dest.mkdir()
    keep = dest / "important.txt"
    keep.write_text("do not delete", encoding="utf-8")
    rc = main(["install-skills", "--target", str(dest)])
    assert rc == 2  # ConfigError -> main() returns exit code 2
    assert keep.read_text(encoding="utf-8") == "do not delete"


def test_refuses_overwriting_a_different_skill(tmp_path: Path) -> None:
    # A directory that is *another* skill (SKILL.md with a different name) must
    # not be wiped: the guard keys on the loop-agent frontmatter, not just on
    # the presence of a SKILL.md.
    dest = tmp_path / "other-skill"
    dest.mkdir()
    other = dest / "SKILL.md"
    other.write_text("---\nname: some-other-skill\n---\nbody\n", encoding="utf-8")
    rc = main(["install-skills", "--target", str(dest)])
    assert rc == 2
    assert other.read_text(encoding="utf-8").startswith("---\nname: some-other-skill")


def test_default_destination_is_project_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No flag -> <cwd>/.claude/skills/loop-agent.
    monkeypatch.chdir(tmp_path)
    rc = main(["install-skills"])
    assert rc == 0
    dest = tmp_path / ".claude" / "skills" / "loop-agent"
    assert (dest / "SKILL.md").is_file()


def test_user_destination_is_home_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # --user -> ~/.claude/skills/loop-agent; redirect home so the real one is
    # never written.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    rc = main(["install-skills", "--user"])
    assert rc == 0
    dest = fake_home / ".claude" / "skills" / "loop-agent"
    assert (dest / "SKILL.md").is_file()


def test_user_and_target_are_mutually_exclusive() -> None:
    # argparse rejects the combination with a usage error (exit code 2).
    with pytest.raises(SystemExit) as exc:
        main(["install-skills", "--user", "--target", "somewhere"])
    assert exc.value.code == 2


def test_install_skills_help_lists_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["install-skills", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "install-skills" in out
    assert "--user" in out and "--target" in out
