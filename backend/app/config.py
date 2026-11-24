from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "充桩条"
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("CHARGEBOUT_DB", str(ROOT / "data" / "chargebout.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

PILE_STATUSES = ("空闲", "占用", "故障", "维护")
GUN_STATUSES = ("可用", "占用", "故障")
SESSION_STATUSES = ("充电中", "结算中", "已关账", "已超时", "已中断")

# 全局唯一口径：同桩互斥单充。
# 同一桩位任意时刻至多一笔「未结」会话；未结会话持有桩位与枪线。
# 充电中/结算中/已超时都算持有，必须结算或中断后才释放。
HOLDING_SESSIONS = ("充电中", "结算中", "已超时")
# 计入「在充电量」的会话（已超时的电量不再计入在充）。
METERED_SESSIONS = ("充电中", "结算中")
PILE_CONCURRENCY_LIMIT = 1
OPEN_POLICY = "同桩互斥单充"
# 自动选枪谓词：桩内编号最小且（按持有态推导后）可用的枪线。
GUN_PICK_RULE = "桩内枪线按编号升序，取第一条可用枪线"
