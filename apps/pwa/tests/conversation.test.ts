import { describe, expect, it, vi } from "vitest";
import { ApiError, type createApi } from "../src/api";
import { conversationView, useConversation } from "../src/useConversation";
import type { Action, ChatReply, ChatScope } from "../src/types";
const scope: ChatScope = { instanceId: "install", instanceLabel: "QA Homebox", collectionId: "A", collectionName: "Home", contextVersion: "A-1" };
const action: Action = { id: "action-1", type: "item.create", revision: 1, status: "proposed", summary: "Add mobile charge", payload: { name: "mobile charge", quantity: 1 }, confirmationKey: "confirm-original" };
function reply(extra: Partial<ChatReply> = {}): ChatReply {
  return { conversationId: "conversation", turnId: "server-turn", revision: 1, outcome: "answered", scope, message: "Here are your items", items: [], ...extra };
}
function setup() {
  let context = 1, actionGeneration = 1;
  const api = vi.fn().mockResolvedValue(reply());
  const receiveAction = vi.fn(), retireAction = vi.fn(), boundaryError = vi.fn();
  const conversation = useConversation({ api: api as unknown as ReturnType<typeof createApi>, context: () => context, scope: () => scope, online: () => true,
    receiveAction, retireAction, boundaryError, hasUnsafeAction: () => false, actionGeneration: () => actionGeneration });
  return { conversation, api, receiveAction, retireAction, boundaryError, changeContext: () => context++, changeAction: () => actionGeneration++ };
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  return { promise: new Promise<T>((yes, no) => { resolve = yes; reject = no; }), resolve: (value: T) => resolve(value), reject: (error: unknown) => reject(error) };
}
const choice = { id: "issued-clarification", slot: "destination", reason: "ambiguous" as const, candidates: [
  { id: "issued-candidate", entityId: "real-entity", label: "Entry", path: "Home / Entry", kind: "location" as const },
] };
describe("server-owned conversation controller", () => {
  it("sends only the new text plus stable server context, never a raw transcript", async () => {
    const { conversation: c, api } = setup();
    await c.send("Show inventory", "read_only");
    api.mockResolvedValue(reply({ revision: 2, turnId: "next" }));
    await c.send("Where is it?");
    expect(api.mock.calls[1][1].body).toMatchObject({ conversationId: "conversation", expectedRevision: 1, message: "Where is it?", mode: "normal" });
    expect(api.mock.calls[1][1].body).not.toHaveProperty("history");
    expect(api.mock.calls[0][1].body.clientTurnId).not.toBe(api.mock.calls[1][1].body.clientTurnId);
  });
  it("shows the user turn immediately and keeps newly typed input when the reply arrives", async () => {
    const { conversation: c, api } = setup(), pending = deferred<ChatReply>();
    api.mockReturnValue(pending.promise); c.updateComposer("First message");
    const work = c.send("First message");
    expect(c.state.messages[0]).toMatchObject({ text: "First message", status: "sending" });
    expect(c.state.composer).toBe("");
    c.updateComposer("My next question"); pending.resolve(reply()); await work;
    expect(c.state.composer).toBe("My next question");
  });
  it("does not overwrite input that was edited and cleared while a request failed", async () => {
    const { conversation: c, api } = setup(), pending = deferred<ChatReply>();
    api.mockReturnValue(pending.promise); c.updateComposer("Original");
    const work = c.send("Original"); c.updateComposer("New"); c.updateComposer("");
    pending.reject(new ApiError(0, "chat_connection_lost", "Interrupted")); await work;
    expect(c.state.composer).toBe("");
    expect(c.state.unsettled).toBe(true);
  });
  it("recovers a lost response with the exact original turn/body, not a second proposal", async () => {
    const { conversation: c, api, receiveAction } = setup();
    api.mockRejectedValueOnce(new ApiError(0, "chat_connection_lost", "Interrupted")).mockResolvedValue(reply({ action, outcome: "proposal" }));
    await c.send("Add mobile charge");
    const original = structuredClone(api.mock.calls[0][1].body);
    await c.send("A second write must wait");
    expect(api).toHaveBeenCalledTimes(1);
    await c.retry(original.clientTurnId);
    expect(api.mock.calls[1][1].body).toEqual(original);
    expect(receiveAction).toHaveBeenCalledTimes(1);
    expect(c.state.messages.filter(turn => turn.role === "user")).toHaveLength(1);
    expect(c.state.unsettled).toBe(false);
  });
  it("uses the issued candidate reference for selection and the entity identity only for viewing", async () => {
    const { conversation: c, api } = setup();
    api.mockResolvedValueOnce(reply({ clarification: choice }));
    await c.send("Add a mobile charge to the entry");
    const turn = c.state.messages.at(-1)!;
    expect(turn.reply!.clarification!.candidates[0]).toMatchObject({ id: "real-entity", candidateId: "issued-candidate" });
    api.mockResolvedValue(reply({ revision: 2, action, outcome: "proposal" }));
    await c.select(turn.id, "issued-candidate");
    expect(api.mock.calls[1][1].body).toMatchObject({ expectedRevision: 1, selection: { clarificationId: "issued-clarification", candidateId: "issued-candidate" } });
    expect(api.mock.calls[1][1].body).not.toHaveProperty("message");
  });
  it("rejects a previous revision's choice after a read-only side question", async () => {
    const { conversation: c, api } = setup();
    api.mockResolvedValueOnce(reply({ clarification: choice }));
    await c.send("Add item"); const old = c.state.messages.at(-1)!.id;
    api.mockResolvedValue(reply({ revision: 2, turnId: "new-choice", clarification: { ...choice, id: "current-choice" } }));
    await c.send("Which destinations?", "read_only");
    await c.select(old, "issued-candidate");
    expect(api).toHaveBeenCalledTimes(2);
    expect(c.state.issue).toContain("earlier choice");
  });
  it("ignores a late A reply even when the active collection has switched A to B to A", async () => {
    const { conversation: c, api, changeContext, receiveAction } = setup(), pending = deferred<ChatReply>();
    api.mockReturnValue(pending.promise); const work = c.send("Add item");
    changeContext(); c.reset(); changeContext();
    pending.resolve(reply({ action })); await work;
    expect(c.state.messages).toEqual([]); expect(receiveAction).not.toHaveBeenCalled();
    expect(api.mock.calls[0][1].signal.aborted).toBe(true);
  });
  it("does not replace a more recent manual preview with a late chat result", async () => {
    const { conversation: c, api, changeAction, receiveAction } = setup(), pending = deferred<ChatReply>();
    api.mockReturnValue(pending.promise); const work = c.send("Add item");
    changeAction(); pending.resolve(reply({ action })); await work;
    expect(receiveAction).not.toHaveBeenCalled();
  });
  it("restores the last server reply and pending task without rerunning inference", async () => {
    const { conversation: c, api, receiveAction } = setup();
    api.mockResolvedValue({ conversation: reply({ action, pendingOperation: { id: "pending", type: "item.create", payload: { name: "mobile charge", quantity: 1 }, status: "proposed", actionId: action.id } }), expired: false });
    await c.recover();
    expect(api.mock.calls[0][0]).toBe("/conversations/current");
    expect(api.mock.calls[0][1].method).toBeUndefined();
    expect(c.state.task?.summary).toContain("mobile charge");
    expect(c.state.messages).toHaveLength(1);
    expect(c.state.messages[0].role).toBe("assistant");
    expect(receiveAction).toHaveBeenCalledWith(action, { previousActionId: null, captureId: undefined });
  });
  it("treats a stale reply scope as a boundary, never as an empty result", async () => {
    const { conversation: c, api, boundaryError, receiveAction } = setup();
    api.mockResolvedValue(reply({ scope: { ...scope, contextVersion: "old-A" }, action }));
    await c.send("Add item");
    expect(boundaryError).toHaveBeenCalledWith(expect.objectContaining({ code: "COLLECTION_CONTEXT_CHANGED" }));
    expect(receiveAction).not.toHaveBeenCalled();
  });
  it("checks the latest response after a stale selection without replaying that selection", async () => {
    const { conversation: c, api } = setup();
    api.mockResolvedValueOnce(reply({ clarification: choice }));
    await c.send("Add item"); const id = c.state.messages.at(-1)!.id;
    api.mockRejectedValueOnce(new ApiError(409, "clarification_expired", "Expired")).mockResolvedValue({ conversation: reply({ revision: 2 }), expired: false });
    await c.select(id, "issued-candidate");
    expect(api.mock.calls.map(call => call[0])).toEqual(["/chat", "/chat", "/conversations/current"]);
  });
  it.each([false, true])("retires stale choices and linked reviews after an authoritative empty read, expired=%s", async expired => {
    const { conversation: c, api, retireAction } = setup();
    api.mockResolvedValueOnce(reply({ clarification: choice, action, pendingOperation: { id: "pending", type: "item.create", payload: { name: "mobile charge" }, actionId: action.id, status: "proposed" },
      pagination: { continuationId: "old-page", totalRecords: 25, page: 1, pageSize: 20, hasMore: true, complete: true } }));
    await c.send("Add item"); const oldTurn = c.state.messages.at(-1)!.id;
    c.edit(); c.updateComposer("Keep my unsent text");
    api.mockRejectedValueOnce(new ApiError(409, "clarification_expired", "Expired"))
      .mockResolvedValueOnce({ conversation: null, expired });
    await c.select(oldTurn, "issued-candidate");
    expect(c.state).toMatchObject({ messages: [], revision: 0, clarification: null, task: null, linkedActionId: null, editing: false,
      composer: "Keep my unsent text", scope, unsettled: false, recovering: false, sending: false, restoreAvailable: false });
    expect(retireAction).toHaveBeenCalledWith(action.id);
    await c.select(oldTurn, "issued-candidate"); await c.page(oldTurn);
    expect(api.mock.calls.map(call => call[0])).toEqual(["/chat", "/chat", "/conversations/current"]);
    api.mockResolvedValue(reply({ conversationId: "fresh-conversation" }));
    await c.send("A fresh question");
    expect(api.mock.calls[3][1].body).not.toHaveProperty("conversationId");
    expect(api.mock.calls[3][1].body).not.toHaveProperty("expectedRevision");
    expect(c.state.composer).toBe("Keep my unsent text");
  });
  it.each([false, true])("retires exact retry bodies when a locked read authoritatively finds no chat, expired=%s", async expired => {
    const { conversation: c, api } = setup();
    api.mockRejectedValueOnce(new ApiError(0, "chat_connection_lost", "Interrupted"))
      .mockResolvedValue({ conversation: null, expired });
    await c.send("Lost turn");
    const oldKey = api.mock.calls[0][1].body.clientTurnId;
    await c.recover(); await c.retry(oldKey);
    expect(api.mock.calls.map(call => call[0])).toEqual(["/chat", "/conversations/current", "/conversations/current"]);
    expect(c.state.messages).toEqual([]); expect(c.state.unsettled).toBe(false);
  });
  it.each([false, true])("also retires a cached linked review when explicit interrupted-turn restore is empty, expired=%s", async expired => {
    const { conversation: c, api, retireAction } = setup();
    api.mockResolvedValueOnce(reply({ action }))
      .mockRejectedValueOnce(new ApiError(409, "chat_turn_interrupted", "Interrupted"))
      .mockResolvedValue({ conversation: null, expired });
    await c.send("Add item"); await c.send("Make that two");
    c.updateComposer("Keep my new draft"); await c.restore();
    expect(retireAction).toHaveBeenCalledWith(action.id);
    expect(c.state).toMatchObject({ messages: [], linkedActionId: null, task: null, clarification: null, revision: 0,
      unsettled: false, restoreAvailable: false, composer: "Keep my new draft" });
    expect(api.mock.calls[2][1]).toMatchObject({ method: "POST", body: {} });
  });
  it.each([404, 503])("blocks new work if initial conversation recovery fails with %s", async status => {
    const { conversation: c, api } = setup();
    api.mockRejectedValue(new ApiError(status, "unavailable", "Unavailable"));
    await c.recover(); await c.send("Add another item");
    expect(c.state.unsettled).toBe(true); expect(api).toHaveBeenCalledTimes(1);
  });
  it("clears an old recovery failure when the server is reachable again", async () => {
    const { conversation: c, api } = setup();
    api.mockRejectedValueOnce(new ApiError(503, "unavailable", "Unavailable")).mockResolvedValue({ conversation: null, expired: false });
    await c.recover(); await c.recover();
    expect(c.state.issue).toBe(""); expect(c.state.unsettled).toBe(false);
    expect(c.state.scope).toEqual(scope);
  });
  it("keeps an interrupted edit blocked until the locked current read succeeds", async () => {
    const { conversation: c, api } = setup();
    api.mockResolvedValueOnce(reply({ action })).mockRejectedValueOnce(new ApiError(0, "chat_connection_lost", "Interrupted"))
      .mockRejectedValueOnce(new ApiError(409, "chat_turn_interrupted", "Interrupted turn"))
      .mockResolvedValue({ conversation: reply({ action, revision: 2 }), expired: false });
    await c.send("Add item"); await c.send("make that two"); await c.recover();
    expect(c.state.unsettled).toBe(true);
    expect(c.state.restoreAvailable).toBe(true);
    await c.recover();
    expect(c.state.unsettled).toBe(false); expect(c.state.restoreAvailable).toBe(false);
    expect(c.state.messages.some(turn => turn.error)).toBe(false);
  });
  it("does not settle an interrupted POST 409 and retains its exact original retry body", async () => {
    const { conversation: c, api } = setup();
    api.mockRejectedValueOnce(new ApiError(409, "chat_turn_interrupted", "Interrupted turn")).mockResolvedValue(reply({ action }));
    await c.send("Make that two"); const original = structuredClone(api.mock.calls[0][1].body);
    await c.send("Another write"); await c.fresh();
    expect(api).toHaveBeenCalledTimes(1);
    expect(c.state.unsettled).toBe(true); expect(c.state.restoreAvailable).toBe(true);
    await c.retry(original.clientTurnId);
    expect(api.mock.calls[1][1].body).toEqual(original);
    expect(c.state.unsettled).toBe(false); expect(c.state.restoreAvailable).toBe(false);
  });
  it("restores the original durable review without resending a lost body after reload", async () => {
    const { conversation: c, api, receiveAction } = setup();
    api.mockRejectedValueOnce(new ApiError(409, "chat_turn_interrupted", "Interrupted turn"))
      .mockRejectedValueOnce(new ApiError(503, "conversation_busy", "Busy"))
      .mockResolvedValue({ conversation: reply({ action }), expired: false });
    await c.recover(); c.updateComposer("Keep my unsent text");
    await c.restore();
    expect(c.state.unsettled).toBe(true); expect(c.state.restoreAvailable).toBe(true);
    await c.restore();
    expect(api.mock.calls.map(call => call[0])).toEqual(["/conversations/current", "/conversations/reset", "/conversations/reset"]);
    expect(api.mock.calls[2][1]).toMatchObject({ method: "POST", body: {} });
    expect(receiveAction).toHaveBeenCalledWith(action, { previousActionId: null, captureId: undefined });
    expect(c.state.composer).toBe("Keep my unsent text");
    expect(c.state.unsettled).toBe(false); expect(c.state.restoreAvailable).toBe(false);
  });
  it("reviews a budget-retained draft with a new normal turn, not confirmation", async () => {
    const { conversation: c, api } = setup();
    api.mockResolvedValueOnce(reply({ clarification: { id: "budget-review", slot: "review", reason: "missing", candidates: [] } })).mockResolvedValue(reply({ revision: 2, action, outcome: "proposal" }));
    await c.send("Move the item");
    const original = api.mock.calls[0][1].body.clientTurnId;
    await c.send("review draft");
    expect(api.mock.calls[1][1].body).toMatchObject({ message: "review draft", mode: "normal", conversationId: "conversation", expectedRevision: 1 });
    expect(api.mock.calls[1][1].body.clientTurnId).not.toBe(original);
    expect(api.mock.calls.every(call => call[0] === "/chat")).toBe(true);
  });
  it("passes exact capture identity across regenerated operation IDs and edited action IDs", async () => {
    const { conversation: c, api, receiveAction } = setup();
    const operation = { id: "temporary-operation", type: "item.create" as const, payload: { name: "camera" }, actionId: action.id, captureId: "exact-photo", status: "proposed" };
    api.mockResolvedValueOnce(reply({ action, pendingOperation: operation }))
      .mockResolvedValueOnce({ conversation: reply({ action, pendingOperation: { ...operation, id: "another-operation" } }), expired: false })
      .mockResolvedValueOnce(reply({ action: { ...action, id: "edited-action" }, revision: 2, pendingOperation: { ...operation, id: "persisted-operation", actionId: "edited-action" } }));
    await c.send("Show my pending review"); await c.recover(); await c.send("make that two");
    expect(receiveAction.mock.calls[2]).toEqual([expect.objectContaining({ id: "edited-action" }), { previousActionId: action.id, captureId: "exact-photo" }]);
  });
  it("keeps normal paging when a fully read page has more matches", () => {
    const view = conversationView(reply({ pagination: { continuationId: "next-page", totalRecords: 45, page: 1, pageSize: 20, hasMore: true, complete: true } }));
    expect(view.pagination).toEqual({ totalRecords: 45, nextCursor: "next-page", complete: true });
  });
  it("disables the old confirmation while preparing edits and synchronizes saved status", async () => {
    const { conversation: c, api } = setup();
    api.mockResolvedValue(reply({ action })); await c.send("Add item");
    c.edit(); expect(c.state.editing).toBe(true);
    c.syncAction({ ...action, status: "succeeded" });
    expect(c.state.editing).toBe(false);
    expect(c.state.messages.at(-1)!.reply!.action!.status).toBe("succeeded");
  });
});
