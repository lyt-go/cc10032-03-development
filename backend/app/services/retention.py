"""retention 领域服务：数据保留生命周期管理。

业务逻辑：
1. 保留策略：目标表保留天数、最小快照数、自动清理
2. 保留链路：cold_archive → retention（归档后的数据按保留策略管理生命周期）
3. 失败场景：例外规则冲突、清理策略误删、最小快照数不满足
4. 与上下游流水线：消费 cold_archive 归档清单；输出保留结果

清理预览（本模块核心视图）：
- 按表列出各对象的清理依据：冷热分层已确认的归档状态回写到清理清单，
  按对象和版本核对归档凭证，用仍可读取的快照计算每个对象的恢复依据。
- 页面分四组展示暂缓项：保护期、例外项、缺凭证、最少快照数。
- 补归档与撤销快照并发提交时，清理只基于执行时刻的完整凭证；
  两类变更的冲突在执行清单里注明采用的资格依据。
- 预览单冻结生成时条件，历史预览按原条件仍可复核。

跨模块数据流：
  cold_archive → retention（归档清单决定保留策略的清理范围）
  snapshot_mgr → retention（可读快照决定每个对象的恢复依据）
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 暂缓分组键（页面按这四组 + 可清理候选展示）
GROUP_ELIGIBLE = "eligible"
GROUP_PROTECTION = "protection"
GROUP_EXCEPTION = "exception"
GROUP_MISSING_CREDENTIAL = "missing_credential"
GROUP_MIN_SNAPSHOT = "min_snapshot"

GROUP_LABELS = {
    GROUP_ELIGIBLE: "可清理候选",
    GROUP_PROTECTION: "保护期",
    GROUP_EXCEPTION: "例外项",
    GROUP_MISSING_CREDENTIAL: "缺凭证",
    GROUP_MIN_SNAPSHOT: "最少快照数",
}


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class RetentionService(BaseModuleService):
    MODULE = "retention"
    ENTITY = "保留策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "目标表", "保留天数", "最小快照数", "自动清理", "例外规则", "清理方式", "生效时间"]
    REQUIRED_FIELDS = ["策略编号", "目标表", "保留天数"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    PIPELINE = "retention_pipeline"
    UPSTREAM_MODULES = ["cold_archive"]
    PRODUCES_ARTIFACT = "数据保留清理记录"

    # ------------------------------------------------------------------
    # 清理预览
    # ------------------------------------------------------------------
    def cleanup_preview(self, policy_id: int) -> dict[str, Any]:
        """生成清理预览单：按表列出各对象的清理依据并分四组展示暂缓项。

        预览单冻结生成时的策略参数、凭证集合与快照集合，
        之后补归档 / 撤销快照不影响本次预览内容（历史预览按原条件可复核）。
        """
        entry = store.find(self.MODULE, policy_id)
        if entry is None:
            return {"ok": False, "reason": f"策略 {policy_id} 不存在"}
        if entry.get("status") != "已生效":
            return {"ok": False, "reason": f"策略未生效，当前状态: {entry.get('status')}"}

        params = self._policy_params(entry)
        table_name = params["目标表"]
        credentials = store.credentials_for_table(table_name)
        snapshots = store.readable_snapshots_for_table(table_name)

        groups = self._classify_objects(table_name, params)

        seq = len(store._cleanup_previews) + 1
        preview_id = f"PV-{time.strftime('%Y%m%d')}-{seq:04d}"
        preview = {
            "preview_id": preview_id,
            "generated_at": _fmt_ts(time.time()),
            "generated_at_ts": time.time(),
            "policy_id": policy_id,
            "策略编号": entry.get("策略编号"),
            "目标表": table_name,
            "policy_params": {
                "保留天数": params["保留天数"],
                "最小快照数": params["最小快照数"],
                "例外规则": params["例外规则原文"],
                "清理方式": params["清理方式"],
            },
            "groups": groups,
            "group_labels": dict(GROUP_LABELS),
            "counts": {key: len(items) for key, items in groups.items()},
            # 冻结生成时条件：凭证与快照集合，供执行时比对冲突、历史复核
            "conditions": {
                "credentials": {
                    c["凭证编号"]: {
                        "表名": c["表名"], "归档版本": c["归档版本"],
                        "覆盖分区": list(c["覆盖分区"]), "状态": c["状态"],
                    }
                    for c in store.all_archive_credentials()
                },
                "snapshots": {
                    s["快照编号"]: {"版本": s["版本"], "覆盖分区": list(s["覆盖分区"]), "可读": True}
                    for s in snapshots
                },
                "valid_snapshot_count": len(snapshots),
            },
            "已执行": False,
            "执行编号": None,
        }
        store.save_cleanup_preview(preview)
        return {"ok": True, "preview": self._preview_view(preview)}

    def get_preview(self, preview_id: str) -> dict[str, Any]:
        """历史预览回查：按生成时冻结的条件原样返回。"""
        preview = store.get_cleanup_preview(preview_id)
        if preview is None:
            return {"ok": False, "reason": f"预览单 {preview_id} 不存在"}
        return {"ok": True, "preview": self._preview_view(preview)}

    def list_previews(self) -> dict[str, Any]:
        items = []
        for p in store.list_cleanup_previews():
            items.append({
                "preview_id": p["preview_id"],
                "generated_at": p["generated_at"],
                "policy_id": p["policy_id"],
                "策略编号": p["策略编号"],
                "目标表": p["目标表"],
                "counts": p["counts"],
                "已执行": p["已执行"],
                "执行编号": p["执行编号"],
            })
        return {"ok": True, "items": items, "total": len(items)}

    def preview_object_detail(self, preview_id: str, partition: str) -> dict[str, Any]:
        """预览对象详情：关联最近可用快照，并呈现当前凭证覆盖的分区。

        - 最近可用快照按预览冻结的快照集计算（与列表中的恢复依据一致）；
        - 凭证覆盖分区取查询时刻的当前凭证（含预览后补录的）。
        """
        preview = store.get_cleanup_preview(preview_id)
        if preview is None:
            return {"ok": False, "reason": f"预览单 {preview_id} 不存在"}

        found = None
        for group_key, items in preview["groups"].items():
            for item in items:
                if item["分区"] == partition:
                    found = (group_key, item)
                    break
            if found:
                break
        if found is None:
            return {"ok": False, "reason": f"预览单 {preview_id} 中不存在分区 {partition}"}

        group_key, item = found
        # 最近可用快照：预览冻结快照集中覆盖该分区的最近一份。
        # 冻结集按快照时间倒序插入（readable_snapshots_for_table 排序），
        # 字典保持插入序，第一个覆盖者即最近可用快照。
        frozen_snaps = preview["conditions"]["snapshots"]
        latest = None
        for snap_id, snap in frozen_snaps.items():
            if partition in snap.get("覆盖分区", []):
                latest = (snap_id, snap)
                break
        latest_snapshot = None
        if latest is not None:
            detail = store.snapshot_detail(latest[0]) or {}
            latest_snapshot = {
                "快照编号": latest[0],
                "版本": latest[1].get("版本", ""),
                "存储路径": detail.get("存储路径", ""),
                "覆盖分区": list(latest[1].get("覆盖分区", [])),
            }

        # 当前凭证覆盖的分区（该表全部凭证，含预览后补录）
        credential_coverage = [
            {
                "凭证编号": c["凭证编号"],
                "归档版本": c["归档版本"],
                "状态": c["状态"],
                "覆盖分区": list(c["覆盖分区"]),
                "来源策略": c.get("来源策略", ""),
            }
            for c in store.credentials_for_table(preview["目标表"], confirmed_only=False)
        ]
        return {
            "ok": True,
            "preview_id": preview_id,
            "group": group_key,
            "group_label": GROUP_LABELS[group_key],
            "object": item,
            "latest_snapshot": latest_snapshot,
            "credential_coverage": credential_coverage,
        }

    # ------------------------------------------------------------------
    # 并发变更：补归档 / 撤销快照
    # ------------------------------------------------------------------
    def supplement_credential(
        self, table_name: str, partition: str, version: str, source_policy: str = "人工补录",
    ) -> dict[str, Any]:
        """补归档：为指定分区补录归档凭证（已确认状态，回写到清理清单）。"""
        if not table_name or not partition or not version:
            return {"ok": False, "reason": "表名、分区、归档版本均不能为空"}
        existing = store.find_credential(table_name, partition, version)
        if existing is not None:
            return {
                "ok": False,
                "reason": f"凭证已存在：{existing['凭证编号']}（{table_name}/{version} 覆盖 {partition}）",
            }
        record = store.add_archive_credential({
            "表名": table_name,
            "覆盖分区": [partition],
            "归档版本": version,
            "状态": "已确认",
            "来源策略": source_policy,
            "归档时间": _fmt_ts(time.time()),
        })
        return {"ok": True, "credential": record, "message": f"凭证 {record['凭证编号']} 已补录并回写清理清单"}

    def revoke_snapshot(self, snap_id: str) -> dict[str, Any]:
        """撤销快照：撤销后该快照不再作为任何对象的恢复依据。"""
        ok, message = store.revoke_snapshot(snap_id)
        return {"ok": ok, "message": message}

    # ------------------------------------------------------------------
    # 提交执行
    # ------------------------------------------------------------------
    def execute_cleanup(self, preview_id: str) -> dict[str, Any]:
        """按预览单提交清理执行。

        清理只基于执行时刻的完整凭证：预览后发生的补归档、撤销快照
        会与冻结条件比对，冲突在执行清单里注明采用的资格依据。
        实际清理范围不超出原预览候选；执行时刻新合格的对象列入
        “下一次可确认的分区”，不在本次清理范围内。
        """
        preview = store.get_cleanup_preview(preview_id)
        if preview is None:
            return {"ok": False, "reason": f"预览单 {preview_id} 不存在"}
        if preview.get("已执行"):
            return {"ok": False, "reason": f"预览单 {preview_id} 已提交执行（执行编号 {preview['执行编号']}）"}

        table_name = preview["目标表"]
        params = preview["policy_params"]
        exec_params = {
            "目标表": table_name,
            "保留天数": int(params["保留天数"]),
            "最小快照数": int(params["最小快照数"]),
            "例外规则原文": str(params["例外规则"]),
            "例外规则": [k.strip() for k in str(params["例外规则"]).split(",") if k.strip()],
            "清理方式": str(params["清理方式"]),
        }

        # ---- 冲突检测：预览冻结条件 vs 执行时刻状态 ----
        conflicts = self._detect_conflicts(preview)

        # ---- 执行时刻完整凭证 + 可读快照，重新核对 ----
        current_snapshots = store.readable_snapshots_for_table(table_name)
        min_snapshots = exec_params["最小快照数"]

        cleaned: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        candidate_partitions = {item["分区"] for item in preview["groups"][GROUP_ELIGIBLE]}

        for item in preview["groups"][GROUP_ELIGIBLE]:
            obj = self._find_object(table_name, item["分区"])
            if obj is None or obj.get("已清理"):
                deferred.append({**item, "暂缓原因": "对象已不存在或已被清理",
                                 "资格依据": "执行时刻清理对象清单"})
                continue
            group, detail = self._classify_object(obj, exec_params, current_snapshots)
            if group == GROUP_ELIGIBLE:
                cleaned.append({**item, **detail})
            else:
                deferred.append({**item, **detail,
                                 "暂缓原因": detail.get("暂缓原因", "执行时刻不再满足清理条件")})

        # ---- 下一次可确认的分区：非本次候选的未清理对象 ----
        next_confirmable: list[dict[str, Any]] = []
        for obj in store.cleanup_objects_for_table(table_name):
            if obj["分区"] in candidate_partitions:
                continue
            group, detail = self._classify_object(obj, exec_params, current_snapshots)
            entry = {"表名": obj["表名"], "分区": obj["分区"], "数据版本": obj["数据版本"]}
            if group == GROUP_ELIGIBLE:
                entry["条件"] = "已具备完整凭证与恢复依据，下一次预览可确认清理"
                entry["清理依据"] = detail.get("清理依据", "")
                entry["恢复依据"] = detail.get("恢复依据", "")
            else:
                entry["条件"] = detail.get("解除条件", detail.get("暂缓原因", ""))
            next_confirmable.append(entry)

        seq = len(store._cleanup_executions) + 1
        exec_id = f"EXEC-{time.strftime('%Y%m%d')}-{seq:04d}"
        execution = {
            "exec_id": exec_id,
            "preview_id": preview_id,
            "executed_at": _fmt_ts(time.time()),
            "目标表": table_name,
            "清理方式": exec_params["清理方式"],
            "cleaned": cleaned,
            "deferred": deferred,
            "next_confirmable": next_confirmable,
            "conflicts": conflicts,
            "valid_snapshot_count": len(current_snapshots),
            "最小快照数": min_snapshots,
            "summary": (
                f"实际清理 {len(cleaned)} 项，暂缓 {len(deferred)} 项，"
                f"下一次可确认 {len(next_confirmable)} 个分区，冲突 {len(conflicts)} 项"
            ),
        }
        store.add_cleanup_execution(execution)

        for item in cleaned:
            store.mark_cleanup_object_cleaned(table_name, item["分区"], exec_id)
        preview["已执行"] = True
        preview["执行编号"] = exec_id

        self.produce_artifact([{
            "id": exec_id,
            "table": table_name,
            "cleaned": len(cleaned),
            "deferred": len(deferred),
            "conflicts": len(conflicts),
        }], f"RETENTION_CLEANUP_{exec_id}")

        return {"ok": True, "execution": execution}

    def list_executions(self) -> dict[str, Any]:
        items = store.list_cleanup_executions()
        return {"ok": True, "items": items, "total": len(items)}

    # ------------------------------------------------------------------
    # 内部分类与核对逻辑
    # ------------------------------------------------------------------
    def _policy_params(self, entry: dict[str, Any]) -> dict[str, Any]:
        exceptions_raw = str(entry.get("例外规则", "") or "")
        return {
            "目标表": str(entry.get("目标表", "")),
            "保留天数": int(str(entry.get("保留天数", "30"))),
            "最小快照数": int(str(entry.get("最小快照数", "3"))),
            "例外规则原文": exceptions_raw,
            "例外规则": [k.strip() for k in exceptions_raw.split(",") if k.strip()],
            "清理方式": str(entry.get("清理方式", "软删除")),
        }

    def _find_object(self, table_name: str, partition: str) -> dict[str, Any] | None:
        for obj in store.cleanup_objects_for_table(table_name, include_cleaned=True):
            if obj.get("分区") == partition:
                return obj
        return None

    def _classify_objects(self, table_name: str, params: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        snapshots = store.readable_snapshots_for_table(table_name)
        groups: dict[str, list[dict[str, Any]]] = {key: [] for key in GROUP_LABELS}
        for obj in store.cleanup_objects_for_table(table_name):
            group, detail = self._classify_object(obj, params, snapshots)
            groups[group].append({
                "表名": obj["表名"],
                "分区": obj["分区"],
                "数据版本": obj["数据版本"],
                "数据时间": _fmt_ts(float(obj["_ts"])),
                "行数": obj.get("行数", 0),
                **detail,
            })
        return groups

    def _classify_object(
        self, obj: dict[str, Any], params: dict[str, Any], snapshots: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        """按优先级判定对象分组：保护期 → 例外项 → 缺凭证 → 最少快照数 → 可清理。"""
        table_name = obj["表名"]
        partition = obj["分区"]
        version = obj["数据版本"]
        retention_days = params["保留天数"]
        min_snapshots = params["最小快照数"]
        obj_ts = float(obj["_ts"])
        cutoff = time.time() - retention_days * 86400

        # 1. 保护期：数据时间仍在保留天数内
        if obj_ts >= cutoff:
            expire_ts = obj_ts + retention_days * 86400
            return GROUP_PROTECTION, {
                "暂缓原因": f"数据在保留期内（{retention_days} 天）",
                "解除条件": f"保留期至 {_fmt_ts(expire_ts)}，到期后可确认",
            }

        # 2. 例外项：命中例外规则关键词
        hits = [kw for kw in params["例外规则"] if kw and kw in str(obj)]
        if hits:
            return GROUP_EXCEPTION, {
                "暂缓原因": f"命中例外规则：{'、'.join(hits)}",
                "解除条件": "例外规则解除前不参与清理",
            }

        # 3. 缺凭证：按对象（表+分区）和版本核对已确认归档凭证
        cred = store.find_credential(table_name, partition, version)
        if cred is None:
            others = [
                c["凭证编号"] for c in store.all_archive_credentials()
                if partition in c.get("覆盖分区", []) and c.get("表名") != table_name
            ]
            reason = f"无本表（{table_name}）{version} 版本已确认归档凭证"
            if others:
                reason += f"；仅有别表凭证：{'、'.join(others)}"
            return GROUP_MISSING_CREDENTIAL, {
                "暂缓原因": reason,
                "解除条件": f"补录本表 {version} 版本归档凭证后可确认",
            }
        basis = f"{cred['凭证编号']}（{cred['归档版本']}）已确认归档"

        # 4. 最少快照数：恢复依据缺失或有效快照数不足
        covering = [s for s in snapshots if partition in s["覆盖分区"]]
        if not covering:
            return GROUP_MIN_SNAPSHOT, {
                "清理依据": basis,
                "暂缓原因": "无可读快照覆盖该分区，恢复依据不足",
                "解除条件": "恢复覆盖该分区的可读快照后可确认",
            }
        latest = covering[0]  # readable_snapshots_for_table 已按时间倒序
        recovery = f"{latest['快照编号']}（{latest['版本']}）"
        if len(snapshots) < min_snapshots:
            return GROUP_MIN_SNAPSHOT, {
                "清理依据": basis,
                "恢复依据": recovery,
                "暂缓原因": f"有效快照数 {len(snapshots)} 低于最小快照数 {min_snapshots}",
                "解除条件": f"有效快照数恢复至 {min_snapshots} 份以上后可确认",
            }

        # 5. 可清理候选
        return GROUP_ELIGIBLE, {"清理依据": basis, "恢复依据": recovery}

    def _detect_conflicts(self, preview: dict[str, Any]) -> list[dict[str, Any]]:
        """比对预览冻结条件与执行时刻状态，注明补归档 / 撤销快照冲突及资格依据。"""
        conflicts: list[dict[str, Any]] = []
        frozen_creds = preview["conditions"]["credentials"]
        frozen_snaps = preview["conditions"]["snapshots"]

        # 补归档：预览后新增的凭证
        for cred in store.all_archive_credentials():
            if cred["凭证编号"] not in frozen_creds:
                conflicts.append({
                    "类型": "补归档",
                    "说明": (
                        f"凭证 {cred['凭证编号']}（{cred['表名']}/{cred['归档版本']}，"
                        f"覆盖 {'、'.join(cred['覆盖分区'])}）为预览生成后补录"
                    ),
                    "资格依据": (
                        f"清理基于执行时刻完整凭证：采用 {cred['凭证编号']}"
                        f"（状态 {cred['状态']}）作为资格依据"
                    ),
                })

        # 撤销快照：预览时可读、执行时不可读
        current_readable = {
            s["快照编号"] for s in store.readable_snapshots_for_table(preview["目标表"])
        }
        for snap_id in frozen_snaps:
            if snap_id not in current_readable:
                remaining = "、".join(sorted(current_readable)) or "无"
                conflicts.append({
                    "类型": "撤销快照",
                    "说明": f"快照 {snap_id} 在预览生成后被撤销，不再作为恢复依据",
                    "资格依据": (
                        f"恢复依据按执行时刻可读快照集（{remaining}）重新计算；"
                        f"失去恢复依据的候选暂缓清理"
                    ),
                })
        return conflicts

    def _preview_view(self, preview: dict[str, Any]) -> dict[str, Any]:
        """预览单对外视图（冻结内容原样返回，保证历史可复核）。"""
        return {
            "preview_id": preview["preview_id"],
            "generated_at": preview["generated_at"],
            "policy_id": preview["policy_id"],
            "策略编号": preview["策略编号"],
            "目标表": preview["目标表"],
            "policy_params": preview["policy_params"],
            "groups": preview["groups"],
            "group_labels": preview["group_labels"],
            "counts": preview["counts"],
            "conditions": preview["conditions"],
            "已执行": preview["已执行"],
            "执行编号": preview["执行编号"],
        }

    # ------------------------------------------------------------------
    # 既有能力：执行保留清理（保留兼容）
    # ------------------------------------------------------------------
    def execute_retention(self, entry_id: int) -> dict[str, Any]:
        """执行数据保留清理。

        失败场景：
        - 例外规则冲突：在例外规则内的数据被误清理
        - 最小快照数不满足：清理后快照数低于最小要求
        - 自动清理误触发：清理了仍在生命周期内的数据
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"策略 {entry_id} 不存在"}
        if entry.get("status") != "已生效":
            return {"ok": False, "reason": f"策略未生效，当前状态: {entry.get('status')}"}

        table_name = str(entry.get("目标表", ""))
        retention_days = int(str(entry.get("保留天数", "30")))
        min_snapshots = int(str(entry.get("最小快照数", "3")))
        auto_clean = str(entry.get("自动清理", "是"))
        exceptions = str(entry.get("例外规则", ""))
        clean_method = str(entry.get("清理方式", "软删除"))

        if retention_days < 1:
            return {"ok": False, "reason": f"保留天数 {retention_days} 不合法"}

        # 消费上游 cold_archive 清单
        arch_records = store.archive_manifest_for_table(table_name)
        total_archived = sum(r.get("archived_rows", 0) for r in arch_records)

        # 模拟清理逻辑
        cleaned = 0
        retained = 0
        excluded = 0
        issues = []

        # 模拟存储的数据行
        store_rows = list(store.rows(table_name))
        cutoff_ts = time.time() - retention_days * 86400

        for row in store_rows:
            row_age = time.time() - float(str(row.get("_ts", time.time())))
            if row_age > retention_days * 86400:
                if exceptions and any(exc in str(row) for exc in exceptions.split(",")):
                    excluded += 1
                    continue
                cleaned += 1
            else:
                retained += 1

        # 最小快照检查
        if len(store_rows) - cleaned < min_snapshots:
            issues.append(f"清理后将低于最小快照数 {min_snapshots}（当前 {len(store_rows)}，清理 {cleaned}）")
            cleaned = max(0, len(store_rows) - min_snapshots)

        entry["生效时间"] = time.strftime("%Y-%m-%d %H:%M:%S")

        batch_id = f"RETENTION_{entry_id}_{int(time.time())}"
        result = [{
            "table": table_name,
            "retention_days": retention_days,
            "cleaned": cleaned,
            "retained": retained,
            "excluded": excluded,
            "total_archived": total_archived,
            "issues": issues,
        }]
        self.produce_artifact(result, batch_id)

        if total_archived > 0:
            # 有归档数据存在，说明 cold_archive → retention 链路贯通
            return {
                "ok": True,
                "entry_id": entry_id,
                "table": table_name,
                "cleaned": cleaned,
                "retained": retained,
                "excluded": excluded,
                "total_archived": total_archived,
                "cross_module_link": "cold_archive→retention 贯通",
                "issues": issues,
            }

        return {
            "ok": True,
            "entry_id": entry_id,
            "table": table_name,
            "cleaned": cleaned,
            "retained": retained,
            "excluded": excluded,
            "total_archived": total_archived,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["active_policies"] = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已生效")
        base["archive_manifest_total"] = len(store._archive_manifest)
        base["cleanup_previews"] = len(store._cleanup_previews)
        base["archive_credentials"] = len(store._archive_credentials)
        return base
