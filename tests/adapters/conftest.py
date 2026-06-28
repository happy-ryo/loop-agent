"""全 act アダプタ共通の契約テストハーネス(Issue #52)。

ここでは、個々のアダプタ(:class:`ClaudeCodeAct` / :class:`CodexAct` と将来の追加
アダプタ)が **`act` シームの 4 か条** と **`ActResult` の形** を満たすことを、1 つの
parametrize 群で横断検証するための :class:`AdapterSpec` と fixture を定義する。
具体的な共通ケースは ``test_contract.py``。

新しいアダプタを足したら :data:`ADAPTER_SPECS` に 1 行登録するだけで、結果の形 /
``failed`` セマンティクス / timeout graceful / 起動失敗 graceful / **token 二重計上
ガード** / 予算計上 / Mock 契約 / auth 環境継承 / stdin 安全性 が自動で適用される。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from loop_agent.adapters import (
    ClaudeCodeAct,
    ClaudeCodeResult,
    CodexAct,
    CodexResult,
    MockClaudeCodeAct,
    MockCodexAct,
)

# parse_tokens は **アダプタごとに意味論が違う**(claude は input+output+cache_creation
# を計上し cache_read を除外、codex は input+output のみで部分集合を除外)ため、共通 __init__ の再公開ではなく
# 各サブモジュールから直接取る。token 二重計上ガードはこの差を固定する。
from loop_agent.adapters.claude_code import parse_tokens as claude_parse_tokens
from loop_agent.adapters.codex import parse_tokens as codex_parse_tokens


# -- フェイク runner: subprocess.run を差し替えてコマンド/出力を制御する --------


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """``subprocess.run`` 互換の戻り値(CompletedProcess)を返す runner を作る。

    呼び出しごとの ``(command, kwargs)`` を ``.calls`` に記録するので、テストは
    渡されたコマンド/環境/stdin を検証できる。
    """

    def _runner(command, **kwargs):
        _runner.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    _runner.calls = []
    return _runner


def _timeout_runner(timeout_value: float = 600.0):
    """常に :class:`subprocess.TimeoutExpired` を送出する runner を作る。"""

    def _runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_value)

    return _runner


# -- アダプタ仕様: 共通ハーネスが各アダプタを駆動するのに必要な最小情報 --------


@dataclass(frozen=True)
class AdapterSpec:
    """1 つの act アダプタを共通契約テストに載せるための記述。

    Attributes:
        name: parametrize の id(``"claude_code"`` など)。
        act_cls: アダプタ本体(``runner=`` と ``<bin>_bin=`` を受ける ``@dataclass``)。
        result_cls: 観測オブジェクトの型(``ActResultBase`` を継承し ``ActResult`` 適合)。
        mock_cls: subprocess を使わない Mock(``responses=`` を受ける)。
        parse_tokens: そのアダプタの token 解析関数(意味論はアダプタ固有)。
        bin_kwarg: 実行ファイルを差し替える引数名(``"claude_bin"`` / ``"codex_bin"``)。
        success_stdout: 成功時の生 stdout サンプル(本文 ``success_text`` を含む)。
        success_text: ``success_stdout`` から取り出されるべき応答本文。
        success_tokens: ``success_stdout`` から計上されるべきトークン総数。
        token_guard_stdout: **素朴な合算なら過大計上されうる** usage サンプル
            (codex は部分集合キー cached/reasoning を含み、claude は除外対象の
            cache_read を巨大値で含む。アダプタごとに「足し方を間違えると総量がずれる」形)。
        token_guard_expected: そのアダプタの意味論で正しいトークン総数
            (二重計上していたら不一致になる; Issue #55 の bug class を catch)。
        expects_devnull: ``__call__`` が ``stdin=DEVNULL`` を渡すべきか
            (対話入力を読む CLI のハング防止)。
    """

    name: str
    act_cls: type
    result_cls: type
    mock_cls: type
    parse_tokens: Callable[[str], int]
    bin_kwarg: str
    success_stdout: str
    success_text: str
    success_tokens: int
    token_guard_stdout: str
    token_guard_expected: int
    expects_devnull: bool

    def make_act(self, **kwargs: Any):
        """テスト用にアダプタを生成する小ヘルパ(``runner`` 等をそのまま渡す)。"""
        return self.act_cls(**kwargs)


# Claude Code: usage の input/output/cache_creation は計上するが cache_read は
# token-cost ポリシで **除外** する(コストが軽く累積で膨らむ; Issue #55)。
# success の総量は 100+40+10=150(cache_read=5 は計上しない)。
_CLAUDE_SUCCESS = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "done fixing", '
    '"usage": {"input_tokens": 100, "output_tokens": 40, '
    '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}}'
)

# token_guard: cache_read を **わざと巨大(999999)** にして、誤って合算したら
# 150 に絶対一致しないようにする(Issue #55 の cache_read 累積 bug を強く検出する。
# codex が部分集合キーを 9999/8888 にするのと同じ狙い)。
_CLAUDE_TOKEN_GUARD = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "done fixing", '
    '"usage": {"input_tokens": 100, "output_tokens": 40, '
    '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 999999}}'
)

# Codex: cached_input_tokens は input の、reasoning_output_tokens は output の
# 部分集合。総量は input+output のみ(100+40=140)。token_guard では部分集合を
# わざと巨大(9999/8888)にして、合算してしまえば 140 に決して一致しないようにする
# (= 二重計上の回帰を強く検出する)。
_CODEX_SUCCESS = "\n".join(
    [
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"done fixing"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":60,'
        '"output_tokens":40,"reasoning_output_tokens":10}}',
    ]
)
_CODEX_TOKEN_GUARD = (
    '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":9999,'
    '"output_tokens":40,"reasoning_output_tokens":8888}}'
)


ADAPTER_SPECS = [
    AdapterSpec(
        name="claude_code",
        act_cls=ClaudeCodeAct,
        result_cls=ClaudeCodeResult,
        mock_cls=MockClaudeCodeAct,
        parse_tokens=claude_parse_tokens,
        bin_kwarg="claude_bin",
        success_stdout=_CLAUDE_SUCCESS,
        success_text="done fixing",
        success_tokens=150,
        token_guard_stdout=_CLAUDE_TOKEN_GUARD,
        token_guard_expected=150,  # cache_read(=999999)は計上しない(cost ポリシ)。
        expects_devnull=False,  # claude は stdin を明示せず継承する。
    ),
    AdapterSpec(
        name="codex",
        act_cls=CodexAct,
        result_cls=CodexResult,
        mock_cls=MockCodexAct,
        parse_tokens=codex_parse_tokens,
        bin_kwarg="codex_bin",
        success_stdout=_CODEX_SUCCESS,
        success_text="done fixing",
        success_tokens=140,
        token_guard_stdout=_CODEX_TOKEN_GUARD,
        token_guard_expected=140,  # 部分集合(cached/reasoning)を除外する。
        expects_devnull=True,  # codex は stdin=DEVNULL で誤読/ハングを防ぐ。
    ),
]


@pytest.fixture(params=ADAPTER_SPECS, ids=lambda spec: spec.name)
def adapter_spec(request) -> AdapterSpec:
    """登録済み全アダプタを横断 parametrize する fixture。"""
    return request.param


@pytest.fixture
def make_runner() -> Callable[..., Any]:
    """``CompletedProcess`` を返すフェイク runner のファクトリ(``.calls`` 記録付き)。"""
    return _completed


@pytest.fixture
def make_timeout_runner() -> Callable[..., Any]:
    """``TimeoutExpired`` を送出するフェイク runner のファクトリ。"""
    return _timeout_runner
