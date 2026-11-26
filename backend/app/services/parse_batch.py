"""解析批次领域服务：汇聚每次解析的行级统计与闭合判定，产出批次记录数据集。

闭合凭据（``闭合凭据``）是每笔闭合事实的可核对标识，由本服务在闭合时统一签发；
解析指标与解析水位通过 app.facts 里的共享事实来源读取闭合范围，不再各自判定状态。
"""
from __future__ import annotations

from typing import Any

from app.facts import fact_source, is_closed_batch
from app.services.base import BaseModuleService
from app.store import store


class ParseBatchService(BaseModuleService):
    MODULE = "parse_batch"
    ENTITY = "批次记录"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["批次记录编号", "关联投递批次", "解析器标识", "总行数", "成功行数", "隔离行数", "批次状态", "闭合凭据", "完成时间"]
    REQUIRED_FIELDS = ["批次记录编号", "关联投递批次", "解析器标识"]
    STATUS_ORDER = ["解析中", "已闭合", "存在隔离", "已作废"]
    ACTION_RULES = {"闭合批次": "已闭合", "登记隔离": "存在隔离", "作废批次": "已作废"}
    NEGATIVE_ACTIONS = ["作废批次"]

    @staticmethod
    def _int(row: dict[str, Any], field: str) -> int:
        text = str(row.get(field) or "").strip()
        return int(text) if text.isdigit() else 0

    def _find_by_intake(self, intake_batch: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("关联投递批次") == intake_batch:
                return row
        return None

    @staticmethod
    def issue_credential(intake_batch: str) -> str:
        """闭合凭据按投递批次稳定签发：同一批次反复发布完成证明仍是同一笔凭据。"""
        return f"CC-{intake_batch}"

    def tally_from_field_parse(self, intake_batch: str) -> dict[str, int]:
        """从字段解析明细聚合行级统计：成功行数与部分失败行数。"""
        parsed = store.rows("field_parse")
        rows_of_batch = [r for r in parsed if r.get("关联批次") == intake_batch]
        row_numbers: dict[str, bool] = {}
        for r in rows_of_batch:
            line = str(r.get("解析行号") or "")
            if not line:
                continue
            ok = r.get("status") == "解析成功"
            row_numbers[line] = row_numbers.get(line, True) and ok
        success = sum(1 for ok in row_numbers.values() if ok)
        failed = sum(1 for ok in row_numbers.values() if not ok)
        return {"total_rows": len(row_numbers), "success_rows": success, "failed_rows": failed}

    def tally_from_quarantine(self, intake_batch: str) -> dict[str, int]:
        """从坏行隔离明细统计真实隔离行数与已回补行数。"""
        rows = [
            r for r in store.rows("row_quarantine")
            if r.get("关联批次") == intake_batch
        ]
        return {
            "quarantined": len(rows),
            "reinstated": sum(1 for r in rows if r.get("status") == "已回补"),
            "discarded": sum(1 for r in rows if r.get("status") == "已废弃"),
            "unreviewed": sum(1 for r in rows if r.get("status") in ("已隔离", "复核中")),
        }

    def refresh_batch_counts(self, intake_batch: str) -> tuple[bool, str, dict[str, Any] | None]:
        """按字段解析与坏行隔离的真实计数刷新批次行数，不允许人工填数。"""
        batch = self._find_by_intake(intake_batch)
        if batch is None:
            return False, f"投递批次 {intake_batch} 没有对应批次记录", None
        if batch.get("status") == "已作废":
            return False, f"批次记录 {batch.get('批次记录编号')} 已作废，不再刷新计数", None

        parse_tally = self.tally_from_field_parse(intake_batch)
        quarantine_tally = self.tally_from_quarantine(intake_batch)
        quarantined = quarantine_tally["quarantined"] - quarantine_tally["reinstated"]
        success = parse_tally["success_rows"] + quarantine_tally["reinstated"]

        batch["总行数"] = str(parse_tally["total_rows"])
        batch["成功行数"] = str(success)
        batch["隔离行数"] = str(max(0, quarantined))
        return True, (
            f"批次 {intake_batch} 计数已按真实明细刷新："
            f"总 {parse_tally['total_rows']} / 成功 {success} / 隔离 {max(0, quarantined)}"
        ), dict(batch)

    def close_batch(self, intake_batch: str, close_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """闭合批次：存在未复核隔离行时不得闭合，计数必须先与明细一致。

        闭合成功即签发闭合凭据，作为指标结算事实来源里可独立核对的闭合证据。
        """
        batch = self._find_by_intake(intake_batch)
        if batch is None:
            return False, f"投递批次 {intake_batch} 没有对应批次记录", None
        if is_closed_batch(batch):
            return False, f"批次 {intake_batch} 已闭合，不能重复闭合", None
        if batch.get("status") == "已作废":
            return False, f"批次 {intake_batch} 已作废，不能闭合", None

        quarantine_tally = self.tally_from_quarantine(intake_batch)
        if quarantine_tally["unreviewed"] > 0:
            batch["批次状态"] = "存在隔离"
            batch["status"] = "存在隔离"
            batch["pending"] = True
            batch["abnormal"] = True
            return False, (
                f"批次 {intake_batch} 仍有 {quarantine_tally['unreviewed']} 行隔离未复核，"
                f"不得闭合"
            ), dict(batch)

        ok, _msg, _row = self.refresh_batch_counts(intake_batch)
        total = self._int(batch, "总行数")
        success = self._int(batch, "成功行数")
        if total > 0 and success == 0:
            batch["批次状态"] = "存在隔离"
            batch["status"] = "存在隔离"
            batch["pending"] = True
            batch["abnormal"] = True
            return False, f"批次 {intake_batch} 成功行数为 0，不得闭合", dict(batch)

        batch["批次状态"] = "已闭合"
        batch["status"] = "已闭合"
        batch["完成时间"] = close_time
        batch["闭合凭据"] = self.issue_credential(intake_batch)
        batch["pending"] = False
        batch["abnormal"] = False
        return True, (
            f"批次 {intake_batch} 已闭合，成功 {success}/{total} 行，"
            f"闭合凭据 {batch['闭合凭据']}"
        ), dict(batch)

    def ensure_close_proof(
        self,
        intake_batch: str,
        close_time: str,
        total_rows: int,
        success_rows: int,
        quarantine_rows: int,
        parser: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """管线结算入口：为投递批次发布完成证明并确保存在可核对的闭合事实。

        - 批次已闭合：沿用原闭合凭据，重复发布同一完成证明不产生新计数；
        - 批次不存在：按管线交付的真实计数登记一条已闭合批次记录并签发凭据；
        - 批次在途：按传入计数闭合（管线已完成全部解析与隔离处置）。
        """
        batch = self._find_by_intake(intake_batch)
        if batch is None:
            entry_id = max(
                (int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0
            ) + 1
            batch = {
                "id": entry_id,
                "批次记录编号": f"PB-{intake_batch}",
                "关联投递批次": intake_batch,
                "解析器标识": parser,
            }
            store.rows(self.MODULE).append(batch)

        if is_closed_batch(batch):
            credential = str(batch.get("闭合凭据") or "").strip() or (
                self.issue_credential(intake_batch)
            )
            batch["闭合凭据"] = credential
            return True, (
                f"批次 {intake_batch} 完成证明已登记，沿用闭合凭据 {credential}"
            ), dict(batch)

        if batch.get("status") == "已作废":
            return False, f"批次 {intake_batch} 已作废，不能发布完成证明", None

        batch.update({
            "解析器标识": batch.get("解析器标识") or parser,
            "总行数": str(total_rows),
            "成功行数": str(success_rows),
            "隔离行数": str(quarantine_rows),
            "批次状态": "已闭合",
            "status": "已闭合",
            "完成时间": close_time,
            "闭合凭据": self.issue_credential(intake_batch),
            "pending": False,
            "abnormal": False,
        })
        return True, (
            f"批次 {intake_batch} 完成证明已发布，闭合凭据 {batch['闭合凭据']}"
        ), dict(batch)

    def invalidate_batch(self, intake_batch: str, reason: str) -> tuple[bool, str]:
        """作废批次：格式无法识别或路由失败时使用，作废后不得再进入契约。"""
        batch = self._find_by_intake(intake_batch)
        if batch is None:
            return False, f"投递批次 {intake_batch} 没有对应批次记录"
        if batch.get("status") == "已作废":
            return False, f"批次 {intake_batch} 已作废"
        batch["批次状态"] = "已作废"
        batch["status"] = "已作废"
        batch["pending"] = False
        batch["abnormal"] = True
        batch["作废原因"] = reason
        return True, f"批次 {intake_batch} 已作废：{reason}"

    def open_batches(self) -> list[dict[str, Any]]:
        """列出全部未闭合批次，供水位推进前置校验消费。"""
        return [
            {
                "批次记录编号": r.get("批次记录编号"),
                "关联投递批次": r.get("关联投递批次"),
                "批次状态": r.get("批次状态"),
                "隔离行数": r.get("隔离行数"),
            }
            for r in store.rows(self.MODULE)
            if r.get("批次状态") != "已作废" and not is_closed_batch(r)
        ]

    def closed_ratio(self) -> float:
        """批次闭合率：已闭合 / 非作废总数。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("批次状态") != "已作废"]
        if not rows:
            return 0.0
        closed = sum(1 for r in rows if is_closed_batch(r))
        return closed / len(rows)

    def closed_evidence(self, channel: str) -> list[dict[str, Any]]:
        """供解析指标页面核对：输出来源渠道去重后的逐笔闭合证据。"""
        facts = fact_source.build(channel)
        return facts.closed_batches
