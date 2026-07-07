"""
第三章 · 工具系统运行示例（纯本地模拟，无需 API Key）。

实现迷你版工具系统，与源码结构一一对应：
  Tool 接口            ←→  src/Tool.ts:281
  build_tool 工厂      ←→  src/Tool.ts:783（fail-closed 默认值）
  partition_tool_calls ←→  src/services/tools/toolOrchestration.ts:84（保序贪心分批）
  run_tools 调度器     ←→  src/services/tools/toolOrchestration.ts:23（批内并发、批间串行）
  并发上限             ←→  src/utils/generators.ts:32 all(generators, cap)

    uv run python examples/tool_demo.py
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

MAX_TOOL_USE_CONCURRENCY = 10  # CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY 默认值


# ── Tool 接口（只保留调度相关成员） ──────────────────────────────


@dataclass
class Tool:
    name: str
    call: Callable[[dict], Awaitable[str]]
    # 谓词接收 input：BashTool 靠它做"按命令内容动态判断"
    is_concurrency_safe: Callable[[dict], bool]
    is_read_only: Callable[[dict], bool]


def build_tool(name: str, call: Callable[[dict], Awaitable[str]], **overrides: Any) -> Tool:
    """buildTool 工厂：未声明的调度属性一律 fail-closed（按写操作处理）。"""
    return Tool(
        name=name,
        call=call,
        is_concurrency_safe=overrides.get("is_concurrency_safe", lambda _input: False),
        is_read_only=overrides.get("is_read_only", lambda _input: False),
    )


# ── 分批：保序贪心，不做全局重排 ────────────────────────────────


@dataclass
class ToolCall:
    tool: Tool
    input: dict


@dataclass
class Batch:
    is_concurrency_safe: bool
    calls: list[ToolCall] = field(default_factory=list)


def partition_tool_calls(calls: list[ToolCall]) -> list[Batch]:
    batches: list[Batch] = []
    for call in calls:
        try:
            safe = bool(call.tool.is_concurrency_safe(call.input))
        except Exception:
            safe = False  # 判断失败也 fail-closed
        if safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].calls.append(call)  # 相邻安全调用并入同批
        else:
            batches.append(Batch(is_concurrency_safe=safe, calls=[call]))
    return batches


# ── 调度：批内并发（受 cap 限制）、批间串行 ─────────────────────


async def run_tools(calls: list[ToolCall]) -> list[str]:
    results: list[str] = []
    semaphore = asyncio.Semaphore(MAX_TOOL_USE_CONCURRENCY)

    async def run_one(c: ToolCall) -> str:
        async with semaphore:
            return await c.tool.call(c.input)

    for i, batch in enumerate(partition_tool_calls(calls), 1):
        mode = "并发" if batch.is_concurrency_safe else "串行"
        names = ", ".join(c.tool.name for c in batch.calls)
        t0 = time.perf_counter()
        if batch.is_concurrency_safe:
            results += await asyncio.gather(*(run_one(c) for c in batch.calls))
        else:
            for c in batch.calls:
                results.append(await c.tool.call(c.input))
        print(f"  批次{i} [{mode}] {{{names}}} 耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
    return results


# ── 示例工具 ─────────────────────────────────────────────────────


def make_io_tool(name: str, duration_ms: int, **overrides: Any) -> Tool:
    async def call(input: dict) -> str:
        await asyncio.sleep(duration_ms / 1000)
        return f"{name}({input.get('arg', '')}) 完成"

    return build_tool(name, call, **overrides)


READ_ONLY = {"is_concurrency_safe": lambda _i: True, "is_read_only": lambda _i: True}

read_tool = make_io_tool("Read", 120, **READ_ONLY)
grep_tool = make_io_tool("Grep", 80, **READ_ONLY)
edit_tool = make_io_tool("Edit", 100)  # 未声明 → fail-closed → 串行

# BashTool：运行时解析命令内容，动态决定是否只读
_READ_ONLY_CMDS = ("ls", "cat", "grep", "echo", "head", "tail")


def _bash_is_read_only(input: dict) -> bool:
    return str(input.get("arg", "")).split(" ")[0] in _READ_ONLY_CMDS


bash_tool = make_io_tool(
    "Bash", 60,
    is_concurrency_safe=_bash_is_read_only,
    is_read_only=_bash_is_read_only,
)


async def main() -> None:
    # 模型同一轮发出的 5 个工具调用（与 3.3 节的示意图一致）
    calls = [
        ToolCall(read_tool, {"arg": "src/query.ts"}),
        ToolCall(grep_tool, {"arg": "isConcurrencySafe"}),
        ToolCall(edit_tool, {"arg": "src/Tool.ts"}),
        ToolCall(read_tool, {"arg": "src/Tool.ts"}),
        ToolCall(bash_tool, {"arg": "echo done"}),
    ]
    print("模型发出:", ", ".join(f"{c.tool.name}" for c in calls))
    print("分批执行（预期 3 批：{Read,Grep} → {Edit} → {Read,Bash}）：")

    t0 = time.perf_counter()
    results = await run_tools(calls)
    total = (time.perf_counter() - t0) * 1000
    serial = 120 + 80 + 100 + 120 + 60

    for r in results:
        print("   ·", r)
    print(f"总耗时 ≈ {total:.0f}ms（全串行需 {serial}ms；并发批次壁钟 = 批内最慢者）")


if __name__ == "__main__":
    asyncio.run(main())
