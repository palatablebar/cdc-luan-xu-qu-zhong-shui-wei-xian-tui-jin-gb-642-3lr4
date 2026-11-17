"""水位线推进：max(event_time) − 迟到窗口，单调不回退。

定义（半开区间）：

- 判定某事件时用的水位线 = 它到达之前已见最大 event_time − 窗口；
- `event_time < 水位线` 记迟到，**等于水位线不算迟到**；
- 水位线只由已应用事件推进（重复、冲突、隔离不推进），保证同一输入可复算；
- 正式水位线有上限：不超过参考时钟（`ceiling`），异常的未来时间不能把它推过今天。

时钟保护不在这里做裁剪，而在 `future` 里单独判定：超前参考时刻太多的记录整条
暂缓，连「已应用」都不算，因此不会出现在本函数的输入里。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..m2_schema.service import Norm


@dataclass
class Tick:
    seq: int
    row_ref: str
    event_time: dt.datetime
    watermark: dt.datetime
    late: bool
    behind_seconds: int


@dataclass
class WatermarkState:
    final: dt.datetime | None = None
    max_event_time: dt.datetime | None = None
    ticks: list[Tick] = field(default_factory=list)
    advance_points: list[dict] = field(default_factory=list)
    ceiling: dt.datetime | None = None
    capped: bool = False

    @property
    def late(self) -> int:
        return sum(1 for t in self.ticks if t.late)

    @property
    def on_time(self) -> int:
        return sum(1 for t in self.ticks if not t.late)

    @property
    def max_behind_seconds(self) -> int:
        return max((t.behind_seconds for t in self.ticks), default=0)


def advance(events: list[Norm], window: dt.timedelta,
            ceiling: dt.datetime | None = None) -> WatermarkState:
    """按到达序推进水位线；`ceiling` 是正式水位线的上限（参考时刻）。

    上限只压水位线，不改事件本身的迟到比较口径：`event_time < 水位线`。
    """
    state = WatermarkState(ceiling=ceiling)
    max_seen: dt.datetime | None = None
    last_mark: dt.datetime | None = None
    for norm in sorted(events, key=lambda n: n.seq):
        if norm.event_time is None:
            continue
        base = max_seen if max_seen is not None else norm.event_time
        mark = base - window
        if ceiling is not None and mark > ceiling:
            mark = ceiling
            state.capped = True
        late = norm.event_time < mark
        behind = 0
        if max_seen is not None and max_seen > norm.event_time:
            behind = int((max_seen - norm.event_time).total_seconds())
        norm.late = late
        norm.behind_seconds = behind
        tick = Tick(seq=norm.seq, row_ref=norm.row_ref, event_time=norm.event_time,
                    watermark=mark, late=late, behind_seconds=behind)
        state.ticks.append(tick)
        if last_mark is None or mark > last_mark:
            state.advance_points.append({
                "seq": norm.seq, "row_ref": norm.row_ref,
                "event_time": norm.event_time, "watermark": mark,
            })
            last_mark = mark
        if max_seen is None or norm.event_time > max_seen:
            max_seen = norm.event_time
    state.max_event_time = max_seen
    if max_seen is None:
        state.final = None
    else:
        raw = max_seen - window
        state.final = _cap(raw, ceiling)
        state.capped = state.final != raw
    return state


def _cap(mark: dt.datetime, ceiling: dt.datetime | None) -> dt.datetime:
    if ceiling is not None and mark > ceiling:
        return ceiling
    return mark


def is_late(event_time: dt.datetime, watermark: dt.datetime) -> bool:
    """半开区间：严格小于才迟到。TRAP-23 的端点归属就在这里。"""
    return event_time < watermark


def series_rows(state: WatermarkState, iso) -> list[dict]:
    rows = []
    for tick in state.ticks:
        rows.append({
            "seq": tick.seq, "row_ref": tick.row_ref,
            "event_time": iso(tick.event_time), "watermark": iso(tick.watermark),
            "late": tick.late, "behind_seconds": tick.behind_seconds,
        })
    return rows


def summary(state: WatermarkState, iso) -> dict:
    return {
        "final": iso(state.final),
        "max_event_time": iso(state.max_event_time),
        "late": state.late,
        "on_time": state.on_time,
        "max_behind_seconds": state.max_behind_seconds,
        "applied": len(state.ticks),
        "ceiling": iso(state.ceiling),
        "capped": state.capped,
    }
