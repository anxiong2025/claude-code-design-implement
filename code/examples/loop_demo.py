"""
Agent Loop 运行示例。

    uv run python examples/loop_demo.py
"""

import json

from claude_code_arch.loop import LoopResult, run_agent_loop


def main() -> None:
    question = "北京今天天气怎么样？另外帮我算一下 (12 + 8) * 3 是多少？"
    print(f"用户：{question}\n")

    gen = run_agent_loop(question, max_turns=10)
    result: LoopResult | None = None

    try:
        while True:
            event = next(gen)
            match event.type:
                case "turn_start":
                    print(f"\n── Turn {event.data['turn']} ──")
                case "assistant_text":
                    text = event.data["text"]
                    preview = text[:100] + "..." if len(text) > 100 else text
                    print(f"  [思考] {preview}")
                case "tool_call":
                    print(f"  [调用] {event.data['tool']}  {json.dumps(event.data['input'], ensure_ascii=False)}")
                case "tool_result":
                    print(f"  [结果] {event.data['tool']} → {event.data['result']}")
                case "model_response":
                    print(f"  [模型] stop_reason={event.data['stop_reason']}")
                case "max_turns_reached":
                    print(f"\n[熔断] 已达最大轮次 {event.data['turn_count']}")
    except StopIteration as e:
        result = e.value

    if result:
        print(f"\n── 完成：reason={result.reason}, turns={result.turn_count} ──")
        if result.final_text:
            print(f"\n最终回答：\n{result.final_text}")


if __name__ == "__main__":
    main()
