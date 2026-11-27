"""B23 导入命令：非占位 Skill 要实际上传、绑到 Agent 并读回核对（假管理端）。"""

from __future__ import annotations

import io
import json
import zipfile

import httpx

from plantkg.integrations.nexent import ROOT, import_assets
from plantkg.integrations.nexent import client as client_module
from plantkg.integrations.nexent.assets import KB_TOOL, load_assets
from plantkg.integrations.nexent.client import AdminClient


class FakeAdmin:
    def __init__(self, assets):
        names = sorted({t for a in assets.values() for t in import_assets._wanted_tools(a)})
        self._tools = [{"name": n, "tool_id": i, "source": "local" if n == KB_TOOL else "mcp", "usage": "plantkg"} for i, n in enumerate(names)]
        self._agents, self._skills, self.bound, self.uploads = {}, {}, {}, []

    def mcp_servers(self):
        return [{"service_name": "plantkg"}]

    def scan_tools(self):
        pass

    def tools(self):
        return self._tools

    def models(self):
        return [{"model_name": "deepseek-v4.1-flash-1017", "model_id": 1}]

    def agents(self):
        return list(self._agents.values())

    def skills(self):
        return list(self._skills.values())

    def upload_skill(self, name, archive, *, replace):
        self.uploads.append((name, zipfile.ZipFile(io.BytesIO(archive)).namelist(), replace))
        self._skills[name] = {"name": name, "skill_id": 100 + len(self._skills)}

    def upsert_agent(self, body):
        agent_id = body["agent_id"] or len(self._agents) + 1
        self._agents[body["name"]] = {"name": body["name"], "agent_id": agent_id, "current_version_no": 0, "body": body}

    def bind_skill(self, agent_id, skill_id):
        self.bound.setdefault(agent_id, []).append(skill_id)

    def publish(self, agent_id, version_name):
        next(a for a in self._agents.values() if a["agent_id"] == agent_id)["current_version_no"] += 1

    def agent_info(self, agent_id):
        body = next(a for a in self._agents.values() if a["agent_id"] == agent_id)["body"]
        return {"tools": [{"name": t["name"]} for t in self._tools if t["tool_id"] in body["enabled_tool_ids"]],
                "sub_agent_id_list": [r["agent_id"] for r in body["related_agents"]]}

    def skill_instances(self, agent_id, version_no):
        names = {s["skill_id"]: s["name"] for s in self._skills.values()}
        return [{"skill_name": names[i]} for i in self.bound.get(agent_id, [])]

    def export(self, agent_id):  # 与真实接口同形：带 Skill 时另给 Skill 包
        names = {s["skill_id"]: s["name"] for s in self._skills.values()}
        return {"agent_id": agent_id, "tenant_id": "t-secret"}, {names[i]: b"PK-zip" for i in self.bound.get(agent_id, [])}


def _assets_with_ready_triage():
    assets = load_assets(ROOT / "nexent_assets" / "agents")
    # 只把 triage 标成就绪：其余专家（如 B19 能源）的 Skill 在各自分支交付后也是就绪的，
    # 本用例只准备 triage 的 Skill 包，所以把其余 Skill 复位为占位，避免依赖其他分支的文件。
    return {name: asset.model_copy(update={"skills": tuple(
        s.model_copy(update={"placeholder": s.name != "triage"}) for s in asset.skills)})
        for name, asset in assets.items()}


def test_ready_skill_is_uploaded_bound_and_verified(tmp_path):
    (tmp_path / "skills" / "triage").mkdir(parents=True)
    (tmp_path / "skills" / "triage" / "SKILL.md").write_text("---\nname: triage\n---\n分诊", encoding="utf-8")
    assets = _assets_with_ready_triage()
    admin = FakeAdmin(assets)
    report = import_assets.sync(admin, assets, "http://x/mcp", tmp_path / "agents")
    assert report["problems"] == []
    assert admin.uploads == [("triage", ["triage/SKILL.md"], False)]
    assert report["bindings"]["plantkg_main"]["skills"] == ["triage"]
    assert "plantkg_main:triage（B27）" not in report["placeholder_skills"]
    exports = tmp_path / "agents" / "exports"
    assert (exports / "plantkg_main.skills" / "triage.zip").read_bytes() == b"PK-zip"
    assert "t-secret" not in (exports / "plantkg_main.export.json").read_text(encoding="utf-8")


def _admin_with_response(monkeypatch, response):
    real = httpx.Client
    monkeypatch.setattr(client_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(lambda req: response), **kw))
    return AdminClient("http://nexent", "jwt")


def test_admin_export_reads_zip_with_skills(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("agent.json", json.dumps({"agent_info": {"16": {"skill_names": ["triage"]}}}))
        archive.writestr("skills/triage.zip", b"PK-inner")
    admin = _admin_with_response(monkeypatch, httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "application/zip"}))
    agent, skills = admin.export(16)
    assert agent["agent_info"]["16"]["skill_names"] == ["triage"] and skills == {"triage": b"PK-inner"}


def test_admin_export_without_skills_stays_json(monkeypatch):
    admin = _admin_with_response(monkeypatch, httpx.Response(200, json={"data": {"agent_info": {}}}))
    assert admin.export(16) == ({"agent_info": {}}, {})


def test_ready_skill_without_files_is_reported(tmp_path):
    assets = _assets_with_ready_triage()
    report = import_assets.sync(FakeAdmin(assets), assets, "http://x/mcp", tmp_path / "agents")
    assert any("triage" in p and "SKILL.md" in p for p in report["problems"])
