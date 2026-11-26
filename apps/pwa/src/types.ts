export interface AiRole { provider: "ollama" | "openai-compatible" | "anthropic-messages" | "gemini"; model: string }
export interface PublicAi {
  chat?: AiRole;
  vision?: AiRole;
  // Accept older servers which exposed one shared provider/model.
  provider?: AiRole["provider"];
  model?: string;
}
export interface Session {
  ai?: PublicAi;
  capabilities?: { canManageAISettings: boolean };
  scope?: ChatScope;
  user: { id: string; email: string; name: string };
  collections: { id: string; name: string }[];
  collectionId: string;
  csrfToken: string;
}
export interface AiSettingsRole extends AiRole { baseUrl: string; keyConfigured: boolean }
export interface AiSettings { revision: number; chat: AiSettingsRole; vision: AiSettingsRole }
export type AiSettingsRoleUpdate = AiRole & { baseUrl: string } & (
  { keyAction: "keep" | "clear"; apiKey?: never } | { keyAction: "replace"; apiKey: string }
);
export interface AiSettingsUpdate { revision: number; chat: AiSettingsRoleUpdate; vision: AiSettingsRoleUpdate }
export interface Entity {
  id: string;
  name: string;
  description?: string | null;
  locationId?: string | null;
  locationPath?: string | null;
  isLocation?: boolean;
  quantity?: number;
  manufacturer?: string | null;
  model?: string | null;
  serialNumber?: string | null;
  tags?: (string | { id?: string; name: string })[];
  children?: Entity[];
}
export interface Location { id: string; name: string; path: string; parentId?: string; isLocation: true }
export interface Draft {
  name: string;
  category: string;
  manufacturer: string;
  model: string;
  serialNumber: string;
  quantity: number;
  notes: string;
  tags: string[];
}
export interface Capture { captureId: string; draft: Draft; duplicates: Entity[]; locations: Location[]; warnings: string[] }
export interface CaptureJob {
  id: string;
  status: "queued" | "running" | "succeeded" | "manual" | "failed" | "expired";
  submissionKey: string;
  captureId: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  expiresAt: string | null;
  result: Capture | null;
  error: { code: string; message: string } | null;
  warningCodes: string[];
}
export type ActionType = "item.create" | "item.move" | "item.update" | "location.create" | "item.tags";
export interface Action {
  id: string;
  type: ActionType;
  revision: number;
  retryable?: boolean;
  confirmationKey?: string;
  status: "proposed" | "running" | "succeeded" | "failed" | "uncertain" | "cancelled";
  summary: string;
  payload: Record<string, unknown>;
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
  result?: Entity | { entity?: Entity } | null;
  error?: string | { code?: string; message?: string } | null;
}
export interface Chat { message: string; items: Entity[]; action?: Action; choices?: { kind: string; candidates: Entity[] } }
export interface ChatScope {
  instanceId: string; instanceLabel: string; collectionId: string; collectionName: string; contextVersion: string | number;
}
export interface ChatCandidate { id: string; entityId: string | null; label: string; path: string; kind: "location" | "item" | "unassigned" }
export interface ChatDestination { id: string; name: string; path: string; kind: "location" | "item"; parentId?: string | null }
export interface ChatReply extends Chat {
  conversationId: string; turnId: string; revision: number;
  outcome: "answered" | "clarification" | "proposal" | "unsupported";
  scope: ChatScope;
  overview?: { itemRecordCount: number; locationCount: number; includeArchived: false; complete: true };
  destinations?: ChatDestination[];
  collections?: { id: string; name: string; current: boolean }[];
  pagination?: { continuationId: string | null; totalRecords: number; page: number; pageSize: number; hasMore: boolean; complete: boolean };
  clarification?: { id: string; slot: string; reason: "missing" | "not_found" | "ambiguous"; candidates: ChatCandidate[] };
  pendingOperation?: { id: string; type: ActionType; payload: Record<string, unknown>; destinationQuery?: string; destination?: ChatDestination | null; actionId?: string; captureId?: string; status: string };
}
export interface ChatRequest {
  clientTurnId: string; conversationId?: string; expectedRevision?: number; mode: "normal" | "read_only";
  message?: string; selection?: { clarificationId: string; candidateId: string }; continuation?: { id: string };
}
export interface Mapping {
  tagId: string;
  targetType: "item" | "location";
  entityId: string;
  label: string;
  status: "active" | "revoked";
  url: string;
  createdAt: string;
  updatedAt: string;
}
