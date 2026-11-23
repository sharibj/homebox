export interface Session {
  user: { id: string; email: string; name: string };
  collections: { id: string; name: string }[];
  collectionId: string;
  csrfToken: string;
}
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
