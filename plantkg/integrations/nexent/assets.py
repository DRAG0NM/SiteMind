"""Nexent Agent 配置（nexent_assets/agents/*.yaml）与注入表。

一份 yaml 描述一个 Agent：名字（tool_params 的桶名，必须是 Agent 名，不能是展示名，P21a）、角色、模型、
绑定的 plantkg 工具（注册名，不带前缀）、是否绑知识库检索、子 Agent 与 Skill 绑定位。
导入命令按它在 Nexent 里建 Agent；网关按它生成注入白名单与按专家分桶的 tool_params，并在启动时自检。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from plantkg.core.contracts import TOOL_PREFIX, ToolSpec
from plantkg.core.registry import RegistryError

KB_TOOL = "knowledge_base_search"  # Nexent 内置检索工具；document_paths 漏注入是“失败放开”（P21c），所以缺了就拒绝运行


class SkillSlot(BaseModel):
    """Skill 绑定位。placeholder=True 表示由后续分支（B27/B28/B32 等）交付后用导入命令替换。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    owner: str  # 负责交付的分支
    placeholder: bool = True


class KnowledgeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index_names: tuple[str, ...] = ()
    # 占位：可见文档清单。正式来源是按 as_of 过滤的资料目录（B26 经 nexent.document_paths 钩子登记）
    document_paths: tuple[str, ...] = ()


class AgentAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str
    role: Literal["main", "expert"]
    domain: str | None = None  # 专家的领域：energy / elevator / hvac / equipment
    description: str = ""
    model: str = "deepseek-v4.1-flash-1017"
    max_steps: int = Field(default=12, gt=0)
    prompt: str
    tools: tuple[str, ...] = ()  # plantkg 注册名（不带 plantkg_ 前缀）
    knowledge: KnowledgeBinding | None = None  # 绑定 Nexent 知识库检索
    sub_agents: tuple[str, ...] = ()
    skills: tuple[SkillSlot, ...] = ()
    placeholder: bool = True  # 本卡的占位配置；业务分支（B19～B22）交付真实配置后改为 false

    @property
    def nexent_tools(self) -> tuple[str, ...]:
        return tuple(TOOL_PREFIX + t for t in self.tools)


def load_assets(directory: Path) -> dict[str, AgentAsset]:
    assets: dict[str, AgentAsset] = {}
    for path in sorted(directory.glob("*.yaml")):
        asset = AgentAsset.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if asset.name in assets:
            raise RegistryError(f"Agent {asset.name} 重复定义（{path.name}）")
        if path.stem != asset.name:
            raise RegistryError(f"{path.name} 的文件名必须等于 Agent 名 {asset.name}")
        assets[asset.name] = asset
    return assets


def check_assets(assets: Mapping[str, AgentAsset], tools: Mapping[str, Any], main_agent: str) -> None:
    """启动自检（P21a/b）：桶名是 Agent 名；绑定的工具都已登记且接受 ctx；子 Agent 都有定义；主 Agent 存在。"""
    problems = []
    if main_agent not in assets:
        problems.append(f"主 Agent {main_agent} 没有配置")
    for asset in assets.values():
        for tool in asset.tools:
            binding = tools.get(tool)
            if binding is None:
                problems.append(f"{asset.name} 绑定的工具 {tool} 未登记")
            elif not binding.spec.accepts_ctx:
                problems.append(f"{asset.name} 绑定的工具 {tool} 不接受 ctx，不能注入")
        for sub in asset.sub_agents:
            if sub not in assets:
                problems.append(f"{asset.name} 的子 Agent {sub} 没有配置")
            elif assets[sub].role != "expert":
                problems.append(f"{asset.name} 的子 Agent {sub} 不是专家")
        if asset.role == "expert" and not asset.domain:
            problems.append(f"专家 {asset.name} 没写 domain")
    if problems:
        raise RegistryError("Nexent Agent 配置自检未过：" + "；".join(problems))


def experts_by_domain(assets: Mapping[str, AgentAsset]) -> dict[str, AgentAsset]:
    return {a.domain: a for a in assets.values() if a.role == "expert" and a.domain}


def participants(assets: Mapping[str, AgentAsset], agent_name: str) -> list[tuple[AgentAsset, int]]:
    """一次运行会用到的 Agent 与 depth：发起的 Agent 为 0，主 Agent 的子 Agent 为 1（不嵌套孙 Agent）。"""
    root = assets[agent_name]
    return [(root, 0)] + [(assets[s], 1) for s in root.sub_agents]


def tool_params(buckets: Iterable[tuple[AgentAsset, str, tuple[str, ...]]], specs: Mapping[str, ToolSpec]) -> dict[str, Any]:
    """按 Agent 名分桶：每个桶只放该 Agent 绑定的、接受 ctx 的 plantkg 工具；绑知识库的再放 document_paths。

    buckets 为 (Agent, 该 Agent 的 ctx, 该 Agent 的可见文档)。注入不接受 ctx 的工具会让整次运行失败，所以严格按白名单。
    """
    agents: dict[str, Any] = {}
    for asset, ctx, docs in buckets:
        tools: dict[str, dict[str, Any]] = {
            TOOL_PREFIX + name: {"ctx": ctx} for name in asset.tools if specs[name].accepts_ctx
        }
        if asset.knowledge is not None:
            tools[KB_TOOL] = {"document_paths": list(docs), **({"index_names": list(asset.knowledge.index_names)}
                                                                if asset.knowledge.index_names else {})}
        if tools:
            agents[asset.name] = {"tools": tools}
    return {"agents": agents}
