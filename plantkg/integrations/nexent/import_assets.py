"""把 nexent_assets/agents/*.yaml 导入 Nexent：python -m plantkg.integrations.nexent.import_assets

步骤：登录管理端 → 确保 plantkg MCP 服务已登记并扫描工具 → 按配置建/更新四位专家并发布 → 建/更新主 Agent（绑定专家为子 Agent，
钉住刚发布的版本）并发布 → 读回实际绑定核对（工具名、子 Agent）→ 导出到 nexent_assets/agents/exports/。
业务分支（B19～B22 专家配置、B27/B28/B32 的 Skill）改了 yaml 后重跑本命令即替换，只按名字更新自己的 Agent，不碰租户里其他 Agent。

凭据：环境变量 NEXENT_ADMIN_EMAIL / NEXENT_ADMIN_PASSWORD，或 --credentials 指向含 email、password 的 json（放仓库外）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

from plantkg.core.contracts import TOOL_PREFIX, PlantkgError
from plantkg.integrations.nexent import ROOT
from plantkg.integrations.nexent.assets import KB_TOOL, AgentAsset, SkillSlot, load_assets
from plantkg.integrations.nexent.client import AdminClient

MCP_NAME = "plantkg"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.integrations.nexent.import_assets")
    parser.add_argument("--admin-url", default="http://127.0.0.1:5010")
    parser.add_argument("--mcp-url", default="http://host.docker.internal:8700/mcp", help="Nexent 容器里访问 plantkg MCP 的地址")
    parser.add_argument("--agents-dir", default=str(ROOT / "nexent_assets" / "agents"))
    parser.add_argument("--credentials", default=None, help="含 email、password 的 json 文件（仓库外）")
    parser.add_argument("--dry-run", action="store_true", help="只读：打印将要做的改动与当前绑定")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    assets = load_assets(Path(args.agents_dir))
    email, password = _credentials(args.credentials)
    try:
        admin = AdminClient.signin(args.admin_url, email, password)
        report = sync(admin, assets, args.mcp_url, Path(args.agents_dir), dry_run=args.dry_run)
    except PlantkgError as exc:
        print(f"导入失败：{exc.message}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["problems"] else 1


def _credentials(path: str | None) -> tuple[str, str]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data["email"], data["password"]
    try:
        return os.environ["NEXENT_ADMIN_EMAIL"], os.environ["NEXENT_ADMIN_PASSWORD"]
    except KeyError:
        raise SystemExit("缺少管理端凭据：设置 NEXENT_ADMIN_EMAIL / NEXENT_ADMIN_PASSWORD，或用 --credentials") from None


def sync(admin: AdminClient, assets: dict[str, AgentAsset], mcp_url: str, out_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    problems: list[str] = []
    servers = {s.get("service_name") or s.get("remote_mcp_server_name"): s for s in admin.mcp_servers()}
    if MCP_NAME not in servers and not dry_run:
        admin.add_mcp(MCP_NAME, mcp_url)
    if not dry_run:
        admin.scan_tools()
    tools = _tool_ids(admin.tools())
    models = {m.get("model_name") or m.get("display_name"): m.get("model_id") for m in admin.models()}
    existing = {a["name"]: a for a in admin.agents()}
    skill_ids = _import_skills(admin, assets, out_dir.parent / "skills", problems, dry_run=dry_run)

    published: dict[str, dict[str, Any]] = {}
    order = sorted(assets.values(), key=lambda a: a.role != "expert")  # 专家先发布，主 Agent 才能钉住子 Agent 版本
    for asset in order:
        missing = [t for t in _wanted_tools(asset) if t not in tools]
        if missing:
            problems.append(f"{asset.name} 需要的工具在 Nexent 里不存在：{missing}（plantkg 服务没在跑或没扫描？）")
            continue
        if asset.model not in models:
            problems.append(f"{asset.name} 的模型 {asset.model} 没在 Nexent 登记")
            continue
        if dry_run:
            published[asset.name] = {"action": "update" if asset.name in existing else "create"}
            continue
        related = [{"agent_id": published[s]["agent_id"], "version_no": published[s]["version_no"]} for s in asset.sub_agents if s in published]
        body = {"agent_id": existing.get(asset.name, {}).get("agent_id"), "name": asset.name, "display_name": asset.display_name,
                "description": asset.description, "prompt": asset.prompt, "max_steps": asset.max_steps, "enabled": True,
                "provide_run_summary": False, "allow_chat_metadata": True, "model_ids": [models[asset.model]],
                "business_logic_model_id": models[asset.model], "enabled_tool_ids": sorted(tools[t] for t in _wanted_tools(asset)),
                "related_agents": related}
        admin.upsert_agent(body)
        agent = next(a for a in admin.agents() if a["name"] == asset.name)
        for slot in _ready_skills(asset):  # 发布前绑到草稿，发布时一并进版本
            if slot.name in skill_ids:
                admin.bind_skill(agent["agent_id"], skill_ids[slot.name])
        admin.publish(agent["agent_id"], f"{asset.name}-{'placeholder' if asset.placeholder else 'v'}")
        agent = next(a for a in admin.agents() if a["name"] == asset.name)
        published[asset.name] = {"agent_id": agent["agent_id"], "version_no": agent.get("current_version_no")}

    bindings: dict[str, Any] = {}
    for name, info in published.items():
        if dry_run:
            continue
        detail = admin.agent_info(info["agent_id"])
        bound = sorted(t.get("name") for t in detail.get("tools") or [])
        subs = detail.get("sub_agent_id_list") or [r.get("agent_id") for r in detail.get("related_agents") or []]
        bound_skills = sorted(i.get("skill_name") for i in admin.skill_instances(info["agent_id"], info["version_no"] or 0)
                              if i.get("enabled", True))
        bindings[name] = {**info, "tools": bound, "sub_agent_ids": subs, "model_ids": detail.get("model_ids"), "skills": bound_skills}
        want_skills = sorted(s.name for s in _ready_skills(assets[name]))
        if bound_skills != want_skills:
            problems.append(f"{name} 实际绑定 Skill {bound_skills} 与配置 {want_skills} 不一致")
        want = sorted(_wanted_tools(assets[name]))
        if bound != want:
            problems.append(f"{name} 实际绑定 {bound} 与配置 {want} 不一致")
        want_subs = sorted(published[s]["agent_id"] for s in assets[name].sub_agents if s in published)
        if sorted(subs) != want_subs:
            problems.append(f"{name} 子 Agent {sorted(subs)} 与配置 {want_subs} 不一致")
        export, skill_zips = admin.export(info["agent_id"])
        (out_dir / "exports").mkdir(parents=True, exist_ok=True)
        (out_dir / "exports" / f"{name}.export.json").write_text(json.dumps(_scrub(export), ensure_ascii=False, indent=2), encoding="utf-8")
        for skill, archive in skill_zips.items():  # 带 Skill 时导出为 ZIP：Skill 包另存，便于对照导入的内容
            (out_dir / "exports" / f"{name}.skills").mkdir(exist_ok=True)
            (out_dir / "exports" / f"{name}.skills" / f"{Path(skill).name}.zip").write_bytes(archive)
    placeholders = sorted(f"{a.name}:{s.name}（{s.owner}）" for a in assets.values() for s in a.skills if s.placeholder)
    return {"agents": published, "bindings": bindings, "placeholder_skills": placeholders, "problems": problems}


def _ready_skills(asset: AgentAsset) -> list[SkillSlot]:
    return [s for s in asset.skills if not s.placeholder]


def _import_skills(admin: AdminClient, assets: dict[str, AgentAsset], skills_dir: Path, problems: list[str], *,
                   dry_run: bool) -> dict[str, int]:
    """非占位 Skill：nexent_assets/skills/<名字>/ 打包上传（已存在按名字替换），返回 名字 → skill_id。占位的只登记不导入。"""
    present = {s.get("name"): s for s in admin.skills()}
    uploaded = []
    for name in sorted({s.name for a in assets.values() for s in _ready_skills(a)}):
        folder = skills_dir / name
        if not (folder / "SKILL.md").is_file():
            problems.append(f"Skill {name} 标为已就绪，但 {folder / 'SKILL.md'} 不存在")
        elif not dry_run:
            admin.upload_skill(name, _zip(folder, name), replace=name in present)
            uploaded.append(name)
    if not uploaded:
        return {}
    present = {s.get("name"): s for s in admin.skills()}
    problems.extend(f"Skill {name} 上传后在 Nexent 里查不到" for name in uploaded if name not in present)
    return {name: present[name]["skill_id"] for name in uploaded if name in present}


def _zip(folder: Path, name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, f"{name}/{path.relative_to(folder).as_posix()}")
    return buffer.getvalue()


def _wanted_tools(asset: AgentAsset) -> list[str]:
    return [TOOL_PREFIX + t for t in asset.tools] + ([KB_TOOL] if asset.knowledge is not None else [])


def _tool_ids(items: list[dict[str, Any]]) -> dict[str, int]:
    """plantkg MCP 的工具按名字取；内置知识库检索取本地工具。同名工具会让绑定出错，所以只认这两类来源。"""
    found: dict[str, int] = {}
    for t in items:
        if (t.get("source") == "mcp" and t.get("usage") == MCP_NAME) or (t.get("name") == KB_TOOL and t.get("source") == "local"):
            if t.get("is_available", True):
                found[t["name"]] = t["tool_id"]
    return found


def _scrub(value: Any) -> Any:
    """导出文件进仓库：去掉租户、作者等账号信息。"""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in ("tenant_id", "author", "created_by", "updated_by", "user_id")}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
