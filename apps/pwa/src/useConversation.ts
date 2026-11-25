import { reactive } from "vue";
import { ApiError, type createApi } from "./api";
import type { Action, Chat, ChatReply, ChatRequest, ChatScope } from "./types";
import type { ConversationReply, ConversationState } from "./conversation-types";

export function conversationView(reply: Chat | ChatReply): ConversationReply {
  const value = reply as Partial<ChatReply> & Chat;
  return {
    message: value.message, items: value.items || [], scope: value.scope, outcome: value.outcome,
    revision: value.revision,
    overview: value.overview ? { itemCount: value.overview.itemRecordCount, locationCount: value.overview.locationCount, includesArchived: value.overview.includeArchived } : undefined,
    destinations: value.destinations?.map(item => ({ ...item, locationPath: item.path, isLocation: item.kind === "location" })),
    collections: value.collections,
    pagination: value.pagination ? { totalRecords: value.pagination.totalRecords, nextCursor: value.pagination.hasMore ? value.pagination.continuationId : null, complete: value.pagination.complete } : undefined,
    clarification: value.clarification ? {
      id: value.clarification.id, revision: value.revision!, slot: value.clarification.slot, reason: value.clarification.reason,
      candidates: value.clarification.candidates.map(item => ({
        id: item.entityId || item.id, candidateId: item.id, name: item.label, path: item.path,
        locationPath: item.path, kind: item.kind, isLocation: item.kind === "location",
      })),
    } : undefined,
    action: value.action,
  };
}
export function useConversation(options: {
  api: ReturnType<typeof createApi>;
  context: () => unknown;
  scope: () => ChatScope | undefined;
  online: () => boolean;
  receiveAction: (action: Action, ownership: { previousActionId: string | null; captureId?: string }) => void;
  retireAction?: (id: string) => void;
  boundaryError: (cause: unknown) => void | Promise<void>;
  hasUnsafeAction: () => boolean;
  actionGeneration?: () => unknown;
}) {
  const state = reactive<ConversationState>({
    messages: [], composer: "", sending: false, recovering: false, issue: "", scope: null,
    clarification: null, task: null, linkedActionId: null, editing: false, unsettled: false, restoreAvailable: false, revision: 0,
  });
  let conversationId: string | undefined;
  let generation = 0, composerVersion = 0;
  let controller: AbortController | null = null;
  const requests = new Map<string, { body: ChatRequest; label: string }>();
  const replies = new Map<string, ChatReply>();
  const live = (context: unknown, version: number) => context === options.context() && version === generation;
  const boundary = (cause: unknown) => cause instanceof ApiError
    && (cause.status === 401 || cause.status === 403 || cause.code === "COLLECTION_CONTEXT_CHANGED");
  function updateComposer(value: string) { state.composer = value; composerVersion++; }
  function reset() {
    generation++; controller?.abort(); controller = null; conversationId = undefined;
    requests.clear(); replies.clear(); composerVersion++;
    Object.assign(state, { messages: [], composer: "", sending: false, recovering: false, issue: "", scope: null, clarification: null, task: null, linkedActionId: null, editing: false, unsettled: false, restoreAvailable: false, revision: 0 });
  }
  function settleRequests() {
    for (const turn of state.messages) if (requests.has(turn.id)) { turn.status = "complete"; turn.error = undefined; }
    requests.clear(); state.unsettled = false; state.restoreAvailable = false;
  }
  function retireCurrent(retireAction = true) {
    const oldAction = state.linkedActionId;
    conversationId = undefined; requests.clear(); replies.clear();
    Object.assign(state, { messages: [], revision: 0, clarification: null, task: null, linkedActionId: null, editing: false });
    if (oldAction && retireAction) options.retireAction?.(oldAction);
  }
  function recoveryFailure(cause: unknown) {
    state.unsettled = true;
    if (cause instanceof ApiError && cause.code === "chat_turn_interrupted") state.restoreAvailable = true;
    state.issue = state.restoreAvailable
      ? "A previous turn did not finish. Restore its saved review before confirming, cancelling, or sending another request. Nothing will be resent."
      : "Could not restore the conversation. Check it again before repeating an unfinished request.";
  }
  function accept(value: Chat | ChatReply, recovered = false, allowAction = true) {
    const reply = value as Partial<ChatReply> & Chat;
    const scope = options.scope();
    if (reply.scope && scope && (reply.scope.collectionId !== scope.collectionId || String(reply.scope.contextVersion) !== String(scope.contextVersion))) {
      throw new ApiError(409, "COLLECTION_CONTEXT_CHANGED", "This response belongs to an earlier collection context.");
    }
    const oldAction = state.linkedActionId;
    conversationId = reply.conversationId;
    state.revision = reply.revision || 0;
    state.scope = reply.scope || scope || null;
    state.clarification = conversationView(value).clarification || null;
    const operation = reply.pendingOperation;
    state.task = operation && !["succeeded", "cancelled", "completed", "resolved"].includes(operation.status) ? {
      summary: `${operation.type === "item.create" ? "Add" : operation.type} ${operation.payload.quantity ? `${operation.payload.quantity} × ` : ""}${operation.payload.name || "item"}${operation.destination ? ` · ${operation.destination.path}` : operation.destinationQuery ? ` · destination: ${operation.destinationQuery}` : ""}`.trim(),
      fields: { ...operation.payload },
    } : null;
    state.linkedActionId = reply.action?.id || operation?.actionId || null;
    if (state.linkedActionId !== oldAction || reply.action?.status !== "proposed") state.editing = false;
    if (reply.action && allowAction) options.receiveAction(reply.action, { previousActionId: oldAction, captureId: operation?.captureId });
    const id = `${reply.turnId || crypto.randomUUID()}:assistant`;
    const message = { id, role: "assistant" as const, text: reply.message, status: "complete" as const, reply: conversationView(value) };
    const existing = state.messages.findIndex(turn => turn.id === id);
    if (existing >= 0) state.messages[existing] = message;
    else state.messages.push(message);
    if (reply.conversationId) replies.set(id, reply as ChatReply);
    state.messages = state.messages.slice(-40);
    const visible = new Set(state.messages.map(turn => turn.id));
    for (const key of replies.keys()) if (!visible.has(key)) replies.delete(key);
    if (recovered) state.issue = "Restored the latest saved response and pending task. Unsaved local message text is not recovered after a reload.";
  }
  async function recover() {
    const context = options.context(), version = generation, actionGeneration = options.actionGeneration?.();
    state.recovering = true; state.issue = ""; state.scope = options.scope() || null;
    try {
      const envelope = await options.api<{ conversation: ChatReply | null; expired: boolean }>("/conversations/current", { timeoutMs: 15_000 });
      if (!live(context, version)) return;
      state.scope = options.scope() || null;
      if (envelope.conversation) accept(envelope.conversation, true, actionGeneration === options.actionGeneration?.());
      else {
        retireCurrent();
        if (envelope.expired) state.issue = "The previous conversation expired. Ask again to get fresh choices. Existing inventory changes remain in recent changes.";
      }
      // A successful current read holds the server's turn lock and is authoritative.
      settleRequests();
    } catch (cause) {
      if (!live(context, version)) return;
      if (boundary(cause)) { await options.boundaryError(cause); return; }
      recoveryFailure(cause);
    } finally { if (live(context, version)) state.recovering = false; }
  }
  async function restore() {
    if (!state.restoreAvailable || state.sending || state.recovering || !options.online()) return;
    const context = options.context(), version = generation, actionGeneration = options.actionGeneration?.();
    state.recovering = true;
    try {
      const envelope = await options.api<{ conversation: ChatReply | null; expired: boolean }>("/conversations/reset", { method: "POST", body: {}, timeoutMs: 15_000 });
      if (!live(context, version)) return;
      retireCurrent(!envelope.conversation); state.issue = "";
      if (envelope.conversation) accept(envelope.conversation, true, actionGeneration === options.actionGeneration?.());
      else state.issue = "Saved review checked. No conversation was restored. Check recent changes before preparing another item.";
      settleRequests();
    } catch (cause) {
      if (!live(context, version)) return;
      if (boundary(cause)) { await options.boundaryError(cause); return; }
      recoveryFailure(cause);
    } finally { if (live(context, version)) state.recovering = false; }
  }
  async function execute(body: ChatRequest, label: string, retry = false) {
    if (state.sending || state.recovering || !options.online()) return;
    const context = options.context(), version = generation, editVersion = composerVersion, actionGeneration = options.actionGeneration?.();
    state.sending = true; state.issue = ""; state.unsettled = false;
    controller = new AbortController();
    if (!retry) {
      requests.set(body.clientTurnId, { body, label });
      state.messages.push({ id: body.clientTurnId, role: "user", text: label, status: "sending" });
    } else {
      const turn = state.messages.find(item => item.id === body.clientTurnId);
      if (turn) { turn.status = "sending"; turn.error = undefined; }
    }
    try {
      const response = await options.api<ChatReply>("/chat", { method: "POST", body, signal: controller.signal, timeoutMs: 75_000 });
      if (!live(context, version)) return;
      accept(response, false, actionGeneration === options.actionGeneration?.());
      const turn = state.messages.find(item => item.id === body.clientTurnId);
      if (turn) { turn.status = "complete"; turn.error = undefined; }
      requests.delete(body.clientTurnId);
      state.restoreAvailable = false;
    } catch (cause) {
      if (!live(context, version)) return;
      if (boundary(cause)) { await options.boundaryError(cause); return; }
      if (cause instanceof ApiError && ["stale_response", "request_cancelled"].includes(cause.code)) return;
      const turn = state.messages.find(item => item.id === body.clientTurnId);
      const stale = cause instanceof ApiError && ["conversation_changed", "clarification_expired", "idempotency_conflict"].includes(cause.code);
      if (turn) {
        turn.status = "failed";
        turn.error = stale ? "The conversation changed. This old choice or request was not retried. Check the latest saved response." : cause instanceof Error ? cause.message : "The reply was interrupted. Check this turn before trying again.";
      }
      if (body.message && composerVersion === editVersion && !state.composer) state.composer = body.message;
      const interrupted = cause instanceof ApiError && cause.code === "chat_turn_interrupted";
      const definitive = cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && !interrupted;
      state.unsettled = !stale && !definitive;
      if (interrupted) recoveryFailure(cause);
      if (definitive && !stale) requests.delete(body.clientTurnId);
      if (stale) { requests.delete(body.clientTurnId); await recover(); }
    } finally { if (live(context, version)) { state.sending = false; controller = null; } }
  }
  function requestBase(mode: ChatRequest["mode"]): ChatRequest {
    return { clientTurnId: crypto.randomUUID(), ...(conversationId ? { conversationId, expectedRevision: state.revision } : {}), mode };
  }
  async function send(text: string, mode: ChatRequest["mode"] = "normal") {
    if (!text.trim() || state.unsettled || state.sending || state.recovering) return;
    if (!options.online()) { state.issue = "You’re offline. Reconnect to ask your inventory. Your message is still here."; return; }
    if (state.composer.trim() === text.trim()) state.composer = "";
    await execute({ ...requestBase(mode), message: text.trim() }, text.trim());
  }
  async function retry(id: string) {
    const request = requests.get(id);
    if (!request) { await recover(); return; }
    // The original turn key and exact body are retained. Never mint a replacement proposal.
    await execute(request.body, request.label, true);
  }
  async function select(turnId: string, candidateId: string) {
    const source = replies.get(turnId);
    if (state.unsettled || !source || source.revision !== state.revision || source.conversationId !== conversationId
      || !source.clarification?.candidates.some(item => item.id === candidateId)) {
      state.issue = "That earlier choice is no longer active. Ask for current destinations."; return;
    }
    const candidate = source.clarification.candidates.find(item => item.id === candidateId)!;
    await execute({ ...requestBase("normal"), selection: { clarificationId: source.clarification.id, candidateId } }, `Use ${candidate.path || candidate.label}`);
  }
  async function page(turnId: string) {
    const source = replies.get(turnId);
    if (state.unsettled || !source || source.revision !== state.revision || source.conversationId !== conversationId || !source.pagination?.hasMore || !source.pagination.continuationId) {
      state.issue = "That earlier page is no longer active. Ask again for current results."; return;
    }
    await execute({ ...requestBase("read_only"), continuation: { id: source.pagination.continuationId } }, "Show more");
  }
  async function fresh() {
    if (state.sending || state.recovering || state.unsettled || options.hasUnsafeAction()) { state.issue = "Finish or check the current request before starting a new chat."; return; }
    const context = options.context(), version = generation;
    state.recovering = true;
    try {
      const envelope = await options.api<{ conversation: ChatReply | null; expired: boolean }>("/conversations/reset", { method: "POST", body: {}, timeoutMs: 15_000 });
      if (!live(context, version)) return;
      reset();
      if (envelope.conversation) accept(envelope.conversation, true);
    } catch (cause) {
      if (!live(context, version)) return;
      if (boundary(cause)) await options.boundaryError(cause);
      else recoveryFailure(cause);
    } finally { if (live(context, version)) state.recovering = false; }
  }
  function syncAction(value: Action) {
    for (const message of state.messages) if (message.reply?.action?.id === value.id) message.reply.action = value;
    if (state.linkedActionId === value.id && value.status !== "proposed") state.editing = false;
  }
  function edit() {
    state.editing = true;
    state.issue = "Describe the changes below, for example “make that two”, then send for a fresh preview.";
  }
  return { state, send, retry, select, page, fresh, recover, restore, reset, updateComposer, syncAction, edit };
}
