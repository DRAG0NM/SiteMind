"""注册表：端口实现、MCP 工具、REST 提供者、确认动作执行函数与领域包清单。

后续模块只往注册表登记（经 pack.yaml 或装配清单），不改 bootstrap、api.py、mcp_server.py。
重复登记、未在契约里声明的工具或路由、未知 schema 版本，都在启动时报错。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field

from plantkg.core.contracts import API_ROUTES, TOOL_SPECS, ErrorCode, PlantkgError, ToolSpec

T = TypeVar("T")
PACK_SCHEMA_VERSIONS = frozenset({1})
# v0.8 运行模式分派（B46 补丁，03 §3.0）：登记模式比运行模式多一个 deterministic
REGISTRY_MODES = frozenset({"live", "trial", "evaluation", "deterministic"})
_FALLBACK_TO_DETERMINISTIC = frozenset({"trial", "evaluation"})


class RegistryError(Exception):
    """装配错误：重复、未声明或版本不符。启动即失败，不带到运行时。"""


class PackProvides(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    adapters: tuple[str, ...] = ()
    rule_kinds: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    ledger_terms: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


class PackManifest(BaseModel):
    """packs/<名称>/pack.yaml。entrypoint 为 "模块:函数"，函数签名 (registry, settings) -> None。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    schema_version: int
    status: str = "active"  # placeholder：骨架占位，不注册任何能力
    order: int = 1000  # packs: auto 时的加载顺序，小的先加载
    description: str = ""
    entrypoint: str | None = None
    provides: PackProvides = PackProvides()


@dataclass(frozen=True)
class ToolBinding:
    spec: ToolSpec
    handler: Callable[..., Any]


@dataclass
class Registry:
    ports: dict[type, Any] = field(default_factory=dict)
    tools: dict[str, ToolBinding] = field(default_factory=dict)
    rest: dict[tuple[str, str], Callable[..., Any]] = field(default_factory=dict)
    action_handlers: dict[str, Any] = field(default_factory=dict)
    packs: dict[str, PackManifest] = field(default_factory=dict)
    # v0.8：按（端口, 模式, 键）登记的实现与按名收集的回调；不影响上面的 register/resolve
    mode_ports: dict[tuple[type, str, str | None], Any] = field(default_factory=dict)
    hooks: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    live_fallback: bool = False  # dev/mock：live 也回退到 deterministic（允许只有确定性实现），由 bootstrap 按 profile 设
    # 由 Fake 占位的登记键（端口类、工具名、(方法, 路径)）。真实实现登记时直接替换，与先后顺序无关
    fallbacks: set[Any] = field(default_factory=set)
    # 由真实默认可替换实现占位的登记键：可被正式实现替换，但不算 Fake，不用于健康检查的 fake 标记
    defaults: set[Any] = field(default_factory=set)
    # 未实现、失败关闭的兜底占位登记键（如 B30 工具缺存储时）；不算测试替身，健康检查标 unavailable
    placeholders: set[Any] = field(default_factory=set)
    _loading_pack: PackManifest | None = field(default=None, repr=False)

    def _claim(self, table: dict, key: Any, label: str, fallback: bool, default: bool = False,
               placeholder: bool = False) -> bool:
        """装配规则：真实实现优先于 Fake、默认实现与占位。返回 False 表示本次登记应跳过。"""
        if key not in table:
            if fallback:
                self.fallbacks.add(key)
            elif placeholder:
                self.placeholders.add(key)
            elif default:
                self.defaults.add(key)
            return True
        if fallback or placeholder:
            return False
        if key in self.fallbacks:
            self.fallbacks.discard(key)
            self.defaults.discard(key)
            self.placeholders.discard(key)
            return True
        if key in self.placeholders:
            self.placeholders.discard(key)
            return True
        if key in self.defaults:
            if default:
                raise RegistryError(f"{label} 重复登记（两个默认实现）")
            self.defaults.discard(key)
            return True
        if default:
            return False
        raise RegistryError(f"{label} 重复登记")

    # ---- 端口
    def register(self, port: type[T], impl: T, *, fallback: bool = False, default: bool = False,
                 placeholder: bool = False) -> None:
        if getattr(port, "_is_runtime_protocol", False) and not isinstance(impl, port):
            raise RegistryError(f"{type(impl).__name__} 没有实现端口 {port.__name__}")
        if self._claim(self.ports, port, f"端口 {port.__name__}", fallback, default, placeholder):
            self.ports[port] = impl

    def resolve(self, port: type[T]) -> T:
        try:
            return self.ports[port]
        except KeyError:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"端口 {port.__name__} 尚未接入") from None

    # ---- 运行模式分派（B46）
    def register_mode(self, port: type[T], mode: str, impl: T, key: str | None = None) -> None:
        """按（端口, 模式, 键）登记；mode 取 live / trial / evaluation / deterministic，重复登记报错。"""
        if mode not in REGISTRY_MODES:
            raise RegistryError(f"未知登记模式 {mode}，只能是 {sorted(REGISTRY_MODES)}")
        if getattr(port, "_is_runtime_protocol", False) and not isinstance(impl, port):
            raise RegistryError(f"{type(impl).__name__} 没有实现端口 {port.__name__}")
        slot = (port, mode, key)
        if slot in self.mode_ports:
            raise RegistryError(f"端口 {port.__name__} 的 {mode} 模式（键 {key}）重复登记")
        self.mode_ports[slot] = impl

    def resolve_mode(self, port: type[T], run_mode: str, key: str | None = None) -> T:
        """先找本模式；trial/evaluation 找不到时回退到 deterministic。不全局切换，同一进程各取各的。"""
        fallback = run_mode in _FALLBACK_TO_DETERMINISTIC or (run_mode == "live" and self.live_fallback)
        modes = (run_mode, "deterministic") if fallback else (run_mode,)
        for mode in modes:
            if (port, mode, key) in self.mode_ports:
                return self.mode_ports[(port, mode, key)]
        raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"端口 {port.__name__} 在 {run_mode} 模式下（键 {key}）尚未接入")

    def register_hook(self, name: str, fn: Callable[..., Any]) -> None:
        """按登记顺序收集多个回调：每步钩子、提案就绪两阶段钩子、指令钩子、ForkHook 等。"""
        self.hooks.setdefault(name, []).append(fn)

    def hooks_for(self, name: str) -> tuple[Callable[..., Any], ...]:
        return tuple(self.hooks.get(name, ()))

    def check_modes(self, profile: str, requirements: Mapping[type, Iterable[str | None]]) -> None:
        """启动自检。production/prod/nexent：live 模式必须有实现；evaluation：缓存或确定性实现之一；
        dev/mock 不检查（允许只有确定性实现）。requirements 为 端口 → 必需的键。"""
        mode = {"production": "live", "prod": "live", "nexent": "live", "evaluation": "evaluation"}.get(profile)
        if mode is None:
            return
        missing = []
        for port, keys in requirements.items():
            for key in keys:
                try:
                    self.resolve_mode(port, mode, key)
                except PlantkgError:
                    missing.append(f"{port.__name__}[{key}]")
        if missing:
            raise RegistryError(f"{profile} profile 的 {mode} 模式缺少实现：{missing}")

    # ---- MCP 工具
    def register_tool(self, name: str, handler: Callable[..., Any], spec: ToolSpec | None = None, *, fallback: bool = False,
                      default: bool = False, placeholder: bool = False) -> None:
        contract = _CONTRACT_TOOLS.get(name)
        if contract is not None:
            if spec is not None and spec != contract:
                raise RegistryError(f"工具 {name} 的规格必须与契约一致，不能改模式或参数")
            spec = contract
        elif spec is None:
            raise RegistryError(f"工具 {name} 未在契约 TOOL_SPECS 中声明；扩展包须随 pack.yaml 提供 ToolSpec")
        elif self._loading_pack is None or name not in self._loading_pack.provides.tools:
            raise RegistryError(f"扩展工具 {name} 只能由在 pack.yaml provides.tools 中声明它的包登记")
        if spec.name != name:
            raise RegistryError(f"登记名 {name} 与规格名 {spec.name} 不一致")
        if self._claim(self.tools, name, f"工具 {name}", fallback, default, placeholder):
            self.tools[name] = ToolBinding(spec, handler)

    # ---- REST
    def register_rest(self, method: str, path: str, provider: Callable[..., Any], *, fallback: bool = False,
                      default: bool = False, placeholder: bool = False) -> None:
        key = (method.upper(), path)
        if key not in _CONTRACT_ROUTES:
            raise RegistryError(f"{method} {path} 不在契约 API_ROUTES 中")
        if self._claim(self.rest, key, f"{method} {path}", fallback, default, placeholder):
            self.rest[key] = provider

    def fake_bindings(self) -> list[str]:
        """仍由测试替身登记的项（供健康检查与 --dry-run 显示）。默认实现与未实现占位不在此列。"""
        return sorted(self._binding_name(key) for key in self.fallbacks)

    def placeholder_bindings(self) -> list[str]:
        """未实现、失败关闭的兜底占位（健康检查标 unavailable，不算 Fake）。"""
        return sorted(self._binding_name(key) for key in self.placeholders)

    @staticmethod
    def _binding_name(key: Any) -> str:
        return key.__name__ if isinstance(key, type) else " ".join(key) if isinstance(key, tuple) else key

    # ---- 统一确认端点的执行函数（B15/B18/B24）
    def register_action_handler(self, kind: str, handler: Any, *, fallback: bool = False, default: bool = False,
                                placeholder: bool = False) -> None:
        key = ("action_handler", kind)
        if kind in self.action_handlers:
            if fallback or placeholder:
                return
            if key in self.fallbacks or key in self.defaults or key in self.placeholders:
                self.fallbacks.discard(key)
                self.defaults.discard(key)
                self.placeholders.discard(key)
            elif default:
                return
            else:
                raise RegistryError(f"确认动作 {kind} 的执行函数重复登记")
        elif fallback:
            self.fallbacks.add(key)
        elif placeholder:
            self.placeholders.add(key)
        elif default:
            self.defaults.add(key)
        self.action_handlers[kind] = handler

    # ---- 领域包
    def load(self, pack_dirs: Iterable[Path], enabled: Iterable[str], settings: Any = None) -> list[PackManifest]:
        """扫描 pack.yaml，校验后按 enabled 顺序调用入口。返回已加载清单。"""
        found: dict[str, tuple[PackManifest, Path]] = {}
        for pack_dir in pack_dirs:
            manifest_path = pack_dir / "pack.yaml"
            if not manifest_path.exists():
                continue
            manifest = PackManifest.model_validate(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))
            if manifest.schema_version not in PACK_SCHEMA_VERSIONS:
                raise RegistryError(f"{manifest.name} 的 schema_version={manifest.schema_version} 不受支持")
            if manifest.status != "placeholder" and not manifest.entrypoint:
                raise RegistryError(f"{manifest.name} 不是 placeholder 却没有 entrypoint；未实现的包请保持 status: placeholder")
            if manifest.name in found:
                raise RegistryError(f"领域包 {manifest.name} 重复（{found[manifest.name][1]} 与 {pack_dir}）")
            found[manifest.name] = (manifest, pack_dir)
        _check_unique_ids(m for m, _ in found.values())
        loaded = []
        for name in enabled:
            if name not in found:
                raise RegistryError(f"配置启用了领域包 {name}，但找不到 packs/{name}/pack.yaml")
            manifest = found[name][0]
            if manifest.entrypoint and manifest.status != "placeholder":
                module_name, _, func = manifest.entrypoint.partition(":")
                before = set(self.tools)
                self._loading_pack = manifest
                try:
                    getattr(importlib.import_module(module_name), func)(self, settings)
                finally:
                    self._loading_pack = None
                undeclared = set(self.tools) - before - set(manifest.provides.tools)
                if undeclared:
                    raise RegistryError(f"领域包 {name} 登记了未在 provides.tools 声明的工具：{sorted(undeclared)}")
            self.packs[name] = manifest
            loaded.append(manifest)
        return loaded

    # ---- 钩子解绑
    def unregister_hook(self, name: str, fn: Callable[..., Any]) -> None:
        """移除 register_hook 登记的回调：没登记过就忽略，移除后列表为空则清掉键。"""
        hooks = self.hooks.get(name)
        if hooks is not None and fn in hooks:
            hooks.remove(fn)
            if not hooks:
                del self.hooks[name]


def _check_unique_ids(manifests: Iterable[PackManifest]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for m in manifests:
        for kind, ids in m.provides.model_dump().items():
            for item in ids:
                if (kind, item) in seen:
                    raise RegistryError(f"{kind} {item} 同时由 {seen[(kind, item)]} 与 {m.name} 提供")
                seen[(kind, item)] = m.name


_CONTRACT_TOOLS = {spec.name: spec for spec in TOOL_SPECS}
_CONTRACT_ROUTES = {(r.method, r.path) for r in API_ROUTES}
