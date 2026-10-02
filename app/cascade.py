"""层叠（cascade）排序：来源 / 层 / 重要度 / 特异性 / 源码次序，以及淘汰证据生成。

普通声明：UA < user < author；同来源未分层 > 分层，晚层 > 早层。
important：来源与层级顺序反转（UA important 最强；分层 > 未分层，早层 > 晚层）。
之后依次比较特异性 (a, b, c) 与源码次序。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import CandidateOut, EliminatedOut

_ORIGIN_RANK = {"ua": 0, "user": 1, "author": 2}


@dataclass(frozen=True)
class Candidate:
    """一条参与层叠的候选声明（不可变，整树分析内核共享）。"""

    selector: str
    origin: str
    layer: str | None
    layer_index: int | None  # 层在声明顺序中的下标；None 表示未分层
    important: bool
    specificity: tuple[int, int, int]
    source_order: int
    rule_index: int  # 规则在请求 rules 数组中的下标（补丁定位用）
    decl_index: int  # 声明在规则块内的下标（同规则同属性后者胜出）
    property: str
    value: str

    def sort_key(self):
        """返回可比较的元组，越大优先级越高。"""
        imp = 1 if self.important else 0
        if not self.important:
            origin_rank = _ORIGIN_RANK[self.origin]  # author > user > ua
            # 未分层 (1, 0) 高于任何分层 (0, i)；分层中晚层（i 大）优先
            layer_rank = (0, self.layer_index) if self.layer_index is not None else (1, 0)
        else:
            origin_rank = -_ORIGIN_RANK[self.origin]  # 反转：ua important 最强
            # 分层 (1, -i) 高于未分层 (0, 0)；分层中早层（i 小）优先
            layer_rank = (1, -self.layer_index) if self.layer_index is not None else (0, 0)
        return (imp, origin_rank, layer_rank, self.specificity, self.source_order, self.decl_index)

    def to_out(self) -> CandidateOut:
        return CandidateOut(
            selector=self.selector,
            origin=self.origin,
            layer=self.layer,
            important=self.important,
            specificity=list(self.specificity),
            source_order=self.source_order,
            rule_index=self.rule_index,
            declaration_index=self.decl_index,
            property=self.property,
            value=self.value,
        )


def _fmt_spec(spec: tuple[int, int, int]) -> str:
    return f"({spec[0]}, {spec[1]}, {spec[2]})"


def _layer_detail(winner: Candidate, loser: Candidate) -> str:
    if winner.important:
        if winner.layer and not loser.layer:
            return f"!important 下层顺序反转，分层高于未分层：层 {winner.layer!r} 胜过未分层声明"
        return (
            f"!important 下层顺序反转，早层高于晚层："
            f"层 {winner.layer!r} 胜过层 {loser.layer!r}"
        )
    if not winner.layer and loser.layer:
        return f"同来源下未分层高于分层：未分层声明胜过层 {loser.layer!r}"
    return f"同来源下晚层高于早层：层 {winner.layer!r} 胜过层 {loser.layer!r}"


def eliminate(winner: Candidate, loser: Candidate) -> EliminatedOut:
    """比较胜者与其余候选，给出淘汰发生的第一个阶段及中文说明。"""
    wk, lk = winner.sort_key(), loser.sort_key()
    if wk[0] != lk[0]:
        stage, detail = "importance", "important 声明优先于普通声明"
    elif wk[1] != lk[1]:
        stage = "origin"
        if winner.important:
            detail = (
                f"!important 下来源顺序反转（ua > user > author）："
                f"{winner.origin} 胜过 {loser.origin}"
            )
        else:
            detail = f"来源优先级 author > user > ua：{winner.origin} 胜过 {loser.origin}"
    elif wk[2] != lk[2]:
        stage, detail = "layer", _layer_detail(winner, loser)
    elif wk[3] != lk[3]:
        stage = "specificity"
        detail = f"特异性 {_fmt_spec(winner.specificity)} 高于 {_fmt_spec(loser.specificity)}"
    else:
        stage = "source_order"
        detail = "重要性、来源、层与特异性均相同，源码次序靠后者胜出"
    return EliminatedOut(candidate=loser.to_out(), stage=stage, detail=detail)


def cascade(candidates: list[Candidate]):
    """按属性分组层叠。返回 {property: (winner, [EliminatedOut, ...])}。"""
    by_prop: dict[str, list[Candidate]] = {}
    for cand in candidates:
        by_prop.setdefault(cand.property, []).append(cand)
    result = {}
    for prop, group in by_prop.items():
        winner = max(group, key=lambda c: c.sort_key())
        losers = sorted((c for c in group if c is not winner), key=lambda c: c.sort_key(), reverse=True)
        result[prop] = (winner, [eliminate(winner, c) for c in losers])
    return result
