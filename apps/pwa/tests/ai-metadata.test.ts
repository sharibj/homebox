import { describe, expect, it } from "vitest";
import { aiRoles, needsEndpointDisclosure, providerLabel } from "../src/ai-metadata";
describe("public role-specific AI metadata", () => {
  it("keeps chat and vision providers and models separate", () => {
    const roles = aiRoles({ chat: { provider: "ollama", model: "chat-model" }, vision: { provider: "openai-compatible", model: "vision-model" } });
    expect(roles.chat?.model).toBe("chat-model");
    expect(roles.vision?.model).toBe("vision-model");
    expect(providerLabel(roles.chat!)).toBe("Ollama");
    expect(providerLabel(roles.vision!)).toBe("OpenAI-compatible");
  });
  it("accepts shared metadata or no metadata from older servers", () => {
    expect(aiRoles()).toEqual({});
    const shared = { provider: "ollama" as const, model: "shared-model" };
    expect(aiRoles(shared)).toEqual({ chat: shared, vision: shared });
  });
  it("names native Anthropic chat without changing Ollama vision", () => {
    const roles = aiRoles({ chat: { provider: "anthropic-messages", model: "claude-chat" }, vision: { provider: "ollama", model: "image-model" } });
    expect(providerLabel(roles.chat!)).toBe("Anthropic Messages");
    expect(needsEndpointDisclosure(roles.chat!)).toBe(true);
    expect(providerLabel(roles.vision!)).toBe("Ollama");
    expect(needsEndpointDisclosure(roles.vision!)).toBe(false);
  });
  it("does not mistake an unknown provider for Ollama or OpenAI", () => {
    const unknown = { provider: "future-provider" };
    expect(providerLabel(unknown)).toBe("Configured AI provider");
    expect(needsEndpointDisclosure(unknown)).toBe(true);
  });
  it("names Gemini vision without changing the separate Ollama chat role", () => {
    const roles = aiRoles({ chat: { provider: "ollama", model: "chat-unchanged" }, vision: { provider: "gemini", model: "gemini-3.1-flash-lite" } });
    expect(roles.chat?.model).toBe("chat-unchanged");
    expect(roles.vision?.model).toBe("gemini-3.1-flash-lite");
    expect(providerLabel(roles.chat!)).toBe("Ollama");
    expect(providerLabel(roles.vision!)).toBe("Gemini");
    expect(needsEndpointDisclosure(roles.chat!)).toBe(false);
    expect(needsEndpointDisclosure(roles.vision!)).toBe(true);
  });
  it("accepts legacy shared Gemini metadata with an operator-selected model string", () => {
    const shared = { provider: "gemini" as const, model: "operator-selected-gemini-model" };
    const roles = aiRoles(shared);
    expect(roles).toEqual({ chat: shared, vision: shared });
    expect(providerLabel(roles.chat!)).toBe("Gemini");
    expect(providerLabel(roles.vision!)).toBe("Gemini");
    expect(needsEndpointDisclosure(roles.chat!)).toBe(true);
    expect(needsEndpointDisclosure(roles.vision!)).toBe(true);
  });
});
