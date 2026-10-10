// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { jsonWithBackendError } from "@/lib/api-errors";
import { p } from "@/lib/api-path";
import { queryKeys } from "@/lib/query-keys";
import type { ErrorResponse, OkResponse } from "@/types/api";

// 剧本设计：项目级在线剧本 CRUD + AI 辅助 + 作为源文导入。
// 后端契约见 src/novelvideo/api/routes/screenplays.py。

export interface ScreenplaySummary {
  id: string;
  title: string;
  synopsis: string;
  status: string;
  char_count: number;
  created_at: string;
  updated_at: string;
}

export interface ScreenplayDocument extends ScreenplaySummary {
  content: string;
}

export interface ScreenplayUpdateInput {
  title?: string;
  synopsis?: string;
  content?: string;
  status?: "draft" | "final";
}

export type ScreenplayAiAction = "polish" | "continue" | "normalize";

export interface ScreenplayAiResult {
  action: ScreenplayAiAction;
  text: string;
  used_selection: boolean;
}

export interface ScreenplayExportResult {
  filename: string;
  spine_template: string;
  char_count: number;
  chapter_count: number;
  format_check: {
    level: string;
    summary: string;
    issues?: unknown[];
  } | null;
  next: string;
}

export function useScreenplays(project: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.screenplays(project),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/projects/${project}/screenplays`, { signal })
        .json<OkResponse<ScreenplaySummary[]>>(),
    enabled: !!project && enabled,
  });
}

export function useScreenplay(project: string, screenplayId: string) {
  return useQuery({
    queryKey: queryKeys.screenplay(project, screenplayId),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/projects/${project}/screenplays/${screenplayId}`, { signal })
        .json<OkResponse<ScreenplayDocument>>(),
    enabled: !!project && !!screenplayId,
  });
}

export function useCreateScreenplay(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { title: string; synopsis: string }) => {
      const response = await jsonWithBackendError<
        OkResponse<ScreenplayDocument> | ErrorResponse
      >(
        api.post(p`api/v1/projects/${project}/screenplays`, {
          json: input,
          throwHttpErrors: false,
        }),
      );
      if (!response.ok) {
        throw new Error(response.error);
      }
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.screenplays(project),
      });
    },
  });
}

export function useUpdateScreenplay(project: string, screenplayId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ScreenplayUpdateInput) => {
      const response = await jsonWithBackendError<
        OkResponse<ScreenplayDocument> | ErrorResponse
      >(
        api.put(p`api/v1/projects/${project}/screenplays/${screenplayId}`, {
          json: input,
          throwHttpErrors: false,
        }),
      );
      if (!response.ok) {
        throw new Error(response.error);
      }
      return response.data;
    },
    onSuccess: (document) => {
      queryClient.setQueryData(
        queryKeys.screenplay(project, screenplayId),
        { ok: true, data: document },
      );
      queryClient.invalidateQueries({
        queryKey: queryKeys.screenplays(project),
      });
    },
  });
}

export function useDeleteScreenplay(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (screenplayId: string) => {
      const response = await jsonWithBackendError<OkResponse<unknown> | ErrorResponse>(
        api.delete(p`api/v1/projects/${project}/screenplays/${screenplayId}`, {
          throwHttpErrors: false,
        }),
      );
      if (!response.ok) {
        throw new Error(response.error);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.screenplays(project),
      });
    },
  });
}

export function useScreenplayAiAssist(project: string, screenplayId: string) {
  return useMutation({
    mutationFn: async (input: {
      action: ScreenplayAiAction;
      selection?: string;
      instruction?: string;
    }) => {
      const response = await jsonWithBackendError<
        OkResponse<ScreenplayAiResult> | ErrorResponse
      >(
        api.post(
          p`api/v1/projects/${project}/screenplays/${screenplayId}/ai/${input.action}`,
          {
            json: {
              selection: input.selection ?? "",
              instruction: input.instruction ?? "",
            },
            throwHttpErrors: false,
            timeout: 180_000, // 同步 LLM 调用，放宽 ky 默认 30s 超时
          },
        ),
      );
      if (!response.ok) {
        throw new Error(response.error);
      }
      return response.data;
    },
  });
}

export function useExportScreenplaySource(project: string, screenplayId: string) {
  return useMutation({
    mutationFn: async () => {
      const response = await jsonWithBackendError<
        OkResponse<ScreenplayExportResult> | ErrorResponse
      >(
        api.post(
          p`api/v1/projects/${project}/screenplays/${screenplayId}/export-source`,
          { json: {}, throwHttpErrors: false },
        ),
      );
      if (!response.ok) {
        throw new Error(response.error);
      }
      return response.data;
    },
  });
}
