"""去重与冲突编排：按显式优先级把事件分成 已应用 / 重复 / 冲突 / 隔离 / 暂缓。

判定顺序（优先级）固定，且每一步都是集合运算，不依赖输入顺序：

    quarantine（契约不通过） > held（时间超前参考时钟） > conflict（版本冲突/墓碑复活）
    > duplicate（同签名） > apply

`held` 在冲突判定之前分流：被挡下的未来时间不参与去重与冲突，既不会伪造冲突，
也不会把同一键的正常版本整组拖进冲突表。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ... import config, reasons
from ..m2_schema.service import Norm, distribution
from . import conflict, dedup

PRIORITY = ("quarantine", "held", "conflict", "duplicate", "apply")


@dataclass
class Classification:
    applied: list[Norm] = field(default_factory=list)
    duplicates: list[Norm] = field(default_factory=list)
    conflicts: list[Norm] = field(default_factory=list)
    quarantine: list[Norm] = field(default_factory=list)
    held: list[Norm] = field(default_factory=list)
    detail_of: dict[int, str] = field(default_factory=dict)
    ref_of: dict[int, str] = field(default_factory=dict)

    @property
    def counters(self) -> dict:
        return {
            "applied": len(self.applied),
            "duplicates": len(self.duplicates),
            "conflicts": len(self.conflicts),
            "quarantine": len(self.quarantine),
            "held": len(self.held),
            "total": (len(self.applied) + len(self.duplicates)
                      + len(self.conflicts) + len(self.quarantine)
                      + len(self.held)),
        }

    @property
    def all_rows(self) -> list[Norm]:
        return sorted(self.applied + self.duplicates + self.conflicts
                      + self.quarantine + self.held, key=lambda n: n.seq)


def classify(norms: list[Norm], hold=None) -> Classification:
    ordered = sorted(norms, key=lambda n: n.seq)
    result = Classification()

    quarantine = [n for n in ordered if n.errors]
    for norm in quarantine:
        norm.row_class = "quarantine"
        norm.reason_code = norm.errors[0]
        result.detail_of[norm.seq] = norm.detail or reasons.label(norm.reason_code)
    result.quarantine = quarantine
    clean = [n for n in ordered if not n.errors]

    held: list[Norm] = []
    if hold is not None:
        held = [n for n in clean if hold.blocks(n)]
        for norm in held:
            norm.row_class = "held"
            norm.reason_code = "W_FUTURE_HELD"
            norm.detail = hold.detail_of.get(norm.seq, reasons.label("W_FUTURE_HELD"))
            result.detail_of[norm.seq] = norm.detail
        result.held = sorted(held, key=lambda n: n.seq)
    held_seqs = {n.seq for n in held}
    live = [n for n in clean if n.seq not in held_seqs]

    in_conflict = conflict.version_conflicts(live)
    conflict_seqs = {n.seq for n in in_conflict}
    peers = conflict.group_peers(in_conflict)
    for norm in in_conflict:
        norm.row_class = "conflict"
        norm.reason_code = "C_VERSION_CONFLICT"
        norm.detail = conflict.conflict_detail(
            norm, peers.get(conflict.group_key(norm), [])) or "同版本内容不同"
        result.detail_of[norm.seq] = norm.detail
    for norm in conflict.tombstone_resurrections(
            [n for n in live if n.seq not in conflict_seqs]):
        norm.row_class = "conflict"
        norm.reason_code = "C_TOMBSTONE_RESURRECT"
        norm.detail = (
            f"版本 {norm.version} 小于该键删除版本 "
            f"{conflict.max_delete_versions(live).get(norm.key(), 0)}")
        result.detail_of[norm.seq] = norm.detail
        in_conflict.append(norm)
    result.conflicts = sorted(in_conflict, key=lambda n: n.seq)
    conflict_seqs = {n.seq for n in result.conflicts}

    remaining = [n for n in live if n.seq not in conflict_seqs]
    kept, dups, dup_codes, dup_refs = dedup.dedup(remaining)
    for norm in dups:
        norm.row_class = "duplicate"
        norm.reason_code = dup_codes.get(norm.seq, "D_EXACT_DUP")
        first = dup_refs.get(norm.seq, "")
        norm.detail = f"与首条 {first} 同签名"
        result.detail_of[norm.seq] = norm.detail
        result.ref_of[norm.seq] = first
    result.duplicates = sorted(dups, key=lambda n: n.seq)

    for norm in kept:
        norm.row_class = "applied"
        norm.reason_code = None
        if norm.codes:
            result.detail_of[norm.seq] = "；".join(reasons.label(c) for c in norm.codes)
    result.applied = sorted(kept, key=lambda n: n.seq)
    return result


def distribution_rows(norms: list[Norm]) -> list[dict]:
    return distribution(norms)


def conflict_page_rows(rows: list[Norm]) -> list[dict]:
    out = []
    for norm in rows:
        out.append({
            "row_ref": norm.row_ref, "key": f"{norm.table}/{norm.pk}",
            "table": norm.table, "pk": norm.pk, "version": norm.version,
            "op": norm.op, "event_time": norm.time_iso,
            "payload": norm.payload,
            "amount": None if norm.amount is None else str(norm.amount),
            "reason_code": norm.reason_code, "detail": norm.detail,
        })
    return out


def rule_table() -> list[dict]:
    """页面上展示的优先级表（判定顺序而非业务描述）。"""
    labels = {
        "quarantine": "契约不通过（字段/类型/必填）",
        "held": "时间超前参考时钟，整条暂缓",
        "conflict": "同版本不同内容 / 墓碑后低版本",
        "duplicate": "同签名重复到达",
        "apply": "按版本升序应用",
    }
    return [{"rank": i + 1, "bucket": key, "label": labels[key]}
            for i, key in enumerate(PRIORITY)]


def late_window_text() -> str:
    return f"{config.LATE_WINDOW_MINUTES} 分钟"


def distribution_from_rows(rows: list[dict]) -> list[dict]:
    """已落库事件 → 原因码分布（页面与导出共用一份聚合）。"""
    counts: dict[str, int] = {}
    for row in rows:
        codes: list[str] = []
        if row.get("reason_code"):
            codes.append(row["reason_code"])
        for code in row.get("flags") or []:
            if code not in codes:
                codes.append(code)
        for code in codes:
            counts[code] = counts.get(code, 0) + 1
    out = []
    for code, count in counts.items():
        reason = reasons.get(code)
        out.append({"code": code, "count": count, "level": reason.level,
                    "stage": reason.stage, "label": reason.title})
    return sorted(out, key=lambda r: (reasons.LEVEL_ORDER[r["level"]], -r["count"], r["code"]))
