"""
Agent Loop 核心实现。

对应 Claude Code 源码：
  src/query.ts  query() / queryLoop()

ReAct 框架（Reasoning + Acting）：
  模型推理 → 发现 tool_use → 执行工具 → 注入 tool_result → 再次推理 → ...
  直到模型不再输出 tool_use（end_turn）或触发熔断（max_turns）。
"""

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .tools import TOOLS, execute_tool

# ── 类型定义 ──────────────────────────────────────────────────────────────────


@dataclass
class LoopEvent:
    """循环过程中 yield 出的事件，供调用方观察进度。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopResult:
    """循环退出时的终态。对应 query.ts Terminal 类型。"""

    reason: str  # completed | max_turns | aborted | error
    turn_count: int
    final_text: str = ""


# ── 核心循环 ──────────────────────────────────────────────────────────────────


def run_agent_loop(
    user_message: str,
    *,
    model: str = "claude-opus-4-8",
    max_tokens: int = 4096,
    max_turns: int = 10,
) -> Generator[LoopEvent, None, LoopResult]:
    """
    ReAct Agent Loop。

    对应 Claude Code 源码：
      query.ts:241   queryLoop() 函数入口
      query.ts:307   while (true) 主循环
      query.ts:204   State.messages 消息列表作为唯一状态载体
      query.ts:558   needsFollowUp 标志驱动循环继续
      query.ts:1704  maxTurns 熔断检查

    用法：
        gen = run_agent_loop("帮我查北京天气")
        try:
            while True:
                event = next(gen)
                print(event)
        except StopIteration as e:
            result = e.value  # LoopResult
    """
    client = anthropic.Anthropic()

    # 1.1 消息列表作为状态载体
    # 对应 query.ts:204  State.messages
    # 模型是无状态的，全量上下文由调用方维护，每轮追加后整体传入
    messages: list[dict] = [{"role": "user", "content": user_message}]

    final_text = ""
    turn_count = 1

    while True:
        # 1.4 maxTurns 熔断
        # 对应 query.ts:1704  if (maxTurns && nextTurnCount > maxTurns)
        if turn_count > max_turns:
            yield LoopEvent("max_turns_reached", {"turn_count": turn_count})
            return LoopResult("max_turns", turn_count, final_text)

        yield LoopEvent("turn_start", {"turn": turn_count})

        # 1.2 ReAct — Reasoning 阶段
        # 对应 query.ts:659  deps.callModel()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=TOOLS,
            messages=messages,
        )

        yield LoopEvent(
            "model_response",
            {"stop_reason": response.stop_reason, "n_blocks": len(response.content)},
        )

        # 解析本轮模型输出，分离 text 和 tool_use block
        # 对应 query.ts:826  assistantMessages.push(message)
        assistant_content: list[dict] = []
        tool_use_blocks: list[Any] = []

        for block in response.content:
            if block.type == "text":
                final_text = block.text
                assistant_content.append({"type": "text", "text": block.text})
                yield LoopEvent("assistant_text", {"text": block.text})
            elif block.type == "tool_use":
                tool_use_blocks.append(block)
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
                yield LoopEvent("tool_call", {"tool": block.name, "input": block.input})

        messages.append({"role": "assistant", "content": assistant_content})

        # 1.2 ReAct — 判断是否继续
        # 对应 query.ts:558  needsFollowUp
        # 关键：不依赖 stop_reason=='tool_use'（该字段不可靠，见 query.ts:553 注释）
        # 唯一判断依据是本轮是否解析到 tool_use block
        needs_follow_up = len(tool_use_blocks) > 0

        if not needs_follow_up:
            # stop_reason == 'end_turn'，模型完成，退出
            # 对应 query.ts:1264  return { reason: 'completed' }
            return LoopResult("completed", turn_count, final_text)

        # 1.3 ReAct — Acting 阶段：执行工具，注入 tool_result
        # 对应 query.ts:1380  runTools() → toolResults
        # 对应 query.ts:1715  messages: [...history, ...assistant, ...toolResults]
        tool_results: list[dict] = []
        for block in tool_use_blocks:
            result = execute_tool(block.name, block.input)
            yield LoopEvent("tool_result", {"tool": block.name, "result": result})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

        # tool_result 以 user 角色注入——这是 Anthropic API 的协议要求
        # tool_use / tool_result 必须成对，且同 id 对应
        messages.append({"role": "user", "content": tool_results})

        turn_count += 1
