import type { AiRole, AiSettings, AiSettingsRole, AiSettingsRoleUpdate, AiSettingsUpdate, Session } from "./types";

export const aiProviders: AiRole["provider"][] = ["ollama", "openai-compatible", "anthropic-messages", "gemini"];
export const aiRoleNames = ["chat", "vision"] as const;
export type AiSettingsDraftRole = Omit<AiSettingsRole, "keyConfigured"> & {
  keyAction: "keep" | "replace" | "clear"; newKey: string;
};
export type AiSettingsDraft = Record<typeof aiRoleNames[number], AiSettingsDraftRole>;
export const canManageAiSettings = (session: Session | null) => session?.capabilities?.canManageAISettings === true;

export function settingsDraft(saved: AiSettings): AiSettingsDraft {
  const role = (value: AiSettingsRole): AiSettingsDraftRole => ({
    provider: value.provider, baseUrl: value.baseUrl, model: value.model, keyAction: "keep", newKey: "",
  });
  return { chat: role(saved.chat), vision: role(saved.vision) };
}
export function settingsDirty(saved: AiSettings, draft: AiSettingsDraft): boolean {
  return aiRoleNames.some(name => {
    const before = saved[name], after = draft[name];
    return after.provider !== before.provider || after.baseUrl !== before.baseUrl
      || after.model !== before.model || after.keyAction !== "keep" || after.newKey !== "";
  });
}
export function canonicalEndpoint(value: string): string | null {
  try {
    const url = new URL(value.trim());
    if (!["http:", "https:"].includes(url.protocol) || !url.hostname
      || url.username || url.password || url.search || url.hash) return null;
    return url.href;
  } catch { return null; }
}
export function endpointChanged(saved: AiSettingsRole, draft: AiSettingsDraftRole): boolean {
  return saved.provider !== draft.provider || canonicalEndpoint(saved.baseUrl) !== canonicalEndpoint(draft.baseUrl);
}
export function roleIssue(saved: AiSettingsRole, draft: AiSettingsDraftRole): string {
  if (!aiProviders.includes(draft.provider)) return "Choose a supported provider.";
  if (!draft.baseUrl.trim() || draft.baseUrl.length > 2048 || !canonicalEndpoint(draft.baseUrl))
    return "Enter an HTTP or HTTPS base URL without credentials, a query, or a fragment.";
  if (!draft.model.trim() || draft.model.length > 256) return "Enter an exact model ID, up to 256 characters.";
  if (endpointChanged(saved, draft) && draft.keyAction === "keep")
    return "The provider or endpoint changed. Explicitly replace or clear its API key before saving.";
  if (draft.keyAction === "replace" && (!draft.newKey.trim() || draft.newKey.length > 4096))
    return "Enter a replacement API key, up to 4096 characters, or choose another key action.";
  return "";
}
export function settingsUpdate(saved: AiSettings, draft: AiSettingsDraft): AiSettingsUpdate {
  const role = (name: typeof aiRoleNames[number]): AiSettingsRoleUpdate => {
    const value = draft[name], issue = roleIssue(saved[name], value);
    if (issue) throw new Error(issue);
    const fields = { provider: value.provider, baseUrl: value.baseUrl.trim(), model: value.model.trim() };
    return value.keyAction === "replace"
      ? { ...fields, keyAction: "replace", apiKey: value.newKey }
      : { ...fields, keyAction: value.keyAction };
  };
  return { revision: saved.revision, chat: role("chat"), vision: role("vision") };
}
export function readSettings(value: unknown): AiSettings {
  const data = value as AiSettings | null;
  if (!data || !Number.isSafeInteger(data.revision) || data.revision < 0
    || !aiRoleNames.every(name => {
      const role = data[name];
      return role && aiProviders.includes(role.provider) && typeof role.baseUrl === "string"
        && typeof role.model === "string" && typeof role.keyConfigured === "boolean";
    })) throw new Error("Invalid AI settings response.");
  // Keep only the public fields, never response extensions or credential material.
  const role = (item: AiSettingsRole): AiSettingsRole => ({
    provider: item.provider, baseUrl: item.baseUrl, model: item.model, keyConfigured: item.keyConfigured,
  });
  return { revision: data.revision, chat: role(data.chat), vision: role(data.vision) };
}
