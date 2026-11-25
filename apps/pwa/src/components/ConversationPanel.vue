<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { Action, Entity } from "../types";
import type { ConversationMessage, ConversationState } from "../conversation-types";
import ChatComposer from "./ChatComposer.vue";
import ChatRecordCards from "./ChatRecordCards.vue";
import ChatDestinations from "./ChatDestinations.vue";
const props = defineProps<{ state: ConversationState; collectionName: string; action: Action | null; disabled: boolean }>();
const emit = defineEmits<{
  composer: [value: string]; send: [text: string, mode: "normal" | "read_only"]; retry: [id: string]; recover: []; restore: [];
  select: [turnId: string, candidateId: string]; page: [turnId: string]; inspect: [entity: Entity];
  collection: [id: string]; fresh: []; edit: []; "stop-edit": [];
}>();
const log = ref<HTMLOListElement | null>(null);
const composer = ref<InstanceType<typeof ChatComposer> | null>(null);
const stick = ref(true);
const unseen = ref(false);
const announcement = ref("");
const unresolved = computed(() => props.action && ["proposed", "running", "uncertain"].includes(props.action.status));
function scroll() {
  const element = log.value;
  if (!element) return;
  const last = props.state.messages.at(-1);
  const previous = props.state.messages.at(-2);
  const target = last && element.querySelector<HTMLElement>(`[data-turn-id="${CSS.escape(last.id)}"]`);
  const start = previous?.role === "user" && last?.role === "assistant"
    ? element.querySelector<HTMLElement>(`[data-turn-id="${CSS.escape(previous.id)}"]`) : target;
  element.scrollTop = target && start && target.offsetTop + target.offsetHeight - start.offsetTop > element.clientHeight
    ? Math.max(0, start.offsetTop - element.offsetTop - 12) : element.scrollHeight;
  stick.value = true; unseen.value = false;
}
function trackScroll() {
  const element = log.value;
  if (element) {
    stick.value = element.scrollHeight - element.clientHeight - element.scrollTop < 80;
    if (stick.value) unseen.value = false;
  }
}
function send(text: string, mode: "normal" | "read_only" = "normal") {
  if (props.state.sending || props.state.unsettled || props.disabled || !text.trim()) return;
  stick.value = true; emit("send", text, mode); composer.value?.focus();
}
function startEdit() { emit("edit"); composer.value?.focus(); }
function currentChoice(turn: ConversationMessage) {
  return Boolean(turn.reply?.clarification && props.state.clarification
    && turn.reply.clarification.id === props.state.clarification.id
    && turn.reply.clarification.revision === props.state.clarification.revision);
}
function activeReview(turn: ConversationMessage, index: number) {
  const id = turn.reply?.action?.id;
  return id && id === props.action?.id && id === props.state.linkedActionId
    && !props.state.messages.slice(index + 1).some(message => message.reply?.action?.id === id);
}
const orphanReview = computed(() => props.action?.id === props.state.linkedActionId
  && !props.state.messages.some(message => message.reply?.action?.id === props.action?.id));
watch(() => props.state.messages.map(turn => `${turn.id}:${turn.status}:${turn.text}`).join("|"), async () => {
  const wasAtEnd = stick.value;
  await nextTick();
  if (wasAtEnd) scroll(); else unseen.value = true;
  const last = props.state.messages.at(-1);
  if (last?.role === "assistant" && last.status === "complete") announcement.value = "Homebox replied. New response available.";
});
watch(() => props.action?.status, async () => { if (stick.value) { await nextTick(); scroll(); } });
</script>
<template>
  <section class="card conversation" aria-label="Inventory conversation">
    <header class="conversation-heading">
      <div><p class="eyebrow">Inventory assistant</p><h2>Let's find its place.</h2>
        <p class="conversation-scope">{{ state.scope?.instanceLabel || "Connected Homebox" }} · {{ state.scope?.collectionName || collectionName }}</p>
      </div>
      <button type="button" class="text-button" :disabled="disabled || state.sending || state.recovering || state.unsettled || !!unresolved" :title="unresolved ? 'Finish or cancel the current change first' : undefined" @click="emit('fresh')">New chat</button>
    </header>
    <aside v-if="state.task" class="pending-conversation-task" aria-label="Pending conversation task">
      <p class="task-summary" :title="state.task.summary"><strong>{{ state.editing ? "Editing:" : "Draft:" }}</strong> {{ state.task.summary }}</p>
      <div class="button-row"><button type="button" class="text-button" aria-label="Browse destinations for the pending draft" :disabled="state.sending || disabled" @click="send('Which destinations do I have access to?', 'read_only')">Destinations</button>
        <button v-if="!unresolved" type="button" class="text-button" :disabled="state.sending || disabled" @click="emit('fresh')">Cancel draft</button>
        <button v-if="state.editing" type="button" class="text-button" :disabled="state.sending || disabled" @click="emit('stop-edit')">Keep original preview</button>
        <details class="task-context-help" :open="state.editing"><summary>Draft help</summary>
          <p v-if="state.editing">Describe the changes below, then Send for a fresh preview. The previous preview cannot be confirmed while you edit.</p>
          <p v-else-if="state.clarification?.slot === 'review'">Use Review draft below to prepare a complete preview. This does not save an inventory change.</p>
          <p v-else-if="state.clarification">Choose a {{ state.clarification.slot }} below. You can ask questions without losing this draft.</p>
          <p v-else>Review the complete preview before confirming. You can still ask read-only questions.</p>
        </details>
      </div>
    </aside>
    <ol ref="log" class="transcript chat-log" aria-label="Conversation messages" @scroll.passive="trackScroll">
      <li v-if="!state.messages.length" class="turn assistant">
        <p class="eyebrow">Homebox</p><p class="message-text">What would you like to find or organize in {{ collectionName }}?</p>
        <p class="small muted">I can show items, browse destinations, and prepare changes for your review.</p>
        <div class="chat-chips"><button type="button" class="secondary" :disabled="disabled || state.sending" @click="send('Which inventory do I have?', 'read_only')">Show inventory</button><button type="button" class="secondary" :disabled="disabled || state.sending" @click="send('Which destinations do I have access to?', 'read_only')">Browse destinations</button><button type="button" class="secondary" :disabled="disabled || state.sending" @click="send('I want to add an item')">Add with chat</button></div>
      </li>
      <li v-for="(turn, index) in state.messages" :key="turn.id" :class="['turn', turn.role]" :data-turn-id="turn.id">
        <p class="eyebrow">{{ turn.role === "user" ? "You" : "Homebox" }}</p>
        <p class="message-text">{{ turn.text }}</p>
        <p v-if="turn.status === 'sending'" class="small muted">Sent · waiting for a response</p>
        <div v-if="turn.error" class="chat-turn-error" role="alert"><p>{{ turn.error }}</p><button type="button" class="text-button" :disabled="disabled || state.sending" @click="stick = true; emit('retry', turn.id)">Check and retry this turn</button></div>
        <template v-if="turn.reply">
          <div v-if="turn.reply.overview" class="overview-counts" aria-label="Current collection overview">
            <div><strong>{{ turn.reply.overview.itemCount }}</strong><span>item records</span></div><div><strong>{{ turn.reply.overview.locationCount }}</strong><span>locations</span></div>
            <p class="small muted">{{ turn.reply.overview.includesArchived ? "Includes archived records" : "Excludes archived records" }} · {{ turn.reply.scope?.collectionName || collectionName }}</p>
          </div>
          <ChatRecordCards v-if="turn.reply.items.length" :items="turn.reply.items" @open="emit('inspect', $event)"/>
          <template v-if="turn.reply.clarification">
            <template v-if="turn.reply.clarification.slot === 'review'">
              <button v-if="currentChoice(turn)" type="button" class="secondary" :disabled="disabled || state.sending || state.recovering || state.unsettled" @click="send('review draft')">Review draft</button>
              <p v-else class="small muted">This earlier review request is no longer active.</p>
            </template>
            <template v-else>
            <h3 class="small">{{ turn.reply.clarification.slot === "destination" ? "Choose the exact destination" : "Choose the exact record" }} · {{ turn.reply.clarification.candidates.length }} {{ turn.reply.clarification.candidates.length === 1 ? "option" : "options" }} on this page</h3>
            <p v-if="!currentChoice(turn)" class="small muted">This earlier choice is no longer active.</p>
            <ChatDestinations :options="turn.reply.clarification.candidates" :selection-kind="turn.reply.clarification.slot === 'destination' ? 'destination' : 'record'" :selectable="true" :disabled="disabled || state.sending || state.unsettled || !currentChoice(turn)" @select="stick = true; emit('select', turn.id, $event.candidateId!)" @open="emit('inspect', $event)"/>
            </template>
          </template>
          <ChatDestinations v-else-if="turn.reply.destinations" :options="turn.reply.destinations" :selectable="false" :disabled="disabled || state.sending" @open="emit('inspect', $event)"/>
          <ul v-if="turn.reply.collections?.length" class="plain-list"><li v-for="collection in turn.reply.collections" :key="collection.id"><button type="button" class="text-button" :disabled="disabled || collection.id === state.scope?.collectionId" @click="emit('collection', collection.id)">{{ collection.name }}{{ collection.id === state.scope?.collectionId ? " (current collection)" : " · Switch collection" }}</button></li></ul>
          <div v-if="turn.reply.pagination" class="small muted">
            <p v-if="turn.reply.pagination.totalRecords !== undefined">{{ turn.reply.pagination.totalRecords }} matching records across all pages.</p>
            <button v-if="turn.reply.pagination.nextCursor" type="button" class="secondary" :disabled="disabled || state.sending || state.unsettled || turn.reply.revision !== state.revision" @click="stick = true; emit('page', turn.id)">Show more</button>
            <p v-else-if="turn.reply.pagination.complete === false">Results are incomplete. Refresh or narrow your question.</p>
          </div>
          <slot v-if="activeReview(turn, index)" name="review" :edit="startEdit"/>
          <p v-else-if="turn.reply.action" class="obsolete-preview">{{ turn.reply.action.summary }} · {{ turn.reply.action.id === action?.id ? action.status : "Earlier preview, no longer actionable" }}</p>
        </template>
      </li>
      <li v-if="orphanReview"><slot name="review" :edit="startEdit"/></li>
    </ol>
    <button v-if="unseen" type="button" class="secondary new-response" @click="scroll">New response ↓</button>
    <div :class="['conversation-status', { quiet: !state.sending && !state.recovering && !state.issue }]" role="status" aria-live="polite" aria-atomic="true">
      <template v-if="state.sending || state.recovering"><span class="spinner" aria-hidden="true"/>{{ state.recovering ? "Restoring this conversation…" : "Checking your request in this collection…" }}</template>
      <template v-else-if="state.issue">{{ state.issue }} <button type="button" class="text-button" :disabled="disabled" @click="emit('recover')">Check conversation</button><button v-if="state.restoreAvailable" type="button" class="text-button" :disabled="disabled" @click="emit('restore')">Restore saved review</button></template>
      <span v-else>{{ announcement || "Only the selected collection is searched." }}</span>
    </div>
    <ChatComposer ref="composer" :model-value="state.composer" :sending="state.sending || state.recovering" :blocked="state.unsettled" :disabled="disabled" @update:model-value="emit('composer', $event)" @send="send(state.composer)"/>
  </section>
</template>
