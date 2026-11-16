import fs from "node:fs";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import type { TSchema } from "typebox";
import { parseConfig } from "./src/config.ts";
import {
  createNotionSavePaperTool,
  createNotionBatchSaveTool,
  createNotionSetupTool,
  createExtractPageImagesTool,
  createExtractPaperFiguresTool,
  createNotionWritePageTool,
  createNotionCreateChildPageTool,
} from "./src/notion-tools.ts";
import { createGenerateSummaryTool } from "./src/api-mode-tools.ts";

function resolveNotionToken(): string {
  const keyFile = process.env.NOTION_API_TOKEN_FILE;
  if (keyFile) {
    return fs.readFileSync(keyFile, "utf8").trim();
  }
  return process.env.NOTION_API_TOKEN?.trim() ?? "";
}

const schemas = {
  type: "object",
  additionalProperties: false,
  properties: {
    databaseId: { type: "string", description: "Paper Collection database ID." },
    parentPageId: { type: "string", description: "Parent page for explicit collection setup." },
    mode: { type: "string", enum: ["agent", "api"], default: "agent" },
    model: { type: "string", default: "gpt-4.1", description: "Provider model ID for API mode." },
    includeImages: { type: "boolean", default: true },
  },
};

const factories = [
  { create: createNotionSavePaperTool },
  { create: createNotionBatchSaveTool },
  { create: createNotionSetupTool },
  { create: createExtractPageImagesTool },
  { create: createExtractPaperFiguresTool },
  { create: createNotionWritePageTool },
  { create: createNotionCreateChildPageTool },
  { create: createGenerateSummaryTool, apiModeOnly: true },
];

const metadataSettings = {
  config: parseConfig(),
  notionToken: "",
  logger: { info() {}, warn() {}, error() {} },
};

export default defineToolPlugin({
  id: "paper-collector",
  name: "Paper Collector",
  description: "Save requested research paper metadata and generate optional Notion write-ups.",
  activation: { onStartup: true, onCapabilities: ["tool"] },
  configSchema: schemas as unknown as TSchema,
  tools: (tool) => factories.map(({ create, apiModeOnly }) => {
    const { name, label, description, parameters } = create(metadataSettings);
    return tool({
      name, label, description, parameters: parameters as unknown as TSchema,
      workspaceAccess: false,
      factory({ api }) {
        const config = parseConfig(api.pluginConfig);
        if (apiModeOnly && config.mode !== "api") return null;
        let notionToken = "";
        try {
          notionToken = resolveNotionToken();
        } catch {
          api.logger.warn("Paper Collector could not read the configured Notion token file");
        }
        return create({ config, notionToken, logger: api.logger });
      },
    });
  }),
});
