// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createLazyFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  ArrowLeft,
  Check,
  FileUp,
  Loader2,
  ListOrdered,
  Save,
  Sparkles,
  Wand2,
} from "lucide-react";

import {
  useExportScreenplaySource,
  useScreenplay,
  useScreenplayAiAssist,
  useUpdateScreenplay,
  type ScreenplayAiAction,
} from "@/lib/queries/screenplays";
import { useStartIngest } from "@/lib/queries/ingest";
import { backendErrorToastMessage } from "@/lib/api-errors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
// 与列表页同款输入框样式（各自持有常量，避免 lazy chunk 反向依赖非 lazy 路由模块）。
const EDITOR_INPUT_CLASS =
  "h-9 rounded-[8px] border-white/10 bg-white/[0.025] px-3 text-sm shadow-none placeholder:text-muted-foreground/60 focus-visible:border-white/20 focus-visible:ring-2 focus-visible:ring-white/8 dark:bg-white/[0.025]";

// 场次头约定（与后端 screenplay_normalizer / format check 同一约定）：
// 「第N场 地点 时间」，N 支持汉字数字。仅用于统计展示，不做硬校验。
const SCENE_HEADER_RE = /^[ \t]*第[0-9〇零一二三四五六七八九十百千]+场/gm;

function countScenes(content: string): number {
  const matches = content.match(SCENE_HEADER_RE);
  return matches ? matches.length : 0;
}

type AiResultDialogState = {
  action: ScreenplayAiAction;
  text: string;
  usedSelection: boolean;
} | null;

function ScreenplayEditorPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { project, screenplayId } = Route.useParams();

  const { data: response, isLoading } = useScreenplay(project, screenplayId);
  const updateMutation = useUpdateScreenplay(project, screenplayId);
  const aiMutation = useScreenplayAiAssist(project, screenplayId);
  const exportMutation = useExportScreenplaySource(project, screenplayId);
  const startIngest = useStartIngest(project);

  const [title, setTitle] = useState("");
  const [synopsis, setSynopsis] = useState("");
  const [content, setContent] = useState("");
  const [hydrated, setHydrated] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [aiDialog, setAiDialog] = useState<AiResultDialogState>(null);
  const [exportDialog, setExportDialog] = useState(false);
  const [exportResult, setExportResult] = useState<Awaited<
    ReturnType<typeof exportMutation.mutateAsync>
  > | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const selectionRef = useRef<{ start: number; end: number }>({ start: 0, end: 0 });
  const lastSavedRef = useRef<string>("");
  const hydratedRef = useRef(false);

  // 初次加载灌入本地状态（之后用户编辑不再被服务端数据覆盖）
  useEffect(() => {
    const document = response?.data;
    if (!document || hydratedRef.current) return;
    hydratedRef.current = true;
    setTitle(document.title);
    setSynopsis(document.synopsis);
    setContent(document.content);
    lastSavedRef.current = JSON.stringify([
      document.title,
      document.synopsis,
      document.content,
    ]);
    setHydrated(true);
  }, [response]);

  // 自动保存：1.5s 防抖；内容与上次保存快照一致时不发请求
  useEffect(() => {
    if (!hydrated) return;
    const snapshot = JSON.stringify([title, synopsis, content]);
    if (snapshot === lastSavedRef.current) return;
    const timer = window.setTimeout(async () => {
      setSaveState("saving");
      try {
        const saved = await updateMutation.mutateAsync({
          title: title.trim() || undefined,
          synopsis,
          content,
        });
        lastSavedRef.current = JSON.stringify([
          saved.title,
          saved.synopsis,
          saved.content,
        ]);
        setSaveState("saved");
      } catch (error) {
        setSaveState("idle");
        toast.error(
          backendErrorToastMessage(error, t("screenplays.saveFailed")),
        );
      }
    }, 1500);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mutateAsync 引用稳定，防抖依赖编辑内容
  }, [title, synopsis, content, hydrated]);

  const charCount = content.length;
  const sceneCount = useMemo(() => countScenes(content), [content]);

  const captureSelection = useCallback(() => {
    const element = textareaRef.current;
    if (!element) return;
    selectionRef.current = {
      start: element.selectionStart,
      end: element.selectionEnd,
    };
  }, []);

  const currentSelection = () => {
    const { start, end } = selectionRef.current;
    if (end > start) return content.slice(start, end);
    return "";
  };

  const handleAi = async (action: ScreenplayAiAction) => {
    captureSelection();
    const selection = currentSelection();
    try {
      const result = await aiMutation.mutateAsync({
        action,
        selection,
        instruction: "",
      });
      setAiDialog({
        action,
        text: result.text,
        usedSelection: result.used_selection,
      });
    } catch (error) {
      toast.error(backendErrorToastMessage(error, t("screenplays.ai.failed")));
    }
  };

  const applyAiResult = () => {
    if (!aiDialog) return;
    const { action, text, usedSelection } = aiDialog;
    if (action === "continue") {
      setContent((previous) => `${previous.replace(/\s+$/, "")}\n\n${text}`);
    } else if (usedSelection) {
      const { start, end } = selectionRef.current;
      if (end > start && end <= content.length) {
        setContent(content.slice(0, start) + text + content.slice(end));
      } else {
        setContent(text);
      }
    } else {
      setContent(text);
    }
    setAiDialog(null);
    toast.success(t("screenplays.ai.applied"));
  };

  const handleManualSave = async () => {
    setSaveState("saving");
    try {
      const saved = await updateMutation.mutateAsync({ title, synopsis, content });
      lastSavedRef.current = JSON.stringify([
        saved.title,
        saved.synopsis,
        saved.content,
      ]);
      setSaveState("saved");
      toast.success(t("screenplays.saved"));
    } catch (error) {
      setSaveState("idle");
      toast.error(backendErrorToastMessage(error, t("screenplays.saveFailed")));
    }
  };

  const handleExportSource = async () => {
    try {
      const result = await exportMutation.mutateAsync();
      setExportResult(result);
      setExportDialog(true);
    } catch (error) {
      toast.error(backendErrorToastMessage(error, t("screenplays.exportFailed")));
    }
  };

  const handleStartIngest = async () => {
    if (!exportResult) return;
    try {
      const task = await startIngest.mutateAsync({
        filename: exportResult.filename,
        rebuild: false,
      });
      toast.success(task.message || t("screenplays.exportStarted"));
      setExportDialog(false);
      await navigate({
        to: "/projects/$project/tasks",
        params: { project },
      });
    } catch (error) {
      toast.error(backendErrorToastMessage(error, t("screenplays.exportStartFailed")));
    }
  };

  if (isLoading || !hydrated) {
    return (
      <div className="flex items-center justify-center py-24 text-muted-foreground">
        <Loader2 className="mr-2 size-4 animate-spin" />
        {t("screenplays.loading")}
      </div>
    );
  }

  const aiPending = aiMutation.isPending;
  const exporting = exportMutation.isPending || startIngest.isPending;

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-4 py-6">
      {/* 顶栏 */}
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="ghost"
          size="sm"
          onClick={() =>
            navigate({ to: "/projects/$project/screenplays", params: { project } })
          }
        >
          <ArrowLeft className="mr-1 size-4" />
          {t("screenplays.back")}
        </Button>
        <Input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder={t("screenplays.titlePlaceholder")}
          className={EDITOR_INPUT_CLASS + " h-9 max-w-xs font-semibold"}
        />
        <span className="text-xs text-muted-foreground/70">
          {saveState === "saving"
            ? t("screenplays.saving")
            : saveState === "saved"
              ? t("screenplays.saved")
              : ""}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-muted-foreground/70">
            {t("screenplays.stats", { chars: charCount, scenes: sceneCount })}
          </span>
          <Button variant="outline" size="sm" onClick={handleManualSave}>
            <Save className="mr-1 size-4" />
            {t("screenplays.save")}
          </Button>
        </div>
      </div>

      {/* 简介与 AI 工具行 */}
      <Input
        value={synopsis}
        onChange={(event) => setSynopsis(event.target.value)}
        placeholder={t("screenplays.synopsisPlaceholder")}
        className={EDITOR_INPUT_CLASS}
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={aiPending}
          onClick={() => handleAi("polish")}
        >
          {aiPending ? (
            <Loader2 className="mr-1 size-4 animate-spin" />
          ) : (
            <Sparkles className="mr-1 size-4" />
          )}
          {t("screenplays.ai.polish")}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={aiPending}
          onClick={() => handleAi("continue")}
        >
          {aiPending ? (
            <Loader2 className="mr-1 size-4 animate-spin" />
          ) : (
            <Wand2 className="mr-1 size-4" />
          )}
          {t("screenplays.ai.continue")}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={aiPending}
          onClick={() => handleAi("normalize")}
        >
          {aiPending ? (
            <Loader2 className="mr-1 size-4 animate-spin" />
          ) : (
            <ListOrdered className="mr-1 size-4" />
          )}
          {t("screenplays.ai.normalize")}
        </Button>
        <div className="ml-auto">
          <Button size="sm" disabled={exporting} onClick={handleExportSource}>
            {exporting ? (
              <Loader2 className="mr-1 size-4 animate-spin" />
            ) : (
              <FileUp className="mr-1 size-4" />
            )}
            {t("screenplays.exportSource")}
          </Button>
        </div>
      </div>

      {/* 正文 */}
      <textarea
        ref={textareaRef}
        value={content}
        onChange={(event) => setContent(event.target.value)}
        onSelect={captureSelection}
        onKeyUp={captureSelection}
        onMouseUp={captureSelection}
        placeholder={t("screenplays.contentPlaceholder")}
        spellCheck={false}
        className="min-h-[560px] w-full resize-y rounded-[12px] border border-white/10 bg-white/[0.02] p-4 font-mono text-sm leading-relaxed shadow-none placeholder:text-muted-foreground/50 focus-visible:border-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/8"
      />

      {/* AI 结果 */}
      <Dialog
        open={aiDialog !== null}
        onOpenChange={(open) => !open && setAiDialog(null)}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {aiDialog
                ? t(`screenplays.ai.${aiDialog.action}`) +
                  (aiDialog.usedSelection ? t("screenplays.ai.scopeSelection") : "")
                : ""}
            </DialogTitle>
          </DialogHeader>
          <textarea
            readOnly
            value={aiDialog?.text ?? ""}
            className="min-h-64 w-full resize-y rounded-[8px] border border-white/10 bg-white/[0.02] p-3 font-mono text-sm leading-relaxed"
          />
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setAiDialog(null)}>
              {t("screenplays.ai.discard")}
            </Button>
            <Button size="sm" onClick={applyAiResult}>
              <Check className="mr-1 size-4" />
              {t("screenplays.ai.apply")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 导出为源文 → 确认后触发标准导入 */}
      <Dialog open={exportDialog} onOpenChange={setExportDialog}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("screenplays.exportTitle")}</DialogTitle>
          </DialogHeader>
          {exportResult && (
            <div className="flex flex-col gap-2 py-2 text-sm">
              <p>
                {t("screenplays.exportFileInfo", {
                  filename: exportResult.filename,
                  chars: exportResult.char_count,
                  chapters: exportResult.chapter_count,
                })}
              </p>
              {exportResult.format_check &&
                exportResult.format_check.level !== "ok" && (
                  <p className="rounded-[8px] border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-300">
                    {exportResult.format_check.summary}
                  </p>
                )}
              <p className="text-xs text-muted-foreground">
                {t("screenplays.exportNextHint")}
              </p>
            </div>
          )}
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setExportDialog(false)}>
              {t("common.cancel")}
            </Button>
            <Button size="sm" disabled={exporting} onClick={handleStartIngest}>
              {startIngest.isPending && (
                <Loader2 className="mr-1 size-4 animate-spin" />
              )}
              {t("screenplays.exportStartIngest")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export const Route = createLazyFileRoute(
  "/_app/projects/$project/screenplays/$screenplayId",
)({
  component: ScreenplayEditorPage,
});
