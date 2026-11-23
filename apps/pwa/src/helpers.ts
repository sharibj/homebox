import type { Action, Draft, Entity } from "./types";
// An idempotency key is an identifier, not a secret. Derive it so a reload can
// resume only the same server-owned action and revision without browser storage.
export const confirmationKey = (action: Pick<Action, "id" | "revision" | "confirmationKey">) => action.confirmationKey || `${action.id}:${action.revision}`;
export const emptyDraft = (): Draft => ({
  name: "", category: "", manufacturer: "", model: "", serialNumber: "", quantity: 1, notes: "", tags: [],
});
export const tagNames = (entity: Entity): string[] => (entity.tags || []).map(tag => typeof tag === "string" ? tag : tag.name);
export function tagRoute(path: string): string | null {
  const match = /^\/t\/([A-Za-z0-9_-]{1,160})\/?$/.exec(path);
  return match?.[1] || null;
}
export function safeTagUrl(value: string): string | null {
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && tagRoute(url.pathname) && !url.search && !url.hash ? url.href : null;
  } catch { return null; }
}
export function validatePhoto(file: File): string | null {
  if (file.size > 12 * 1024 * 1024) return "Choose a photo smaller than 12 MB.";
  if (!file.size) return "This photo is empty. Choose another file.";
  // Some phones omit MIME types. The server checks actual image bytes.
  if (file.type && !["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"].includes(file.type)) {
    return "Choose a JPEG, PNG, WebP, HEIC, or HEIF photo.";
  }
  return null;
}
