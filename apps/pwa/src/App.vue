<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { ApiError, createApi } from "./api";
import { confirmationKey, emptyDraft, tagNames, tagRoute, validatePhoto } from "./helpers";
import type { Action, ActionType, AiSettings as AiSettingsValue, CaptureJob, Draft, Entity, Location, Mapping, Session } from "./types";
import { canManageAiSettings } from "./ai-settings-editor";
import { mergeCaptureDraft, type DraftField } from "./capture-draft";
import { useCaptureJobs } from "./useCaptureJobs";
import { useConversation } from "./useConversation";
import ConversationPanel from "./components/ConversationPanel.vue";
import "./conversation.css";
import ActionCard from "./components/ActionCard.vue";
import AiDetails from "./components/AiDetails.vue";
import AiSettings from "./components/AiSettings.vue";
import CaptureProgress from "./components/CaptureProgress.vue";
import DraftForm from "./components/DraftForm.vue";
import EntityList from "./components/EntityList.vue";
import TagPanel from "./components/TagPanel.vue";

const session = ref<Session | null>(null);
const api = createApi(() => session.value?.csrfToken, () => epoch, () => session.value?.collectionId, () => session.value?.scope?.contextVersion);
const initialLoading = ref(true);
const busy = ref(false);
const busyLabel = ref("");
const error = ref("");
const notice = ref("");
const online = ref(navigator.onLine);
const username = ref("");
const password = ref("");
type Tab = "ask" | "capture" | "places" | "settings";
const tab = ref<Tab>(new URL(window.location.href).searchParams.get("page") === "ai-settings" ? "settings" : "ask");
const settingsView = ref<InstanceType<typeof AiSettings> | null>(null);
const canManageSettings = computed(() => canManageAiSettings(session.value));
const inventoryDisabled = computed(() => busy.value || !online.value || !session.value?.collectionId || !recoveryReady.value);
const locations = ref<Location[]>([]);
const query = ref("");
const results = ref<Entity[]>([]);
const resultTotal = ref(0);
const searched = ref(false);
const action = ref<Action | null>(null);
const history = ref<Action[]>([]);
const recoveryReady = ref(false);
const recoveryWarning = ref("");
const draft = ref<Draft>(emptyDraft());
const draftLocation = ref("");
const captureId = ref<string>();
const duplicates = ref<Entity[]>([]);
const warnings = ref<string[]>([]);
const photo = ref<File | null>(null);
const preview = ref("");
const hint = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
const detail = ref<Entity | null>(null);
const contents = ref<Entity[]>([]);
const editMode = ref<"" | "move" | "edit" | "tags">("");
const editDraft = ref<Draft>(emptyDraft());
const destination = ref("");
const editedTags = ref("");
const mappings = ref<Mapping[]>([]);
const locationName = ref("");
const locationParent = ref("");
const locationDescription = ref("");
const tagId = ref(tagRoute(window.location.pathname));
const tagError = ref("");
const mappingLabel = ref("");
let epoch = 0;
let photoIntent = 0;
let actionIntent = 0;
let draftEditVersion = 0;
let captureFormVersion = 0;
let draftReview: { actionId: string; photoIntent: number; editVersion: number; captureId?: string } | null = null;
let captureBaseline = emptyDraft();
const editedCaptureFields = new Set<DraftField>();
function rememberCaptureEdits() {
  captureFormVersion = draftEditVersion;
  captureBaseline = { ...draft.value, tags: [...draft.value.tags] };
  editedCaptureFields.clear();
}
function recordDraftEdit(field: DraftField) { draftEditVersion++; editedCaptureFields.add(field); }
function updateDraftLocation(value: string) { draftEditVersion++; draftLocation.value = value; }
const captureJobs = useCaptureJobs({
  api,
  context: () => `${epoch}:${photoIntent}`,
  online: () => online.value,
  receive(response) {
    captureId.value = response.captureId;
    draft.value = mergeCaptureDraft(draft.value, response.draft, captureBaseline, editedCaptureFields);
    duplicates.value = response.duplicates; warnings.value = response.warnings;
    if (response.locations.length) locations.value = response.locations;
    notice.value = response.draft.name
      ? "Photo suggestions are ready. Your manual edits were kept. Review before saving."
      : "Photo uploaded. Enter the item details manually, then review before saving.";
  },
  boundaryError(cause) {
    if (cause instanceof ApiError && cause.status === 403) {
      clearPrivate(); session.value = null;
      error.value = "Access to this photo is no longer available. Sign in again to refresh collection access.";
    } else { void handleFailure(cause, "Checking photo status"); }
  },
});
const { job: captureJob, jobs: captureHistory, issue: captureIssue, pending: capturePending } = captureJobs;
const pending = computed(() => action.value && !["succeeded", "cancelled"].includes(action.value.status));
const currentCollection = computed(() => session.value?.collections.find(c => c.id === session.value?.collectionId)?.name || "Choose a collection");
const conversation = useConversation({
  api, context: () => epoch, scope: () => session.value?.scope, online: () => online.value,
  actionGeneration: () => actionIntent,
  hasUnsafeAction: () => !!pending.value,
  retireAction(id) {
    if (action.value?.id === id && ["proposed", "failed"].includes(action.value.status)) {
      // An empty chat is not an authoritative action-status read.
      action.value = { ...action.value, status: "uncertain", error: "This cached review is no longer linked to a current conversation. Check its status before acting." };
    }
  },
  receiveAction(value, ownership) {
    if (draftReview && (draftReview.actionId === value.id
      || (draftReview.captureId ? ownership.captureId === draftReview.captureId : ownership.previousActionId === draftReview.actionId))) {
      draftReview = { ...draftReview, actionId: value.id };
    } else if (ownership.captureId && ownership.captureId === captureId.value) {
      draftReview = { actionId: value.id, photoIntent, captureId: ownership.captureId, editVersion: captureFormVersion };
    }
    if (value.type === "item.create" && value.status === "succeeded" && ownership.captureId) captureJobs.consume(ownership.captureId);
    action.value = value;
  },
  async boundaryError(cause) {
    if (cause instanceof ApiError && cause.status === 403) {
      clearPrivate(); session.value = null; error.value = "Access changed. Sign in again to refresh your collection access.";
    } else await handleFailure(cause, "Asking your inventory");
  },
});
const conversationAction = computed(() => !!action.value && action.value.id === conversation.state.linkedActionId);
const conversationRecoveryBlocked = computed(() => conversation.state.recovering || conversation.state.unsettled || conversation.state.sending);
const conversationConfirmBlocked = computed(() => conversationRecoveryBlocked.value || (conversationAction.value && conversation.state.editing));
function belongsToLocalDraft(value: Action) {
  return draftReview?.actionId === value.id && draftReview.photoIntent === photoIntent;
}
const reviewPhoto = computed(() => action.value && belongsToLocalDraft(action.value) ? preview.value : "");
const matchingLocations = computed(() => locations.value.filter(location =>
  !query.value.trim() || `${location.path} ${location.name}`.toLocaleLowerCase().includes(query.value.toLocaleLowerCase())));
watch(() => action.value?.id, id => {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("action", id);
  else url.searchParams.delete("action");
  window.history.replaceState(null, "", url);
});
watch(action, current => {
  if (current) {
    history.value = [current, ...history.value.filter(previous => previous.id !== current.id)].slice(0, 50);
    conversation.syncAction(current);
    if (draftReview?.actionId === current.id) {
      if (current.status === "succeeded" && current.type === "item.create") {
        const consumed = draftReview;
        draftReview = null;
        if (consumed.captureId) captureJobs.consume(consumed.captureId);
        if (consumed.photoIntent === photoIntent && consumed.captureId === captureId.value) {
          if (consumed.editVersion === draftEditVersion) resetDraft();
          else {
            clearPhoto();
            notice.value = "The reviewed photo was saved. Your newer manual details were kept. Choose a new photo before preparing another item.";
          }
        }
      }
      else if (current.status === "cancelled") draftReview = null;
    }
  }
});

function updateOnline() { online.value = navigator.onLine; }
function removeActionQuery() {
  const url = new URL(window.location.href);
  url.searchParams.delete("action");
  window.history.replaceState(null, "", url);
}
function clearPhoto() {
  photoIntent++; captureJobs.detach(); editedCaptureFields.clear();
  if (preview.value) URL.revokeObjectURL(preview.value);
  photo.value = null; preview.value = ""; hint.value = "";
  captureId.value = undefined; duplicates.value = []; warnings.value = [];
  if (fileInput.value) fileInput.value.value = "";
}
function resetDraft() {
  draftEditVersion++;
  clearPhoto(); draft.value = emptyDraft(); captureId.value = undefined;
  duplicates.value = []; warnings.value = []; draftLocation.value = "";
}
function clearPrivate() {
  settingsView.value?.clearPrivate();
  if (tab.value === "settings") { tab.value = "ask"; settingsUrl("ask"); }
  draftReview = null; draftEditVersion++;
  epoch++; locations.value = []; results.value = []; resultTotal.value = 0; searched.value = false;
  conversation.reset(); actionIntent++; query.value = ""; action.value = null;
  history.value = []; recoveryReady.value = false;
  captureJobs.reset();
  recoveryWarning.value = ""; removeActionQuery();
  detail.value = null; contents.value = []; mappings.value = [];
  editMode.value = ""; mappingLabel.value = ""; resetDraft(); tagError.value = "";
  editDraft.value = emptyDraft(); editedTags.value = ""; destination.value = "";
  locationName.value = ""; locationParent.value = ""; locationDescription.value = "";
}
function settingsUrl(next: Tab) {
  const url = new URL(window.location.href);
  if (next === "settings") url.searchParams.set("page", "ai-settings");
  else url.searchParams.delete("page");
  window.history.replaceState(null, "", url);
}
async function canLeaveSettings() { return tab.value !== "settings" || !settingsView.value || await settingsView.value.confirmLeave(); }
async function showTab(next: Tab) {
  if (next === tab.value || !await canLeaveSettings()) return;
  tab.value = next; detail.value = null; editMode.value = ""; settingsUrl(next);
}
async function home(event: MouseEvent) {
  if (tab.value !== "settings" || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  if (await canLeaveSettings()) window.location.assign("/");
}
function settingsSnapshot(value: AiSettingsValue) {
  if (session.value) session.value.ai = {
    chat: { provider: value.chat.provider, model: value.chat.model },
    vision: { provider: value.vision.provider, model: value.vision.model },
  };
}
async function settingsBoundary(cause: unknown) {
  if (cause instanceof ApiError && cause.status === 403) {
    settingsView.value?.clearPrivate();
    if (session.value) session.value.capabilities = { canManageAISettings: false };
    error.value = "AI settings access is unavailable. Only the original installation account can manage these settings.";
  } else await handleFailure(cause, "Managing AI settings");
}
async function perform(label: string, task: () => Promise<void>) {
  if (busy.value) return;
  if (!online.value) { error.value = "You’re offline. Reconnect to read or change inventory. Private inventory is not saved on this device."; return; }
  busy.value = true; busyLabel.value = label; error.value = ""; notice.value = "";
  try { await task(); }
  catch (cause) { await handleFailure(cause, label); }
  finally { busy.value = false; busyLabel.value = ""; }
}
async function handleFailure(cause: unknown, label: string) {
    if (cause instanceof ApiError && cause.code === "stale_response") return;
    if (cause instanceof ApiError && cause.code === "COLLECTION_CONTEXT_CHANGED") {
      clearPrivate();
      try {
        session.value = await api<Session>("/session");
        await afterSignIn();
        error.value = "The collection changed in another tab. Old drafts, results, and changes were cleared. Review the active collection before continuing. Your request was not retried.";
      } catch (recoveryError) {
        if (recoveryError instanceof ApiError && recoveryError.status === 401) session.value = null;
        error.value = "The collection changed in another tab. Old private data was cleared. Reload or sign in again before continuing.";
      }
      return;
    }
    if (cause instanceof ApiError && cause.status === 401) {
      clearPrivate(); session.value = null;
      error.value = label === "Signing in" ? cause.message : "Your session ended. Sign in again to continue.";
    } else { error.value = cause instanceof Error ? cause.message : "Something went wrong. Please try again."; }
}
async function loadLocations() {
  const version = epoch;
  const data = await api<{ locations: Location[] }>("/locations");
  if (version === epoch) locations.value = data.locations;
}
async function resolveTag() {
  if (!tagId.value) return;
  tagError.value = "";
  try {
    const data = await api<{ mapping: Mapping; entity: Entity }>(`/tags/resolve/${encodeURIComponent(tagId.value)}`);
    mappingLabel.value = data.mapping.label;
    await openEntity(data.entity);
  } catch (cause) {
    if (cause instanceof ApiError && (cause.status === 401 || cause.code === "COLLECTION_CONTEXT_CHANGED")) throw cause;
    tagError.value = cause instanceof Error ? cause.message : "This tag is unavailable.";
  }
}
async function afterSignIn() {
  recoveryReady.value = false;
  recoveryWarning.value = "";
  const recent = await api<{ actions: Action[] }>("/actions");
  history.value = recent.actions;
  const restoredId = new URL(window.location.href).searchParams.get("action");
  if (restoredId && /^[A-Za-z0-9_-]{1,160}$/.test(restoredId)) {
    try { action.value = await api<Action>(`/actions/${encodeURIComponent(restoredId)}`); }
    catch (cause) {
      if (!(cause instanceof ApiError && cause.status === 404)) throw cause;
      removeActionQuery(); action.value = null;
      recoveryWarning.value = "That previous change is unavailable in this session or collection. Review recent changes before creating another item. Nothing was retried.";
    }
  } else {
    action.value = recent.actions.find(candidate => ["proposed", "running", "uncertain", "failed"].includes(candidate.status)) || null;
  }
  await loadLocations(); await resolveTag();
  rememberCaptureEdits();
  await captureJobs.recover();
  await conversation.recover();
  recoveryReady.value = true;
}
async function login() {
  await perform("Signing in", async () => {
    session.value = await api<Session>("/auth/login", { method: "POST", body: { username: username.value, password: password.value } });
    password.value = ""; username.value = "";
    await afterSignIn();
  });
  password.value = "";
}
async function logout() {
  if (!await canLeaveSettings()) return;
  await perform("Signing out", async () => {
    await api("/auth/logout", { method: "POST", body: {} });
    clearPrivate(); session.value = null; notice.value = "Signed out.";
  });
}
async function changeCollection(event: Event) {
  const id = (event.target as HTMLSelectElement).value;
  (event.target as HTMLSelectElement).value = session.value?.collectionId || "";
  await switchCollection(id);
}
async function switchCollection(id: string) {
  if (!session.value?.collections.some(collection => collection.id === id)) return;
  if (id === session.value?.collectionId) return;
  if (!await canLeaveSettings()) return;
  await perform("Changing collection", async () => {
    clearPrivate();
    session.value = await api<Session>("/session/collection", { method: "POST", body: { collectionId: id } });
    await afterSignIn();
  });
}
async function selectHistoryAction(value: Action) {
  const intent = ++actionIntent;
  await perform("Checking previous change", async () => {
    const current = await api<Action>(`/actions/${encodeURIComponent(value.id)}`);
    if (intent === actionIntent) action.value = current;
  });
}
function dismissAction() { actionIntent++; action.value = null; }
async function search() {
  await perform("Searching inventory", async () => {
    const response = await api<{ items: Entity[]; total: number }>(`/items?q=${encodeURIComponent(query.value.trim())}`);
    results.value = response.items; resultTotal.value = response.total; searched.value = true;
  });
}
async function openEntity(entity: Entity) {
  const data = await api<Entity>(`/entities/${encodeURIComponent(entity.id)}`);
  detail.value = data; editMode.value = ""; contents.value = [];
  if (data.isLocation) {
    const response = await api<{ items: Entity[] }>(`/items?locationId=${encodeURIComponent(data.id)}`);
    contents.value = response.items;
  }
  const response = await api<{ mappings: Mapping[] }>("/tags/mappings");
  mappings.value = response.mappings.filter(m => m.entityId === data.id);
}
async function view(entity: Entity) { await perform("Opening record", () => openEntity(entity)); }
async function openLocation(location: Location) { await view({ ...location, locationPath: location.path }); }
function choosePhoto(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  const validation = validatePhoto(file);
  if (validation) { error.value = validation; return; }
  clearPhoto(); photo.value = file; preview.value = URL.createObjectURL(file);
  captureId.value = undefined; duplicates.value = []; warnings.value = []; error.value = "";
}
async function analyze() {
  if (!photo.value || capturePending.value) return;
  rememberCaptureEdits();
  await perform("Uploading photo to your home server", async () => {
    const form = new FormData();
    form.append("image", photo.value!);
    if (hint.value.trim()) form.append("hint", hint.value.trim());
    if (draftLocation.value) form.append("locationId", draftLocation.value);
    await captureJobs.submit(form);
  });
}
function resumeCaptureJob(value: CaptureJob) {
  clearPhoto(); rememberCaptureEdits(); captureJobs.select(value);
  detail.value = null; tab.value = "capture";
}
async function propose(type: ActionType, payload: Record<string, unknown>, photoId?: string) {
  if (conversationRecoveryBlocked.value) { error.value = "Check the current conversation request before reviewing another change. Your entered details are still here."; return; }
  if (!recoveryReady.value || pending.value) { error.value = "Load recent changes, then finish or cancel the current change first."; return; }
  actionIntent++;
  await perform("Preparing change for review", async () => {
    try {
      const proposed = await api<Action>("/actions", { method: "POST", body: { type, payload, ...(photoId ? { captureId: photoId } : {}) } });
      if (type === "item.create") draftReview = { actionId: proposed.id, photoIntent, editVersion: draftEditVersion, captureId: photoId };
      action.value = proposed;
    } catch (cause) {
      if (!(cause instanceof ApiError && cause.status === 409 && cause.code !== "COLLECTION_CONTEXT_CHANGED")) throw cause;
      // A concurrent chat/manual workflow can win the server's proposal lock.
      // Inspect it without retrying this submission or discarding the local form/photo.
      conversation.state.unsettled = true;
      conversation.state.issue = "Another change is pending. Check the conversation before preparing or confirming another change.";
      try {
        const recent = await api<{ actions: Action[] }>("/actions");
        history.value = recent.actions;
        action.value = recent.actions.find(candidate => ["proposed", "running", "uncertain", "failed"].includes(candidate.status)) || action.value;
        await conversation.recover();
      } catch (recoveryError) {
        if (recoveryError instanceof ApiError && [401, 403, 409].includes(recoveryError.status)) throw recoveryError;
        recoveryWarning.value = "Could not load the existing change. Check recent changes before preparing another item.";
      }
      throw new ApiError(cause.status, cause.code, `${cause.message} Your photo and entered details are still here. Check the existing change before preparing another.`);
    }
    detail.value = null;
  });
}
async function proposeCreate() {
  if (capturePending.value) { error.value = "The photo is still processing. Keep editing, or remove the photo to continue without it."; return; }
  await propose("item.create", {
    ...draft.value, name: draft.value.name.trim(), ...(draftLocation.value ? { locationId: draftLocation.value } : {}),
  }, captureId.value);
}
async function confirm() {
  const current = action.value;
  if (conversationConfirmBlocked.value) return;
  if (!current || !(current.status === "proposed" || (current.status === "failed" && current.retryable === true))) return;
  await perform("Saving confirmed change", async () => {
    const key = confirmationKey(current);
    try {
      action.value = await api<Action>(`/actions/${encodeURIComponent(current.id)}/confirm`, {
        method: "POST", body: { revision: current.revision }, idempotencyKey: key,
      });
    } catch (cause) {
      if (cause instanceof ApiError && ["stale_response", "request_cancelled"].includes(cause.code)) throw cause;
      // A lost response must never present a fresh create/confirm as the default.
      action.value = { ...current, status: "uncertain" };
      throw cause;
    }
    if (action.value.status === "succeeded") {
      results.value = []; searched.value = false;
      await loadLocations();
    }
    if (conversationAction.value) await conversation.recover();
  });
}
async function refreshAction() {
  if (!action.value) return;
  await perform("Checking change status", async () => {
    action.value = await api<Action>(`/actions/${encodeURIComponent(action.value!.id)}`);
    if (action.value.status === "succeeded") {
      await loadLocations();
    }
    if (conversationAction.value) await conversation.recover();
  });
}
async function cancelAction() {
  if (conversationRecoveryBlocked.value) return;
  if (!action.value) return;
  await perform("Cancelling proposal", async () => {
    action.value = await api<Action>(`/actions/${encodeURIComponent(action.value!.id)}/cancel`, { method: "POST", body: {} });
    if (conversationAction.value) await conversation.recover();
  });
}
function beginEdit(mode: "move" | "edit" | "tags") {
  if (!detail.value) return;
  editMode.value = mode;
  destination.value = detail.value.locationId || "";
  editedTags.value = tagNames(detail.value).join(", ");
  editDraft.value = { ...emptyDraft(), name: detail.value.name, quantity: detail.value.quantity || 1,
    manufacturer: detail.value.manufacturer || "", model: detail.value.model || "", serialNumber: detail.value.serialNumber || "",
    notes: detail.value.description || "" };
}
async function proposeEdit() {
  if (!detail.value) return;
  await propose("item.update", { entityId: detail.value.id, name: editDraft.value.name,
    quantity: editDraft.value.quantity, manufacturer: editDraft.value.manufacturer, model: editDraft.value.model,
    serialNumber: editDraft.value.serialNumber, description: editDraft.value.notes });
}
async function createTag(label: string) {
  if (!detail.value) return;
  await perform("Creating stable tag", async () => {
    const mapping = await api<Mapping>("/tags/mappings", { method: "POST", body: {
      entityId: detail.value!.id, targetType: detail.value!.isLocation ? "location" : "item", label: label || detail.value!.name,
    } });
    mappings.value.push(mapping); notice.value = "Stable tag created. Open it below for its QR code and NFC URL.";
  });
}
async function updateTag(mapping: Mapping, payload: Record<string, string>) {
  await perform("Updating tag mapping", async () => {
    const updated = await api<Mapping>(`/tags/mappings/${encodeURIComponent(mapping.tagId)}`, { method: "PATCH", body: payload });
    mappings.value = mappings.value.map(m => m.tagId === updated.tagId ? updated : m).filter(m => m.entityId === detail.value?.id);
    notice.value = "Tag mapping updated. The tag URL has not changed.";
  });
}
function addHere() {
  draftLocation.value = detail.value!.id; detail.value = null; tab.value = "capture";
}
onMounted(async () => {
  window.addEventListener("online", updateOnline); window.addEventListener("offline", updateOnline);
  try { session.value = await api<Session>("/session"); await perform("Loading your inventory", afterSignIn); }
  catch (cause) {
    if (!(cause instanceof ApiError && cause.status === 401)) error.value = cause instanceof Error ? cause.message : "Could not connect.";
  } finally { initialLoading.value = false; }
});
onUnmounted(() => {
  conversation.reset(); clearPhoto(); window.removeEventListener("online", updateOnline); window.removeEventListener("offline", updateOnline);
});
</script>

<template>
  <a class="skip-link" href="#main">Skip to inventory</a>
  <div class="app-frame">
    <header class="app-header">
      <a href="/" class="brand" aria-label="Homebox Companion home" @click="home"><img src="/icon.svg" width="38" height="38" alt=""><span>homebox<small>COMPANION</small></span></a>
      <span class="private-label">On your home server</span>
      <AiDetails v-if="session" :ai="session.ai"/>
      <button v-if="session" class="text-button" :disabled="busy || !online" @click="logout">Sign out</button>
    </header>
    <main id="main">
      <div v-if="!online" class="banner warning" role="status"><strong>You’re offline.</strong> The app shell is available, but inventory, photos, and changes need a connection. Private data is not cached.</div>
      <div v-if="error" class="banner error" role="alert">{{ error }}<button type="button" class="text-button" @click="error = ''" aria-label="Dismiss error">Dismiss</button></div>
      <div v-if="notice" class="banner success" role="status">{{ notice }}</div>
      <div v-if="busy" class="busy-status" role="status"><span class="spinner" aria-hidden="true"></span>{{ busyLabel }}…</div>
      <section v-if="initialLoading" class="card loading" aria-label="Loading session">Connecting to your inventory…</section>
      <section v-else-if="!session" class="login-layout">
        <div class="intro"><p class="eyebrow">A little less searching</p><h1>Everything has<br>a place.</h1><p>Find it, photograph it, and put it back where it belongs.</p><div class="privacy-note">Your inventory stays in Homebox. AI suggestions are optional and always need your review.</div></div>
        <div class="card login-card">
          <h2>Welcome home</h2><p class="muted">Sign in with your existing Homebox account.</p>
          <p v-if="tagId" class="inset">You opened a storage tag. Sign in to view it. The tag does not grant access to inventory.</p>
          <form class="stack" @submit.prevent="login"><fieldset :disabled="busy || !online">
            <label>Email or username<input v-model="username" autocomplete="username" required maxlength="255" autocapitalize="none" spellcheck="false"></label>
            <label>Password<input v-model="password" type="password" autocomplete="current-password" required></label>
            <button type="submit" class="primary full-width">Sign in</button>
          </fieldset></form>
          <p class="small muted">No account yet? Ask your Homebox administrator. This app uses the collections you already have access to.</p>
          <p class="small muted">Use your native Homebox password. OIDC-only single sign-on is not supported by this companion sign-in.</p>
          <p class="small muted">Install from your browser menu: “Install app” or “Add to Home Screen”. Use HTTPS on your phone.</p>
        </div>
      </section>
      <template v-else>
        <section v-if="tab !== 'settings'" class="collection-bar" aria-label="Active collection"><div><p class="eyebrow">Your inventory</p><h1>{{ currentCollection }}</h1></div><label>Collection<select aria-label="Collection" :value="session.collectionId" :disabled="busy || !online" @change="changeCollection"><option v-for="collection in session.collections" :key="collection.id" :value="collection.id">{{ collection.name }}</option></select></label></section>
        <nav class="tabs" :class="{ 'has-ai-settings': canManageSettings }" aria-label="Companion tools"><button :disabled="inventoryDisabled" :aria-current="tab === 'ask' ? 'page' : undefined" @click="showTab('ask')">Find &amp; ask</button><button :disabled="inventoryDisabled" :aria-current="tab === 'capture' ? 'page' : undefined" @click="showTab('capture')">Add an item</button><button :disabled="inventoryDisabled" :aria-current="tab === 'places' ? 'page' : undefined" @click="showTab('places')">Places</button><button v-if="canManageSettings" :aria-current="tab === 'settings' ? 'page' : undefined" @click="showTab('settings')">AI settings</button></nav>
        <AiSettings v-if="tab === 'settings' && canManageSettings" ref="settingsView" :api="api" :online="online" @snapshot="settingsSnapshot" @boundary="settingsBoundary"/>
        <section v-else-if="tab === 'settings'" class="card stack" role="alert"><h2>AI settings unavailable</h2><p>Only the original installation account can manage AI settings. Sign in with that account or ask your local operator to check its access.</p></section>
        <template v-else>
        <p v-if="!session.collections.length" class="banner warning">Your account has no accessible collections. Ask your Homebox administrator for access.</p>
        <p v-if="recoveryWarning" class="banner warning" role="status">{{ recoveryWarning }}</p>
        <div v-if="tagError" class="banner warning" role="alert"><strong>Tag unavailable.</strong> {{ tagError }}<button class="text-button" :disabled="busy || !online" @click="perform('Checking tag', resolveTag)">Try tag again</button></div>
        <p v-if="tagId && mappingLabel && detail" class="tag-context">Opened from tag: {{ mappingLabel }}</p>
        <div v-if="!recoveryReady && !busy" class="banner warning">Recent changes must load before you continue.<button class="secondary" :disabled="!online" @click="perform('Loading recent changes', afterSignIn)">Load recent changes</button></div>
        <fieldset class="workspace" :disabled="busy || !online || !session.collectionId || !recoveryReady">
          <CaptureProgress v-if="captureJob || captureIssue || capturePending" :job="captureJob" :pending="capturePending" :issue="captureIssue" :has-local-photo="!!preview" @open="detail = null; tab = 'capture'" @dismiss="clearPhoto"/>
          <details v-if="captureHistory.length" class="card"><summary>Recent photo jobs ({{ captureHistory.length }})</summary><p class="small muted">Resume an existing upload instead of sending it again. Check recent inventory changes before creating an item from an older photo.</p><ul class="plain-list"><li v-for="previous in captureHistory" :key="previous.id"><button type="button" class="text-button" @click="resumeCaptureJob(previous)">{{ new Date(previous.createdAt).toLocaleString() }} · {{ previous.status }}</button></li></ul></details>
          <details v-if="history.length" class="card"><summary>Recent changes &amp; recovery ({{ history.length }})</summary><p class="small muted">Check previous attempts before creating the same item again. Uncertain changes are never retried automatically.</p><ul class="plain-list"><li v-for="previous in history" :key="previous.id"><button class="text-button" type="button" :data-action-id="previous.id" @click="selectHistoryAction(previous)">{{ previous.summary }} · {{ previous.status }}</button></li></ul></details>
          <ActionCard v-if="action && (!conversationAction || detail || tab !== 'ask')" :action="action" :busy="busy || conversation.state.sending || (conversationAction && (conversation.state.unsettled || conversation.state.recovering))" :confirm-disabled="conversationConfirmBlocked" :cancel-disabled="conversationRecoveryBlocked" :photo-preview="reviewPhoto" @confirm="confirm" @cancel="cancelAction" @refresh="refreshAction" @dismiss="dismissAction" @open="view"/>
          <section v-if="detail" class="card detail-view">
            <button class="text-button" type="button" aria-label="Back to inventory" @click="detail = null; editMode = ''">‹ Back to inventory</button>
            <p class="eyebrow">{{ detail.isLocation ? "Location" : "Inventory item" }}</p><h2>{{ detail.name }}</h2>
            <p class="location-path">{{ detail.locationPath || "No location assigned" }}</p>
            <p v-if="detail.description">{{ detail.description }}</p>
            <dl class="details"><dt>Entity ID</dt><dd class="break-anywhere">{{ detail.id }}</dd><dt v-if="detail.quantity !== undefined">Quantity</dt><dd v-if="detail.quantity !== undefined">{{ detail.quantity }}</dd><dt v-if="detail.manufacturer">Manufacturer</dt><dd v-if="detail.manufacturer">{{ detail.manufacturer }}</dd><dt v-if="detail.model">Model</dt><dd v-if="detail.model">{{ detail.model }}</dd><dt v-if="tagNames(detail).length">Tags</dt><dd v-if="tagNames(detail).length">{{ tagNames(detail).join(", ") }}</dd></dl>
            <template v-if="detail.isLocation">
              <button class="primary" :disabled="!!pending" @click="addHere">Add item here</button>
              <h3>Contents</h3><EntityList v-if="contents.length" :items="contents" @open="view"/><p v-else class="muted">No items in this location.</p>
              <h3 v-if="locations.some(l => l.parentId === detail!.id)">Nested locations</h3>
              <ul class="plain-list"><li v-for="location in locations.filter(l => l.parentId === detail!.id)" :key="location.id"><button class="text-button" @click="openLocation(location)">{{ location.name }} ›</button></li></ul>
            </template>
            <div v-else class="button-row"><button class="secondary" :disabled="!!pending" @click="beginEdit('move')">Move item</button><button class="secondary" :disabled="!!pending" @click="beginEdit('edit')">Edit details</button><button class="secondary" :disabled="!!pending" @click="beginEdit('tags')">Edit tags</button></div>
            <form v-if="editMode === 'move'" class="inset stack" @submit.prevent="propose('item.move', {entityId: detail!.id, locationId: destination})">
              <label>Move to<select v-model="destination" aria-label="Move to" required><option value="" disabled>Choose an exact destination</option><option v-for="location in locations" :key="location.id" :value="location.id">{{ location.path || location.name }}</option></select></label>
              <button class="primary" type="submit">Review move</button>
            </form>
            <form v-if="editMode === 'edit'" class="inset stack" @submit.prevent="proposeEdit">
              <label>Item name<input v-model="editDraft.name" required maxlength="255"></label><label>Quantity<input v-model.number="editDraft.quantity" type="number" required min="1" step="1"></label><label>Manufacturer<input v-model="editDraft.manufacturer" maxlength="255"></label><label>Model<input v-model="editDraft.model" maxlength="255"></label><label>Serial number<input v-model="editDraft.serialNumber" maxlength="255"></label><label>Description<textarea v-model="editDraft.notes" rows="3" maxlength="10000"></textarea></label><button type="submit" class="primary">Review edits</button>
            </form>
            <form v-if="editMode === 'tags'" class="inset stack" @submit.prevent="propose('item.tags', {entityId: detail!.id, tags: editedTags.split(',').map(t => t.trim()).filter(Boolean)})"><label>Replacement tags, separated by commas<input v-model="editedTags" maxlength="2000"></label><p class="muted small">This replaces the item’s tags. An empty list removes all tags.</p><button class="primary" type="submit">Review tag changes</button></form>
            <hr><TagPanel :key="detail.id" :mappings="mappings" :entity="detail" :locations="locations" @create="createTag" @update="updateTag"/>
          </section>
          <template v-else>
            <section v-if="tab === 'ask'" class="stack">
              <ConversationPanel :state="conversation.state" :collection-name="currentCollection" :action="action" :disabled="busy || !online || !recoveryReady"
                @composer="conversation.updateComposer" @send="conversation.send" @retry="conversation.retry" @recover="conversation.recover" @restore="conversation.restore"
                @select="conversation.select" @page="conversation.page" @inspect="view" @collection="switchCollection" @fresh="conversation.fresh"
                @edit="conversation.edit" @stop-edit="conversation.state.editing = false">
                <template #review="{ edit }"><ActionCard v-if="action && conversationAction" :action="action" :busy="busy || conversation.state.sending || conversation.state.recovering || conversation.state.unsettled" :editable="action.status === 'proposed' && !conversation.state.editing" :confirm-disabled="conversationConfirmBlocked" :cancel-disabled="conversationRecoveryBlocked" :photo-preview="reviewPhoto"
                  @confirm="confirm" @edit="edit" @cancel="cancelAction" @refresh="refreshAction" @dismiss="dismissAction" @open="view"/></template>
              </ConversationPanel>
              <form class="search-row" @submit.prevent="search"><label class="grow"><span class="sr-only">Search inventory</span><input v-model="query" type="search" placeholder="Search items directly by name…" maxlength="1000"></label><button type="submit" class="primary">Search</button></form>
              <section v-if="searched" class="card"><h3>{{ resultTotal }} {{ resultTotal === 1 ? "result" : "results" }}</h3><EntityList :items="results" @open="view"/><p v-if="!results.length" class="muted">No matching items. Try fewer words or browse Places.</p><p v-if="resultTotal > results.length" class="small muted">Showing {{ results.length }} of {{ resultTotal }} matches. Narrow your search to find the right item.</p></section>
            </section>
            <section v-if="tab === 'capture'" class="stack">
              <div class="hero-copy"><p class="eyebrow">A new thing, a proper home</p><h2>Start with a photo.<br>Or just the details.</h2><p class="muted">You review every suggestion before anything is saved.</p></div>
              <div v-if="pending" class="inset">Finish or cancel the change above before creating another item.</div>
              <template v-else>
                <section class="card stack">
                  <label class="photo-picker">Take or choose a photo<input ref="fileInput" type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" capture="environment" @change="choosePhoto"></label>
                  <p class="small muted">JPEG, PNG, WebP, HEIC or HEIF · up to 12 MB. Your server validates the photo. Device HEIC support varies.</p>
                  <template v-if="preview"><img :src="preview" class="photo-preview" alt="Selected item photo preview"><label>Optional hint<input v-model="hint" placeholder="Anything the camera can’t see?" maxlength="1000" :disabled="capturePending"></label><div class="button-row"><button class="primary" :disabled="capturePending" @click="analyze">Suggest details from photo</button><button class="secondary" @click="clearPhoto">Remove photo</button></div><p v-if="!captureId" class="small muted">Send the photo to your server before reviewing the item to attach it. If the model is unavailable, an accepted upload can still be saved with manually entered details.</p></template>
                  <div v-else-if="captureId" class="inset stack"><p>Your uploaded photo is retained on the server. A local preview is not available after reload.</p><button type="button" class="secondary" @click="clearPhoto">Remove uploaded photo</button></div>
                </section>
                <section class="card stack"><h3>{{ captureId ? "Review item details" : "Enter item details" }}</h3><p v-for="warning in warnings" :key="warning" class="banner warning">{{ warning }}</p>
                  <section v-if="duplicates.length"><h3>Possible duplicates</h3><p class="small muted">These are suggestions, not automatic merges. Check them before creating another item.</p><EntityList :items="duplicates" @open="view"/></section>
                  <p v-if="capturePending" class="small muted">You can edit details while the photo is processing. Review when it finishes, or remove the photo to continue without attaching it.</p>
                  <DraftForm v-model="draft" :location-id="draftLocation" @update:location-id="updateDraftLocation" :locations="locations" :review-disabled="capturePending" @edit="recordDraftEdit" @submit="proposeCreate"/>
                  <button type="button" class="text-button" @click="resetDraft">Clear draft and photo</button>
                </section>
              </template>
            </section>
            <section v-if="tab === 'places'" class="stack">
              <div class="hero-copy"><p class="eyebrow">A place for everything</p><h2>Look inside.</h2><p class="muted">Open a location to see its contents, add an item, or make a label.</p></div>
              <label>Filter locations<input v-model="query" type="search" placeholder="Garage, electronics…" maxlength="1000"></label>
              <div class="card"><ul class="plain-list location-list"><li v-for="location in matchingLocations" :key="location.id"><button class="entity-button" @click="openLocation(location)"><span class="entity-icon" aria-hidden="true">▤</span><span><strong>{{ location.name }}</strong><small>{{ location.path }}</small></span><span class="chevron" aria-hidden="true">›</span></button></li></ul><p v-if="!matchingLocations.length" class="muted">No matching locations yet.</p></div>
              <details class="card"><summary>Create a storage location</summary><form class="stack" @submit.prevent="propose('location.create', {name: locationName.trim(), ...(locationParent ? {parentId: locationParent} : {}), description: locationDescription})"><label>Location name<input v-model="locationName" required maxlength="255"></label><label>Inside<select v-model="locationParent" aria-label="Inside"><option value="">Top level</option><option v-for="location in locations" :key="location.id" :value="location.id">{{ location.path || location.name }}</option></select></label><label>Description<textarea v-model="locationDescription" maxlength="10000" rows="2"></textarea></label><button class="primary" type="submit" :disabled="!!pending">Review new location</button></form></details>
            </section>
          </template>
        </fieldset>
        </template>
      </template>
    </main>
    <footer>Private by design. Organized by you.<span>Homebox Companion</span></footer>
  </div>
</template>
