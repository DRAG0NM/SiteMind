"""B20 Nexent 配置：导入命令读取的占位配置已换成电梯专家的提示词与 Skill。"""
from __future__ import annotations

from pathlib import Path

import yaml

from datetime import datetime, timezone

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.registry import Registry
from plantkg.experts.elevator import PROPOSE_HOOK, install
from plantkg.integrations.nexent.assets import check_assets, load_assets

ROOT = Path(__file__).resolve().parents[3]
AGENTS = ROOT / "nexent_assets" / "agents"
SKILL = ROOT / "nexent_assets" / "skills" / "domain-elevator" / "SKILL.md"


def test_elevator_agent_asset_is_ready():
    assets = load_assets(AGENTS)
    asset = assets["plantkg_expert_elevator"]
    assert asset.placeholder is False
    assert "elevator_propose" in asset.tools and "list_assets" in asset.tools
    assert any(slot.name == "domain-elevator" and not slot.placeholder for slot in asset.skills)
    assert "plantkg_elevator_propose" in asset.prompt


def test_assets_pass_import_command_self_check():
    assets = load_assets(AGENTS)
    tools = build_services(load_settings(profile="mock")).registry.tools  # 含各领域包登记的工具
    check_assets(assets, tools, "plantkg_main")  # 不抛异常即通过 B23 导入前的自检


def test_domain_skill_file_is_well_formed():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---")
    front = yaml.safe_load(text.split("---", 2)[1])
    assert front["name"] == "domain-elevator"
    assert "困人" in text and "分区" in text


def test_pack_registers_propose_hook_with_synthetic_context():
    class Binding:
        ticket_id = "ticket://tk-elevator"
        trigger_at = datetime(2026, 7, 1, 0, 5, tzinfo=timezone.utc)

    registry = Registry()
    install(registry)
    (handler,) = registry.hooks_for(PROPOSE_HOOK)
    proposal = handler(None, Binding(), focus="早高峰人梯拥堵")
    assert proposal.expert == "elevator" and proposal.ticket_id == "ticket://tk-elevator"
    assert proposal.actions and proposal.confidence > 0
