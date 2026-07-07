"""
第四章 · SubAgent 运行示例（纯本地模拟，无需 API Key）。

演示"把循环装进工具里"的递归结构，与源码对应：
  AgentDefinition       ←→  src/tools/AgentTool/loadAgentsDir.ts（frontmatter）
  resolve_agent_tools   ←→  src/tools/AgentTool/agentToolUtils.ts:122（防递归过滤）
  run_agent             ←→  src/tools/AgentTool/runAgent.ts:248（隔离装配 + 子循环）
  spawn_agent 工具      ←→  src/tools/AgentTool/AgentTool.tsx（isConcurrencySafe=true）

    uv run python examples/subagent_demo.py
"""

import asyncio
from dataclasses import dataclass, field


# ── AgentDefinition：一种子 Agent = 人格 + 工具白名单 ───────────


@dataclass
class AgentDefinition:
    agent_type: str
    system_prompt: str
    allowed_tools: list[str]  # 工具白名单（Explore 只给只读工具）


EXPLORE_AGENT = AgentDefinition(
    agent_type="Explore",
    system_prompt="你是文件搜索专家。只读模式，禁止任何写操作。",
    allowed_tools=["read", "grep"],
)

GENERAL_PURPOSE_AGENT = AgentDefinition(
    agent_type="general-purpose",
    system_prompt="你是通用工程 Agent，独立完成任务后汇报结论。",
    allowed_tools=["read", "grep", "edit"],
)


# ── 模拟的工具与"模型" ──────────────────────────────────────────


async def sim_tool(name: str, arg: str) -> str:
    await asyncio.sleep(0.05)  # 模拟 I/O
    return f"{name}({arg}) 的结果"


def resolve_agent_tools(definition: AgentDefinition, all_tools: list[str]) -> list[str]:
    """装配子 Agent 工具池：按白名单过滤，并移除 spawn_agent 防止递归派生。"""
    return [t for t in all_tools if t in definition.allowed_tools and t != "spawn_agent"]


async def sim_model(system_prompt: str, messages: list[dict], tools: list[str]) -> dict:
    """脚本化的"模型"：先发几个工具调用，收到结果后给出最终文本。"""
    turn = sum(1 for m in messages if m["role"] == "assistant")
    if turn == 0:
        # 第一轮：并行调用可用的只读工具（体现子 Agent 的自主探索）
        calls = [{"tool": t, "arg": messages[0]["content"][:12]} for t in tools[:2]]
        return {"tool_calls": calls}
    # 第二轮：综合出结论——只有这段文本会返回父 Agent
    return {"text": f"[{system_prompt[:9]}…] 结论：基于 {turn * 2} 次工具调用，任务已完成，要点 3 条。"}


# ── run_agent：隔离装配 + 完整子循环 ────────────────────────────


@dataclass
class AgentResult:
    agent_type: str
    final_text: str
    turns: int
    burned_messages: int  # 烧在子窗口里、不会回传的消息数


async def run_agent(definition: AgentDefinition, prompt: str) -> AgentResult:
    # ① 隔离的起点：初始消息只有任务书，没有父的历史
    messages: list[dict] = [{"role": "user", "content": prompt}]
    # ② 工具池按定义过滤，spawn_agent 被移除（递归只有一层）
    tools = resolve_agent_tools(definition, ["read", "grep", "edit", "spawn_agent"])

    turns = 0
    while True:  # ③ 与父循环同构的完整 Agent Loop
        turns += 1
        reply = await sim_model(definition.system_prompt, messages, tools)
        if "text" in reply:
            messages.append({"role": "assistant", "content": reply["text"]})
            # ④ 唯一的出口：最后一条 assistant 文本；过程全部留在子窗口
            return AgentResult(definition.agent_type, reply["text"], turns, len(messages) - 2)
        messages.append({"role": "assistant", "content": reply["tool_calls"]})
        results = await asyncio.gather(
            *(sim_tool(c["tool"], c["arg"]) for c in reply["tool_calls"])
        )
        messages.append({"role": "user", "content": list(results)})  # tool_result 注入


# ── 父 Agent：一轮并行派生两个 SubAgent ─────────────────────────


async def main() -> None:
    parent_messages: list[dict] = [
        {"role": "user", "content": "调研 auth 模块和 db 模块，然后汇总。"},
    ]
    print("父 Agent：同一轮发出 2 个 spawn_agent 调用（isConcurrencySafe → 并发批次）\n")

    # spawn_agent 是并发安全工具 → 两个子循环真正并行
    results = await asyncio.gather(
        run_agent(EXPLORE_AGENT, "调研 auth 模块的登录流程实现"),
        run_agent(GENERAL_PURPOSE_AGENT, "调研 db 模块的连接池配置"),
    )

    for r in results:
        print(f"  [{r.agent_type}] {r.turns} 轮，{r.burned_messages} 条过程消息已隔离丢弃")
        print(f"    回传父窗口的只有 → {r.final_text}\n")
        # 结果以 tool_result（user 角色）注入父的消息数组——与第一章同一机制
        parent_messages.append({"role": "user", "content": f"tool_result: {r.final_text}"})

    print(f"父 Agent 窗口最终只增加 {len(parent_messages) - 1} 条消息，")
    print("子 Agent 的所有中间过程（工具噪音、弯路）都烧在了各自的一次性窗口里。")


if __name__ == "__main__":
    asyncio.run(main())
