"""时钟保护：把「超前参考时钟太多」的记录整条挡在正式水位与正式快照之外。

为什么要挡：CDC 导出偶尔混入源库时钟错乱的未来时间。只要让它进「已应用」，
`max(event_time) − 窗口` 就会一次跳到未来，后续正常变更全被标成迟到，快照也像
当天已经封板。直接丢弃又会把「确实跨日到达」的补数一起漏掉。

裁决：

- 参考时刻 = 本次运行的参考时钟（默认墙钟，可用 `reference_now` 固定，便于复算）；
- 容差内的轻微超前照常应用，避免误伤传输延迟与跨日到达；
- 超前超过容差 → `W_FUTURE_HELD`，整条暂缓（held）：不进水位、不进正式快照、
  不参与去重冲突，只作为证据保留，在核对页逐条列出；
- 参考时钟追上该事件后，用固定的新参考时刻重新采信即可放行（新批次，不重复）。

暂缓不是失败：本批仍然完成，正常数据照常发布；若整批都被挡下，正式快照保持
上一版（批次状态 `held_only`，不会顶掉已发布的快照）。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..m2_schema.coerce import iso_utc, parse_iso
from ..m2_schema.service import Norm


@dataclass
class Hold:
    reference: dt.datetime
    tolerance: dt.timedelta
    horizon: dt.datetime
    seqs: set[int] = field(default_factory=set)
    detail_of: dict[int, str] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    signature: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.seqs)

    def blocks(self, norm: Norm) -> bool:
        return norm.seq in self.seqs


def horizon(reference: dt.datetime, tolerance: dt.timedelta) -> dt.datetime:
    return reference + tolerance


def is_future(event_time: dt.datetime | None, horizon_at: dt.datetime) -> bool:
    """严格大于容差线才算超前；正好等于容差线照常应用（与半开口径一致）。"""
    if event_time is None:
        return False
    return event_time > horizon_at


def skew_seconds(event_time: dt.datetime, reference: dt.datetime) -> int:
    return int((event_time - reference).total_seconds())


def detail_text(event_time: dt.datetime, reference: dt.datetime,
                tolerance: dt.timedelta) -> str:
    skew = skew_seconds(event_time, reference)
    return (f"事件时间超前参考 {iso_utc(reference)} 约 {human(skew)}"
            f"（容差 {int(tolerance.total_seconds() // 60)} 分钟），整条暂缓")


def plan(norms: list[Norm], reference: dt.datetime,
         tolerance: dt.timedelta) -> Hold:
    """在契约通过的行里挑出暂缓行；契约错误优先，不重复判定。"""
    edge = horizon(reference, tolerance)
    hold = Hold(reference=reference, tolerance=tolerance, horizon=edge)
    for norm in sorted(norms, key=lambda n: n.seq):
        if norm.errors or norm.event_time is None:
            continue
        if not is_future(norm.event_time, edge):
            continue
        hold.seqs.add(norm.seq)
        hold.detail_of[norm.seq] = detail_text(norm.event_time, reference, tolerance)
        hold.signature.append(f"{norm.seq}:{iso_utc(norm.event_time)}")
        hold.rows.append({
            "seq": norm.seq, "row_ref": norm.row_ref,
            "table": norm.table, "pk": norm.pk, "version": norm.version,
            "op": norm.op, "event_time": iso_utc(norm.event_time),
            "event_time_raw": norm.event_time_raw,
            "skew_seconds": skew_seconds(norm.event_time, reference),
            "skew_text": human(skew_seconds(norm.event_time, reference)),
            "detail": hold.detail_of[norm.seq],
        })
    return hold


def release_ready(held_events: list[dict], reference: dt.datetime,
                  tolerance: dt.timedelta) -> list[dict]:
    """已落库的暂缓行里，哪些在给定参考时刻下可以放行（核对页提示用）。"""
    edge = horizon(reference, tolerance)
    out = []
    for row in held_events:
        when = parse_iso(row.get("event_time_utc"))
        if when is not None and when <= edge:
            out.append(row)
    return out


def human(seconds: int) -> str:
    seconds = abs(int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"
    days, rest_hours = divmod(hours, 24)
    return f"{days} 天 {rest_hours} 小时" if rest_hours else f"{days} 天"
