"""电梯专家的私有知识摘要（B20）。

先放最小摘要，标 synthetic：B26 接入《电梯维保与安全规程》等原文后，把这里的条目换成带资料版本与
原文定位的规则。专家用条目 ID 作 evidence_refs，回答里可以回溯到具体条目。
"""
from __future__ import annotations

KNOWLEDGE: tuple[dict[str, str], ...] = (
    {
        "id": "elevator_peak_dispatch",
        "title": "早高峰人梯调度",
        "text": "早高峰以上行为主：首层集中派梯，低区（1～6 层）与高区（7～12 层）分区服务；"
                "平均候梯超过 60 秒或排队超过 20 人时增派轿厢并保持分区。午间双向、晚高峰下行、低谷停部分梯节能。",
        "source": "《电梯群控运行规程》摘要（synthetic，B26 接入原文）",
        "tags": "人梯,高峰,分区,拥堵",
    },
    {
        "id": "elevator_trapped_rescue",
        "title": "困人处置",
        "text": "轿厢困人时立即停止该梯、禁止层门被强行开启，通知持证维保人员；救援动作须人工确认后执行。"
                "同一时段其他货梯接管该梯的生产任务，避免产线断料。",
        "source": "《电梯维保与安全规程》摘要（synthetic，B26 接入原文）",
        "tags": "困人,安全,救援,货梯接管",
    },
    {
        "id": "elevator_freight_priority",
        "title": "货梯调度",
        "text": "货梯按生产任务优先级与物料紧急度排队：紧急物料先走，普通任务让行人梯高峰；"
                "峰电时段合并批次，减少往返次数与用电。",
        "source": "《物料搬运与货梯调度指引》摘要（synthetic，B26 接入原文）",
        "tags": "货梯,优先级,紧急度,峰电,合批",
    },
    {
        "id": "elevator_maintenance",
        "title": "维保与停用",
        "text": "门锁、制动器与限速器按月检查，年度检验合格才投入群控；故障或检修中的轿厢不计运力，"
                "调度方案里不得把它算成可用梯。",
        "source": "《电梯维保与安全规程》摘要（synthetic，B26 接入原文）",
        "tags": "维保,安全,停用,年检",
    },
)

BY_ID = {item["id"]: item for item in KNOWLEDGE}
