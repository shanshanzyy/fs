"""lsp_diagnostics（E6）：让模型看一眼「这门语言自己怎么看这段代码」。

与 `grep` / `read_file` 的分工：那两者回答「代码长什么样」，
这里回答「**语言服务器报了什么问题**」—— 类型错误、未定义符号、未使用变量。
它能省掉一整轮「改 → 跑测试 → 报错 → 再改」的循环。

**只在 LSP 可用时注册**（见 `build_default_registry`）：机器上没装语言服务器时，
这个工具根本不出现在工具清单里 —— 否则弱模型会挑中一个必然失败的工具，
白烧一轮上下文。工具内部也不假设 LSP 一定在（用户中途卸载也一样降级）。

只读：不落盘、不改代码。位置口径与 `/api/lsp/diagnostics` 一致（行号 1 基）。
"""

from __future__ import annotations

from ..errors import ToolError
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec, wrap_untrusted

# 送进模型上下文的诊断条数上限。诊断可能上百条，全塞进去会冲掉正文；
# 这里只保留最靠前的若干条，并在结尾说明还有多少条。
_MAX_ITEMS = 40
_SEVERITY_LABEL = {"error": "错误", "warning": "警告", "info": "提示", "hint": "建议"}


def _render(rel: str, counts: dict, items: list[dict]) -> str:
    total = len(items)
    head = (
        f"[lsp_diagnostics] {rel}："
        f"{counts.get('error', 0)} 个错误，{counts.get('warning', 0)} 个警告"
    )
    if total == 0:
        return head + "\n语言服务器没有报告任何问题。"
    lines = [head]
    for item in items[:_MAX_ITEMS]:
        label = _SEVERITY_LABEL.get(item["severity"], item["severity"])
        code = f" [{item['code']}]" if item.get("code") else ""
        lines.append(
            f"  {item['line']}:{item['char']}  {label}{code}  {item['message']}"
        )
    if total > _MAX_ITEMS:
        lines.append(f"  …还有 {total - _MAX_ITEMS} 条未列出")
    return "\n".join(lines)


def _lsp_diagnostics(ctx: ToolContext, args: dict) -> ToolResult:
    raw = str(args.get("path") or "").strip()
    if not raw:
        raise ToolError("path 不能为空")

    # 路径同样是不可信输入：越权/敏感文件在这里就被拒
    abs_path = ctx.paths.check_read(raw)
    rel = ctx.paths.relative(abs_path)
    if not abs_path.is_file():
        raise ToolError(f"{rel} 不存在或不是普通文件")

    # 延迟导入：`agent_core.tools` 与 `agent_core.lsp` 互相引用，
    # 模块级 import 会形成环；这里在调用时才解析，环就不存在了。
    from ..lsp.manager import get_manager

    result = get_manager().diagnostics(ctx.config, abs_path, rel)
    if not result.get("available"):
        reason = result.get("reason") or "LSP 不可用"
        return ToolResult(
            ok=False,
            content=(
                f"[lsp_diagnostics] 不可用：{reason}。\n"
                "改用 read_file / grep 直接读代码，或让用户安装语言服务器后重试。"
            ),
            meta={"available": False, "path": rel},
        )

    ctx.mark_read(rel)
    counts = result.get("counts") or {}
    items = result.get("diagnostics") or []
    body = _render(rel, counts, items)
    truncated = len(items) > _MAX_ITEMS
    ctx.audit.record(
        "tool.lsp_diagnostics",
        path=rel,
        diagnostics=len(items),
        error_count=int(counts.get("error", 0)),
        warning_count=int(counts.get("warning", 0)),
        # 服务器名与语言都不算正文，只用来回答「是哪台服务器报的」
        server=result.get("server") or result.get("language") or "",
        truncated=truncated,
    )
    return ToolResult(
        ok=True,
        content=wrap_untrusted("lsp_diagnostics", body),
        meta={
            "available": True,
            "path": rel,
            "language": result.get("language") or "",
            "diagnostics": len(items),
            "errors": int(counts.get("error", 0)),
            "warnings": int(counts.get("warning", 0)),
            "truncated": truncated,
        },
    )


LSP_DIAGNOSTICS_SPEC = ToolSpec(
    name="lsp_diagnostics",
    description=(
        "查看**语言服务器**对某个源文件报告的诊断：类型错误、未定义符号、"
        "未使用变量等，带行号与严重级别。\n"
        "用途：改完代码后**先看静态诊断**，往往能免掉一整轮「跑测试→报错→再改」。\n"
        "与 grep / read_file 的分工：那两个看「代码长什么样」，这个看「有几处问题」。\n"
        "只读工具：不修改任何文件。**仅在本机装了对应语言服务器时可用**。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "源文件路径（工作区相对路径）"},
        },
        "required": ["path"],
    },
    handler=_lsp_diagnostics,
    risk=Risk.SAFE,
)


__all__ = ["LSP_DIAGNOSTICS_SPEC"]