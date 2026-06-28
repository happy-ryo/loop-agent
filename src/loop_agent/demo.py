"""検証駆動デモのエンジン: 実テストの exit-code を ground truth に回す再利用フック。

このモジュールは loop-agent のループコア(:func:`loop_agent.run_loop`)を、
*実際のテスト実行* に当てるための最小の足場を提供する。検証(verify)は
LLM judge ではなく、subprocess で起動したテストコマンドの ``returncode`` を
唯一の真実として使う(report.md R1: ground truth 優先)。

3 つの注入可能フックを提供する:

- :class:`CandidateApplier` -- ``act``。反復ごとに「次の修正候補ソース」を
  対象ファイルへ書き込む(LLM 修正役の決定的スタブ)。
- :class:`ExitCodeVerifier`    -- ``verify``。sandbox 内でテストコマンドを実行し、
  exit-code 0(green)で ``goal_met=True`` を返す。green でループは自然終了する。
- :func:`attempt_index`    -- ``gather``。次に試す候補の番号(= 反復回数)を
  context として act へ渡す最小の観測シーム。

具体シナリオ(壊れた関数を直すまで回す)は ``examples/verify_driven_demo.py``、
pytest による実走検証は ``tests/test_verify_demo.py`` を参照。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Mapping, Optional, Sequence

from .errors import ConfigError
from .loop import ActOutcome, VerifyOutcome
from .state import LoopState

# sandbox のテストを最も素朴に「exit-code で」回すデフォルトコマンド。
#
# ``-B`` は重要: 反復で書き換える対象ファイルは「同じ byte 長・粗い mtime 解像度」
# だと CPython の .pyc 検証(mtime+size)をすり抜け、前反復の壊れた版の bytecode
# キャッシュを読み続けてしまう(直したのに red が続く偽陰性)。毎回 source から
# 再コンパイルさせるため bytecode 書き込みを全実行で無効化する。
#
# ``-B`` は bytecode の *書き込み* を抑止する(等 byte 長候補 + 粗い mtime 解像度で
# stale .pyc を量産する偽陰性を防ぐ)。ただし *既存* の .pyc の *読み込み* は防げないため、
# verify 前に sandbox の __pycache__ を毎回削除する(:func:`_clear_pycache`)。両者で
# 「毎回 source から再コンパイル」を保証する。
#
# 末尾の ``"."`` と ``-o addopts=`` は重要: workdir が(一時ディレクトリではなく)
# 祖先に pytest 設定を持つ checkout 内に置かれた場合、位置引数なしの pytest は
# 祖先を rootdir に選び ``testpaths`` で別スイートを収集したり ``addopts`` を適用したり
# しうる。すると exit-code は「その sandbox の」ground truth でなくなる。
#   - ``"."`` : 収集対象を cwd(= sandbox)に限定する。明示パスは ``testpaths`` を上書きする。
#   - ``-o addopts=`` : 祖先 ini の ``addopts`` を空に上書きし、起動側設定の混入を断つ。
# ``-p no:cacheprovider`` は pytest キャッシュ設定を拾わないため。
DEFAULT_TEST_COMMAND: tuple[str, ...] = (
    sys.executable,
    "-B",
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "-o",
    "addopts=",
    ".",
)

# 1 回のテスト実行に課す上限(秒)。ハングする候補(例: 無限ループを入れた修正)で
# verify が永久にブロックすると、ループ境界で評価される Timeout/MaxIterations すら
# 効かなくなる。これを防ぐため subprocess に timeout を課し、超過は red 扱いにして
# 制御をループへ戻す(暴走防止。本格実証は #7)。
DEFAULT_TEST_TIMEOUT: float = 120.0


# 子プロセスのテスト実行(exit-code = ground truth)を、起動側の環境に依存させ
# ないための除外キー。これらは nested pytest に CLI オプション等を注入でき、green な
# sandbox を false red/green に反転させて検証を非決定にしうる:
#   - PYTEST_ADDOPTS: pytest 公式の「nested 実行へオプションを伝播」する仕組み。
#     例えば外側を ``PYTEST_ADDOPTS='-m somemarker'`` で起動すると子の rc=5(no tests)
#     になり、green な sandbox が false red になる(tox / CI / -W 付与で実際に起こる)。
#   - PYTEST_PLUGINS: 特定プラグインを強制ロードさせる。
#   - COV_CORE_*: 外側 --cov 実行が子へ計測を注入する。
# これらを除いた os.environ のコピーを子へ渡す。
# なお PYTEST_DISABLE_PLUGIN_AUTOLOAD は「除外」ではなく後で 1 に「強制」する
# (除外すると起動側の =1 を取り消して周囲プラグインを再有効化してしまうため。下記参照)。
_ENV_DENYLIST = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
)


def sandbox_env() -> dict[str, str]:
    """sandbox のテスト実行に渡す、起動環境から隔離した環境変数を返す。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _ENV_DENYLIST and not key.startswith("COV_CORE_")
    }
    # bytecode を一切書かせない(DEFAULT_TEST_COMMAND の -B と同趣旨。stale .pyc 回避)。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 周囲の pytest プラグインの autoload を一律無効化し、インストール済みプラグインの
    # 有無に依存しない hermetic な実行にする。起動側が未設定/0 でも強制的に 1 にする
    # (sandbox のテストは素の pytest core のみで完結する)。
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _clear_pycache(workdir: Path) -> None:
    """``workdir`` 配下の ``__pycache__`` を全削除し、stale .pyc の読み込みを防ぐ。

    ``-B`` は .pyc の *書き込み* しか抑止せず、(非 -B の手動 pytest 実行などで)
    既に存在する .pyc は読まれてしまう。等 byte 長候補 + 粗い mtime では (mtime,size)
    検証をすり抜けて前反復の壊れた bytecode が使われうるため、verify ごとに消す。
    削除対象は sandbox 内に限定される。
    """
    for cache in workdir.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def write_sandbox(workdir: Path, files: Mapping[str, str]) -> None:
    """``files``(相対パス -> 内容)を ``workdir`` 配下へ書き出す。

    日本語を含みうるので UTF-8 を明示する。中間ディレクトリは自動生成する。
    """
    for rel, content in files.items():
        path = workdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def attempt_index(state: LoopState) -> int:
    """``gather`` フック: 次に試す候補の番号(= これまでの反復回数)を返す。

    検証(ground truth)は :class:`ExitCodeVerifier` の 1 回だけに集約したいので、
    gather はテストを実行せず、どの候補を当てるかという軽い context のみを渡す。
    """
    return state.iteration


@dataclass
class CandidateApplier:
    """``act`` フック: 反復ごとに次の修正候補ソースを ``target`` へ書き込む。

    実運用では LLM が失敗を見て修正パッチを生成する箇所。PoC では候補列を
    決定的に当てるスタブとし、ループ機構と ground truth 検証の実証に集中する。
    候補を使い切ったら最後の候補に張り付く(= "現状の最善手を試し続ける")ので、
    直らないシナリオでもハード上限が止めるまで安全に反復できる。

    ``cost_per_step`` は 1 ステップあたりに計上するトークン量で、
    :class:`~loop_agent.conditions.TokenBudget` のデモに使える(既定は 0)。
    """

    target: Path
    candidates: Sequence[str]
    cost_per_step: int = 0
    applied: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ConfigError("CandidateApplier requires at least one candidate")

    def __call__(self, attempt: int) -> ActOutcome:
        index = min(attempt, len(self.candidates) - 1)
        self.target.write_text(self.candidates[index], encoding="utf-8")
        self.applied.append(index)
        return ActOutcome(
            observation=f"applied candidate #{index}",
            tokens=self.cost_per_step,
        )


@dataclass
class ExitCodeVerifier:
    """``verify`` フック: sandbox でテストを実行し exit-code を ground truth に使う。

    ``returncode == 0`` を green と見なし ``goal_met=True`` を返す。これにより
    ループは「テストが green になった瞬間に」自然終了する。各実行の returncode は
    :attr:`exit_codes` に記録され、テストや観測から後で参照できる。

    ``timeout`` を超えたテスト実行は子プロセスを kill し、番兵 exit-code(124)を
    記録して red 扱い(``goal_met=False``)で返す。これによりハングする候補でも
    制御がループへ戻り、境界の Timeout/MaxIterations が働く。``None`` で無制限。
    """

    workdir: Path
    command: Sequence[str] = DEFAULT_TEST_COMMAND
    timeout: Optional[float] = DEFAULT_TEST_TIMEOUT
    exit_codes: list[int] = field(default_factory=list)

    # ハング(timeout)を表す番兵 exit-code。慣例的なタイムアウト終了コードに合わせる。
    # ClassVar なので dataclass のフィールド(コンストラクタ引数)にはならない。
    TIMEOUT_EXIT_CODE: ClassVar[int] = 124

    def __call__(self, _outcome: ActOutcome) -> VerifyOutcome:
        # 既存 .pyc を消してから走らせ、必ず source から再コンパイルさせる。
        _clear_pycache(self.workdir)
        try:
            proc = subprocess.run(
                list(self.command),
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                env=sandbox_env(),
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            # 子は subprocess.run により kill 済み。green ではないので red 扱い。
            self.exit_codes.append(self.TIMEOUT_EXIT_CODE)
            return VerifyOutcome(
                goal_met=False, detail=f"red (timeout {self.timeout:g}s)"
            )
        self.exit_codes.append(proc.returncode)
        green = proc.returncode == 0
        detail = "green" if green else f"red (exit={proc.returncode})"
        return VerifyOutcome(goal_met=green, detail=detail)
