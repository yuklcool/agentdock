import { API_BASE } from "./base";
import { ApiError } from "./client";
import type { ApiErrorBody, Skill } from "./types";

export type ArchiveDiscoveredSkill = {
  subpath: string;
  name: string;
  description: string;
  valid: boolean;
  error: string | null;
  installed: boolean;
  bundle_size: number | null;
};

export type SkillArchiveDiscoverResponse = {
  ok: boolean;
  truncated: boolean;
  skills: ArchiveDiscoveredSkill[];
};

export type SkillArchiveImportResponse = {
  ok: boolean;
  imported: Skill[];
};

async function postArchive<T>(path: string, file: File): Promise<T> {
  const response = await fetch(API_BASE + path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  const text = await response.text();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : undefined;
  } catch {
    parsed = undefined;
  }
  if (!response.ok) {
    const envelope = parsed as ApiErrorBody | undefined;
    if (envelope?.error?.code) {
      throw new ApiError(
        envelope.error.code,
        envelope.error.message,
        response.status,
        envelope.error.field,
      );
    }
    throw new ApiError(
      `http_${response.status}`,
      text || response.statusText,
      response.status,
    );
  }
  return parsed as T;
}

export function discoverSkillArchive(file: File) {
  const params = new URLSearchParams({ filename: file.name });
  return postArchive<SkillArchiveDiscoverResponse>(
    `/v1/skills/archive-discover?${params.toString()}`,
    file,
  );
}

export function importSkillArchive(file: File, selected: string[]) {
  const params = new URLSearchParams({ filename: file.name });
  for (const subpath of selected) params.append("selected", subpath);
  return postArchive<SkillArchiveImportResponse>(
    `/v1/skills/archive-import?${params.toString()}`,
    file,
  );
}
