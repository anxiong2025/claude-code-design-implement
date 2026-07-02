"""
第一章 · Agent Loop（核心循环）
对照 Claude Code 源码：src/query.ts queryLoop()

运行：
    uv run python ch01_agent_loop.py
"""

import os
import json
from typing import Generator
import anthropic

# ── 工具定义（对应 query.ts 中 tools 参数） ──────────────────────────────────
TOOLS = [
    {
        "name": "calculator",
        "description": "执行基本数学运算",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "数学表达式，如 '2 + 3 * 4'"}
            },
            "required": ["expression"],
        },
    },
    {
        "name": "get_weather",
        "description": "查询某城市的天气",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"}
            },
            "required": ["city"],
        },
    },
]


def execute_tool(name: str, tool_input: dict) -> str:
    """工具执行层。对应 query.ts runTools() + toolOrchestration.ts"""
    if name == "calculator":
        try:
            result = eval(tool_input["expression"], {"__builtins__": {}})
            return str(result)
        except Exception as e:
            return f"计算错误: {e}"
    elif name == "get_weather":
        # 模拟数据，真实场景对接外部 API
        mock = {"北京": "晴，18°C", "上海": "多云，22°C", "广州": "小雨，28°C"}
        return mock.get(tool_input["city"], "未找到该城市天气数据")
    return "未知工具"


# ── 核心：Agent Loop ─────────────────────────────────────────────────────────

def agent_loop(
    user_message: str,
    max_turns: int = 10,
) -> Generator[dict, None, dict]:
    """
    ReAct 框架的完整实现。

    对照 Claude Code 源码：
      - query.ts:241  queryLoop() 函数签名
      - query.ts:307  while (true) 主循环
      - query.ts:204  State 类型（messages 是状态载体）
      - query.ts:558  needsFollowUp 标志驱动循环继续
      - query.ts:1704 maxTurns 熔断

    ReAct = Reasoning（模型思考）+ Acting（工具调用）交替执行：
        [用户消息] → 模型推理 → 发现 tool_use → 执行工具
                   → 注入 tool_result → 再次推理 → ...
                   → 无 tool_use → end_turn → 返回
    """
    client = anthropic.Anthropic()

    # ── 1.1 消息列表作为状态载体 ──────────────────────────────────────────────
    # 对应 query.ts:204 State.messages
    # 消息数组是循环的唯一状态——每一轮把新产出（assistant + tool_result）追加进去
    # 下一轮调用模型时把整个数组传入，模型靠它还原完整上下文
    messages: list[dict] = [
        {"role": "user", "content": user_message}
    ]

    turn_count = 1

    while True:
        # ── 1.4 maxTurns 熔断机制 ──────────────────────────────────────────
        # 对应 query.ts:1704  if (maxTurns && nextTurnCount > maxTurns)
        if turn_count > max_turns:
            yield {"type": "max_turns_reached", "turn_count": turn_count}
            return {"reason": "max_turns", "turn_count": turn_count}

        yield {"type": "turn_start", "turn": turn_count}

        # ── 1.2 ReAct 框架：Reasoning 阶段 ───────────────────────────────
        # 对应 query.ts:659  deps.callModel()
        # 把完整 messages 数组传给模型，让它推理并决定下一步
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            tools=TOOLS,
            messages=messages,
        )

        yield {"type": "model_response", "stop_reason": response.stop_reason,
               "content_blocks": len(response.content)}

        # 收集本轮模型输出，追加到消息列表
        # 对应 query.ts:826  assistantMessages.push(message)
        assistant_content = []
        tool_use_blocks = []

        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                yield {"type": "thinking", "text": block.text[:120] + "..." if len(block.text) > 120 else block.text}
            elif block.type == "tool_use":
                tool_use_blocks.append(block)
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                yield {"type": "tool_call", "tool": block.name, "input": block.input}

        # 把 assistant 这一轮的输出追加到消息列表
        messages.append({"role": "assistant", "content": assistant_content})

        # ── 1.2 ReAct 框架：Acting 阶段 ──────────────────────────────────
        # needsFollowUp 对应 query.ts:558
        # 关键点：Claude Code 不依赖 stop_reason=='tool_use'（它不可靠）
        # 而是看有没有解析到 tool_use block
        needs_follow_up = len(tool_use_blocks) > 0

        if not needs_follow_up:
            # stop_reason == 'end_turn'，模型认为任务完成，退出循环
            return {"reason": "completed", "turn_count": turn_count}

        # ── 1.3 工具调用结果注入 ──────────────────────────────────────────
        # 对应 query.ts:1380  runTools() → toolResults
        # 对应 query.ts:1715  messages: [...history, ...assistant, ...toolResults]
        tool_results = []
        for block in tool_use_blocks:
            result = execute_tool(block.name, block.input)
            yield {"type": "tool_result", "tool": block.name, "result": result}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        # tool_result 以 user 角色注入，构成下一轮模型的输入
        # 这是 ReAct 循环闭合的关键：Acting 结果变成下一次 Reasoning 的上下文
        messages.append({"role": "user", "content": tool_results})

        turn_count += 1


# ── 运行入口 ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("第一章 · Agent Loop 演示")
    print("=" * 60)

    question = "北京今天天气怎么样？另外帮我算一下 (12 + 8) * 3 是多少？"
    print(f"\n用户：{question}\n")

    gen = agent_loop(question, max_turns=10)
    terminal = None

    try:
        while True:
            event = next(gen)
            match event["type"]:
                case "turn_start":
                    print(f"\n── Turn {event['turn']} ──")
                case "thinking":
                    print(f"  [推理] {event['text']}")
                case "tool_call":
                    print(f"  [工具调用] {event['tool']}({json.dumps(event['input'], ensure_ascii=False)})")
                case "tool_result":
                    print(f"  [工具结果] {event['tool']} → {event['result']}")
                case "model_response":
                    print(f"  [模型] stop_reason={event['stop_reason']}, blocks={event['content_blocks']}")
                case "max_turns_reached":
                    print(f"\n[熔断] 达到最大轮次 {event['turn_count']}")
    except StopIteration as e:
        terminal = e.value

    print(f"\n── 循环结束：reason={terminal['reason']}, turns={terminal['turn_count']} ──")


if __name__ == "__main__":
    main()
