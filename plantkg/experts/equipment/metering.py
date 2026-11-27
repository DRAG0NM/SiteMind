"""D05 的 24 条计量故障 → 剧本可引用的事件（B22）。

原包 `metadata/meter_data_quality_log.csv` 的 24 条记录已转成
`fixtures/equipment/d05_metering_anomalies.yaml`（nature=derived，时间按 UTC 原样保留）。
`to_script_events` 把它们平移到剧本首日：事件时刻取原记录的**钟面时刻**（小时:分钟），对象取记录里
映射到的本厂配电柜（plant_panel），事件类型统一为 `metering_anomaly`。这是时间映射，不是原时刻。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURE = ROOT / "fixtures" / "equipment" / "d05_metering_anomalies.yaml"


def load_events(path: str | Path | None = None) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path or DEFAULT_FIXTURE).read_text(encoding="utf-8"))
    return [dict(event) for event in data["events"]]


def to_script_event(event: Mapping[str, Any], *, plant_id: str = "demo-electronics", panel_entity: str | None = None,
                    at: str | None = None) -> dict[str, Any]:
    """一条计量故障 → 剧本事件（dict）。默认按原记录 UTC 钟面放到剧本首日同一钟面。"""
    source = datetime.fromisoformat(str(event["start"]))
    clock = at or f"{source.hour:02d}:{source.minute:02d}"
    panel = panel_entity or f"entity://{plant_id}/panel/{event.get('plant_panel') or 'P-PROD'}"
    label = event.get("issue_label") or event.get("issue_type") or "计量异常"
    return {"at": clock, "id": f"meter-{event['event_id']}", "type": "metering_anomaly", "entity_id": panel,
            "severity": 2, "source": "script", "summary": f"表计 {event['asset_id']} 计量异常：{label}",
            "payload": {"kind": "metering", "meter": event["asset_id"], "asset_type": event.get("asset_type"),
                        "issue_type": event.get("issue_type"), "issue_label": label, "panel": panel,
                        "source": "d05", "source_event_id": event["event_id"], "start": str(event["start"]),
                        "end": str(event["end"]), "nature": "derived"}}


def to_script_events(events: list[Mapping[str, Any]] | None = None, *, plant_id: str = "demo-electronics",
                     panel_entity: str | None = None) -> list[dict[str, Any]]:
    return [to_script_event(e, plant_id=plant_id, panel_entity=panel_entity) for e in (events or load_events())]
