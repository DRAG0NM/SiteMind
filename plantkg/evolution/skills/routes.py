"""B43 只读路由：Skill 列表与单个 Skill 的版本链（供 B35 工作台显示）。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from plantkg.core.contracts import Page, SkillSummary, SkillVersionRecord
from plantkg.core.versions import page_args
from plantkg.evolution.skills.history import skill_history, skill_summaries


def register_rest(registry: Any, versions: Any, store: Any) -> None:
    """登记 GET /evolution/skills 与 GET /evolution/skills/{name}/history；provider(context, path, query)。"""

    def skills(context: Any, _path: Mapping[str, str], query: Mapping[str, str]) -> Page[SkillSummary]:
        summaries = skill_summaries(versions, store)
        after, size = page_args(query.get("cursor"), query.get("page_size"))
        items = summaries[after:after + size]
        next_cursor = str(after + size) if after + size < len(summaries) else None
        return Page[SkillSummary](items=tuple(items), next_cursor=next_cursor, as_of=context.as_of)

    def history(context: Any, path: Mapping[str, str], _query: Mapping[str, str]) -> Page[SkillVersionRecord]:
        name = str(path.get("name") or "")
        return Page[SkillVersionRecord](items=tuple(skill_history(versions, store, name)),
                                        as_of=context.as_of)

    registry.register_rest("GET", "/evolution/skills", skills)
    registry.register_rest("GET", "/evolution/skills/{name}/history", history)
