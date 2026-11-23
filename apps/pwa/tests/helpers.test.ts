import { afterEach, describe, expect, it, vi } from "vitest";
import { confirmationKey, emptyDraft, safeTagUrl, tagNames, tagRoute, validatePhoto } from "../src/helpers";
afterEach(() => vi.unstubAllGlobals());
describe("review and tag inputs", () => {
  it("reuses a confirmation key only for the same exact action revision, even after reload", () => {
    expect(confirmationKey({ id: "action-one", revision: 1 })).toBe(confirmationKey({ id: "action-one", revision: 1 }));
    expect(confirmationKey({ id: "action-one", revision: 2 })).not.toBe(confirmationKey({ id: "action-one", revision: 1 }));
  });
  it("starts with manual editable metadata and independent tags", () => {
    const first = emptyDraft(); first.tags.push("cables");
    expect(emptyDraft().tags).toEqual([]);
    expect(emptyDraft().quantity).toBe(1);
  });
  it("accepts only opaque tag landing routes", () => {
    expect(tagRoute("/t/t_01JAbc-32/")).toBe("t_01JAbc-32");
    for (const path of ["/t/../api", "/t/x/y", "/t/<script>", "/entities/id", "/t/"]) expect(tagRoute(path)).toBeNull();
  });
  it("never links QR codes to a foreign origin or unsafe protocol", () => {
    vi.stubGlobal("window", { location: { origin: "https://inventory.test" } });
    expect(safeTagUrl("https://inventory.test/t/t_safe")).toBe("https://inventory.test/t/t_safe");
    for (const url of ["javascript:alert(1)", "https://attacker.test/t/t_safe", "//attacker.test/t/t_safe", "/api/session", "/t/t_safe?token=x"]) expect(safeTagUrl(url)).toBeNull();
  });
  it("accepts phone image formats and rejects oversized or empty photos", () => {
    expect(validatePhoto(new File(["image"], "photo.heic", { type: "image/heic" }))).toBeNull();
    expect(validatePhoto(new File(["image"], "photo.jpg", { type: "" }))).toBeNull();
    expect(validatePhoto(new File(["x"], "file.svg", { type: "image/svg+xml" }))).toContain("Choose a JPEG");
    expect(validatePhoto(new File([], "empty.jpg"))).toContain("empty");
    expect(validatePhoto(new File([new Uint8Array(12 * 1024 * 1024 + 1)], "large.jpg"))).toContain("12 MB");
  });
  it("shows both string and Homebox-style tag objects", () => {
    expect(tagNames({ id: "1", name: "Stove", tags: ["camping", { id: "2", name: "outdoors" }] })).toEqual(["camping", "outdoors"]);
  });
});
