"""剧本设计端点：项目级在线剧本 CRUD、AI 辅助与「作为源文导入」。

剧本稿件存项目 SQLite 的 ``screenplays`` 表（见 sqlite_store.py）。
「作为源文导入」刻意不直写 novel.txt —— 那是 ingest 任务的完成标记，
由导入状态机负责；这里只把稿件落成 ``uploads/*.txt`` 并做与上传路径
完全相同的格式预检，导入仍走标准 ``POST /ingest/start``（计费、断点、
限流全部复用）。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from novelvideo.api.auth import get_api_user, require_project_scope
from novelvideo.api.deps import get_sqlite_store, resolve_project_scope
from novelvideo.api.schemas import (
    ScreenplayAiAssistRequest,
    ScreenplayCreateRequest,
    ScreenplayUpdateRequest,
)
from novelvideo.api.chapter_preview import (
    build_chapter_preview,
    count_billable_novel_chars,
)
from novelvideo.ports import get_usage_meter
from novelvideo.sqlite_store import SQLiteStore
from novelvideo.utils.document_parsers import (
    MAX_NOVEL_IMPORT_CHARS,
    is_supported_novel_path,
)
from novelvideo.utils.screenplay_quality import build_import_format_check
from novelvideo.utils.upload_safety import (
    is_safe_upload_target,
    sanitize_upload_filename,
)

logger = logging.getLogger("novelvideo.api.screenplays")

router = APIRouter()

SCREENPLAY_AI_FEATURE_KEY = "mainline.screenplay_ai"
SCREENPLAY_AI_ACTIONS = ("polish", "continue", "normalize")
MODEL_CALL_CREDIT_POLICY_FEATURE_INCLUDED = "feature_included"

# 续写只送尾部上下文，控制单次模型请求的规模。
CONTINUE_CONTEXT_CHARS = 4000


def _requester_user_id_for_billing(resolved: Any, user: dict) -> str:
    ctx = getattr(resolved, "ctx", None)
    return str(
        getattr(ctx, "requester_user_id", "")
        or user.get("id")
        or user.get("user_id")
        or user.get("username")
        or ""
    )


async def _load_screenplay_or_404(store: SQLiteStore, screenplay_id: str) -> dict:
    document = await store.get_screenplay(screenplay_id)
    if document is None:
        raise HTTPException(status_code=404, detail="剧本不存在或已删除")
    return document


@router.get("/projects/{project}/screenplays")
async def list_screenplays(
    project: str,
    user: dict = Depends(get_api_user),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    """项目下全部剧本（按更新时间倒序，不含正文）。"""
    items = await store.list_screenplays()
    return {"ok": True, "data": items}


@router.post("/projects/{project}/screenplays")
async def create_screenplay(
    project: str,
    body: ScreenplayCreateRequest,
    user: dict = Depends(require_project_scope("projects:write")),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    """新建剧本稿件。"""
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="剧本标题不能为空")
    document = await store.create_screenplay(
        title=title,
        synopsis=(body.synopsis or "").strip(),
        content=body.content or "",
    )
    logger.info("[%s] screenplay created: %s (%s)", project, document["id"], title)
    return {"ok": True, "data": document}


@router.get("/projects/{project}/screenplays/{screenplay_id}")
async def get_screenplay(
    project: str,
    screenplay_id: str,
    user: dict = Depends(get_api_user),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    """读取剧本全文。"""
    document = await _load_screenplay_or_404(store, screenplay_id)
    return {"ok": True, "data": document}


@router.put("/projects/{project}/screenplays/{screenplay_id}")
async def update_screenplay(
    project: str,
    screenplay_id: str,
    body: ScreenplayUpdateRequest,
    user: dict = Depends(require_project_scope("projects:write")),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    """保存剧本（列级更新，自动保存只带 content）。"""
    document = await store.update_screenplay(
        screenplay_id,
        title=body.title,
        synopsis=body.synopsis,
        content=body.content,
        status=body.status,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="剧本不存在或已删除")
    return {"ok": True, "data": document}


@router.delete("/projects/{project}/screenplays/{screenplay_id}")
async def delete_screenplay(
    project: str,
    screenplay_id: str,
    user: dict = Depends(require_project_scope("projects:write")),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    """删除剧本稿件。"""
    deleted = await store.delete_screenplay(screenplay_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="剧本不存在或已删除")
    return {"ok": True, "data": {"id": screenplay_id}}


@router.post("/projects/{project}/screenplays/{screenplay_id}/ai/{action}")
async def screenplay_ai_assist(
    project: str,
    screenplay_id: str,
    action: str,
    body: ScreenplayAiAssistRequest,
    user: dict = Depends(require_project_scope("projects:write")),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    """AI 辅助（polish/continue/normalize）。只返回文本，不自动写回稿件。"""
    if action not in SCREENPLAY_AI_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 AI 动作: {action}（可用: {'/'.join(SCREENPLAY_AI_ACTIONS)}）",
        )
    document = await _load_screenplay_or_404(store, screenplay_id)
    selection = (body.selection or "").strip()
    content = (document.get("content") or "").strip()

    if action == "continue":
        base = selection or content
        if not base:
            raise HTTPException(status_code=400, detail="剧本还没有内容，先写一点再续写")
        input_text = base[-CONTINUE_CONTEXT_CHARS:]
    else:
        input_text = selection or content
        if not input_text:
            raise HTTPException(
                status_code=400,
                detail="没有可处理的内容：剧本为空且未选中任何文本",
            )

    resolved = await resolve_project_scope(project, user, required_role="editor")
    ctx = getattr(resolved, "ctx", None)
    project_id = str(getattr(ctx, "project_id", "") or "")
    billing_user_id = _requester_user_id_for_billing(resolved, user)
    usage_meter = get_usage_meter()
    billing_context = {
        "source": "sync_api",
        "endpoint": "screenplay_ai_assist",
        "screenplay_id": screenplay_id,
        "action": action,
        "used_selection": bool(selection),
    }
    reservation = await usage_meter.reserve_feature_start_credits(
        user_id=billing_user_id,
        feature_key=SCREENPLAY_AI_FEATURE_KEY,
        product_surface="mainline",
        project_id=project_id,
        resource_kind="script",
        task_type=f"screenplay_ai_{action}",
        metadata=billing_context,
        require_price_rule=True,
        require_positive_cost=True,
    )
    reservation_id = str(reservation.get("id") or "")
    model_billing_metadata: dict[str, Any] = {
        "model_call_credit_policy": MODEL_CALL_CREDIT_POLICY_FEATURE_INCLUDED,
        "feature_key": SCREENPLAY_AI_FEATURE_KEY,
        "source": "sync_api",
        "action": action,
    }
    if reservation_id:
        model_billing_metadata.update(
            {
                "feature_credit_reservation_id": reservation_id,
                "feature_credit_charge_id": reservation_id,
                "feature_credit_cost": str(reservation.get("cost") or 0),
            }
        )

    from novelvideo.agents import screenplay_assistant

    try:
        usage_meter.set_llm_usage_context(
            billing_user_id,
            project_id=project_id,
            resource_kind="script",
            billing_metadata=model_billing_metadata,
        )
        instruction = (body.instruction or "").strip()
        if action == "polish":
            output_text = await screenplay_assistant.polish_screenplay_text(
                input_text, instruction=instruction
            )
        elif action == "continue":
            output_text = await screenplay_assistant.continue_screenplay_text(
                input_text, instruction=instruction
            )
        else:
            output_text = await screenplay_assistant.normalize_screenplay_headers(
                input_text
            )
    except Exception as exc:
        if reservation_id:
            try:
                await usage_meter.settle_cancelled_feature_credit_reservation(
                    reservation_id,
                    metadata={**billing_context, "error": str(exc)},
                )
            except Exception:
                logger.exception(
                    "Failed to settle interrupted screenplay AI credit reservation"
                )
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    finally:
        usage_meter.clear_llm_usage_context()

    if reservation_id:
        try:
            await usage_meter.settle_feature_credit_reservation(
                reservation_id,
                action="confirm",
                metadata=billing_context,
            )
        except Exception:
            logger.exception(
                "Screenplay AI assist succeeded but credit confirmation remains pending"
            )

    if not (output_text or "").strip():
        return {
            "ok": False,
            "error": "AI 未返回有效内容，请稍后重试或调整指令",
        }
    return {
        "ok": True,
        "data": {"action": action, "text": output_text, "used_selection": bool(selection)},
    }


@router.post("/projects/{project}/screenplays/{screenplay_id}/export-source")
async def export_screenplay_as_source(
    project: str,
    screenplay_id: str,
    user: dict = Depends(require_project_scope("projects:write")),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    """把剧本稿件落成 ``uploads/*.txt``，供标准导入流程（ingest/start）使用。

    不触发导入：导入是一个有计费与断点语义的后台任务，由前端拿到
    ``filename`` 后调用现有 ``POST /projects/{project}/ingest/start``。
    """
    resolved = await resolve_project_scope(project, user, required_role="editor")
    document = await _load_screenplay_or_404(store, screenplay_id)
    content = (document.get("content") or "").replace("\r\n", "\n").strip()
    if not content:
        raise HTTPException(status_code=400, detail="剧本内容为空，先写点东西再导出")

    billable_chars = count_billable_novel_chars(content)
    if billable_chars > MAX_NOVEL_IMPORT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"剧本共 {billable_chars:,} 字，超过单次导入上限 "
                f"{MAX_NOVEL_IMPORT_CHARS:,} 字。请拆分后再导出。"
            ),
        )

    project_dir = Path(resolved.project_dir)
    state_dir = resolved.state_dir
    uploads_dir = project_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    filename = _export_filename(document, uploads_dir)
    dest = uploads_dir / filename
    staging_dir = uploads_dir / ".staging"
    staging_dir.mkdir(exist_ok=True)
    staged_path = staging_dir / f"{filename}.{int(time.time() * 1000)}.tmp"
    try:
        staged_path.write_text(content, encoding="utf-8")
        os.replace(staged_path, dest)
    except OSError:
        logger.exception(
            "[%s] failed to persist screenplay source: %s", project, filename
        )
        raise HTTPException(status_code=500, detail="保存源文失败，请重试") from None
    finally:
        staged_path.unlink(missing_ok=True)

    # 与 ingest/start 的 drama 分支同一把尺子：项目类型是 narrated 时不做
    # 场次头硬校验（narrated 分支本来就不 block），只给章节数参考。
    from novelvideo.project_config import load_project_config_file_from_state_dir

    spine_template = str(
        load_project_config_file_from_state_dir(state_dir).get("spine_template")
        or "drama"
    ).strip()
    preview = build_chapter_preview(
        content,
        include_scene_blocks=spine_template != "narrated",
    )
    format_check = None
    if spine_template != "narrated":
        format_check = build_import_format_check(
            content,
            has_chapters=bool(preview.get("chapters")),
            chapters=preview.get("chapters"),
            require_scene_headers=True,
        )

    logger.info(
        "[%s] screenplay exported as source: %s (%s chars)",
        project,
        filename,
        billable_chars,
    )
    return {
        "ok": True,
        "data": {
            "filename": filename,
            "spine_template": spine_template,
            "char_count": billable_chars,
            "chapter_count": len(preview.get("chapters") or []),
            "format_check": format_check,
            "next": (
                f"调用 POST /api/v1/projects/{project}/ingest/start "
                f"(filename={filename}) 开始导入"
            ),
        },
    }


def _export_filename(document: dict, uploads_dir: Path) -> str:
    """从标题派生上传文件名；非法或冲突时回退到 id 派生（id 唯一）。"""
    title = str(document.get("title") or "").strip()
    candidate = sanitize_upload_filename(f"{title}.txt") if title else ""
    if (
        not candidate
        or not is_supported_novel_path(candidate)
        or not is_safe_upload_target(uploads_dir, candidate)
        or (uploads_dir / candidate).exists()
    ):
        candidate = f"screenplay-{document.get('id')}.txt"
    return candidate
