"""episodic memory: 試行をまたぐ言語的 lesson の有界な蓄積 + 取込前検証 (Issue #22).

外側 Reflexion ループ (report.md S4.4 / S5 Phase3) が episode 境界で生成する「言語的
指針 (lesson)」を保持し、次 episode の context へ ``render()`` で配線するための器。
本モジュールは **葉モジュール** で、``claude_loop.state`` (StepRecord) 以外に依存しない
(LLM 依存も無い)。意味的な検証が要るなら :data:`LessonVerifier` を注入する。

安全設計 (report.md S6 + RQGM / Issue #4) ― ここで担保する不変条件:

- **取込前検証 (memory 取込前検証 / false lesson 注入を弾く)**: lesson は admit される
  前に :func:`default_admit` の **構造的 (LLM 非依存) ゲート** を通す。lesson の
  ``provenance`` が実際に起きた軌跡 step の署名 (:func:`step_signature`) に一致すること、
  ``support`` がしきい値以上であること、テキストが上限内であることを要求する。注入された
  でっち上げ lesson (実 step に紐づかない) は ``provenance`` 不一致で弾かれる。
- **自己申告 support を信用しない**: ``Lesson.support`` は **駆動側 (run_reflexion) が
  権威ある信号から再計算して上書き** する値であり、``reflect`` フックが詐称しても
  admission には効かない (driver が overwrite する。:mod:`claude_loop.reflexion` 参照)。
- **肥大化・劣化の抑止 (反復上限)**: 保持件数 ``cap`` / 1 lesson の文字上限
  ``per_lesson_chars`` / ``render()`` の総バイト上限 ``render_byte_cap`` で、蓄積も
  next-context への描画も有界にする (report.md「3 反復で 4 倍膨張」緩和)。
- **決定的・価値考慮の eviction**: 溢れたら **support 最小 → episode 最古** の決定的順序で
  捨てる。高 support の load-bearing な lesson を低 support の周辺 lesson で押し出さない
  (= 効いた学びを忘れない)。eviction 順は agent が動かせるスコアに依存しない。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

from .state import StepRecord


def step_signature(record: StepRecord) -> str:
    """軌跡 step の **内容ベース** の安定署名を返す (provenance grounding 用)。

    iteration だけだと ``step-0`` のように常に存在する署名を詐称できてしまうため、
    observation / detail / goal_met まで含めた短いハッシュにする。実際に起きた step を
    引用する lesson だけが一致し、軌跡にアクセスできない注入 lesson は一致できない。
    """
    payload = f"{record.iteration}|{record.goal_met}|{record.detail}|{record.observation!r}"
    digest = hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()
    return f"step-{record.iteration}-{digest[:12]}"


def trajectory_signatures(history: Sequence[StepRecord]) -> frozenset[str]:
    """軌跡全 step の署名集合 (provenance 一致判定に使う)。"""
    return frozenset(step_signature(s) for s in history)


@dataclass(frozen=True)
class Lesson:
    """1 つの言語的指針 (Reflexion の verbal reinforcement の蓄積単位)。

    - ``text``       : 次 context に配線する言語的指針。
    - ``episode``    : 由来 episode 番号 (eviction の tie-break と監査に使う)。
    - ``provenance`` : 由来 step の署名 (:func:`step_signature`)。grounding の鍵。
    - ``support``    : 権威ある信号から **driver が再計算** した支持度。``reflect`` が
      設定した値は driver に上書きされるので、admission で信用してよい値はこれだけ。
    """

    text: str
    episode: int
    provenance: str
    support: float = 0.0


@dataclass(frozen=True)
class LessonVerdict:
    """取込前検証の判定。``admit`` が False なら memory に入れない。"""

    admit: bool
    reason: str = ""


# 取込前検証フック: (lesson, outcome) -> LessonVerdict。outcome は ``.history`` を持つ
# read-only view (duck typing。EpisodeOutcome を import せず葉性を保つ)。既定は
# :func:`default_admit` (構造的)。意味的・効果ベースの検証は呼び出し側が差し替える。
LessonVerifier = Callable[[Lesson, Any], LessonVerdict]


# 構造ゲートの既定しきい値。support は driver が grounding から再計算した値 (0.0 or 1.0
# 相当) なので、> 0 を要求すれば「実 step に紐づかない注入 lesson」を弾ける。
DEFAULT_MIN_SUPPORT = 1e-9


def default_admit(lesson: Lesson, outcome: Any) -> LessonVerdict:
    """LLM 非依存の **構造的** 取込前検証 (false lesson 注入を弾く既定ゲート)。

    判定基準 (すべて構造的・数値的。意味判定や model 呼び出しを一切しない):

    1. ``text`` が非空であること。
    2. ``provenance`` が ``outcome.history`` の実 step 署名のいずれかに一致すること
       (grounding。注入されたでっち上げ lesson は実 step に紐づかないので弾かれる)。
    3. ``support`` が :data:`DEFAULT_MIN_SUPPORT` 以上であること (driver が再計算した
       権威支持度。自己申告ではない)。

    文字数上限・重複排除は :class:`EpisodicMemory.admit` 側で行う (容量ポリシーは memory
    の責務)。意味的な検証や production 分布での効果検証が要るなら、本関数の代わりに
    :data:`LessonVerifier` を注入すること (本関数が load-bearing であることはテストで実証)。
    """
    if not lesson.text or not lesson.text.strip():
        return LessonVerdict(admit=False, reason="empty lesson text")
    history = getattr(outcome, "history", ())
    if lesson.provenance not in trajectory_signatures(history):
        return LessonVerdict(
            admit=False,
            reason=f"provenance {lesson.provenance!r} not grounded in trajectory",
        )
    if lesson.support < DEFAULT_MIN_SUPPORT:
        return LessonVerdict(
            admit=False, reason=f"insufficient recomputed support {lesson.support!r}"
        )
    return LessonVerdict(admit=True, reason="grounded")


@dataclass
class EpisodicMemory:
    """有界な episodic memory。次 context へ配線する lesson の器。

    Args:
        cap: 保持する lesson の最大件数。超過分は決定的順序で evict する。
        per_lesson_chars: 1 lesson の ``text`` 文字上限 (超過は admit 時に切り詰める)。
        render_byte_cap: :meth:`render` が返す文字列の総文字上限 (next-context の肥大化を
            止める最終ガード)。

    すべての上限は report.md S6 の「reflection 出力の肥大化・劣化」を反復上限で防ぐための
    もの。``cap`` を満たす eviction は **support 最小 → episode 最古 → 挿入最古** の決定的
    順序で、agent が動かせるスコアに依存しない (高 support の load-bearing lesson を守る)。
    """

    cap: int = 8
    per_lesson_chars: int = 512
    render_byte_cap: int = 4096
    _lessons: list[Lesson] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cap < 1:
            raise ValueError("EpisodicMemory cap must be >= 1")
        if self.per_lesson_chars < 1:
            raise ValueError("EpisodicMemory per_lesson_chars must be >= 1")
        if self.render_byte_cap < 1:
            raise ValueError("EpisodicMemory render_byte_cap must be >= 1")

    def lessons(self) -> tuple[Lesson, ...]:
        """現在保持している lesson の **読み取り専用** ビュー (gather 配線用)。"""
        return tuple(self._lessons)

    def __len__(self) -> int:
        return len(self._lessons)

    def _normalized(self, text: str) -> str:
        return " ".join(text.split()).lower()

    def admit(self, lesson: Lesson, verdict: LessonVerdict) -> bool:
        """検証を通った lesson を取り込む。取り込めたら ``True``。

        ``verdict.admit`` が False、または正規化テキストが既存 lesson と重複する場合は
        取り込まない (重複は memory の責務)。``text`` は ``per_lesson_chars`` に切り詰める。
        ``cap`` 超過時は決定的順序 (support 最小 → episode 最古) で 1 件 evict する。
        """
        if not verdict.admit:
            return False
        norm = self._normalized(lesson.text)
        if any(self._normalized(existing.text) == norm for existing in self._lessons):
            return False
        if len(lesson.text) > self.per_lesson_chars:
            lesson = replace(lesson, text=lesson.text[: self.per_lesson_chars])
        self._lessons.append(lesson)
        if len(self._lessons) > self.cap:
            self._evict_one()
        return True

    def _evict_one(self) -> None:
        """容量超過時に 1 件捨てる: support 最小 → episode 最古 → 挿入最古 の決定的順序。"""
        # min() は最初の最小要素を返すので、リスト順 (挿入順) が最終 tie-break になる。
        victim = min(
            range(len(self._lessons)),
            key=lambda i: (self._lessons[i].support, self._lessons[i].episode, i),
        )
        del self._lessons[victim]

    def render(self) -> str:
        """next-context へ配線する lesson ブロックを返す (``render_byte_cap`` で有界)。

        support 降順 (効いた学びを優先) → episode 昇順で並べ、1 行ずつ積む。**UTF-8 エンコード
        後のバイト数**が ``render_byte_cap`` を超える行は積まずに打ち切る (肥大化の最終ガード)。
        非 ASCII (日本語・絵文字等) でも文字数ではなく実バイト数で有界にする。lesson が無ければ
        空文字列を返す。
        """
        if not self._lessons:
            return ""
        ordered = sorted(self._lessons, key=lambda l: (-l.support, l.episode))
        lines: list[str] = ["## Lessons from prior episodes"]
        for lesson in ordered:
            line = f"- {lesson.text}"
            candidate = "\n".join(lines + [line])
            if len(candidate.encode("utf-8")) > self.render_byte_cap:
                break
            lines.append(line)
        if len(lines) == 1:
            # ヘッダだけで上限超過する病的ケース: バイト境界で安全に丸める (壊れた multibyte を
            # 残さないよう errors='ignore' で復号)。
            return lines[0].encode("utf-8")[: self.render_byte_cap].decode("utf-8", "ignore")
        return "\n".join(lines)


__all__ = [
    "Lesson",
    "LessonVerdict",
    "LessonVerifier",
    "EpisodicMemory",
    "default_admit",
    "step_signature",
    "trajectory_signatures",
    "DEFAULT_MIN_SUPPORT",
]
