"""模块边界检查（docs/architecture/ownership.md「导入规则」）。

    python scripts/check_boundaries.py [--root 仓库根]

规则：core 只依赖标准库、pydantic、yaml 与 core 自身；平面与包只依赖 core 与自身；薄入口不直连平面；
前端不得出现模型/平台密钥或直连模型、Nexent 的地址。违规时打印并以退出码 1 结束。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

CORE_THIRD_PARTY = {"pydantic", "yaml"}
PLANES = ("knowledge", "decision", "world", "evolution", "integrations", "backends", "adapters", "storage", "application")
WEB_FORBIDDEN = re.compile(r"dashscope|api\.deepseek|NEXENT_API_KEY|DASHSCOPE_API_KEY|/nb/v1/|VITE_[A-Z_]*KEY", re.I)


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path, root: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_name(path, root).rsplit(".", 0 if path.name == "__init__.py" else 1)[0]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.append((node.lineno, module))
    return found


def _allowed(owner: str, module: str) -> str | None:
    """返回违规说明；合法返回 None。owner 为文件所属模块路径，如 plantkg.packs.pv.pack。"""
    top = module.split(".")[0]
    parts = owner.split(".")
    if parts[:2] == ["plantkg", "core"]:
        if top in sys.stdlib_module_names or top in CORE_THIRD_PARTY or top == "__future__" or module.startswith("plantkg.core"):
            return None
        return "core 只能依赖标准库、pydantic、yaml 与 plantkg.core"
    if not module.startswith("plantkg"):
        return None  # 平面与包可用第三方库（pyomo 等）
    if parts[-1] == "__main__" and parts[1] != "core":
        return None  # 模块的命令行入口（python -m plantkg.<平面>.<模块>）与 bootstrap 同属装配层
    if module.startswith("plantkg.core") or module == "plantkg":
        return None
    if len(parts) >= 3 and parts[1] == "packs":
        own = ".".join(parts[:3])
        if module == "plantkg.packs._validation":
            return None  # 领域包共享的数值校验 helper（#162 定案）
        return None if module.startswith(own) else f"领域包只能依赖 plantkg.core 与 {own}"
    if len(parts) >= 2 and parts[1] in PLANES:
        own = ".".join(parts[:2])
        return None if module.startswith(own) else f"{parts[1]} 只能依赖 plantkg.core 与自身，跨平面只交契约记录"
    if owner in ("plantkg.api", "plantkg.mcp_server"):
        return f"{owner} 是薄入口，只能依赖 plantkg.core（装配在 bootstrap）"
    return None  # bootstrap、__main__、testing 是装配层


def check(root: Path) -> list[str]:
    violations = []
    package_root = root / "plantkg"
    for path in sorted(package_root.rglob("*.py")):
        owner = _module_name(path, root)
        for lineno, module in _imports(path, root):
            reason = _allowed(owner, module)
            if reason:
                violations.append(f"{path.relative_to(root).as_posix()}:{lineno} 导入 {module}：{reason}")
    web = root / "web"
    if web.exists():
        for path in sorted(p for p in web.rglob("*") if p.is_file() and "node_modules" not in p.parts and p.suffix in {".ts", ".tsx", ".js", ".jsx", ".vue", ".env"}):
            for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if WEB_FORBIDDEN.search(line):
                    violations.append(f"{path.relative_to(root).as_posix()}:{lineno} 前端不得直连模型/Nexent 或引用密钥")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    violations = check(args.root.resolve())
    for v in violations:
        print(v)
    print(f"边界检查：{'通过' if not violations else f'{len(violations)} 处违规'}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
