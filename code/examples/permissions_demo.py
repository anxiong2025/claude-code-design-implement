"""
第七章 · 权限与安全 运行示例（纯本地模拟，无需 API Key）。

一条迷你判定流水线 + 中止传播，演示与源码对应：
  parse_rule / match_command      ←→  permissionRuleParser.ts + shellRuleMatching.ts
  check_bash                      ←→  bashPermissions.ts（拆子命令、一票否决）
  check_tool                      ←→  permissions.ts:1158 hasPermissionsToUseToolInner
                                       （1a deny → … → 1g 安全检查 → 2a bypass → 3 ask）
  resolve_hook_decision           ←→  toolHooks.ts:332（hook allow 压不过 deny/ask 规则）
  AbortController 树形传播         ←→  abortController.ts + query.ts:1015（合成 tool_result）

    uv run python examples/permissions_demo.py
"""

import re

# ── 7.2 规则小语言 ──────────────────────────────────────────────


def parse_rule(rule_str: str):
    """'Bash(npm:*)' → ('Bash', 'npm:*')；'WebSearch' → ('WebSearch', None)"""
    m = re.match(r"^([A-Za-z]+)\((.*)\)$", rule_str)
    if not m:
        return rule_str, None
    tool, content = m.group(1), m.group(2)
    if content in ("", "*"):  # Bash() / Bash(*) 等价于整工具规则
        return tool, None
    return tool, content


def match_command(rule_content: str, cmd: str) -> bool:
    """三种匹配：exact / prefix（:*）/ wildcard（*）。"""
    prefix = re.match(r"^(.+):\*$", rule_content)
    if prefix:  # 前缀规则 'npm:*'
        return cmd == prefix.group(1) or cmd.startswith(prefix.group(1) + " ")
    if "*" in rule_content:  # 通配符规则 'git diff *'
        # 源码的讲究：单个尾部通配可选——'git *' 也匹配裸 'git'
        optional_tail = rule_content.endswith(" *") and rule_content.count("*") == 1
        body = rule_content[:-2] if optional_tail else rule_content
        pat = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        if optional_tail:
            pat += "( .*)?"
        return re.fullmatch(pat, cmd, re.S) is not None
    return cmd == rule_content  # 精确规则


# ── 7.2 复合命令：拆分 → 逐段判 → 一票否决 ────────────────────────


def check_bash(cmd: str, rules: dict) -> dict:
    """rules = {'deny': [...], 'ask': [...], 'allow': [...]}（已解析的 Bash 规则内容）"""
    parts = [p.strip() for p in re.split(r"&&|\|\||;", cmd)]  # 简化版拆分
    needs_approval, hit_ask_rule = [], False
    for part in parts:
        if any(match_command(r, part) for r in rules["deny"]):
            return {"behavior": "deny", "part": part, "reason": "rule"}  # 一票否决
        if any(match_command(r, part) for r in rules["ask"]):
            needs_approval.append(part)
            hit_ask_rule = True  # 显式 ask 规则：bypass 也拦
        elif not any(match_command(r, part) for r in rules["allow"]):
            needs_approval.append(part)  # 没规则罩着的段：普通 ask，可被 bypass 放行
    if needs_approval:  # 弹窗只列没过关的段
        return {"behavior": "ask", "parts": needs_approval,
                "reason": "rule" if hit_ask_rule else "no-rule"}
    return {"behavior": "allow", "reason": "rule"}


# ── 7.1 判定流水线（顺序即安全语义）────────────────────────────────

SAFETY_PATHS = (".git/", ".claude/", ".bashrc", ".zshrc")  # 1g 安全检查：bypass 也拦


def check_tool(tool: str, tool_input: dict, mode: str, rules: dict) -> dict:
    """外层包装：dontAsk 的"问变拒"放在出口，任何提前 return 都逃不过。
    对应 permissions.ts:503 —— "done at the end so it can't be bypassed"。"""
    result = check_tool_inner(tool, tool_input, mode, rules)
    if result["behavior"] == "ask" and mode == "dontAsk":
        return {"behavior": "deny", "reason": "mode:dontAsk"}
    return result


def check_tool_inner(tool: str, tool_input: dict, mode: str, rules: dict) -> dict:
    rule_of = lambda behavior: [
        c for r in rules.get(behavior, []) for t, c in [parse_rule(r)] if t == tool
    ]
    tool_wide = lambda behavior: any(
        parse_rule(r) == (tool, None) for r in rules.get(behavior, [])
    )

    # 1a. 整个工具被 deny → 游戏结束（永远先于 2a bypass）
    if tool_wide("deny"):
        return {"behavior": "deny", "reason": "rule"}
    # 1b. 整个工具有 ask 规则
    if tool_wide("ask"):
        return {"behavior": "ask", "reason": "rule"}

    # 1c. 工具自查
    result = {"behavior": "passthrough"}
    if tool == "Bash":
        bash_rules = {b: [c for c in rule_of(b) if c] for b in ("deny", "ask", "allow")}
        result = check_bash(tool_input["command"], bash_rules)
        if result["behavior"] == "deny":  # 1d
            return result
        if result["behavior"] == "ask" and result["reason"] == "rule":
            return result  # 1f 显式 ask 规则：bypass 也拦；无规则的 ask 继续往下走
    if tool in ("Edit", "Write"):
        path = tool_input["file_path"]
        if any(s in path for s in SAFETY_PATHS):  # 1g 安全检查：bypass 也拦
            return {"behavior": "ask", "reason": "safetyCheck"}
        if any(match_command(c, path) for c in rule_of("deny")):
            return {"behavior": "deny", "reason": "rule"}
        if mode == "acceptEdits" and path.startswith("./"):  # 工作目录内免弹窗
            return {"behavior": "allow", "reason": "mode:acceptEdits"}

    # 2a. bypass 模式放行（deny/ask/safetyCheck 已经在上面拦掉了）
    if mode == "bypassPermissions":
        return {"behavior": "allow", "reason": "mode:bypass"}
    # 2b. 整个工具被 allow 规则放行
    if tool_wide("allow") or (result.get("behavior") == "allow"):
        return {"behavior": "allow", "reason": "rule"}

    # 3. 兜底 fail-closed：没结论（passthrough）→ 一律转成 ask
    return {"behavior": "ask", "reason": "no-rule",
            **({"parts": result["parts"]} if "parts" in result else {})}


# ── 7.4 钩子边界：hook 是自动化的用户，不是超级用户 ─────────────────


def resolve_hook_decision(hook_behavior: str, tool, tool_input, mode, rules):
    if hook_behavior == "allow":
        rule_check = check_tool(tool, tool_input, "default", rules)
        if rule_check["behavior"] == "deny":
            return {**rule_check, "note": "deny 规则压过钩子的 allow"}
        if rule_check["reason"] in ("rule", "safetyCheck") and rule_check["behavior"] == "ask":
            return {**rule_check, "note": "ask 规则：钩子批了也要弹窗"}
        return {"behavior": "allow", "note": "钩子代批，跳过弹窗"}
    if hook_behavior == "deny":
        return {"behavior": "deny", "note": "钩子的否决立即生效"}
    return {**check_tool(tool, tool_input, mode, rules), "note": "钩子沉默，走正常流水线"}


# ── 7.5 中止：回合级 controller、单向传播、合成 tool_result ─────────


class AbortController:
    def __init__(self):
        self.aborted = False
        self._children = []

    def child(self):  # createChildAbortController：父传子，子不上传
        c = AbortController()
        self._children.append(c)
        return c

    def abort(self):
        self.aborted = True
        for c in self._children:
            c.abort()


def abort_demo():
    turn = AbortController()          # 一回合一个（REPL.tsx）
    sync_sub = turn                   # 同步 SubAgent：直接共享（runAgent.ts:527）
    async_sub = AbortController()     # 异步 SubAgent：故意不链，TaskStop 单独管
    bash_child = turn.child()         # 工具拿到的是子 controller

    pending_tool_uses = ["toolu_bash_01", "toolu_grep_02"]  # 跑到一半
    turn.abort()                      # 用户按下 Esc

    # 善后：为每个悬空 tool_use 合成配对 tool_result（query.ts:1015）
    synthesized = [
        {"type": "tool_result", "tool_use_id": t, "content": "Interrupted by user"}
        for t in pending_tool_uses
    ]
    print(f"  Esc → turn.aborted={turn.aborted}  bash_child={bash_child.aborted}  "
          f"sync_sub={sync_sub.aborted}  async_sub={async_sub.aborted}（后台不连坐）")
    for s in synthesized:
        print(f"  合成 {s['tool_use_id']} → \"{s['content']}\"  （对话仍是合法状态）")


# ── 运行所有场景 ──────────────────────────────────────────────────

RULES = {
    "deny":  ["Bash(rm -rf /:*)", "Edit(**/prod.env)", "WebSearch"],
    "ask":   ["Bash(npm publish:*)"],
    "allow": ["Bash(cd:*)", "Bash(git diff *)", "Bash(npm test)"],
}

SCENARIOS = [
    ("Bash", {"command": "git diff --stat"},            "default"),
    ("Bash", {"command": "cd src && npm test"},         "default"),
    ("Bash", {"command": "cd src && rm -rf / --force"}, "bypassPermissions"),
    ("Bash", {"command": "npm publish"},                "bypassPermissions"),
    ("Bash", {"command": "cargo build"},                "default"),
    ("Bash", {"command": "cargo build"},                "dontAsk"),
    ("Edit", {"file_path": "./src/app.ts"},             "acceptEdits"),
    ("Edit", {"file_path": "./.claude/settings.json"},  "bypassPermissions"),
    ("WebSearch", {"query": "..."},                     "bypassPermissions"),
]

if __name__ == "__main__":
    print("== 7.1/7.2/7.3 判定流水线（deny 永远先于 bypass，兜底 fail-closed）==")
    for tool, tool_input, mode in SCENARIOS:
        r = check_tool(tool, tool_input, mode, RULES)
        arg = tool_input.get("command") or tool_input.get("file_path") or ""
        print(f"  [{mode:>17}] {tool}({arg!r:<38}) → {r['behavior']:<5} ({r['reason']}"
              + (f", 待批: {r['parts']}" if "parts" in r else "") + ")")

    print("\n== 7.4 钩子边界（hook allow 压不过 deny/ask 规则）==")
    for hook, tool, tool_input in [
        ("allow", "Bash", {"command": "cargo build"}),   # 钩子代批成功
        ("allow", "WebSearch", {"query": "x"}),          # deny 规则压过钩子
        ("allow", "Bash", {"command": "npm publish"}),   # ask 规则照样弹窗
        ("deny",  "Bash", {"command": "git diff"}),      # 钩子否决立即生效
    ]:
        r = resolve_hook_decision(hook, tool, tool_input, "default", RULES)
        print(f"  hook={hook:<5} {tool:<9} → {r['behavior']:<5}  {r['note']}")

    print("\n== 7.5 中止传播（单向下行 + 合成 tool_result 保协议不变量）==")
    abort_demo()
