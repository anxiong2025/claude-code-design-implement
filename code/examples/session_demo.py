"""
第八章 · Session 持久化与恢复 运行示例（纯本地模拟，无需 API Key）。

一个迷你 Session 系统，演示与源码对应：
  record / append-only JSONL      ←→  sessionStorage.ts:1408 recordTranscript（UUID 去重）
  load                            ←→  sessionStorage.ts:3472 loadTranscriptFile
  build_chain（叶子回走+反转+防环）  ←→  sessionStorage.ts:2069 buildConversationChain
  resume（走链 + 挖 todos）         ←→  sessionRestore.ts:409 processResumedConversation
  rewind（分叉：旧枝留在文件里）      ←→  parentUuid 树
  compact（boundary 掐链换页）      ←→  compact.ts:387 compactConversation

    uv run python examples/session_demo.py
"""

import json
import os
import tempfile

SESSION_DIR = tempfile.mkdtemp(prefix="claude-projects-")  # 模拟 ~/.claude/projects/
TRANSCRIPT = os.path.join(SESSION_DIR, "9f3c-demo.jsonl")

# ── 8.1 落盘：append-only + UUID 去重 ────────────────────────────

_recorded: set[str] = set()  # 已落盘的 UUID 集合（源码里是 getSessionMessages）


def record(entries: list[dict]) -> int:
    """只追加新条目；重复写无害（幂等）。崩溃最多废正在写的半行。"""
    n = 0
    with open(TRANSCRIPT, "a") as f:
        for e in entries:
            if e.get("uuid") in _recorded:
                continue  # UUID 去重：调用方不必记"哪些存过了"
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            if "uuid" in e:
                _recorded.add(e["uuid"])
            n += 1
    return n


def msg(uuid: str, parent: str | None, role: str, text: str, **extra) -> dict:
    return {"type": role, "uuid": uuid, "parentUuid": parent,
            "message": {"content": text}, **extra}


# ── 8.2 读回：JSONL → Map → 从叶子回走一条链 ──────────────────────


def load(path: str):
    """loadTranscriptFile 的迷你版：消息进 Map，元数据单独归档。"""
    messages, meta, order = {}, {}, []
    with open(path) as f:
        for line in f:
            e = json.loads(line)
            if e["type"] in ("user", "assistant", "system"):
                messages[e["uuid"]] = e
                order.append(e["uuid"])
            else:
                meta[e["type"]] = e  # custom-title / worktree-state / …
    return messages, meta, order


def build_chain(messages: dict, leaf_uuid: str) -> list[dict]:
    """buildConversationChain：叶子沿 parentUuid 回走到根，反转。seen 防环。"""
    chain, seen = [], set()
    cur = messages.get(leaf_uuid)
    while cur:
        if cur["uuid"] in seen:
            break  # 环保护：文件是外部输入，读时当不可信数据
        seen.add(cur["uuid"])
        chain.append(cur)
        cur = messages.get(cur["parentUuid"]) if cur["parentUuid"] else None
    chain.reverse()
    return chain


def resume(path: str) -> dict:
    """--continue：取最新叶子走链；顺带从转录里挖 todos（单一事实源）。"""
    messages, meta, order = load(path)
    children_of = {m["parentUuid"] for m in messages.values() if m["parentUuid"]}
    leaves = [u for u in order if u not in children_of]
    leaf = leaves[-1]  # --continue 取最新叶子；--resume 让用户挑
    chain = build_chain(messages, leaf)
    todos = next(  # extractTodosFromTranscript：倒着找最后一次 TodoWrite
        (m["todos"] for m in reversed(chain) if m.get("todos")), [])
    return {"chain": chain, "todos": todos, "meta": meta, "leaf": leaf}


# ── 8.3 Compact：换页，不是清空 ──────────────────────────────────

AUTOCOMPACT_BUFFER = 13_000    # autoCompact.ts:62
POST_COMPACT_FILES = 5         # compact.ts:122 灾后重建预算
POST_COMPACT_TOKEN_BUDGET = 50_000


def compact(chain: list[dict], recent_files: list[str]) -> list[dict]:
    """模型给"下一个自己"写交接文档（这里用拼接冒充模型）。"""
    summary_text = "；".join(
        f"[{m['type']}] {m['message']['content']}" for m in chain[:-1])
    boundary = {"type": "system", "uuid": "boundary-1", "parentUuid": None,  # 掐链！
                "message": {"content": "compact boundary"}, "isCompactBoundary": True}
    summary = msg("summary-1", "boundary-1", "user",
                  f"九段式交接摘要（Primary Request / Files / Errors / Next Step…）：{summary_text}")
    rebuild = [  # 灾后重建：重读最近文件，否则模型醒来第一件事是全部重读一遍
        msg(f"rebuild-{i}", "summary-1" if i == 0 else f"rebuild-{i-1}", "user",
            f"<post-compact 重读> {p}")
        for i, p in enumerate(recent_files[:POST_COMPACT_FILES])
    ]
    new_page = [boundary, summary, *rebuild]
    record(new_page)  # 旧消息原样留在文件里——磁盘保全量，换掉的只是工作集
    return new_page


# ── 演练 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"转录文件：{TRANSCRIPT}\n")

    print("== 8.1 逐轮落盘（append-only + UUID 去重）==")
    turn1 = [msg("u-1", None, "user", "修复登录超时"),
             msg("a-2", "u-1", "assistant", "读了 auth.ts，超时在 refresh 逻辑",
                 todos=["复现超时", "改 refresh"]),
             {"type": "custom-title", "customTitle": "修复登录超时"},
             {"type": "worktree-state", "worktreePath": "/tmp/wt-login-fix"}]
    turn2 = [msg("u-3", "a-2", "user", "把超时改成 30s"),
             msg("a-4", "u-3", "assistant", "方案 A：改常量")]
    print(f"  第 1 轮写入 {record(turn1)} 条；第 2 轮写入 {record(turn2)} 条；"
          f"重复写第 2 轮 → 新增 {record(turn2)} 条（幂等）")

    print("\n== 8.1 rewind 分叉：旧枝不删，parentUuid 指回历史点 ==")
    record([msg("a-4b", "u-3", "assistant", "方案 B：改配置 + 重试"),
            msg("u-5", "a-4b", "user", "B 好，就这么改")])
    print("  a-4（方案 A）和 a-4b（方案 B）的 parentUuid 都是 u-3 —— 树上两根枝")

    print("\n== 8.2 恢复：--continue 从最新叶子回走一条链 ==")
    r = resume(TRANSCRIPT)
    print(f"  叶子 = {r['leaf']}，链 = {' → '.join(m['uuid'] for m in r['chain'])}")
    print(f"  （方案 A 那根旧枝不在链上，但还在文件里）")
    print(f"  挖出 todos = {r['todos']}（从最后一次 TodoWrite，不单独存文件）")
    print(f"  恢复环境：标题 = {r['meta']['custom-title']['customTitle']!r}，"
          f"chdir → {r['meta']['worktree-state']['worktreePath']}")

    print("\n== 8.3 Compact：boundary 掐链换页，磁盘保全量 ==")
    new_page = compact(r["chain"], ["src/auth.ts", "src/config.ts"])
    r2 = resume(TRANSCRIPT)
    print(f"  换页后 --continue 的链 = {' → '.join(m['uuid'] for m in r2['chain'])}")
    with open(TRANSCRIPT) as f:
        total = sum(1 for _ in f)
    print(f"  文件总行数 = {total}（压缩前的历史一行没少，只是不再进内存）")
