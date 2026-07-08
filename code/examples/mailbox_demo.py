"""
第六章 · Mailbox 运行示例（纯本地模拟，无需 API Key）。

两个"进程"（领队 + 队友）只通过文件通信，演示与源码对应：
  inbox_path / write_to_mailbox  ←→  utils/teammateMailbox.ts（锁 + 重读 + 追加）
  classify                       ←→  isStructuredProtocolMessage（数据面/控制面分拣）
  leader_poller                  ←→  hooks/useInboxPoller.ts（1s 轮询，先递送再标已读）
  teammate_wait_loop             ←→  swarm/inProcessRunner.ts:689（阻塞循环，shutdown 插队）
  permission / shutdown 协议     ←→  swarm/permissionSync.ts + useInboxPoller.ts:677

    uv run python examples/mailbox_demo.py
"""

import asyncio
import json
import os
import tempfile

TEAMS_DIR = tempfile.mkdtemp(prefix="claude-teams-")  # 模拟 ~/.claude/teams/
TEAM = "refactor"

# ── 传输层：一只加锁的 JSON 信箱 ────────────────────────────────


def inbox_path(name: str) -> str:
    d = os.path.join(TEAMS_DIR, TEAM, "inboxes")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.json")


def _with_lock(path: str, fn):
    """锁文件 + 重试退避：并发写者排队等锁而不是失败（LOCK_OPTIONS）。"""
    lock = path + ".lock"
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL)  # 原子抢锁
            try:
                return fn()
            finally:
                os.close(fd)
                os.unlink(lock)
        except FileExistsError:
            continue  # 真实实现是 5-100ms 退避，这里简化
    raise RuntimeError("lock timeout")


def _read(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def write_to_mailbox(to: str, sender: str, text: str) -> None:
    path = inbox_path(to)

    def append():
        msgs = _read(path)  # 拿到锁后重读最新状态——覆盖旧数组会丢信
        msgs.append({"from": sender, "text": text, "read": False})
        with open(path, "w") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=1)

    _with_lock(path, append)


def take_unread(name: str) -> list[dict]:
    """先取走、后标已读（at-least-once：宁可重复，绝不丢失）。"""
    path = inbox_path(name)

    def take():
        msgs = _read(path)
        unread = [m for m in msgs if not m["read"]]
        for m in msgs:
            m["read"] = True  # 演示从简：真实实现在"安全递送之后"才写回
        with open(path, "w") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=1)
        return unread

    return _with_lock(path, take)


# ── 分拣器：数据面 vs 控制面 ────────────────────────────────────

PROTOCOL_TYPES = {"permission_request", "permission_response", "shutdown_request",
                  "shutdown_approved", "idle_notification"}


def classify(text: str) -> dict | None:
    """能解析出已知 JSON type → 控制面；否则 → 数据面（给模型）。"""
    try:
        parsed = json.loads(text)
        if parsed.get("type") in PROTOCOL_TYPES:
            return parsed
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


# ── 队友：阻塞等待循环（常驻空闲，不是跑完即死）─────────────────


async def teammate_wait_loop(name: str) -> None:
    while True:
        await asyncio.sleep(0.1)  # 真实实现 500ms
        unread = take_unread(name)
        # shutdown 插队：先全量扫描，防止被聊天消息淹没
        for m in unread:
            p = classify(m["text"])
            if p and p["type"] == "shutdown_request":
                print(f"  [{name}] 收到关机请求（插队处理），检查手头工作…都提交了")
                write_to_mailbox("lead", name, json.dumps(
                    {"type": "shutdown_approved", "paneId": "%2", "from": name}))
                return  # 自己收尾退出
        for m in unread:
            p = classify(m["text"])
            if p and p["type"] == "permission_response":  # 控制面：resolve 挂起的回调
                print(f"  [{name}] 权限响应到达 → 回调 resolve，工具从挂起处继续执行")
                write_to_mailbox("lead", name, "rm -rf build/ 已执行，产物已清理")
            elif p is None:  # 数据面：<teammate-message> 进模型上下文
                print(f"  [{name}] <teammate-message from=lead> {m['text']}")
                print(f"  [{name}] 模型决定先申请权限再动手")
                write_to_mailbox("lead", name, json.dumps(
                    {"type": "permission_request", "request_id": "req-1",
                     "tool_name": "Bash", "input": "rm -rf build/"}))


# ── 领队：非阻塞轮询 ────────────────────────────────────────────


async def leader_poller() -> None:
    while True:
        await asyncio.sleep(0.1)  # 真实实现 1s
        for m in take_unread("lead"):
            p = classify(m["text"])
            if p is None:  # 数据面
                print(f"[lead] <teammate-message from={m['from']}> {m['text']}")
                print("[lead] 活干完了，发起优雅关机")
                write_to_mailbox(m["from"], "lead", json.dumps(
                    {"type": "shutdown_request", "reason": "任务完成"}))
            elif p["type"] == "permission_request":  # 控制面：瞬移进领队的权限 UI
                print(f"[lead] ⚠ 权限弹窗（来自 {m['from']}）：{p['tool_name']} "
                      f"{p['input']} —— 用户点了允许")
                write_to_mailbox(m["from"], "lead", json.dumps(
                    {"type": "permission_response", "request_id": p["request_id"],
                     "subtype": "success"}))
            elif p["type"] == "shutdown_approved":  # 善后三连
                print(f"[lead] {m['from']} 同意关机 → killPane({p['paneId']}) + "
                      f"名册除名 + 回收任务")
                return


async def main() -> None:
    print(f"信箱目录：{TEAMS_DIR}/{TEAM}/inboxes/（两个'进程'只共享这个目录）\n")
    write_to_mailbox("ada", "lead", "请清理构建产物，完成后汇报")
    await asyncio.gather(leader_poller(), teammate_wait_loop("ada"))
    print("\n团队解散。信箱文件仍在磁盘上，随时可审计。")


if __name__ == "__main__":
    asyncio.run(main())
