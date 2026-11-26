<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { ApiError, createApi } from "../api";
import { aiProviders, aiRoleNames, endpointChanged, readSettings, roleIssue, settingsDirty, settingsDraft, settingsUpdate, type AiSettingsDraft } from "../ai-settings-editor";
import { providerLabel } from "../ai-metadata";
import type { AiSettings } from "../types";

const props = defineProps<{ api: ReturnType<typeof createApi>; online: boolean }>();
const emit = defineEmits<{ snapshot: [value: AiSettings]; boundary: [cause: unknown] }>();
const saved = ref<AiSettings | null>(null), draft = ref<AiSettingsDraft | null>(null);
const loading = ref(false), saving = ref(false), issue = ref(""), notice = ref(""), reloadRequired = ref(false);
const discardDialog = ref<HTMLDialogElement | null>(null);
const roleTitles = { chat: "Chat", vision: "Images" };
const dirty = computed(() => !!saved.value && !!draft.value && settingsDirty(saved.value, draft.value));
const invalid = computed(() => !saved.value || !draft.value || aiRoleNames.some(name => roleIssue(saved.value![name], draft.value![name])));
let controller: AbortController | null = null, disposed = false;
let leavePromise: Promise<boolean> | null = null, decideLeave: ((value: boolean) => void) | null = null;
let previousFocus: HTMLElement | null = null;

function clearPrivate() {
  if (draft.value) for (const name of aiRoleNames) draft.value[name].newKey = "";
  draft.value = null; saved.value = null; issue.value = ""; notice.value = "";
}
function install(value: AiSettings) {
  clearPrivate(); saved.value = value; draft.value = settingsDraft(value); reloadRequired.value = false;
  emit("snapshot", value);
}
function fail(cause: unknown, writing = false) {
  if (disposed || (cause instanceof ApiError && ["request_cancelled", "stale_response"].includes(cause.code))) return;
  if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403 || cause.code === "COLLECTION_CONTEXT_CHANGED")) {
    clearPrivate(); emit("boundary", cause); return;
  }
  if (cause instanceof ApiError && cause.status === 409) {
    reloadRequired.value = true;
    issue.value = "AI settings changed elsewhere. Your draft is still here. Reload saved settings before saving again; reloading discards this draft.";
  } else if (cause instanceof ApiError && cause.status === 422) {
    issue.value = "Settings were not saved. Check both roles, including the base URLs, exact model IDs, and explicit API key actions.";
  } else if (writing) {
    reloadRequired.value = true;
    issue.value = "The save could not be verified. It may have reached the server. Your draft is still here. Reload saved settings before making another change. Nothing will be retried automatically.";
  } else issue.value = "Could not load AI settings. Reconnect and try loading them again.";
}
async function load() {
  if (!props.online || loading.value || saving.value) return;
  loading.value = true; issue.value = ""; notice.value = "";
  controller = new AbortController();
  try { const value = readSettings(await props.api("/settings/ai", { signal: controller.signal })); if (!disposed) install(value); }
  catch (cause) { fail(cause); }
  finally { if (!disposed) loading.value = false; }
}
async function save() {
  if (!saved.value || !draft.value || !props.online || loading.value || saving.value || invalid.value || !dirty.value || reloadRequired.value) return;
  saving.value = true; issue.value = ""; notice.value = ""; controller = new AbortController();
  try {
    const value = readSettings(await props.api("/settings/ai", { method: "PUT", body: settingsUpdate(saved.value, draft.value), signal: controller.signal }));
    if (!disposed) { install(value); notice.value = "AI settings saved. New chat and photo requests use these settings. Saving did not test a provider."; }
  } catch (cause) { fail(cause, true); }
  finally { if (!disposed) saving.value = false; }
}
function keyActionChanged(name: typeof aiRoleNames[number]) {
  if (draft.value) draft.value[name].newKey = "";
}
function resolveLeave(discard: boolean) {
  if (discard && saved.value) {
    if (draft.value) for (const name of aiRoleNames) draft.value[name].newKey = "";
    draft.value = settingsDraft(saved.value); notice.value = ""; issue.value = "";
  }
  discardDialog.value?.close();
  const resolve = decideLeave; decideLeave = null; leavePromise = null; resolve?.(discard);
  if (!discard) previousFocus?.focus();
}
async function confirmLeave(): Promise<boolean> {
  if (saving.value) return false;
  if (!dirty.value) return true;
  if (leavePromise) return leavePromise;
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  leavePromise = new Promise<boolean>(resolve => { decideLeave = resolve; });
  const answer = leavePromise;
  await nextTick(); discardDialog.value?.showModal();
  return answer;
}
async function reload() { if (await confirmLeave()) await load(); }
async function discard() { if (await confirmLeave()) notice.value = reloadRequired.value ? "Draft discarded. Reload saved settings to see what is active." : "Draft discarded. Saved settings have not changed."; }
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || saving.value) { event.preventDefault(); event.returnValue = ""; }
}
function pageHide() { controller?.abort(); resolveLeave(false); clearPrivate(); loading.value = false; saving.value = false; }
function pageShow(event: PageTransitionEvent) { if (event.persisted) void load(); }
watch(dirty, value => { if (value) notice.value = ""; });
onMounted(() => {
  window.addEventListener("beforeunload", beforeUnload); window.addEventListener("pagehide", pageHide);
  window.addEventListener("pageshow", pageShow); void load();
});
onUnmounted(() => {
  disposed = true; controller?.abort(); clearPrivate(); decideLeave?.(false); decideLeave = null;
  window.removeEventListener("beforeunload", beforeUnload);
  window.removeEventListener("pagehide", pageHide); window.removeEventListener("pageshow", pageShow);
});
defineExpose({ confirmLeave, clearPrivate });
</script>

<template>
  <section class="ai-settings stack" aria-labelledby="ai-settings-heading" :aria-busy="loading || saving">
    <header class="hero-copy stack">
      <div><p class="eyebrow">Original installation account</p><h2 id="ai-settings-heading">AI settings</h2></div>
      <p class="muted">Choose the provider and exact model ID for chat and images. These settings apply across this companion, not just one collection.</p>
      <p class="small muted">Edits stay on this page until you Save both roles. Saving does not connect to a provider, list or download models, or run AI. Existing work keeps its original settings.</p>
    </header>
    <p v-if="!online" class="banner warning" role="status">Reconnect to load or save AI settings. Unsaved edits stay only in this page’s memory.</p>
    <p v-if="loading" class="inset" role="status">Loading saved AI settings…</p>
    <p v-if="issue" class="banner error" role="alert">{{ issue }}</p>
    <p v-if="notice" class="banner success" role="status">{{ notice }}</p>
    <form v-if="saved && draft" class="stack" autocomplete="off" @submit.prevent="save">
      <fieldset :disabled="!online || loading || saving" class="stack">
        <fieldset v-for="name in aiRoleNames" :key="name" class="card stack ai-role" :aria-describedby="`${name}-role-note`">
          <legend>{{ roleTitles[name] }}</legend>
          <p :id="`${name}-role-note`" class="small muted">{{ name === "chat" ? "Used for inventory conversations." : "Used to suggest item details from photos." }} Choosing a remote provider sends that role’s content to its configured endpoint.</p>
          <div class="form-grid ai-settings-grid">
            <label :for="`${name}-provider`">Provider<select :id="`${name}-provider`" v-model="draft[name].provider" :aria-label="`${roleTitles[name]} provider`"><option v-for="provider in aiProviders" :key="provider" :value="provider">{{ providerLabel({ provider }) }}</option></select></label>
            <label :for="`${name}-model`">Model ID<input :id="`${name}-model`" v-model="draft[name].model" :aria-label="`${roleTitles[name]} model ID`" required maxlength="256" autocapitalize="none" spellcheck="false" :list="`${name}-models`"><small>Use the exact ID supported by your endpoint. Custom IDs are allowed.</small></label>
          </div>
          <datalist :id="`${name}-models`"><option v-if="draft[name].provider === saved[name].provider" :value="saved[name].model">Currently saved model</option></datalist>
          <label :for="`${name}-base-url`">Base URL<input :id="`${name}-base-url`" v-model="draft[name].baseUrl" :aria-label="`${roleTitles[name]} base URL`" type="url" required maxlength="2048" autocapitalize="none" spellcheck="false"><small>Full provider base endpoint, including its path. Do not put credentials or API keys in the URL.</small></label>
          <p :id="`${name}-key-state`" class="small" role="status">{{ saved[name].keyConfigured ? "An API key is configured. Its value is never returned." : "No API key is configured." }}</p>
          <label :for="`${name}-key-action`">API key action<select :id="`${name}-key-action`" v-model="draft[name].keyAction" :aria-label="`${roleTitles[name]} API key action`" :aria-describedby="`${name}-key-state`" @change="keyActionChanged(name)"><option value="keep">Keep current key setting</option><option value="replace">Replace with a new key</option><option value="clear">Clear the stored key</option></select></label>
          <label v-if="draft[name].keyAction === 'replace'" :for="`${name}-api-key`">New API key<input :id="`${name}-api-key`" v-model="draft[name].newKey" :aria-label="`${roleTitles[name]} new API key`" type="password" autocomplete="new-password" required maxlength="4096" autocapitalize="none" spellcheck="false"><small>Write-only. Kept only in memory until Save, discard, sign-out, or leaving this page.</small></label>
          <p v-if="draft[name].keyAction === 'clear'" class="inset small">The stored {{ roleTitles[name].toLowerCase() }} API key will be removed only when you Save both roles.</p>
          <p v-if="endpointChanged(saved[name], draft[name]) && draft[name].keyAction === 'keep'" class="banner warning" role="status">The provider or endpoint changed. Choose Replace or Clear explicitly, even if no key was previously configured.</p>
          <p v-if="roleIssue(saved[name], draft[name])" class="small ai-field-error">{{ roleIssue(saved[name], draft[name]) }}</p>
        </fieldset>
        <p class="small muted" role="status">{{ saving ? "Saving both roles…" : dirty ? "You have unsaved changes." : `Saved revision ${saved.revision}. No unsaved changes.` }}</p>
        <div class="button-row"><button type="submit" class="primary" :disabled="!dirty || !!invalid || reloadRequired">Save both roles</button><button type="button" class="secondary" :disabled="!dirty" @click="discard">Discard changes</button></div>
      </fieldset>
    </form>
    <button type="button" class="text-button" :disabled="!online || loading || saving" @click="reload">{{ saved ? "Reload saved settings" : "Load saved settings" }}</button>
    <dialog ref="discardDialog" class="ai-discard-dialog" aria-labelledby="ai-discard-heading" aria-describedby="ai-discard-description" @cancel.prevent="resolveLeave(false)">
      <div class="stack"><h2 id="ai-discard-heading">Discard unsaved AI settings?</h2><p id="ai-discard-description">Your provider, model and key edits will be cleared. Nothing will be saved.</p><div class="button-row"><button type="button" class="primary" autofocus @click="resolveLeave(false)">Keep editing</button><button type="button" class="secondary" @click="resolveLeave(true)">Discard draft</button></div></div>
    </dialog>
  </section>
</template>

<style scoped>
.ai-role{min-width:0}.ai-role legend{font-size:1.1rem;font-weight:700;padding:0 .45rem;margin-left:-.45rem}.ai-settings-grid{align-items:start}.ai-settings input,.ai-settings select{min-width:0}.ai-field-error{color:#842f21}.ai-discard-dialog{max-width:min(480px,calc(100vw - 32px));max-height:calc(100dvh - 32px);overflow:auto;padding:24px;border:1px solid var(--line);border-radius:16px;color:inherit;background:#fff}.ai-discard-dialog::backdrop{background:#102c2599}@media(max-width:480px){.ai-settings-grid{grid-template-columns:1fr}}
</style>
