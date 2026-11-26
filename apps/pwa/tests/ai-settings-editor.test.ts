import { describe, expect, it } from "vitest";
import { canManageAiSettings, canonicalEndpoint, endpointChanged, readSettings, roleIssue, settingsDirty, settingsDraft, settingsUpdate } from "../src/ai-settings-editor";
import type { AiSettings, Session } from "../src/types";

function saved(): AiSettings {
  return { revision: 7, chat: { provider: "openai-compatible", baseUrl: "https://example.test/v1", model: "custom-chat", keyConfigured: true },
    vision: { provider: "ollama", baseUrl: "http://ollama:11434", model: "custom-vision", keyConfigured: false } };
}
describe("AI settings editor", () => {
  it("uses only the explicit server capability, never owner, superuser, or user identity", () => {
    expect(canManageAiSettings(null)).toBe(false);
    expect(canManageAiSettings({ user: { id: "first", isSuperuser: true }, isOwner: true } as unknown as Session)).toBe(false);
    expect(canManageAiSettings({ capabilities: { canManageAISettings: false } } as Session)).toBe(false);
    expect(canManageAiSettings({ capabilities: { canManageAISettings: true } } as Session)).toBe(true);
  });
  it("starts with keep and an empty write-only key, independently for both roles", () => {
    const source = saved(), draft = settingsDraft(source);
    expect(draft.chat.keyAction).toBe("keep"); expect(draft.chat.newKey).toBe("");
    expect(draft.vision.keyAction).toBe("keep"); expect(draft.vision.newKey).toBe("");
    expect(settingsDirty(source, draft)).toBe(false);
    draft.chat.model = "exact/new:model";
    expect(settingsDirty(source, draft)).toBe(true); expect(source.chat.model).toBe("custom-chat");
  });
  it("allows model-only changes to keep the existing key without emitting any key field", () => {
    const source = saved(), draft = settingsDraft(source); draft.chat.model = "new-exact-model";
    const body = settingsUpdate(source, draft);
    expect(body.revision).toBe(7); expect(body.chat.keyAction).toBe("keep");
    expect(body.chat).not.toHaveProperty("apiKey"); expect(body.vision).not.toHaveProperty("apiKey");
    expect(body).not.toHaveProperty("keyConfigured");
  });
  it("requires explicit key clear or replacement after provider, path, scheme, or effective-port changes", () => {
    for (const change of [{ provider: "gemini" }, { baseUrl: "https://example.test/other" }, { baseUrl: "http://example.test/v1" }, { baseUrl: "https://example.test:8443/v1" }]) {
      const source = saved(), draft = settingsDraft(source); Object.assign(draft.chat, change);
      expect(endpointChanged(source.chat, draft.chat)).toBe(true);
      expect(roleIssue(source.chat, draft.chat)).toContain("replace or clear");
      expect(() => settingsUpdate(source, draft)).toThrow("replace or clear");
      draft.chat.keyAction = "clear"; expect(settingsUpdate(source, draft).chat).not.toHaveProperty("apiKey");
    }
  });
  it("requires explicit clear even when an endpoint previously had no key", () => {
    const source = saved(), draft = settingsDraft(source); draft.vision.baseUrl = "http://another:11434";
    expect(roleIssue(source.vision, draft.vision)).toContain("replace or clear");
  });
  it("normalizes host, default port and root URL, but distinguishes complete paths", () => {
    expect(canonicalEndpoint(" HTTP://EXAMPLE.TEST:80 ")).toBe(canonicalEndpoint("http://example.test/"));
    expect(canonicalEndpoint("https://example.test/v1")).not.toBe(canonicalEndpoint("https://example.test/v2"));
    for (const url of ["ftp://example.test", "https://u:p@example.test", "https://example.test/?key=not-a-real-key", "https://example.test/#x", "bad"])
      expect(canonicalEndpoint(url)).toBeNull();
  });
  it("never treats a blank key as clear and includes apiKey only for explicit replace", () => {
    const source = saved(), draft = settingsDraft(source); draft.chat.keyAction = "replace";
    expect(() => settingsUpdate(source, draft)).toThrow("replacement API key");
    draft.chat.newKey = "synthetic-write-only-value";
    const body = settingsUpdate(source, draft);
    expect(body.chat.apiKey).toBe(draft.chat.newKey); expect(body.vision).not.toHaveProperty("apiKey");
    draft.chat.keyAction = "keep";
    expect(settingsUpdate(source, draft).chat).not.toHaveProperty("apiKey");
    draft.chat.keyAction = "clear";
    expect(settingsUpdate(source, draft).chat).not.toHaveProperty("apiKey");
  });
  it("rejects empty or over-bound fields before constructing an atomic two-role save", () => {
    const source = saved(), draft = settingsDraft(source);
    for (const model of ["", " ".repeat(3), "x".repeat(257)]) {
      draft.vision.model = model; expect(() => settingsUpdate(source, draft)).toThrow("model ID");
    }
    draft.vision.model = "vision";
    draft.chat.keyAction = "replace"; draft.chat.newKey = "x".repeat(4097);
    expect(() => settingsUpdate(source, draft)).toThrow("4096");
    draft.chat.newKey = "synthetic"; draft.vision.baseUrl = "https://example.test/" + "x".repeat(2048);
    expect(() => settingsUpdate(source, draft)).toThrow("base URL");
  });
  it("copies only redacted public response fields and rejects malformed revision or roles", () => {
    const source = saved();
    expect(readSettings({ ...source, apiKey: "synthetic", chat: { ...source.chat, apiKey: "synthetic" } })).toEqual(source);
    for (const value of [null, {}, { ...source, revision: -1 }, { ...source, revision: 1.5 }, { ...source, chat: { ...source.chat, keyConfigured: "yes" } }])
      expect(() => readSettings(value)).toThrow("Invalid AI settings response");
  });
});
