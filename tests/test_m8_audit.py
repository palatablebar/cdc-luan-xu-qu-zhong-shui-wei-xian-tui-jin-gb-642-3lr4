"""时钟保护与发布裁决：未来时间不能推进正式水位、不能封板快照，但可被放行。"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

from app import reasons
from app.modules.m4_watermark import future, watermark
from app.modules.m5_snapshot import service as m5_service
from app.modules.m6_batches import service
from app.modules.m8_audit import publish, verify
from app.store import Store

REF = "2026-11-15T09:10:00Z"          # 参考时钟：样例最大事件 09:02 刚过
TOLERANCE = 10                        # 分钟


def _write(directory, name: str, lines: list[str]) -> None:
    header = f"# cdc-export v2 rows={len(lines)} charset=utf-8\n"
    (directory / name).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _event(pk: str, version: int, when: str, amount: str = "10.00",
           table: str = "orders") -> str:
    return ('{"table":"%s","pk":"%s","version":%d,"op":"upsert",'
            '"event_time":"%s","schema":"v2",'
            '"payload":{"status":"新建","amount":"%s"}}'
            % (table, pk, version, when, amount))


def _settings(settings, directory, reference=REF):
    return replace(settings, sample_dir=str(directory), reference_now=reference,
                   future_tolerance_minutes=TOLERANCE)


# ---------------------------------------------------------------- 判定单元

def test_future_hold_boundary_is_inclusive(norm_factory):
    """等于「参考 + 容差」照常应用，严格超前才暂缓（与半开口径一致）。"""
    reference = dt.datetime(2026, 11, 15, 9, 10, tzinfo=dt.timezone.utc)
    tolerance = dt.timedelta(minutes=10)
    edge = future.horizon(reference, tolerance)
    assert future.is_future(edge, edge) is False
    assert future.is_future(edge + dt.timedelta(seconds=1), edge) is True


def test_watermark_capped_by_reference_clock(norm_factory):
    """水位线不得超过参考时钟；事件本身仍按 max−窗口 判定迟到。"""
    reference = dt.datetime(2026, 11, 15, 9, 10, tzinfo=dt.timezone.utc)
    norms = [norm_factory(pk="1", event_time="2026-11-15T09:20:00Z"),
             norm_factory(pk="2", event_time="2026-11-15T09:25:00Z")]
    state = watermark.advance(norms, dt.timedelta(minutes=10), ceiling=reference)
    assert state.final == reference
    assert state.capped is True
    assert max(t.watermark for t in state.ticks) == reference


# ---------------------------------------------------------------- 混合批次

def test_future_event_does_not_advance_watermark_or_snapshot(settings, store, tmp_samples):
    """一条错乱未来时间 + 两条正常：水位只由正常数据推进，未来那条被挡下。"""
    _write(tmp_samples, "events.jsonl", [
        _event("8001", 1, "2026-11-15T08:30:00Z", "100.00"),
        _event("8002", 1, "2026-11-16T23:59:00Z", "999.00"),
        _event("8003", 1, "2026-11-15T08:40:00Z", "20.00"),
    ])
    result = service.run(settings=_settings(settings, tmp_samples), store=store,
                         source="events.jsonl")
    assert result["status"] == "done"
    assert result["counters"]["applied"] == 2
    assert result["counters"]["held"] == 1
    assert result["publish"]["published"] is True
    # 正式水位 = 08:40 − 10 分钟，绝不被 11-16 那条带走（08:40 进待定窗口是既有定义）
    assert result["totals"]["watermark_utc"] == "2026-11-15T08:30:00Z"
    assert result["totals"]["max_event_time"] == "2026-11-15T08:40:00Z"

    batch_id = result["batch"]["batch_id"]
    held = store.load_events(batch_id, "held")
    assert len(held) == 1
    assert held[0]["row_ref"] == "events.jsonl:3"
    assert held[0]["reason_code"] == "W_FUTURE_HELD"
    assert "超前" in held[0]["detail"]
    keys = {row["pk"] for row in store.load_snapshot(batch_id)}
    assert keys == {"8001"}
    assert {"8002", "8003"}.isdisjoint(keys)
    assert [row["row_ref"] for row in result["held"]] == ["events.jsonl:3"]


def test_held_row_not_in_watermark_series(store, settings, tmp_samples):
    _write(tmp_samples, "events.jsonl", [
        _event("8001", 1, "2026-11-15T08:50:00Z"),
        _event("8002", 1, "2026-11-16T23:59:00Z"),
    ])
    result = service.run(settings=_settings(settings, tmp_samples), store=store,
                         source="events.jsonl")
    batch_id = result["batch"]["batch_id"]
    series = store.load_watermarks(batch_id)
    assert [row["row_ref"] for row in series] == ["events.jsonl:2"]
    assert result["totals"]["late_events"] + result["totals"]["on_time_events"] == 1


def test_row_accounting_includes_held(run):
    """行账把暂缓算进右端；样例无暂缓时账仍平。"""
    rows = run["reconciliation"]
    assert rows[0]["name"] == "行账"
    assert "暂缓" in rows[0]["right"]
    assert rows[0]["ok"] is True


# ---------------------------------------------------------------- 全部暂缓

def test_all_held_keeps_official_snapshot(settings, store, tmp_samples):
    """正式批次先发布，再来一批全是未来时间：不得用空快照覆盖。"""
    _write(tmp_samples, "base.jsonl", [_event("9001", 1, "2026-11-15T08:50:00Z", "50.00")])
    first = service.run(settings=_settings(settings, tmp_samples), store=store,
                        source="base.jsonl")
    official = first["batch"]["batch_id"]
    snapshot_before = store.load_snapshot(official)

    future_dir = tmp_samples / "future"
    future_dir.mkdir()
    _write(future_dir, "future.jsonl", [
        _event("9002", 1, "2026-11-17T12:00:00Z", "777.00")])
    second = service.run(settings=_settings(settings, future_dir), store=store,
                         source="future.jsonl")
    assert second["status"] == "held_only"
    assert second["publish"]["published"] is False
    assert second["publish"]["code"] == "B_HELD_ONLY"
    assert second["publish"]["official_batch_id"] == official
    assert second["counters"]["applied"] == 0
    assert publish.official(store)["batch_id"] == official
    assert store.load_snapshot(official) == snapshot_before
    assert store.latest_published()["batch_id"] == official


def test_rebuild_rejected_for_non_official_batch(settings, store, tmp_samples):
    _write(tmp_samples, "future.jsonl", [_event("9002", 1, "2026-11-17T12:00:00Z")])
    result = service.run(settings=_settings(settings, tmp_samples), store=store,
                         source="future.jsonl")
    batch_id = result["batch"]["batch_id"]
    try:
        m5_service.rebuild(store, batch_id)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "B_HELD_ONLY"
    else:  # pragma: no cover - 明确失败
        raise AssertionError("非正式批次不应允许重算快照")


# ---------------------------------------------------------------- 放行与幂等

def test_same_input_same_reference_replays(settings, store, tmp_samples):
    """同一输入、同一暂缓集合重跑：命中同一批次，不新增行。"""
    _write(tmp_samples, "events.jsonl", [
        _event("8001", 1, "2026-11-15T08:50:00Z"),
        _event("8002", 1, "2026-11-16T23:59:00Z"),
    ])
    cfg = _settings(settings, tmp_samples)
    first = service.run(settings=cfg, store=store, source="events.jsonl")
    before = store.count_rows(first["batch"]["batch_id"])
    second = service.run(settings=cfg, store=store, source="events.jsonl")
    assert second["idempotent_replay"] is True
    assert second["batch"]["batch_id"] == first["batch"]["batch_id"]
    assert store.count_rows(first["batch"]["batch_id"]) == before
    assert len(store.list_batches()) == 1


def test_released_after_reference_catches_up(settings, store, tmp_samples):
    """参考时钟追上后重新采信：那一条被放行，且同一来源只落一次。"""
    _write(tmp_samples, "events.jsonl", [
        _event("8001", 1, "2026-11-15T08:50:00Z"),
        _event("8002", 1, "2026-11-15T09:35:00Z", "30.00"),
    ])
    early = service.run(settings=_settings(settings, tmp_samples, REF), store=store,
                        source="events.jsonl")
    assert early["counters"]["held"] == 1
    released = service.run(
        settings=_settings(settings, tmp_samples, "2026-11-15T09:40:00Z"),
        store=store, source="events.jsonl")
    assert released["idempotent_replay"] is False
    assert released["batch"]["batch_id"] != early["batch"]["batch_id"]
    assert released["counters"]["held"] == 0
    assert released["counters"]["applied"] == 2
    applied = store.load_events(released["batch"]["batch_id"], "applied")
    assert sorted(row["pk"] for row in applied) == ["8001", "8002"]
    # 放行后按既有窗口定义：09:35 落在 as_of(09:25) 之后 → 进待定，不再被挡
    assert released["totals"]["watermark_utc"] == "2026-11-15T09:25:00Z"
    pending = {row["key"]: row for row in m5_service.pending_rows(
        store, released["batch"]["batch_id"])}
    assert pending["orders/8002"]["event_time"] == "2026-11-15T09:35:00Z"


def test_releasable_detected_by_audit(store, settings, tmp_samples):
    """核对页能指出哪些暂缓记录按当前参考时钟已可放行。"""
    from app.modules.m8_audit import service as audit_service

    _write(tmp_samples, "events.jsonl", [
        _event("8001", 1, "2026-11-15T08:50:00Z"),
        _event("8002", 1, "2026-11-15T09:35:00Z"),
    ])
    result = service.run(settings=_settings(settings, tmp_samples, REF), store=store,
                         source="events.jsonl")
    later = _settings(settings, tmp_samples, "2026-11-15T09:40:00Z")
    data = audit_service.overview(store, settings=later,
                                  batch_id=result["batch"]["batch_id"])
    assert data["held_count"] == 1
    assert data["releasable_count"] == 1
    assert data["held"][0]["releasable"] is True
    assert data["held"][0]["row_ref"] == "events.jsonl:3"
    assert data["official"]["batch_id"] == result["batch"]["batch_id"]


# ---------------------------------------------------------------- 恢复失败

def test_audit_detects_missing_snapshot_rows(settings, store, run):
    """恢复中断丢了快照行：核对结果与已采信内容不一致。"""
    batch_id = run["batch"]["batch_id"]
    assert verify.audit(store, batch_id)["consistent"] is True
    with store.connect() as conn:
        conn.execute("DELETE FROM snapshots WHERE batch_id=? AND pk=?",
                     (batch_id, "1002"))
    result = verify.audit(store, batch_id)
    assert result["consistent"] is False
    assert result["code"] == "E_RECOVERY_MISMATCH"
    assert "orders/1002" in result["snapshot"]["missing"]


def test_rebuild_refuses_and_keeps_old_snapshot(settings, store, run):
    """不一致时拒绝重算，旧正式快照原样保留（不被半批数据覆盖）。"""
    batch_id = run["batch"]["batch_id"]
    before = store.load_snapshot(batch_id)
    with store.connect() as conn:
        conn.execute("DELETE FROM snapshots WHERE batch_id=? AND pk=?",
                     (batch_id, "1002"))
    broken = store.load_snapshot(batch_id)
    assert len(broken) == len(before) - 1
    try:
        m5_service.rebuild(store, batch_id)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "E_RECOVERY_MISMATCH"
    else:  # pragma: no cover
        raise AssertionError("落库不一致时不应重算成功")
    assert store.load_snapshot(batch_id) == broken


def test_failed_resume_does_not_downgrade_official(settings, store, monkeypatch):
    """续跑失败：失败只记状态，上一版正式批次仍是 done、快照不变。"""
    first = service.run(settings=settings, store=store, source="all")
    official = first["batch"]["batch_id"]
    snapshot_before = store.load_snapshot(official)

    def boom(**kwargs):
        raise RuntimeError("模拟恢复中断")

    monkeypatch.setattr(service, "run", boom)
    try:
        service.resume_batch(settings=settings, store=store, batch_id=official)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("应抛出原异常")
    assert store.get_batch(official)["status"] == "done"
    assert publish.official(store)["batch_id"] == official
    assert store.load_snapshot(official) == snapshot_before


def test_save_attempt_never_downgrades_done(store):
    store.save_attempt(batch={
        "batch_id": "B-done01", "batch_key": "k", "run_id": "CDC-1",
        "source": "all", "status": "done", "created_at": "2026-11-15T09:00:00Z",
        "input_digest": "d", "params": {},
    }, inputs=[])
    store.save_attempt(batch={
        "batch_id": "B-done01", "batch_key": "k", "run_id": "CDC-2",
        "source": "all", "status": "failed", "created_at": "2026-11-15T09:05:00Z",
        "input_digest": "d", "params": {},
    }, inputs=[], note="失败")
    assert store.get_batch("B-done01")["status"] == "done"


# ---------------------------------------------------------------- 原因码

def test_reason_codes_registered():
    for code in ("W_FUTURE_HELD", "B_HELD_ONLY", "B_FAILED_KEEP_SNAPSHOT",
                 "E_RECOVERY_MISMATCH", "E_BAD_REFERENCE"):
        assert reasons.get(code).code == code
        assert reasons.get(code).title != code


# ---------------------------------------------------------------- 页面与 API

def _seed_held(settings, store, tmp_samples):
    _write(tmp_samples, "events.jsonl", [
        _event("8001", 1, "2026-11-15T08:00:00Z", "100.00"),
        _event("8002", 1, "2026-11-16T23:59:00Z", "999.00"),
        _event("8003", 1, "2026-11-15T08:20:00Z", "20.00"),
    ])
    return service.run(settings=_settings(settings, tmp_samples), store=store,
                       source="events.jsonl")


def test_audit_api_lists_held_and_publish(settings, store, tmp_samples):
    from app.main import create_app
    from fastapi.testclient import TestClient

    cfg = _settings(settings, tmp_samples)
    app = create_app(cfg, seed=False)
    with TestClient(app) as client:
        store2 = app.state.store
        result = _seed_held(cfg, store2, tmp_samples)
        official = result["batch"]["batch_id"]
        data = client.get("/api/audit").json()
        assert data["official"]["batch_id"] == official
        assert data["held_count"] == 1
        assert data["held"][0]["row_ref"] == "events.jsonl:3"
        assert data["publish"]["state"] == "published"
        assert data["audit"]["consistent"] is True
        page = client.get("/audit").text
        assert "被挡下的记录" in page
        assert "events.jsonl:3" in page
        assert "仍可发布" in page


def test_held_only_batch_keeps_published_pages_and_exports(settings, store, tmp_samples):
    """全暂缓批次：健康检查指向最近尝试，快照/导出仍指向正式版本。"""
    from app.main import create_app
    from fastapi.testclient import TestClient

    cfg = _settings(settings, tmp_samples)
    official = _seed_held(cfg, store, tmp_samples)["batch"]["batch_id"]

    future_dir = tmp_samples / "future"
    future_dir.mkdir()
    _write(future_dir, "future.jsonl", [
        _event("9100", 1, "2026-12-01T10:00:00Z", "5.00")])
    held = service.run(settings=_settings(settings, future_dir), store=store,
                       source="future.jsonl")
    assert held["status"] == "held_only"

    app = create_app(cfg, seed=False)
    app.state.store = store
    app.state.ctx.store = store
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["batch_id"] == held["batch"]["batch_id"]
        assert health["batch_status"] == "held_only"
        assert health["held"] == 1

        snap = client.get("/api/snapshot").json()
        assert snap["batch_id"] == official
        assert snap["is_official"] is True
        assert snap["attempt_status"] == "held_only"

        csv_body = client.get("/api/export/snapshot.csv").content.decode("utf-8-sig")
        assert "8001" in csv_body
        assert "9100" not in csv_body

        blocked = client.post("/api/snapshot", json={})
        assert blocked.status_code == 200  # 页面默认物化正式批次
        assert blocked.json()["batch_id"] == official

        rejected = client.post("/api/snapshot", params={"batch_id": held["batch"]["batch_id"]})
        assert rejected.status_code == 409
        assert rejected.json()["error"] in ("B_HELD_ONLY", "B_FAILED_KEEP_SNAPSHOT")

        page = client.get("/snapshot").text
        assert "不是正式批次" in page
        assert official in page


def test_audit_api_reports_recovery_mismatch(settings, store, run):
    from app.main import create_app
    from fastapi.testclient import TestClient

    batch_id = run["batch"]["batch_id"]
    with store.connect() as conn:
        conn.execute("DELETE FROM snapshots WHERE batch_id=? AND pk=?",
                     (batch_id, "1002"))
    app = create_app(settings, seed=False)
    app.state.store = store
    app.state.ctx.store = store
    with TestClient(app) as client:
        data = client.get("/api/audit").json()
        assert data["audit"]["consistent"] is False
        assert data["audit"]["code"] == "E_RECOVERY_MISMATCH"
        assert "orders/1002" in data["audit"]["snapshot"]["missing"]
        assert client.get("/audit").status_code == 200
        rejected = client.post("/api/snapshot", json={})
        assert rejected.status_code == 409
        assert rejected.json()["error"] == "E_RECOVERY_MISMATCH"


def test_home_and_batches_mark_unpublished_attempt(settings, store, tmp_samples):
    """全暂缓批次：首页显式提示本批未发布、正式水位仍是上一版。"""
    from app.main import create_app
    from fastapi.testclient import TestClient

    cfg = _settings(settings, tmp_samples)
    official = _seed_held(cfg, store, tmp_samples)
    future_dir = tmp_samples / "future"
    future_dir.mkdir()
    _write(future_dir, "future.jsonl", [
        _event("9100", 1, "2026-12-01T10:00:00Z", "5.00")])
    held = service.run(settings=_settings(settings, future_dir), store=store,
                       source="future.jsonl")

    app = create_app(cfg, seed=False)
    app.state.store = store
    app.state.ctx.store = store
    with TestClient(app) as client:
        home = client.get("/").text
        assert "本批未发布" in home
        assert official["batch"]["batch_id"] in home
        batches = client.get("/batches").text
        assert "全部暂缓" in batches
        assert held["batch"]["batch_id"] in batches
        # 事件页把暂缓单列一个筛选入口
        events = client.get("/api/events?row_class=held").json()
        assert events["count"] == 1
        assert events["rows"][0]["reason_code"] == "W_FUTURE_HELD"


def test_manifest_records_reference_and_clock_parameters(settings, store, tmp_samples):
    result = _seed_held(_settings(settings, tmp_samples), store, tmp_samples)
    doc = store.load_manifest(result["batch"]["batch_id"])
    assert doc["reference_utc"] == REF
    assert doc["future_horizon_utc"] == "2026-11-15T09:20:00Z"
    assert doc["future_tolerance_minutes"] == TOLERANCE
    assert doc["status"] == "done"
    assert doc["counters"]["held"] == "1"


def test_empty_rerun_does_not_clobber_official(settings, store, tmp_samples):
    """恢复中断把文件读成 0 行：不写空快照，正式快照保持上一版。"""
    official = _seed_held(_settings(settings, tmp_samples), store, tmp_samples)
    official_id = official["batch"]["batch_id"]
    before = store.load_snapshot(official_id)

    empty_dir = tmp_samples / "empty"
    empty_dir.mkdir()
    (empty_dir / "empty.jsonl").write_bytes(b"")
    result = service.run(settings=_settings(settings, empty_dir), store=store,
                         source="empty.jsonl")
    assert result["status"] == "empty_only"
    assert result["publish"]["published"] is False
    assert result["publish"]["code"] == "B_EMPTY_KEEP_SNAPSHOT"
    assert result["publish"]["official_batch_id"] == official_id
    assert store.load_snapshot(official_id) == before
    assert publish.official(store)["batch_id"] == official_id
    try:
        m5_service.rebuild(store, result["batch"]["batch_id"])
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "B_EMPTY_KEEP_SNAPSHOT"
    else:  # pragma: no cover
        raise AssertionError("空结果批次不应允许物化")


def test_resume_plan_for_unpublished_batches(settings, store, tmp_samples):
    from app.modules.m6_batches import resume

    official = _seed_held(_settings(settings, tmp_samples), store, tmp_samples)
    plan = resume.plan(store, official["batch"]["batch_id"])
    assert plan["can_resume"] is False
    assert plan["code"] == "B_IDEMPOTENT_REPLAY"

    empty_dir = tmp_samples / "empty"
    empty_dir.mkdir()
    (empty_dir / "empty.jsonl").write_bytes(b"")
    empty = service.run(settings=_settings(settings, empty_dir), store=store,
                        source="empty.jsonl")
    plan = resume.plan(store, empty["batch"]["batch_id"])
    assert plan["can_resume"] is False
    assert plan["code"] == "B_EMPTY_KEEP_SNAPSHOT"

    future_dir = tmp_samples / "future"
    future_dir.mkdir()
    _write(future_dir, "future.jsonl", [
        _event("9100", 1, "2026-12-01T10:00:00Z", "5.00")])
    held = service.run(settings=_settings(settings, future_dir), store=store,
                       source="future.jsonl")
    plan = resume.plan(store, held["batch"]["batch_id"])
    assert plan["can_resume"] is False
    assert plan["code"] == "B_HELD_ONLY"


def test_empty_batch_replays_without_new_rows(settings, store, tmp_samples):
    """空结果批次同样幂等：同输入同参考重跑命中同一批次、不新增行。"""
    empty_dir = tmp_samples / "empty"
    empty_dir.mkdir()
    (empty_dir / "empty.jsonl").write_bytes(b"")
    cfg = _settings(settings, empty_dir)
    first = service.run(settings=cfg, store=store, source="empty.jsonl")
    second = service.run(settings=cfg, store=store, source="empty.jsonl")
    assert first["status"] == "done"  # 首次运行没有正式快照可比，空快照即正式
    assert second["idempotent_replay"] is True
    assert len(store.list_batches()) == 1
