"""解析水位领域服务：按来源渠道维护解析位点推进、滞后判定与推进中断处置，产出水位读数数据集。"""
from __future__ import annotations

from typing import Any

from app.facts import is_closed_batch
from app.services.base import BaseModuleService
from app.store import store


class ParseWatermarkService(BaseModuleService):
    MODULE = "parse_watermark"
    ENTITY = "水位读数"
    OPERATION_TERM = "水位"
    LIST_FIELDS = ["水位编号", "来源渠道", "已解析至位点", "待解析数量", "最近批次", "水位推进时间", "滞后批次数", "滞后原因"]
    REQUIRED_FIELDS = ["水位编号", "来源渠道", "已解析至位点"]
    STATUS_ORDER = ["已推进", "轻微滞后", "严重滞后", "推进中断"]
    ACTION_RULES = {"推进水位": "轻微滞后", "登记滞后": "严重滞后", "标记中断": "推进中断"}
    NEGATIVE_ACTIONS = ["标记中断"]

    SEVERE_LAG = 3

    def _find_by_channel(self, channel: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("来源渠道") == channel:
                return row
        return None

    def unclosed_batches(self, channel: str) -> list[str]:
        """列出该来源渠道下尚未闭合的投递批次编号。

        闭合判定收口到共享事实来源（app.facts），与指标结算的已闭合批次计数同源。
        """
        closed_positions = {
            r.get("关联投递批次")
            for r in store.rows("parse_batch")
            if is_closed_batch(r)
        }
        unclosed: list[str] = []
        for row in store.rows("file_intake"):
            if row.get("来源渠道") != channel:
                continue
            batch_no = row.get("批次编号")
            if batch_no in closed_positions:
                continue
            if row.get("status") in ("探测失败",):
                unclosed.append(batch_no)
                continue
            parse_row = next(
                (r for r in store.rows("parse_batch")
                 if r.get("关联投递批次") == batch_no), None)
            if parse_row is None or not is_closed_batch(parse_row):
                unclosed.append(batch_no)
        return sorted(set(unclosed))

    def advance_position(self, channel: str, new_position: str,
                         advance_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """推进水位：只有该来源全部批次闭合后才允许推进。

        未闭合批次存在时水位读数保持原位点并写入滞后原因，推进请求被拒绝。
        """
        reading = self._find_by_channel(channel)
        if reading is None:
            return False, f"来源渠道 {channel} 没有水位读数", None

        unclosed = self.unclosed_batches(channel)
        if unclosed:
            reading["待解析数量"] = str(len(unclosed))
            reading["滞后批次数"] = str(len(unclosed))
            reading["滞后原因"] = f"未闭合批次 {','.join(unclosed[:5])} 阻断水位推进"
            severity = "严重滞后" if len(unclosed) >= self.SEVERE_LAG else "轻微滞后"
            reading["status"] = severity
            reading["pending"] = True
            reading["abnormal"] = True
            return False, (
                f"来源 {channel} 仍有 {len(unclosed)} 个批次未闭合"
                f"（{','.join(unclosed[:5])}），水位不得推进"
            ), dict(reading)

        old_position = reading.get("已解析至位点", "")
        if new_position <= old_position and old_position:
            return False, (
                f"目标位点 {new_position} 不晚于当前位点 {old_position}，水位不得回退"
            ), dict(reading)

        reading["已解析至位点"] = new_position
        reading["最近批次"] = new_position
        reading["水位推进时间"] = advance_time
        reading["待解析数量"] = "0"
        reading["滞后批次数"] = "0"
        reading["滞后原因"] = ""
        reading["status"] = "已推进"
        reading["pending"] = False
        reading["abnormal"] = False
        return True, f"来源 {channel} 水位已推进至 {new_position}", dict(reading)

    def register_lag(self, channel: str, reason: str) -> tuple[bool, str]:
        """登记滞后：按未闭合批次数判定轻微/严重滞后。"""
        reading = self._find_by_channel(channel)
        if reading is None:
            return False, f"来源渠道 {channel} 没有水位读数"
        unclosed = self.unclosed_batches(channel)
        lag = len(unclosed)
        reading["滞后批次数"] = str(lag)
        reading["待解析数量"] = str(lag)
        reading["滞后原因"] = reason
        reading["status"] = "严重滞后" if lag >= self.SEVERE_LAG else "轻微滞后"
        reading["pending"] = True
        reading["abnormal"] = lag >= self.SEVERE_LAG
        return True, f"来源 {channel} 登记滞后 {lag} 个批次，判定为{reading['status']}"

    def mark_interrupted(self, channel: str, reason: str) -> tuple[bool, str]:
        """标记推进中断：中断期间该来源不得再推进水位。"""
        reading = self._find_by_channel(channel)
        if reading is None:
            return False, f"来源渠道 {channel} 没有水位读数"
        if reading.get("status") == "推进中断":
            return False, f"来源 {channel} 已处于推进中断状态"
        reading["status"] = "推进中断"
        reading["滞后原因"] = reason
        reading["pending"] = True
        reading["abnormal"] = True
        return True, f"来源 {channel} 水位推进已中断：{reason}"

    def lag_ranking(self) -> list[dict[str, Any]]:
        """按滞后批次数倒序输出水位读数，供分析看板消费。"""
        rows = [
            {
                "来源渠道": r.get("来源渠道"),
                "已解析至位点": r.get("已解析至位点"),
                "滞后批次数": int(r.get("滞后批次数") or 0),
                "滞后原因": r.get("滞后原因", ""),
                "status": r.get("status"),
            }
            for r in store.rows(self.MODULE)
        ]
        return sorted(rows, key=lambda x: -x["滞后批次数"])
