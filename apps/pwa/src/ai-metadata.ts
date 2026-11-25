import type { AiRole, PublicAi } from "./types";
export function aiRoles(ai?: PublicAi): { chat?: AiRole; vision?: AiRole } {
  if (!ai) return {};
  const shared = ai.provider && ai.model ? { provider: ai.provider, model: ai.model } : undefined;
  return { chat: ai.chat || shared, vision: ai.vision || shared };
}
export function providerLabel(role: { provider: string }): string {
  switch (role.provider) {
    case "ollama": return "Ollama";
    case "openai-compatible": return "OpenAI-compatible";
    case "anthropic-messages": return "Anthropic Messages";
    case "gemini": return "Gemini";
    default: return "Configured AI provider";
  }
}
export const needsEndpointDisclosure = (role: { provider: string }) => role.provider !== "ollama";
