"""
第五章 · 协调器模式运行示例（纯本地模拟，无需 API Key）。

演示"一份提示词 + 三个工具 = 完整协调器"的星形结构，与源码对应：
  COORDINATOR_SYSTEM_PROMPT ←→  src/coordinator/coordinatorMode.ts:111（人格即框架）
  NotificationQueue         ←→  src/utils/messageQueueManager.ts（now/next/later 优先级）
  build_task_notification   ←→  src/tasks/LocalAgentTask/LocalAgentTask.tsx:197（XML + 去重）
  spawn_worker              ←→  AgentTool 的 run_in_background 路径（立即返回 task_id）
  send_message              ←→  src/tools/SendMessageTool/SendMessageTool.ts:741（路由）
  WORKER_TOOLS              ←→  src/constants/tools.ts:55（无 spawn 无 send：星形约束）

    uv run python examples/coordinator_demo.py
"""

import asyncio
import itertools
from dataclasses import dataclass, field

# ── 协调器人格：整个"框架"就是这份提示词 ────────────────────────

COORDINATOR_SYSTEM_PROMPT = """你是协调者。拆解任务派给 Worker，综合结果，汇报用户。
纪律：每条消息说给用户听；Worker 通知是内部信号，不要感谢它；
永远先综合再派工——spec 必须带文件路径与行号。"""

# Worker 工具池：没有 spawn_worker（不能再派生）、没有 send_message（不能互连）
WORKER_TOOLS = ["read", "grep", "edit", "bash"]


# ── Worker 任务：状态机 running → completed/killed，notified 去重 ──


@dataclass
class WorkerTask:
    task_id: str
    prompt: str
    status: str = "running"  # running | completed | killed
    result: str = ""
    notified: bool = False
    pending_messages: list[str] = field(default_factory=list)  # 运行中收到的追加指令
    transcript: list[dict] = field(default_factory=list)  # "落盘"的完整对话，复活用


TASKS: dict[str, WorkerTask] = {}
_ids = itertools.count(1)


# ── 通知队列：用户输入(next) 永远排在通知(later) 前面 ────────────


class NotificationQueue:
    PRIORITY = {"now": 0, "next": 1, "later": 2}

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []  # (priority, message)

    def enqueue(self, message: str, priority: str = "next") -> None:
        self._items.append((priority, message))

    def drain(self) -> list[str]:
        """回合边界一次性取走全部排队消息，按优先级稳定排序。"""
        self._items.sort(key=lambda it: self.PRIORITY[it[0]])
        drained = [m for _, m in self._items]
        self._items.clear()
        return drained


QUEUE = NotificationQueue()


def build_task_notification(task: WorkerTask) -> str:
    """对应 enqueueAgentNotification：XML 构造 + notified 原子去重。"""
    if task.notified:
        return ""  # 已通知过，绝不重复
    task.notified = True
    return (
        f"<task-notification>\n"
        f"  <task-id>{task.task_id}</task-id>\n"
        f"  <status>{task.status}</status>\n"
        f"  <result>{task.result}</result>\n"
        f"</task-notification>"
    )


# ── Worker 子循环：跑完把通知投进队列（priority='later'）───────


async def _worker_loop(task: WorkerTask, resumed: bool = False) -> None:
    task.transcript.append({"role": "user", "content": task.prompt})
    for step in range(2):  # 模拟若干工具轮
        await asyncio.sleep(0.1)
        # 工具轮边界：取走协调器排队注入的追加指令（drainPendingMessages）
        if task.pending_messages:
            for msg in task.pending_messages:
                print(f"    [{task.task_id}] 工具轮边界收到追加指令：{msg}")
                task.transcript.append({"role": "user", "content": msg})
            task.pending_messages.clear()
        task.transcript.append({"role": "assistant", "content": f"第 {step+1} 轮工具调用…"})
    verb = "复活续跑" if resumed else "调研"
    task.result = f"{verb}完成：结论 20 行（过程 200KB 已烧在自己窗口里）"
    task.status = "completed"
    task.transcript.append({"role": "assistant", "content": task.result})
    QUEUE.enqueue(build_task_notification(task), priority="later")  # 不与用户抢话


def spawn_worker(prompt: str) -> str:
    """对应 run_in_background 派生：立即返回 task_id，绝不等待。"""
    task = WorkerTask(task_id=f"agent-{next(_ids):03d}", prompt=prompt)
    TASKS[task.task_id] = task
    asyncio.get_running_loop().create_task(_worker_loop(task))
    return task.task_id


def send_message(to: str, message: str) -> str:
    """对应 SendMessageTool.call() 的路由决策树（本进程 Worker 分支）。"""
    task = TASKS[to]
    if task.status == "running":
        task.pending_messages.append(message)  # ① 运行中：排队，不打断
        return f"已排队，将在 {to} 的下一个工具轮送达"
    # ② 终态：从 transcript 复活（resumeAgentBackground），旧上下文全在
    task.status, task.notified, task.prompt = "running", False, message
    asyncio.get_running_loop().create_task(_worker_loop(task, resumed=True))
    return f"{to} 已从 transcript（{len(task.transcript)} 条消息）复活"


# ── Coordinator 主循环：派发 → 回合边界收通知 → 综合 → 再派 ──────


async def main() -> None:
    print("回合 1｜用户：auth 模块有个空指针，修一下")
    print("  协调器派发两个调研 Worker（同轮并行，星形扇出）：")
    a = spawn_worker("调研 src/auth/ 的空指针成因，报路径行号，勿改文件")
    b = spawn_worker("调研 auth 相关测试覆盖，报缺口，勿改文件")
    print(f"    已派发 {a}、{b}，本回合收口——绝不虚构结果\n")

    send_message(a, "补充：重点看 session 过期分支")  # 运行中 → 排队注入
    await asyncio.sleep(0.5)  # Worker 们在后台各自跑完

    print("回合 2｜回合边界，队列注入（user 角色，靠标签识别）：")
    for msg in QUEUE.drain():
        print("  " + msg.replace("\n", "\n  "))

    print("\n  协调器综合两份调研 → 写出带路径行号的 spec，续跑 Worker A：")
    print("    " + send_message(a, "修复 src/auth/validate.ts:42 的空指针，提交并报 hash"))
    await asyncio.sleep(0.5)

    print("\n回合 3｜实现结果抵达：")
    for msg in QUEUE.drain():
        print("  " + msg.replace("\n", "\n  "))
    print("\n协调器全程没写一行代码——它的产出物只有任务书和这句汇报：")
    print("  「修复完成并通过验证，提交在 abc123。」")


if __name__ == "__main__":
    asyncio.run(main())
