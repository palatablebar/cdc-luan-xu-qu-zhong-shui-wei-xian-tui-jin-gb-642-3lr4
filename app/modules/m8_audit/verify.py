"""落库核对：已采信的证据行与实际落库的快照/合计是否一致。

恢复中断最怕的不是报错，而是「少了几行却看起来正常」：快照键数、金额、对账
三条身份都可能刚好自洽，却和原始证据对不上。这里把两边逐项比出来：

1. **行证据**：已落库事件行数 = 原始行；各分桶之和 = 原始行；
2. **快照**：用已落库的「已应用」事件重算一遍，与已落库快照逐键比较
   （缺失 / 多出 / 版本或金额不同）；
3. **合计**：已应用条数、已应用金额、快照金额与重算结果比较。

任何一项不齐 → `E_RECOVERY_MISMATCH`，核对页显式列出差异；此时不允许重算覆盖
正式快照（旧版保留），避免「恢复失败顺手把旧快照也删了」。
"""
from __future__ import annotations

from decimal import Decimal

from ..m5_snapshot import apply as apply_mod
from ..m5_snapshot import materialize


def _dec(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0.00")
    return Decimal(str(value))


def row_account(events: list[dict], totals: dict) -> dict:
    counts = {name: 0 for name in
              ("applied", "duplicate", "conflict", "quarantine", "held")}
    for row in events:
        counts[row["row_class"]] = counts.get(row["row_class"], 0) + 1
    raw = int(totals.get("raw_lines", 0) or 0)
    actual = len(events)
    parts = sum(counts.values())
    return {
        "name": "证据行",
        "left": "已落库事件行", "left_value": actual,
        "right": "原始行", "right_value": raw,
        "ok": actual == raw,
        "counts": counts,
        "bucket_sum": parts,
        "buckets_ok": parts == raw,
    }


def snapshot_check(store, batch_id: str, totals: dict) -> dict:
    applied = [apply_mod.Applied.from_row(row)
               for row in store.load_events(batch_id, "applied")]
    as_of = _parse(totals.get("snapshot_as_of"))
    if as_of is None and applied:
        # 没有 as_of 就无法重算：直接按不一致处理，不能默默放过
        stored = store.load_snapshot(batch_id)
        return {
            "name": "快照",
            "left": "按已落库事件重算", "left_value": None,
            "right": "已落库快照", "right_value": len(stored),
            "ok": False, "missing": [], "extra": [], "changed": [],
            "amount_ok": False, "amount_expected": None,
            "amount_stored": str(_dec(totals.get("amount_snapshot"))),
            "applied_ok": False, "applied_events": len(applied),
            "applied_totals": int(totals.get("applied", 0) or 0),
            "keys_ok": False, "keys_expected": None,
            "keys_stored": int(totals.get("keys_total", 0) or 0),
            "detail": "缺少 snapshot_as_of，无法重算正式快照",
        }
    expected = materialize.materialize(applied, as_of)
    stored = store.load_snapshot(batch_id)

    want = {(row["table"], row["pk"]): row for row in expected.rows}
    got = {(row["table_name"], row["pk"]): row for row in stored}
    missing = sorted(want.keys() - got.keys())
    extra = sorted(got.keys() - want.keys())
    changed = []
    for key in sorted(want.keys() & got.keys()):
        left = want[key]
        right = got[key]
        diffs = []
        if left["version"] != right["version"]:
            diffs.append(f"版本 {left['version']} ≠ {right['version']}")
        if left["op"] != right["op"]:
            diffs.append(f"操作 {left['op']} ≠ {right['op']}")
        if _dec(left["amount"]) != _dec(right["amount"]):
            diffs.append(f"金额 {_dec(left['amount'])} ≠ {_dec(right['amount'])}")
        if diffs:
            changed.append({"key": f"{key[0]}/{key[1]}", "diff": "；".join(diffs)})

    amount_ok = (_dec(totals.get("amount_snapshot"))
                 == expected.amounts["snapshot"])
    applied_ok = int(totals.get("applied", 0) or 0) == len(applied)
    keys_ok = int(totals.get("keys_total", 0) or 0) == expected.counters["keys_total"]
    ok = (not missing and not extra and not changed
          and amount_ok and applied_ok and keys_ok)
    return {
        "name": "快照",
        "left": "按已落库事件重算", "left_value": len(want),
        "right": "已落库快照", "right_value": len(got),
        "ok": ok,
        "missing": [_label(key) for key in missing],
        "extra": [_label(key) for key in extra],
        "changed": changed,
        "amount_ok": amount_ok,
        "amount_expected": str(expected.amounts["snapshot"]),
        "amount_stored": str(_dec(totals.get("amount_snapshot"))),
        "applied_ok": applied_ok,
        "applied_events": len(applied),
        "applied_totals": int(totals.get("applied", 0) or 0),
        "keys_ok": keys_ok,
        "keys_expected": expected.counters["keys_total"],
        "keys_stored": int(totals.get("keys_total", 0) or 0),
    }


def audit(store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id) or {}
    totals = store.load_totals(batch_id)
    events = store.load_events(batch_id)
    row = row_account(events, totals)
    snap = snapshot_check(store, batch_id, totals)
    consistent = bool(row["ok"] and row["buckets_ok"] and snap["ok"])
    return {
        "batch_id": batch_id,
        "status": batch.get("status"),
        "consistent": consistent,
        "code": None if consistent else "E_RECOVERY_MISMATCH",
        "rows": row,
        "snapshot": snap,
        "totals": {
            "raw_lines": int(totals.get("raw_lines", 0) or 0),
            "applied": int(totals.get("applied", 0) or 0),
            "held": int(totals.get("held", 0) or 0),
            "amount_applied": str(_dec(totals.get("amount_applied"))),
            "amount_snapshot": str(_dec(totals.get("amount_snapshot"))),
        },
    }


def audit_payload(store, batch_id: str) -> dict:
    """JSON 形状：键数汇总 + 前 20 条差异，页面与导出共用。"""
    result = audit(store, batch_id)
    snap = result["snapshot"]
    return {
        "batch_id": batch_id,
        "status": result["status"],
        "consistent": result["consistent"],
        "code": result["code"],
        "row_account": {
            "ok": result["rows"]["ok"], "buckets_ok": result["rows"]["buckets_ok"],
            "left": result["rows"]["left_value"],
            "right": result["rows"]["right_value"],
            "counts": result["rows"]["counts"],
        },
        "snapshot": {
            "ok": snap["ok"],
            "left": snap["left_value"], "right": snap["right_value"],
            "missing": snap["missing"][:20], "extra": snap["extra"][:20],
            "changed": snap["changed"][:20],
            "missing_count": len(snap["missing"]),
            "extra_count": len(snap["extra"]),
            "changed_count": len(snap["changed"]),
            "amount_ok": snap["amount_ok"],
            "amount_expected": snap["amount_expected"],
            "amount_stored": snap["amount_stored"],
            "keys_ok": snap["keys_ok"],
            "keys_expected": snap["keys_expected"],
            "keys_stored": snap["keys_stored"],
        },
        "totals": result["totals"],
    }


def ensure_consistent(store, batch_id: str) -> dict:
    """重算正式快照前的闸门：不一致就抛错，旧快照原样保留。"""
    from ... import errors

    result = audit(store, batch_id)
    if result["consistent"]:
        return result
    missing = result["snapshot"]["missing"]
    raise errors.RecoveryIncomplete(
        "已落库快照与已采信内容不一致，拒绝覆盖上一版正式快照",
        code="E_RECOVERY_MISMATCH",
        detail={
            "batch_id": batch_id,
            "row_account_ok": result["rows"]["ok"],
            "buckets_ok": result["rows"]["buckets_ok"],
            "missing_keys": missing[:20],
            "extra_keys": result["snapshot"]["extra"][:20],
            "changed": result["snapshot"]["changed"][:20],
            "hint": "恢复可能中断并丢失记录；重新从原始导出采信或从备份恢复",
        })


def _label(key) -> str:
    return f"{key[0]}/{key[1]}"


def _parse(value):
    from ..m2_schema.coerce import parse_iso
    return parse_iso(value)
