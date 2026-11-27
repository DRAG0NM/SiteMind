"""图片 → 结构化描述：deepseek-flash 识读铭牌与图表，写成可检索的 Markdown。

真实调用走百炼 compatible-mode 的 `/chat/completions`（与 `nexent_assets/probes/b_bailian/p9_vision.py`
同一条路径）：模型 `deepseek-v4.1-flash-1017`，非思考模式，图片以 data URI 传入。密钥只从环境变量读，
不写文件、不打印。默认 pytest 不联网，测试注入 transport 替身；仓库里的描述文件是真实调用后的产物。

描述文件 = YAML front matter（字段、模型、生成时间与来源）+ 中文正文，既能被本地检索演示读取，
也能当作纯文本上传给 Nexent 知识库，使图片内容可被文本检索到。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from plantkg.core.contracts import ErrorCode, PlantkgError

VISION_MODEL = "deepseek-v4.1-flash-1017"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

NAMEPLATE_KEYS = ("kw", "hp", "rpm", "hz", "voltage_v", "current_a", "ip", "ins_class", "frame", "connection", "serial_no")
CHART_KEYS = ("peak_hour", "peak_kw", "kw_at_9", "kw_at_15")
SCHEMAS: dict[str, tuple[str, ...]] = {"nameplate": NAMEPLATE_KEYS, "chart": CHART_KEYS}
LABELS = {
    "kw": "额定功率 kW", "hp": "马力 hp", "rpm": "转速 r/min", "hz": "频率 Hz", "voltage_v": "电压 V",
    "current_a": "电流 A", "ip": "防护等级", "ins_class": "绝缘等级", "frame": "机座号", "connection": "接法",
    "serial_no": "出厂编号", "peak_hour": "出力最大整点", "peak_kw": "峰值出力 kW", "kw_at_9": "9 点出力 kW",
    "kw_at_15": "15 点出力 kW",
}
PROMPTS = {
    "nameplate": "读这张设备铭牌，只输出一个 JSON，键为 kw, hp, rpm, hz, voltage_v, current_a, ip, ins_class, frame, "
                 "connection, serial_no，值按铭牌原样写成字符串；看不清的写 null。不要解释。",
    "chart": "读这张折线图，只输出一个 JSON：peak_hour（出力最大的整点小时，整数）、peak_kw（该点数值）、"
             "kw_at_9（9 点的数值）、kw_at_15（15 点的数值）。数值按图上标注读取。不要解释。",
}


class VisionTransport(Protocol):
    def __call__(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...


class BailianVision:
    """百炼 OpenAI 兼容接口；错误只报状态码，不回显响应体（可能含账号信息）。"""

    def __init__(self, *, model: str = VISION_MODEL, base_url: str = DASHSCOPE_BASE,
                 api_key_env: str = "DASHSCOPE_API_KEY", timeout_s: float = 180) -> None:
        self.model, self.base_url, self.api_key_env, self.timeout_s = model, base_url.rstrip("/"), api_key_env, timeout_s

    def __call__(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"缺少环境变量 {self.api_key_env}，无法识读图片")
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"视觉模型返回 HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "视觉模型请求失败") from None


@dataclass(frozen=True)
class ImageDescription:
    document_id: str
    image_path: str
    kind: str
    model: str
    generated_at: datetime
    fields: dict[str, Any]
    text: str
    elapsed_s: float = 0.0
    nature: str = "derived"


def describe_image(image: Path, *, document_id: str, kind: str, transport: VisionTransport | None = None,
                   model: str = VISION_MODEL, generated_at: datetime | None = None) -> ImageDescription:
    """识读一张图。transport 缺省用百炼；测试传替身即可离线验证整条管线。"""
    if kind not in SCHEMAS:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, f"未知图片类型 {kind}，只支持 {sorted(SCHEMAS)}")
    image = Path(image)
    try:
        content = image.read_bytes()
    except FileNotFoundError:
        raise PlantkgError(ErrorCode.NOT_FOUND, f"找不到图片 {image}") from None
    mime = mimetypes.guess_type(image.name)[0] or "image/png"
    uri = f"data:{mime};base64," + base64.b64encode(content).decode()
    body = {"model": model, "temperature": 0, "max_tokens": 400, "enable_thinking": False,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": PROMPTS[kind]}]}]}
    started = time.monotonic()
    response = (transport or BailianVision(model=model))(body)
    elapsed = time.monotonic() - started
    raw = _content(response)
    fields = _parse_fields(raw, SCHEMAS[kind])
    return ImageDescription(document_id=document_id, image_path=Path(image).as_posix(), kind=kind, model=model,
                            generated_at=generated_at or datetime.now(timezone.utc), fields=fields,
                            text=render_text(kind, fields), elapsed_s=round(elapsed, 2))


def render_text(kind: str, fields: Mapping[str, Any]) -> str:
    """把字段写成一句可引用的中文；空值不编造。"""
    parts = [f"{LABELS.get(key, key)} {value}" for key, value in fields.items() if value not in (None, "")]
    subject = "设备铭牌" if kind == "nameplate" else "出力曲线"
    return f"{subject}识读结果：" + "，".join(parts) + "。" if parts else f"{subject}没有识读出可用字段。"


def write_description(description: ImageDescription, path: Path) -> Path:
    """写 Markdown：front matter 存字段，正文给一段中文描述与字段表。重复写入结果相同。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    front = {"document_id": description.document_id, "image": description.image_path, "kind": description.kind,
             "model": description.model, "generated_at": description.generated_at.astimezone(timezone.utc).isoformat(),
             "nature": "derived", "fields": description.fields}
    table = "\n".join(f"| {LABELS.get(key, key)} | {value if value is not None else '未读出'} |"
                      for key, value in description.fields.items())
    body = (f"# {'设备铭牌' if description.kind == 'nameplate' else '出力曲线'}结构化描述（deepseek-flash）\n\n"
            f"{description.text}\n\n| 字段 | 识读值 |\n| --- | --- |\n{table}\n")
    path.write_text("---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False) + "---\n\n" + body, encoding="utf-8")
    return path


def load_description(path: Path) -> dict[str, Any]:
    """读回描述文件的 front matter 与正文，用于入库、检索与演示输出。"""
    text = Path(path).read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n\n?(.*)$", text, re.S)
    if not match:
        raise PlantkgError(ErrorCode.PRECONDITION, f"图片描述 {path} 缺少 front matter")
    front = yaml.safe_load(match.group(1)) or {}
    return {**front, "body": match.group(2).strip()}


def _content(response: Mapping[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "视觉模型返回里没有 message.content") from None
    return content if isinstance(content, str) else ""


def _parse_fields(raw: str, keys: tuple[str, ...]) -> dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "视觉模型没有返回 JSON")
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "视觉模型的 JSON 解析失败") from None
    if not isinstance(parsed, dict):
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "视觉模型返回的 JSON 不是对象")
    return {key: parsed.get(key) for key in keys}

