import { emptyDraft } from "./helpers";
import type { Draft } from "./types";

export type DraftField = keyof Draft;
export function mergeCaptureDraft(current: Draft, suggested: Draft, baseline: Draft, edited: ReadonlySet<DraftField>): Draft {
  const result = { ...current, tags: [...current.tags] };
  const defaults = emptyDraft();
  for (const key of Object.keys(defaults) as DraftField[]) {
    // Preserve manual values, including edits that were later restored to their
    // original value while inference was running.
    if (edited.has(key) || JSON.stringify(current[key]) !== JSON.stringify(baseline[key])
      || JSON.stringify(baseline[key]) !== JSON.stringify(defaults[key])) continue;
    Object.assign(result, { [key]: Array.isArray(suggested[key]) ? [...suggested[key]] : suggested[key] });
  }
  return result;
}
