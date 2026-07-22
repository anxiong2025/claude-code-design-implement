"""
第九章 · 生成式交互模式运行示例（纯本地模拟，无需 API Key）。

把"向人提问"做成一个模型可调用的工具：模型发出结构化的 AskUserQuestion 工具调用，
运行时不去执行代码，而是把 UI 交给人；人选完，选择被包成一条 user 角色的 tool_result
回流进对话——人类成了 ReAct 循环里的一种"可被调用的能力"。

与源码结构一一对应：
  inputSchema 校验         ←→  src/tools/AskUserQuestionTool/AskUserQuestionTool.tsx:62
    questions .min(1).max(4) / options .min(2).max(4) / multiSelect 默认 false
  UNIQUENESS_REFINE        ←→  AskUserQuestionTool.tsx:32（问题文本、选项 label 各自唯一）
  自动注入 "Other"          ←→  QuestionView.tsx:206（label "__other__"；预览题不注入，见 PreviewQuestionView.tsx:69）
  "推荐项放第一 + (Recommended)" ←→  prompt.ts:41（ASK_USER_QUESTION_TOOL_PROMPT）
  侧栏并排预览              ←→  PreviewQuestionView.tsx:233（LEFT_PANEL_WIDTH=30, GAP=4）
  hasAnyPreview            ←→  QuestionView.tsx:235（!multiSelect && options.some(preview)）
  答案回流 tool_result      ←→  AskUserQuestionTool.tsx:224 mapToolResultToToolResultBlockParam
    多选用 ", " 连接；behavior:'ask' 表示"这次工具调用要问人"

    uv run python examples/askuserquestion_demo.py
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 源码里的两个界面常量（PreviewQuestionView.tsx:233-234）
LEFT_PANEL_WIDTH = 30
GAP = 4
OTHER_VALUE = "__other__"  # QuestionView.tsx:206


# ── 输入模型（对应 questionOptionSchema / questionSchema / inputSchema） ──


@dataclass
class Option:
    label: str
    description: str
    preview: str | None = None  # 可选：并排预览内容（mockup / 配置 / 代码片段）


@dataclass
class Question:
    question: str
    header: str  # 极短标签，UI 里当 chip（源码上限 12 字符）
    options: list[Option]
    multi_select: bool = False


# ── Schema 校验：结构约束 fail-fast（对应 zod 的 .min/.max/.refine） ──────


class SchemaError(ValueError):
    """对应 inputSchema 校验失败：模型这次工具调用不合法，直接打回。"""


def validate(questions: list[Question]) -> None:
    # questions .min(1).max(4)
    if not (1 <= len(questions) <= 4):
        raise SchemaError(f"questions 必须是 1–4 个，收到 {len(questions)}")

    seen_questions: set[str] = set()
    for q in questions:
        # options .min(2).max(4)
        if not (2 <= len(q.options) <= 4):
            raise SchemaError(f'"{q.question}" 的 options 必须是 2–4 个，收到 {len(q.options)}')
        # header 是 chip，源码限 12 字符
        if len(q.header) > 12:
            raise SchemaError(f'header "{q.header}" 超过 12 字符')
        # UNIQUENESS_REFINE：问题文本唯一
        if q.question in seen_questions:
            raise SchemaError(f'问题文本重复："{q.question}"')
        seen_questions.add(q.question)
        # UNIQUENESS_REFINE：同一问题内 label 唯一
        labels = [o.label for o in q.options]
        if len(labels) != len(set(labels)):
            raise SchemaError(f'"{q.question}" 内有重复的选项 label')
        # 预览只支持单选（PREVIEW_FEATURE_PROMPT / QuestionView.tsx:235）
        if q.multi_select and any(o.preview for o in q.options):
            raise SchemaError(f'"{q.question}" 是多选，不能带 preview')


# ── 渲染：把一次工具调用变成给人看的界面 ───────────────────────────


def has_any_preview(q: Question) -> bool:
    # QuestionView.tsx:235: const hasAnyPreview = !multiSelect && options.some(preview)
    return (not q.multi_select) and any(o.preview for o in q.options)


def render_options(q: Question) -> list[Option]:
    """返回真正展示给用户的选项列表。

    关键细节：普通问题末尾自动追加一个 "Other"（自由输入），
    但**预览题不追加**——源码 PreviewQuestionView.tsx:69
    "Only real options — no 'Other' for preview questions"。
    """
    opts = list(q.options)
    if not has_any_preview(q):
        opts.append(Option(label="Other", description="自己输入一个答案", preview=None))
    return opts


def render_question(q: Question, index: int, total: int) -> None:
    tag = "多选" if q.multi_select else "单选"
    print(f"\n[{q.header}] 问题 {index + 1}/{total}（{tag}）")
    print(f"  {q.question}")
    opts = render_options(q)

    if has_any_preview(q):
        # 侧栏并排：左边选项列表（宽 30），右边预览面板
        print(f"  ── 并排预览（左栏宽 {LEFT_PANEL_WIDTH}，间距 {GAP}） ──")
        for i, o in enumerate(opts):
            left = f"  {i + 1}. {o.label}"
            print(f"{left:<{LEFT_PANEL_WIDTH}}{' ' * GAP}│ {o.description}")
            if o.preview:
                for line in o.preview.strip().splitlines():
                    print(f"{'':<{LEFT_PANEL_WIDTH}}{' ' * GAP}│   {line}")
    else:
        for i, o in enumerate(opts):
            marker = "☐" if q.multi_select else "○"
            note = "" if o.label == "Other" else f" —— {o.description}"
            print(f"    {marker} {i + 1}. {o.label}{note}")


# ── 收集答案：模拟用户在 UI 里的选择 ───────────────────────────────


def collect_answer(q: Question, picks: list[int], other_text: str | None = None) -> str:
    """把用户的按键选择折算成一个答案字符串。

    对应 AskUserQuestionPermissionRequest.tsx:426 起：
      - 多选 → 各 label 用 ", " 连接
      - 选了 "Other" → 用自由输入文本替代 label
    """
    opts = render_options(q)
    chosen = [opts[i] for i in picks]
    labels: list[str] = []
    for o in chosen:
        if o.label == "Other":
            labels.append(other_text or "(空)")
        else:
            labels.append(o.label)
    return ", ".join(labels) if q.multi_select else labels[0]


# ── 回流：把选择包成一条 user 角色的 tool_result ────────────────────


def map_tool_result(answers: dict[str, str], tool_use_id: str) -> dict:
    """对应 AskUserQuestionTool.tsx:224 mapToolResultToToolResultBlockParam。

    人的选择不是控制信号，而是**回流进对话的数据**——和第三章任何工具的
    tool_result 走同一条路（user 角色、按 tool_use_id 配对）。
    """
    answers_text = ", ".join(f'"{q}"="{a}"' for q, a in answers.items())
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "role": "user",  # 第一章：tool_result 以 user 角色注入下一轮
        "content": (
            f"User has answered your questions: {answers_text}. "
            "You can now continue with the user's answers in mind."
        ),
    }


# ── 演示 ─────────────────────────────────────────────────────────


def demo_preview_question() -> None:
    print("=" * 68)
    print("场景：模型要给项目加缓存，但后端选型需要人来拍板 → 发起 AskUserQuestion")
    print("=" * 68)

    q = Question(
        question="缓存后端用哪种？",
        header="缓存后端",  # ≤12 字符
        multi_select=False,
        options=[
            # 推荐项放第一，label 带 (Recommended)（prompt.ts:41）
            Option(
                "Redis (Recommended)",
                "独立缓存服务，多进程共享、可持久化；需要多起一个依赖",
                preview="backend: redis\nurl: redis://localhost:6379\nttl: 3600s\n共享: ✅ 多进程   持久化: ✅",
            ),
            Option(
                "内存",
                "进程内 dict，零依赖最快；重启即失、不跨进程",
                preview="backend: memory\nmax_entries: 10000\n共享: ❌ 单进程   持久化: ❌",
            ),
            Option(
                "文件",
                "落到本地文件，零依赖且重启不失；并发写有锁开销",
                preview="backend: file\npath: ./.cache\n共享: ⚠️ 需加锁  持久化: ✅",
            ),
        ],
    )
    validate([q])
    render_question(q, 0, 1)

    # 模拟：用户高亮对比右侧配置后，选了推荐项（第 1 个）
    picked = [0]
    answer = collect_answer(q, picked)
    print(f"\n  → 用户选择：{answer}")

    # 单问题、单选 → 隐藏"提交"页、答完即回流（hideSubmitTab，tsx:264）
    result = map_tool_result({q.question: answer}, tool_use_id="toolu_ask_01")
    print("\n  回流给模型的 tool_result（user 角色）：")
    print(f"    {result['content']}")


def demo_multi_question() -> None:
    print("\n" + "=" * 68)
    print("场景二：一次问两件事（多问 + 其中一个多选 + 自动 Other）")
    print("=" * 68)

    q1 = Question(
        question="确认后要一起做哪些事？",
        header="收尾动作",
        multi_select=True,  # 多选 → 不注入 Other、不能带 preview
        options=[
            Option("写单元测试", "为新代码补测试"),
            Option("更新文档", "同步 README / 注释"),
            Option("提交 PR", "推分支并开 PR"),
        ],
    )
    q2 = Question(
        question="目标运行环境？",
        header="运行环境",
        options=[  # 单选、无预览 → 末尾自动追加 Other
            Option("Node 20", "当前 LTS"),
            Option("Node 18", "上一个 LTS"),
        ],
    )
    validate([q1, q2])
    for i, q in enumerate([q1, q2]):
        render_question(q, i, 2)

    a1 = collect_answer(q1, [0, 2])  # 多选：写单元测试 + 提交 PR
    a2 = collect_answer(q2, [2], other_text="Bun 1.1")  # 选了 Other → 自由输入
    print(f"\n  → 问题1（多选）：{a1}")
    print(f"  → 问题2（Other）：{a2}")

    result = map_tool_result({q1.question: a1, q2.question: a2}, tool_use_id="toolu_ask_02")
    print("\n  回流给模型的 tool_result（user 角色）：")
    print(f"    {result['content']}")


def demo_schema_rejection() -> None:
    print("\n" + "=" * 68)
    print("场景三：模型发了不合法的工具调用 → schema 当场打回（不问人）")
    print("=" * 68)
    bad = Question(
        question="要不要继续？",
        header="确认",
        options=[Option("好", "继续")],  # 只有 1 个选项，违反 .min(2)
    )
    try:
        validate([bad])
    except SchemaError as e:
        print(f"  ✗ 校验失败：{e}")
        print("  → 和普通工具的入参校验一样：结构不对，第一道关就拦下，模型需重发。")


def main() -> None:
    demo_preview_question()
    demo_multi_question()
    demo_schema_rejection()
    print("\n" + "─" * 68)
    print("一句话：AskUserQuestion 把'人的选择'变成一条 tool_result 喂回循环——")
    print("和第三章的文件读取、命令执行走同一条数据通路，人成了可被调用的能力。")


if __name__ == "__main__":
    main()
