# ruff: noqa: E501 - the long prompts are intentionally kept readable for prompt editing.
"""剧本设计 AI 辅助：润色 / 续写 / 场次头规范化。

与 content_rewriter 同一套约定：模型经 newAPI 网关的文本工厂选取，
环境变量 ``SCREENPLAY_ASSISTANT_MODEL`` 可按部署覆盖默认模型。
输出一律是纯文本（output_type=str），调用方决定是否写回稿件 ——
AI 结果不落库，由用户在编辑器里确认合并，避免误覆盖手写内容。
"""

from __future__ import annotations

from pydantic_ai import Agent

from novelvideo.config import get_newapi_text_pydantic_model
from novelvideo.model_gateway_runtime import model_gateway_output_retries

# 单次请求送入模型的正文上限（字符）。剧本可能很长，续写只带尾部上下文、
# 润色/规范化按用户选区或分次调用来控规模；这里挡住明显的失控请求。
MAX_ASSIST_INPUT_CHARS = 20000

POLISH_PROMPT = """你是短剧剧本的资深编剧编辑。用户会给你一段剧本片段，请做**文字润色**。

## 硬规则
- 只润色文字表达：节奏、动作描写、对白语气、镜头提示的措辞
- **不得改变剧情走向、不得新增或删除场次、不得增删角色、不得改写对白的核心信息**
- 保持原文的场次结构：场次头行（如「第1场 城墙外 黄昏」）**原样保留**，一字不改
- 对白保持 `角色名（说话方式）：台词` 的既有格式；引号与冒号风格跟随原文
- 润色后的总长度与原文相当（±20% 以内）
- 只输出润色后的文本，不要解释、不要前后缀说明

## 用户附加要求（可能为空）
{instruction}
"""


CONTINUE_PROMPT = """你是短剧剧本的编剧。请从用户给的剧本结尾处**继续往下写**。

## 硬规则
- 先读透给定片段的：类型题材、叙事文风、人物关系、当前悬念
- 续写内容必须自然承接结尾，保持同样的文风与人称
- 按剧本格式书写：场次头用「第N场 地点 日/夜」格式（N 续接现有编号，没有场次头就从 第1场 开始）
- 对白用 `角色名（说话方式）：台词` 格式
- 输出 {chars_target} 字左右（±30%），在情节推进到一个小转折或钩子处收笔
- 只输出续写的正文，不要复述已有内容，不要解释
{instruction}
"""


NORMALIZE_PROMPT = """你在维护一份短剧剧本的格式规范。请**只规范场次头与格式**，不改写内容。

## 规范
- 场次头统一为：`第N场 地点 时间`，时间是 日/夜/黄昏/清晨 之一
  - 「1、城墙外，黄昏」「SCENE 1 城墙外-黄昏」「城墙外 黄昏」等写法都要归一
  - 场次编号按出现顺序重排为 1,2,3,…（保持场次相对顺序不变）
- 场次头独立成行，前后留空行
- 正文内容**逐字保留**：不改写叙述、不增删对白、不调整段落顺序
- 角色名后的对白保持 `角色名（说话方式）：台词` 或原文既有对白格式
- 只输出规范后的完整文本，不要解释
"""


def _clip_for_model(text: str) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= MAX_ASSIST_INPUT_CHARS:
        return stripped
    return stripped[-MAX_ASSIST_INPUT_CHARS:]


def _assistant_agent(system_prompt: str, name: str) -> Agent:
    return Agent(
        get_newapi_text_pydantic_model(
            "SCREENPLAY_ASSISTANT_MODEL",
            "gpt-5.4-mini",
            capability="text.generate",
        ),
        system_prompt=system_prompt,
        output_type=str,
        output_retries=model_gateway_output_retries(2),
        name=name,
    )


async def polish_screenplay_text(text: str, *, instruction: str = "") -> str:
    """润色剧本片段；场次头与剧情结构原样保留。"""
    source = _clip_for_model(text)
    if not source:
        return ""
    prompt = POLISH_PROMPT.replace("{instruction}", instruction.strip() or "（无）")
    agent = _assistant_agent(prompt, "剧本润色编辑")
    result = await agent.run(source)
    return str(result.output or "").strip()


async def continue_screenplay_text(
    context_text: str,
    *,
    instruction: str = "",
    chars_target: int = 600,
) -> str:
    """接续剧本尾部续写一段（约 chars_target 字）。"""
    context = _clip_for_model(context_text)
    if not context:
        return ""
    prompt = CONTINUE_PROMPT.replace(
        "{chars_target}", str(max(100, min(2000, int(chars_target))))
    ).replace("{instruction}", f"\n## 用户附加要求\n{instruction.strip()}" if instruction.strip() else "")
    agent = _assistant_agent(prompt, "剧本续写编剧")
    result = await agent.run(context)
    return str(result.output or "").strip()


async def normalize_screenplay_headers(text: str) -> str:
    """把场次头归一为「第N场 地点 时间」，正文逐字保留。"""
    source = _clip_for_model(text)
    if not source:
        return ""
    agent = _assistant_agent(NORMALIZE_PROMPT, "剧本格式规范化")
    result = await agent.run(source)
    return str(result.output or "").strip()
