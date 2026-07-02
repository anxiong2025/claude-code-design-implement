"""
工具定义与执行。

对应 Claude Code 源码：
  src/tools/           内置工具目录
  src/utils/tools.ts   工具类型定义
"""

from typing import Any

# 工具 schema，直接传给 Anthropic API 的 tools 参数
TOOLS: list[dict] = [
    {
        "name": "calculator",
        "description": "执行基本数学运算",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，如 '2 + 3 * 4'",
                }
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
                "city": {
                    "type": "string",
                    "description": "城市名",
                }
            },
            "required": ["city"],
        },
    },
]

# 模拟天气数据
_WEATHER_DATA: dict[str, str] = {
    "北京": "晴，18°C",
    "上海": "多云，22°C",
    "广州": "小雨，28°C",
    "深圳": "晴，26°C",
}


def execute_tool(name: str, tool_input: dict[str, Any]) -> str:
    """
    工具执行分发层。

    对应 Claude Code 源码：
      src/services/tools/toolOrchestration.ts  runTools()
    """
    match name:
        case "calculator":
            return _run_calculator(tool_input["expression"])
        case "get_weather":
            return _run_get_weather(tool_input["city"])
        case _:
            return f"[error] unknown tool: {name}"


def _run_calculator(expression: str) -> str:
    try:
        # 限制 eval 作用域，禁止访问内置函数
        result = eval(expression, {"__builtins__": {}})  # noqa: S307
        return str(result)
    except Exception as e:
        return f"[error] {e}"


def _run_get_weather(city: str) -> str:
    return _WEATHER_DATA.get(city, f"[error] 未找到城市：{city}")
