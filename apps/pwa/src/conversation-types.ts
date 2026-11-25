import type { Action, Entity } from "./types";
export interface ConversationScope {
  instanceId: string;
  instanceLabel: string;
  collectionId: string;
  collectionName: string;
  contextVersion?: number | string;
}
export interface ConversationChoice {
  id: string;
  revision: number;
  slot: string;
  reason: string;
  candidates: (Entity & { path?: string; kind?: string; candidateId?: string })[];
}
export interface ConversationReply {
  message: string;
  scope?: ConversationScope;
  outcome?: string;
  revision?: number;
  items: Entity[];
  destinations?: (Entity & { path?: string; kind?: string })[];
  collections?: { id: string; name: string }[];
  overview?: { itemCount: number; locationCount: number; includesArchived: boolean };
  pagination?: { totalRecords?: number; nextCursor?: string | null; complete?: boolean };
  clarification?: ConversationChoice;
  action?: Action;
}
export interface ConversationMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  status: "sending" | "complete" | "failed";
  reply?: ConversationReply;
  error?: string;
}
export interface ConversationState {
  messages: ConversationMessage[];
  composer: string;
  sending: boolean;
  recovering: boolean;
  issue: string;
  scope: ConversationScope | null;
  clarification: ConversationChoice | null;
  task: { summary: string; fields: Record<string, unknown> } | null;
  linkedActionId: string | null;
  editing: boolean;
  unsettled: boolean;
  restoreAvailable: boolean;
  revision: number;
}
