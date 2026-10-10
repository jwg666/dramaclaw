// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { FileText, Loader2, Plus, Trash2 } from "lucide-react";

import {
  useCreateScreenplay,
  useDeleteScreenplay,
  useScreenplays,
  type ScreenplaySummary,
} from "@/lib/queries/screenplays";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

export const SCREENPLAYS_INPUT_CLASS =
  "h-9 rounded-[8px] border-white/10 bg-white/[0.025] px-3 text-sm shadow-none placeholder:text-muted-foreground/60 focus-visible:border-white/20 focus-visible:ring-2 focus-visible:ring-white/8 dark:bg-white/[0.025]";

function ScreenplaysPage() {
  const { t } = useTranslation();
  const { project } = Route.useParams();
  const { data: response, isLoading } = useScreenplays(project);
  const createMutation = useCreateScreenplay(project);
  const deleteMutation = useDeleteScreenplay(project);

  const [createOpen, setCreateOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newSynopsis, setNewSynopsis] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<ScreenplaySummary | null>(null);

  const items = response?.data ?? [];

  const handleCreate = async () => {
    if (!newTitle.trim()) return;
    try {
      const document = await createMutation.mutateAsync({
        title: newTitle.trim(),
        synopsis: newSynopsis.trim(),
      });
      toast.success(t("screenplays.createSuccess"));
      setCreateOpen(false);
      setNewTitle("");
      setNewSynopsis("");
      await Route.navigate({
        to: "/projects/$project/screenplays/$screenplayId",
        params: { project, screenplayId: document.id },
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("screenplays.createFailed"));
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteMutation.mutateAsync(deleteTarget.id);
      toast.success(t("screenplays.deleteSuccess"));
      setDeleteTarget(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("screenplays.deleteFailed"));
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-4 py-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-foreground">
            {t("screenplays.title")}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {t("screenplays.subtitle")}
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)} size="sm">
          <Plus className="mr-1 size-4" />
          {t("screenplays.create")}
        </Button>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />
          {t("screenplays.loading")}
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-[12px] border border-dashed border-white/10 py-16 text-center">
          <FileText className="size-8 text-muted-foreground/50" />
          <p className="text-sm text-muted-foreground">{t("screenplays.empty")}</p>
          <Button variant="outline" size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="mr-1 size-4" />
            {t("screenplays.createFirst")}
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {items.map((item) => (
            <div
              key={item.id}
              className={cn(
                "group relative rounded-[12px] border border-white/10 bg-white/[0.025] p-4",
                "transition-colors hover:border-white/20 hover:bg-white/[0.04]",
              )}
            >
              <Link
                to="/projects/$project/screenplays/$screenplayId"
                params={{ project, screenplayId: item.id }}
                className="block"
              >
                <div className="flex items-center gap-2">
                  <FileText className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate text-sm font-semibold text-foreground">
                    {item.title}
                  </span>
                  {item.status === "final" && (
                    <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-400">
                      {t("screenplays.statusFinal")}
                    </span>
                  )}
                </div>
                {item.synopsis && (
                  <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
                    {item.synopsis}
                  </p>
                )}
                <div className="mt-3 flex items-center gap-3 text-[11px] text-muted-foreground/70">
                  <span>{t("screenplays.charCount", { count: item.char_count })}</span>
                  <span>{item.updated_at.slice(0, 16).replace("T", " ")}</span>
                </div>
              </Link>
              <button
                type="button"
                aria-label={t("screenplays.delete")}
                onClick={() => setDeleteTarget(item)}
                className="absolute right-3 top-3 rounded p-1 text-muted-foreground/50 opacity-0 transition-opacity hover:bg-red-500/10 hover:text-red-400 focus-visible:opacity-100 group-hover:opacity-100"
              >
                <Trash2 className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* 新建剧本 */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("screenplays.createTitle")}</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-4 py-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="screenplay-title">{t("screenplays.titleLabel")}</Label>
              <Input
                id="screenplay-title"
                value={newTitle}
                onChange={(event) => setNewTitle(event.target.value)}
                placeholder={t("screenplays.titlePlaceholder")}
                className={SCREENPLAYS_INPUT_CLASS}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="screenplay-synopsis">{t("screenplays.synopsisLabel")}</Label>
              <Textarea
                id="screenplay-synopsis"
                value={newSynopsis}
                onChange={(event) => setNewSynopsis(event.target.value)}
                placeholder={t("screenplays.synopsisPlaceholder")}
                className="min-h-20 resize-none"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setCreateOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              size="sm"
              disabled={!newTitle.trim() || createMutation.isPending}
              onClick={handleCreate}
            >
              {createMutation.isPending && <Loader2 className="mr-1 size-4 animate-spin" />}
              {t("common.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 删除确认 */}
      <Dialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{t("screenplays.deleteTitle")}</DialogTitle>
          </DialogHeader>
          <p className="py-2 text-sm text-muted-foreground">
            {t("screenplays.deleteMessage", { title: deleteTarget?.title ?? "" })}
          </p>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              size="sm"
              disabled={deleteMutation.isPending}
              onClick={handleDelete}
            >
              {deleteMutation.isPending && <Loader2 className="mr-1 size-4 animate-spin" />}
              {t("screenplays.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export const Route = createFileRoute("/_app/projects/$project/screenplays")({
  component: ScreenplaysPage,
});
