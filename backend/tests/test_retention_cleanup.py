"""保留管理 · 清理预览验收测试。

验收场景：
1. 准备三份有效快照（SNAP-0003/0004/0005）、最低保留两份（RETE-0003）
2. 一笔保留期内的数据（dt=2026-11-20）→ 保护期组
3. 一笔只有别表凭证的旧分区（dt=2026-05-01）→ 缺凭证组
4. 生成预览：候选含 dt=2026-06-01、dt=2026-07-01
5. 预览之后撤销一份快照（SNAP-0003），同时为另一旧分区（dt=2026-04-01）
   补到正确归档凭证 —— 两类变更并发提交
6. 提交执行：旧候选 dt=2026-06-01 失去恢复依据被暂缓；新增候选
   dt=2026-04-01 仍可恢复且不会减少有效快照数；执行清单注明冲突的资格依据
7. 原预览按原条件仍可回查
"""
from __future__ import annotations

import copy

import pytest

from app.seed import SEED_ARCHIVE_CREDENTIALS, SEED_CLEANUP_OBJECTS, SEED_SNAPSHOT_DETAILS
from app.services import service_for
from app.store import store

POLICY_ID = 3  # RETE-0003：dwd_trades_merged，保留 30 天，最小快照数 2


@pytest.fixture(autouse=True)
def reset_cleanup_state():
    """清理预览相关状态隔离：每个用例从种子状态出发，结束后还原现场。"""
    saved_creds = copy.deepcopy(store._archive_credentials)
    saved_objects = copy.deepcopy(store._cleanup_objects)
    saved_snap_details = copy.deepcopy(store._snapshot_details)
    saved_previews = copy.deepcopy(store._cleanup_previews)
    saved_execs = copy.deepcopy(store._cleanup_executions)
    saved_snap_rows = copy.deepcopy(store.rows("snapshot_mgr"))

    store._archive_credentials[:] = copy.deepcopy(SEED_ARCHIVE_CREDENTIALS)
    store._cleanup_objects[:] = copy.deepcopy(SEED_CLEANUP_OBJECTS)
    store._snapshot_details.clear()
    store._snapshot_details.update(copy.deepcopy(SEED_SNAPSHOT_DETAILS))
    store._cleanup_previews.clear()
    store._cleanup_executions.clear()
    for row in store.rows("snapshot_mgr"):
        if row.get("快照编号") == "SNAP-0003":
            row["status"] = "已恢复"
        elif row.get("快照编号") in ("SNAP-0004", "SNAP-0005"):
            row["status"] = "已完成"
    yield

    store._archive_credentials[:] = saved_creds
    store._cleanup_objects[:] = saved_objects
    store._snapshot_details.clear()
    store._snapshot_details.update(saved_snap_details)
    store._cleanup_previews.clear()
    store._cleanup_previews.update(saved_previews)
    store._cleanup_executions[:] = saved_execs
    store.rows("snapshot_mgr")[:] = saved_snap_rows


@pytest.fixture()
def svc():
    return service_for("retention")


def _make_preview(svc) -> dict:
    result = svc.cleanup_preview(POLICY_ID)
    assert result["ok"] is True
    return result["preview"]


# ============================================================
# 预览生成：清理依据、恢复依据、四组分组
# ============================================================
class TestCleanupPreview:
    def test_preview_groups_four_categories(self, svc):
        """预览按四组展示暂缓项：保护期、例外项、缺凭证、最少快照数"""
        preview = _make_preview(svc)
        groups = preview["groups"]

        # 可清理候选：dt=2026-06-01（CRED-0001）、dt=2026-07-01（CRED-0002）
        eligible_parts = {i["分区"] for i in groups["eligible"]}
        assert eligible_parts == {"dt=2026-06-01", "dt=2026-07-01"}

        # 保护期：dt=2026-11-20（5 天前，保留 30 天内）
        assert [i["分区"] for i in groups["protection"]] == ["dt=2026-11-20"]
        assert "保留期内" in groups["protection"][0]["暂缓原因"]

        # 例外项：dt=2026-08-01 命中“审计数据”
        assert [i["分区"] for i in groups["exception"]] == ["dt=2026-08-01"]
        assert "审计数据" in groups["exception"][0]["暂缓原因"]

        # 缺凭证：dt=2026-05-01（只有别表凭证）、dt=2026-04-01（无凭证）
        missing = {i["分区"]: i for i in groups["missing_credential"]}
        assert set(missing) == {"dt=2026-05-01", "dt=2026-04-01"}
        assert "CRED-0003" in missing["dt=2026-05-01"]["暂缓原因"]  # 别表凭证被点名
        assert "别表凭证" in missing["dt=2026-05-01"]["暂缓原因"]

        # 最少快照数组：当前为空（3 份有效快照 ≥ 最低 2 份）
        assert groups["min_snapshot"] == []

        # 计数与分组一致
        for key, items in groups.items():
            assert preview["counts"][key] == len(items)

    def test_preview_items_carry_basis(self, svc):
        """候选项带清理依据（归档凭证）和恢复依据（可读快照）"""
        preview = _make_preview(svc)
        items = {i["分区"]: i for i in preview["groups"]["eligible"]}

        assert "CRED-0001" in items["dt=2026-06-01"]["清理依据"]
        assert items["dt=2026-06-01"]["恢复依据"].startswith("SNAP-0003")
        assert "CRED-0002" in items["dt=2026-07-01"]["清理依据"]
        assert items["dt=2026-07-01"]["恢复依据"].startswith("SNAP-0004")

    def test_preview_freezes_conditions(self, svc):
        """预览单冻结生成时的凭证与快照集合，供历史复核"""
        preview = _make_preview(svc)
        conditions = preview["conditions"]
        assert conditions["valid_snapshot_count"] == 3
        assert set(conditions["snapshots"]) == {"SNAP-0003", "SNAP-0004", "SNAP-0005"}
        assert "CRED-0001" in conditions["credentials"]
        assert preview["policy_params"]["最小快照数"] == 2

    def test_preview_requires_active_policy(self, svc):
        """未生效策略不能生成预览"""
        result = svc.cleanup_preview(1)  # RETE-0001 草稿
        assert result["ok"] is False
        assert "未生效" in result["reason"]


# ============================================================
# 对象详情：最近可用快照 + 当前凭证覆盖的分区
# ============================================================
class TestPreviewObjectDetail:
    def test_detail_links_latest_snapshot(self, svc):
        """展开详情时关联最近可用快照"""
        preview = _make_preview(svc)
        detail = svc.preview_object_detail(preview["preview_id"], "dt=2026-06-01")
        assert detail["ok"] is True
        assert detail["group"] == "eligible"
        assert detail["latest_snapshot"]["快照编号"] == "SNAP-0003"
        assert "dt=2026-06-01" in detail["latest_snapshot"]["覆盖分区"]

    def test_detail_shows_current_credential_coverage(self, svc):
        """详情呈现当前凭证覆盖的分区（含预览后补录的凭证）"""
        preview = _make_preview(svc)
        svc.supplement_credential("dwd_trades_merged", "dt=2026-04-01", "v5")

        detail = svc.preview_object_detail(preview["preview_id"], "dt=2026-04-01")
        assert detail["ok"] is True
        coverage = {c["凭证编号"]: c for c in detail["credential_coverage"]}
        # 当前凭证包含补录的新凭证，且覆盖分区正确
        new_cred = [c for c in coverage.values() if "dt=2026-04-01" in c["覆盖分区"]]
        assert len(new_cred) == 1
        assert new_cred[0]["状态"] == "已确认"
        assert new_cred[0]["归档版本"] == "v5"

    def test_detail_rejects_unknown_partition(self, svc):
        preview = _make_preview(svc)
        result = svc.preview_object_detail(preview["preview_id"], "dt=1999-01-01")
        assert result["ok"] is False


# ============================================================
# 并发变更：补归档 + 撤销快照
# ============================================================
class TestConcurrentChanges:
    def test_supplement_credential_writes_back(self, svc):
        """补归档凭证回写清理清单，且幂等拒绝重复补录"""
        result = svc.supplement_credential("dwd_trades_merged", "dt=2026-04-01", "v5")
        assert result["ok"] is True
        assert result["credential"]["状态"] == "已确认"

        dup = svc.supplement_credential("dwd_trades_merged", "dt=2026-04-01", "v5")
        assert dup["ok"] is False
        assert "已存在" in dup["reason"]

    def test_revoke_snapshot(self, svc):
        """撤销快照后不再可读，且不可重复撤销"""
        result = svc.revoke_snapshot("SNAP-0003")
        assert result["ok"] is True
        remaining = store.readable_snapshots_for_table("dwd_trades_merged")
        assert {s["快照编号"] for s in remaining} == {"SNAP-0004", "SNAP-0005"}

        again = svc.revoke_snapshot("SNAP-0003")
        assert again["ok"] is False
        missing = svc.revoke_snapshot("SNAP-9999")
        assert missing["ok"] is False


# ============================================================
# 提交执行：清理范围、暂缓项、下一次可确认、冲突注明
# ============================================================
class TestExecuteCleanup:
    def _preview_then_concurrent_changes(self, svc) -> dict:
        preview = _make_preview(svc)
        # 并发：撤销一份快照 + 为另一旧分区补到正确归档凭证
        assert svc.revoke_snapshot("SNAP-0003")["ok"] is True
        assert svc.supplement_credential("dwd_trades_merged", "dt=2026-04-01", "v5")["ok"] is True
        return preview

    def test_execute_scope_and_deferred(self, svc):
        """旧候选失去恢复依据被暂缓；其余候选按完整凭证清理"""
        preview = self._preview_then_concurrent_changes(svc)
        result = svc.execute_cleanup(preview["preview_id"])
        assert result["ok"] is True
        execution = result["execution"]

        # 实际清理范围：dt=2026-07-01（凭证 CRED-0002 + 快照 SNAP-0004 仍完整）
        assert [i["分区"] for i in execution["cleaned"]] == ["dt=2026-07-01"]

        # 被暂缓的项：dt=2026-06-01 的恢复依据 SNAP-0003 已撤销
        deferred = execution["deferred"]
        assert len(deferred) == 1
        assert deferred[0]["分区"] == "dt=2026-06-01"
        assert "恢复依据不足" in deferred[0]["暂缓原因"]

        # 有效快照数未因清理减少：撤销后 2 份 = 最低保留 2 份
        assert execution["valid_snapshot_count"] == 2
        assert execution["最小快照数"] == 2

    def test_execute_next_confirmable(self, svc):
        """下一次可确认的分区：新增候选可恢复，缺凭证项给出解除条件"""
        preview = self._preview_then_concurrent_changes(svc)
        execution = svc.execute_cleanup(preview["preview_id"])["execution"]

        nxt = {i["分区"]: i for i in execution["next_confirmable"]}
        # 新增候选：补到正确凭证，仍可恢复（SNAP-0005），等待下一次预览确认
        assert "dt=2026-04-01" in nxt
        assert "下一次预览可确认" in nxt["dt=2026-04-01"]["条件"]
        assert "SNAP-0005" in nxt["dt=2026-04-01"]["恢复依据"]
        # 仍缺本表凭证的旧分区：给出补录条件
        assert "补录" in nxt["dt=2026-05-01"]["条件"]
        # 保护期内的数据：给出到期时间
        assert "保留期至" in nxt["dt=2026-11-20"]["条件"]

    def test_execute_notes_conflict_basis(self, svc):
        """补归档与撤销快照的冲突在执行清单里注明采用的资格依据"""
        preview = self._preview_then_concurrent_changes(svc)
        execution = svc.execute_cleanup(preview["preview_id"])["execution"]

        types = {c["类型"] for c in execution["conflicts"]}
        assert types == {"补归档", "撤销快照"}
        for conflict in execution["conflicts"]:
            assert conflict["资格依据"]
        supplement = [c for c in execution["conflicts"] if c["类型"] == "补归档"][0]
        assert "CRED-" in supplement["说明"] and "完整凭证" in supplement["资格依据"]
        revoke = [c for c in execution["conflicts"] if c["类型"] == "撤销快照"][0]
        assert "SNAP-0003" in revoke["说明"]

    def test_execute_marks_objects_cleaned(self, svc):
        """实际清理的对象被标记，不再出现在后续预览"""
        preview = self._preview_then_concurrent_changes(svc)
        svc.execute_cleanup(preview["preview_id"])
        obj = [o for o in store._cleanup_objects if o["分区"] == "dt=2026-07-01"][0]
        assert obj["已清理"] is True

        preview2 = _make_preview(svc)
        all_parts = {i["分区"] for items in preview2["groups"].values() for i in items}
        assert "dt=2026-07-01" not in all_parts

    def test_execute_twice_rejected(self, svc):
        """同一预览单不可重复执行"""
        preview = _make_preview(svc)
        assert svc.execute_cleanup(preview["preview_id"])["ok"] is True
        again = svc.execute_cleanup(preview["preview_id"])
        assert again["ok"] is False
        assert "已提交执行" in again["reason"]


# ============================================================
# 历史预览回查：按原条件仍可复核
# ============================================================
class TestHistoricalPreview:
    def test_historical_preview_reviewable_under_original_conditions(self, svc):
        """并发变更与执行之后，原预览仍按生成时条件回查"""
        preview = _make_preview(svc)
        pid = preview["preview_id"]

        svc.revoke_snapshot("SNAP-0003")
        svc.supplement_credential("dwd_trades_merged", "dt=2026-04-01", "v5")
        svc.execute_cleanup(pid)

        historical = svc.get_preview(pid)
        assert historical["ok"] is True
        hp = historical["preview"]
        # 原条件：3 份有效快照、dt=2026-06-01 仍是候选、dt=2026-04-01 仍缺凭证
        assert hp["conditions"]["valid_snapshot_count"] == 3
        eligible_parts = {i["分区"] for i in hp["groups"]["eligible"]}
        assert "dt=2026-06-01" in eligible_parts
        missing_parts = {i["分区"] for i in hp["groups"]["missing_credential"]}
        assert "dt=2026-04-01" in missing_parts
        # 已执行标记与执行编号可追溯
        assert hp["已执行"] is True
        assert hp["执行编号"].startswith("EXEC-")

    def test_list_previews_and_executions(self, svc):
        """预览单与执行清单均可列表查询"""
        preview = _make_preview(svc)
        svc.execute_cleanup(preview["preview_id"])

        previews = svc.list_previews()
        assert previews["total"] == 1
        assert previews["items"][0]["preview_id"] == preview["preview_id"]
        assert previews["items"][0]["已执行"] is True

        executions = svc.list_executions()
        assert executions["total"] == 1
        assert executions["items"][0]["preview_id"] == preview["preview_id"]

    def test_unknown_preview(self, svc):
        assert svc.get_preview("PV-0000-0000")["ok"] is False
        assert svc.execute_cleanup("PV-0000-0000")["ok"] is False
