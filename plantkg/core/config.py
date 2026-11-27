"""分层配置与日志。

优先级：configs/defaults.yaml → configs/plant.yaml → configs/scenarios/<scenario>.yaml → 非敏感 CLI 覆盖。
密钥只从环境变量读取（配置里写变量名 *_env），模型填参与 CLI 都不能覆盖厂区、行业与安全上下文。
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from plantkg.core.contracts import SolverBudget, UtcDatetime, safe_error_details
from plantkg.core.registry import PackManifest

Profile = Literal["mock", "dev", "demo", "prod", "evaluation"]  # demo：本地演示装配（存储开、复核员非空、令牌后端注入）；evaluation：评测与回测，不装 Fake，Agent 结果取缓存或确定性实现（B46）
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"
PACKS_DIR = Path(__file__).resolve().parents[1] / "packs"
# 不允许 CLI 覆盖的键（前缀匹配）：安全上下文与密钥来源
PROTECTED_KEYS = ("profile", "plant_id", "industry_pack", "security", "nexent.api_key_env", "nexent.eval_api_key_env", "judge.jev")


class ConfigError(Exception):
    """配置缺失或不合法。消息只含键名与文件，不含任何密钥值。"""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8700, ge=1, le=65535)
    mcp_path: str = "/mcp"


class PlanningConfig(_Section):
    horizon_hours: int = Field(gt=0)
    step_minutes: int = Field(gt=0)
    solver: str
    solver_budget: SolverBudget
    background_budget: dict[str, float]
    export_limit_kw: float = Field(default=0.0, ge=0)  # 本期不外送：默认 0，外送动作在门控与执行处显式拒绝


class WorldConfig(_Section):
    state_provider: Literal["replay"]
    executor: Literal["record"]
    default_as_of: UtcDatetime


class NexentConfig(_Section):
    version: str
    enabled: bool = False
    base_url: str
    api_key_env: str
    eval_api_key_env: str
    tool_prefix: str = "plantkg_"
    max_steps: int = Field(default=12, gt=0)
    # B23 网关：北向地址、Agent 配置目录、每次运行截止与并发上限（03 §3.0 运行截止、专家并行）
    northbound_url: str = "http://127.0.0.1:5013"
    agents_dir: str = "nexent_assets/agents"
    main_agent: str = "plantkg_main"
    run_deadline_s: float = Field(default=120, gt=0)
    run_retries: int = Field(default=1, ge=0)
    max_parallel_runs: int = Field(default=4, gt=0)


class GatewayConfig(_Section):
    tool_deadline_s: float = Field(gt=0, lt=60)  # 平台 MCP 单次调用 60 秒硬上限
    job_wait_max_s: float = Field(gt=0, le=40)
    keepalive_s: float = Field(gt=0)
    session_ttl_s: int = Field(gt=0)


class DefaultCtxConfig(_Section):
    enabled: bool = False
    actor_id: str = "dev-reviewer"
    roles: tuple[str, ...] = ("viewer",)


class SecurityConfig(_Section):
    default_write: Literal["deny"] = "deny"
    trusted_context_required: bool = True
    ctx_required_for_write: bool = True
    default_ctx: DefaultCtxConfig = DefaultCtxConfig()


class JudgeConfig(_Section):
    backend: Literal["jev", "fallback", "fake"]
    jev: dict[str, Any] = Field(default_factory=dict)


class Settings(BaseModel):
    """生效配置。顶层允许其他模块的配置节（各模块自行校验），已知节强类型。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    profile: Profile
    plant_id: str = Field(min_length=1)
    industry_pack: str = Field(min_length=1)
    timezone: str
    packs: tuple[str, ...]
    server: ServerConfig = ServerConfig()
    planning: PlanningConfig
    world: WorldConfig
    judge: JudgeConfig
    nexent: NexentConfig
    gateway: GatewayConfig
    security: SecurityConfig = SecurityConfig()
    logging: dict[str, Any] = Field(default_factory=lambda: {"level": "INFO"})
    extensions: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()  # 实际读入的配置文件，便于排查

    @model_validator(mode="after")
    def _profile_rules(self) -> Settings:
        if self.profile == "prod" and self.security.default_ctx.enabled:
            raise ValueError("prod 不允许开启 security.default_ctx")
        if self.profile == "mock" and self.nexent.enabled:
            raise ValueError("mock profile 不连接 Nexent（nexent.enabled 必须为 false）")
        return self

    def section(self, name: str) -> Any:
        """读取其他模块的配置节（未建模的顶层键）。"""
        return getattr(self, name, None) if name in type(self).model_fields else (self.model_extra or {}).get(name)


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} 不是合法 YAML：{exc.__class__.__name__}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 顶层必须是映射")
    return data


def _apply_override(data: dict[str, Any], raw_key: str, value: Any) -> None:
    dotted = raw_key.strip()
    lowered = dotted.lower()
    for p in PROTECTED_KEYS:
        # 自身、子键、祖先键（整体替换上级节）都不行；大小写不敏感
        if lowered == p or lowered.startswith(p + ".") or p.startswith(lowered + "."):
            raise ConfigError(f"{dotted} 属于安全上下文或密钥来源，不能从命令行覆盖")
    if dotted.split(".")[0] not in data:
        raise ConfigError(f"{dotted} 不是已有配置节，命令行只能覆盖已有配置")
    node = data
    *parents, last = dotted.split(".")
    for part in parents:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"{dotted} 的上级不是映射")
    node[last] = yaml.safe_load(value) if isinstance(value, str) else value


def discover_packs(packs_dir: Path = PACKS_DIR) -> tuple[str, ...]:
    """packs: auto 的启用清单：扫描 pack.yaml，按 order、名称排序（钩子与仿真器的先后跟随这个顺序）。"""
    manifests = [PackManifest.model_validate(_read_yaml(path)) for path in packs_dir.glob("*/pack.yaml")]
    return tuple(m.name for m in sorted(manifests, key=lambda m: (m.order, m.name)))


def load_settings(
    profile: Profile | None = None,
    config_dir: Path | None = None,
    scenario: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    defaults = config_dir / "defaults.yaml"
    if not defaults.exists():
        raise ConfigError(f"缺少配置文件 {defaults.name}（目录 {config_dir.name}）")
    data = _read_yaml(defaults)
    sources = [defaults.name]
    for path in (config_dir / "plant.yaml", config_dir / "scenarios" / f"{scenario}.yaml" if scenario else None):
        if path is None:
            continue
        if not path.exists():
            raise ConfigError(f"缺少配置文件 {path.relative_to(config_dir)}")
        data = _deep_merge(data, _read_yaml(path))
        sources.append(str(path.relative_to(config_dir)))
    if profile == "demo":  # demo 启动配置：开存储、填复核员，令牌由后端运行时写入
        demo = config_dir / "demo.yaml"
        if not demo.exists():
            raise ConfigError(f"缺少配置文件 {demo.name}")
        data = _deep_merge(data, _read_yaml(demo))
        sources.append(demo.name)
    for key, value in (overrides or {}).items():
        _apply_override(data, key, value)
    if data.get("packs") == "auto":
        data["packs"] = discover_packs()
    if profile is not None:
        data["profile"] = profile  # 显式 profile 最后写入，覆盖不能改它
    data["sources"] = sources
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(d['loc'])}: {d['msg']}" for d in safe_error_details(exc))
        raise ConfigError(f"配置不合法：{details}") from None


def require_secret(env_name: str, environ: Mapping[str, str] | None = None) -> str:
    """读取密钥。缺失时报变量名，不打印任何值。"""
    value = (environ if environ is not None else os.environ).get(env_name, "")
    if not value:
        raise ConfigError(f"缺少环境变量 {env_name}（见 .env.example）")
    return value


def require_jev_workspace(env_name: str, environ: Mapping[str, str] | None = None) -> str:
    """工作空间是端点组成部分；校验并避免在错误里输出其值。"""
    value = (environ if environ is not None else os.environ).get(env_name, "")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]+", value):
        raise ConfigError(f"缺少或无效的工作空间环境变量 {env_name}")
    return value


def secret_env_names(settings: Settings) -> tuple[str, ...]:
    return (settings.nexent.api_key_env, settings.nexent.eval_api_key_env, str(settings.judge.jev.get("api_key_env", "DASHSCOPE_API_KEY")))


def secret_values(settings: Settings, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """进程里实际存在的密钥值，供出站检查与日志脱敏比对。"""
    env = environ if environ is not None else os.environ
    return tuple(v for name in secret_env_names(settings) if len(v := env.get(name, "")) >= 8)


def check_runtime_secrets(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """按已启用的集成检查密钥是否齐全；mock profile 不需要任何密钥。"""
    if settings.nexent.enabled:
        require_secret(settings.nexent.api_key_env, environ)
    if settings.judge.backend == "jev":
        require_secret(settings.judge.jev.get("api_key_env", "DASHSCOPE_API_KEY"), environ)
        require_jev_workspace(settings.judge.jev.get("workspace_env", "BAILIAN_WS"), environ)


# ---------------------------------------------------------------- 日志

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


class _RunContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.trace_id = trace_id_var.get()
        return True


def configure_logging(settings: Settings) -> None:
    """日志带 run_id/trace_id，并经 security.RedactingFilter 去掉 ctx 与密钥。"""
    from plantkg.core.security import RedactingFilter

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s run=%(run_id)s trace=%(trace_id)s %(name)s: %(message)s"))
    handler.addFilter(_RunContextFilter())
    handler.addFilter(RedactingFilter(secret_values(settings)))
    root = logging.getLogger("plantkg")
    root.handlers[:] = [handler]
    root.setLevel(str(settings.logging.get("level", "INFO")).upper())
    root.propagate = False
