"""Nexent HTTP 客户端：北向（运行、中止、会话历史，北向 Key）与管理端（建 Agent、发布、导出，管理员 JWT）。

北向 Key 只在网关进程的环境变量里；请求体（含注入的 ctx）不写日志。
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from typing import Any

import httpx

from plantkg.core.contracts import ErrorCode, NexentRunPayload, PlantkgError
from plantkg.integrations.nexent.sse import parse_lines


class NorthboundClient:
    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 300) -> None:
        self._base = base_url.rstrip("/") + "/nb/v1"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout_s

    def run(self, payload: NexentRunPayload, request_id: str) -> Iterator[dict[str, Any]]:
        """发起一次北向运行并逐条给出 SSE 事件；连接断开时迭代结束（由调用方判定终态）。"""
        body = payload.model_dump(mode="json", exclude_none=True)
        if body.get("conversation_id") is not None:
            body["conversation_id"] = int(body["conversation_id"])
        headers = {**self._headers, "Accept": "text/event-stream", "X-Request-Id": request_id}
        timeout = httpx.Timeout(self._timeout, connect=10, read=self._timeout)
        with httpx.Client(timeout=timeout) as client, client.stream("POST", f"{self._base}/chat/run", json=body, headers=headers) as resp:
            if resp.status_code != 200:
                resp.read()
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"Nexent 北向返回 {resp.status_code}：{resp.text[:300]}")
            conversation = resp.headers.get("conversation_id")
            if conversation:
                yield {"type": "conversation_created", "content": {"conversation_id": conversation}}
            yield from parse_lines(resp.iter_lines())

    def stop(self, conversation_id: str) -> None:
        with httpx.Client(timeout=15) as client:
            client.get(f"{self._base}/chat/stop/{int(conversation_id)}", headers=self._headers)

    def last_answer(self, conversation_id: str) -> str | None:
        """断流后查会话历史补结局：取最后一条 assistant 文本。"""
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(f"{self._base}/conversations/{int(conversation_id)}", headers=self._headers)
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        history = (data.get("data") or {}).get("history") if isinstance(data, dict) else None
        for message in reversed(history or []):
            if message.get("role") == "assistant":
                text = message.get("message") or message.get("content")
                if isinstance(text, list):
                    text = "".join(str(part.get("content", "")) for part in text if isinstance(part, dict))
                return str(text) if text else None
        return None


class AdminClient:
    """管理端（:5010）接口，只给导入命令用。"""

    def __init__(self, base_url: str, jwt: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {jwt}"}

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        with httpx.Client(timeout=120) as client:
            resp = client.request(method, self._base + path, json=body, headers=self._headers)
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if resp.status_code >= 400:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"Nexent {method} {path} 返回 {resp.status_code}：{json.dumps(data, ensure_ascii=False)[:300]}")
        return data

    @staticmethod
    def signin(base_url: str, email: str, password: str) -> AdminClient:
        with httpx.Client(timeout=30) as client:
            data = client.post(base_url.rstrip("/") + "/user/signin", json={"email": email, "password": password}).json()
        token = ((data.get("data") or {}).get("session") or {}).get("access_token")
        if not token:
            raise PlantkgError(ErrorCode.FORBIDDEN, "Nexent 管理端登录失败")
        return AdminClient(base_url, token)

    def agents(self) -> list[dict[str, Any]]:
        data = self._call("GET", "/agent/list")
        return data if isinstance(data, list) else data.get("data", [])

    def agent_info(self, agent_id: int) -> dict[str, Any]:
        data = self._call("POST", "/agent/search_info", {"agent_id": agent_id})
        return data.get("data", data)

    def upsert_agent(self, body: dict[str, Any]) -> None:
        self._call("POST", "/agent/update", body)

    def publish(self, agent_id: int, version_name: str) -> None:
        self._call("POST", f"/agent/{agent_id}/publish", {"version_name": version_name, "release_note": "plantkg import_assets"})

    def export(self, agent_id: int) -> tuple[dict[str, Any], dict[str, bytes]]:
        """返回 (agent 导出 JSON, Skill 名 → zip 包)。带 Skill 的 Agent 导出为 ZIP：agent.json + skills/<名>.zip（P7 实测）。"""
        with httpx.Client(timeout=120) as client:
            resp = client.post(self._base + "/agent/export", json={"agent_id": agent_id}, headers=self._headers)
        if resp.status_code >= 400:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"Nexent POST /agent/export 返回 {resp.status_code}：{resp.text[:300]}")
        if "zip" in resp.headers.get("content-type", ""):
            with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
                names = archive.namelist()
                agent = json.loads(archive.read(next(n for n in names if n.endswith(".json"))))
                skills = {n.rsplit("/", 1)[-1].removesuffix(".zip"): archive.read(n) for n in names if n.endswith(".zip")}
            return agent, skills
        data = resp.json()
        data = data.get("data", data) if isinstance(data, dict) else data
        return (json.loads(data) if isinstance(data, str) else data), {}

    def tools(self) -> list[dict[str, Any]]:
        data = self._call("GET", "/tool/list")
        return data if isinstance(data, list) else data.get("data", [])

    def scan_tools(self) -> None:
        self._call("GET", "/tool/scan_tool")

    def mcp_servers(self) -> list[dict[str, Any]]:
        return self._call("GET", "/mcp/list").get("remote_mcp_server_list", [])

    def add_mcp(self, name: str, url: str) -> None:
        self._call("POST", "/mcp/add", {"name": name, "server_url": url, "description": "plantkg MCP", "enabled": True})

    def skills(self) -> list[dict[str, Any]]:
        return self._call("GET", "/skills").get("skills", [])

    def upload_skill(self, name: str, archive: bytes, *, replace: bool) -> None:
        """上传 Skill 包（zip，内含 <name>/SKILL.md）；已存在则按名字替换。"""
        method, path = ("PUT", f"/skills/{name}/upload") if replace else ("POST", "/skills/upload")
        with httpx.Client(timeout=120) as client:
            resp = client.request(method, self._base + path, headers=self._headers, files={"file": (f"{name}.zip", archive, "application/zip")},
                                  data=None if replace else {"skill_name": name, "source": "custom"})
        if resp.status_code >= 400:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"Nexent {method} {path} 返回 {resp.status_code}：{resp.text[:300]}")

    def bind_skill(self, agent_id: int, skill_id: int) -> None:
        """绑到 Agent 草稿（version_no=0），随后发布进版本。"""
        self._call("POST", "/skills/instance/update", {"agent_id": agent_id, "skill_id": skill_id, "enabled": True, "version_no": 0})

    def skill_instances(self, agent_id: int, version_no: int) -> list[dict[str, Any]]:
        with httpx.Client(timeout=60) as client:
            resp = client.get(self._base + "/skills/instance/list", params={"agent_id": agent_id, "version_no": version_no}, headers=self._headers)
        if resp.status_code >= 400:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"Nexent 读 Skill 绑定返回 {resp.status_code}：{resp.text[:300]}")
        return resp.json().get("instances", [])

    def models(self) -> list[dict[str, Any]]:
        data = self._call("GET", "/model/list")
        return data.get("data", data) if isinstance(data, dict) else data
