"""发布裁决：哪一批算「正式快照」，哪些只留证据。

规则：

- 有已应用事件 → `done`，本批快照即正式快照（暂缓记录只是不参与，不影响发布）；
- 全部记录都被暂缓 → `held_only`，正式快照保持上一版，绝不用空快照覆盖已发布内容；
- 空输入且已有正式快照 → `empty_only`，同样保持上一版（恢复中断常表现为读成 0 行）；
- 运行失败 → `failed`，同样不触碰任何已发布批次。

「正式快照」= 状态为 `done` 的最新批次；`held_only` / `empty_only` / `failed` / `running`
都不是正式。
"""
from __future__ import annotations

from dataclasses import dataclass

PUBLISHED = "done"
HELD_ONLY = "held_only"
EMPTY_ONLY = "empty_only"
FAILED = "failed"
UNPUBLISHED = ("held_only", "empty_only", "failed", "running")


@dataclass
class Decision:
    status: str
    published: bool
    code: str | None
    note: str


def decide(classified, snapshot, *, has_official: bool = False) -> Decision:
    held = len(classified.held)
    applied = len(classified.applied)
    if applied == 0 and held > 0:
        return Decision(
            status=HELD_ONLY, published=False, code="B_HELD_ONLY",
            note=f"全部 {held} 条被暂缓，正式快照保持上一版")
    if applied == 0:
        if has_official:
            return Decision(
                status=EMPTY_ONLY, published=False, code="B_EMPTY_KEEP_SNAPSHOT",
                note="本次没有可应用事件，正式快照保持上一版（空结果不覆盖）")
        return Decision(
            status=PUBLISHED, published=True, code=None,
            note="首次运行且没有可应用事件，快照为空")
    if held:
        return Decision(
            status=PUBLISHED, published=True, code="W_FUTURE_HELD",
            note=f"正常数据已发布；{held} 条时间超前被挡下")
    return Decision(status=PUBLISHED, published=True, code=None,
                    note=f"正常发布 {snapshot.counters['snapshot_keys']} 键")


def official(store) -> dict | None:
    """当前正式快照所属批次（最新 `done`）。"""
    return store.latest_published()


def is_published(batch: dict | None) -> bool:
    return bool(batch) and batch.get("status") == PUBLISHED
