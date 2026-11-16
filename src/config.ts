import type { PaperCollectorConfig } from "./types.ts";

const DEFAULTS: PaperCollectorConfig = {
  databaseId: "",
  parentPageId: "",
  model: "gpt-4.1",
  includeImages: true,
  mode: "agent",
};

export function parseConfig(raw?: Record<string, unknown>): PaperCollectorConfig {
  if (!raw) return { ...DEFAULTS };
  return {
    databaseId: typeof raw.databaseId === "string" ? raw.databaseId : DEFAULTS.databaseId,
    parentPageId:
      typeof raw.parentPageId === "string" ? raw.parentPageId : DEFAULTS.parentPageId,
    model: typeof raw.model === "string" ? raw.model : DEFAULTS.model,
    includeImages: typeof raw.includeImages === "boolean" ? raw.includeImages : DEFAULTS.includeImages,
    mode: raw.mode === "api" ? "api" : DEFAULTS.mode,
  };
}
