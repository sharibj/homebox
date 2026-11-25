import { describe, expect, it } from "vitest";
import { mergeCaptureDraft } from "../src/capture-draft";
import { emptyDraft } from "../src/helpers";

describe("late photo suggestions", () => {
  const suggested = { ...emptyDraft(), name: "Model name", quantity: 3, manufacturer: "Model maker", tags: ["suggested"] };
  it("fills untouched default fields", () => {
    expect(mergeCaptureDraft(emptyDraft(), suggested, emptyDraft(), new Set())).toEqual(suggested);
  });
  it("preserves fields edited while waiting, even when restored to their initial value", () => {
    const current = { ...emptyDraft(), name: "My name" };
    const result = mergeCaptureDraft(current, suggested, emptyDraft(), new Set(["name", "quantity"]));
    expect(result.name).toBe("My name");
    expect(result.quantity).toBe(1);
    expect(result.manufacturer).toBe("Model maker");
  });
  it("preserves metadata entered before uploading, without aliasing tag arrays", () => {
    const baseline = { ...emptyDraft(), name: "Already entered", tags: ["manual"] };
    const result = mergeCaptureDraft(baseline, suggested, baseline, new Set());
    expect(result.name).toBe("Already entered");
    expect(result.tags).toEqual(["manual"]);
    result.tags.push("another");
    expect(baseline.tags).toEqual(["manual"]);
  });
});
