<script setup lang="ts">
import { computed, ref, useId } from "vue";
import { aiRoles, needsEndpointDisclosure, providerLabel } from "../ai-metadata";
import type { PublicAi } from "../types";

const props = defineProps<{ ai?: PublicAi }>();
const roles = computed(() => aiRoles(props.ai));
const trigger = ref<HTMLButtonElement | null>(null);
const dialog = ref<HTMLDialogElement | null>(null);
const expanded = ref(false);
const dialogId = useId();
const titleId = useId();
function open() {
  dialog.value?.showModal();
  expanded.value = !!dialog.value?.open;
}
function closed() {
  expanded.value = false;
  trigger.value?.focus({ preventScroll: true });
}
function dismissBackdrop(event: MouseEvent) {
  if (event.target !== dialog.value || !dialog.value) return;
  const bounds = dialog.value.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.value.close();
}
</script>

<template>
  <template v-if="roles.chat || roles.vision">
    <button ref="trigger" type="button" class="text-button ai-details-trigger" aria-haspopup="dialog" :aria-controls="dialogId" :aria-expanded="expanded" @click="open">AI details</button>
    <dialog :id="dialogId" ref="dialog" class="ai-dialog" :aria-labelledby="titleId" @close="closed" @click="dismissBackdrop">
      <div class="ai-dialog-heading">
        <h2 :id="titleId">AI details</h2>
        <button type="button" class="text-button" aria-label="Close AI details" autofocus @click="dialog?.close()">Close</button>
      </div>
      <div class="ai-disclosure" aria-label="Configured AI provider">
        <p v-if="roles.chat">Chat: {{ providerLabel(roles.chat) }} · {{ roles.chat.model || "Model not specified" }}.<span v-if="needsEndpointDisclosure(roles.chat)"> Chat messages are sent to the chat AI endpoint configured by your administrator.</span></p>
        <p v-if="roles.vision">Images: {{ providerLabel(roles.vision) }} · {{ roles.vision.model || "Model not specified" }}.<span v-if="needsEndpointDisclosure(roles.vision)"> Photos are sent to the image AI endpoint configured by your administrator.</span></p>
      </div>
    </dialog>
  </template>
</template>

<style scoped>
.ai-details-trigger{flex-shrink:0;min-height:44px;font-size:.8rem;white-space:nowrap}
.ai-dialog{width:min(480px,calc(100vw - 32px));max-height:calc(100vh - 48px);max-height:calc(100dvh - 48px);padding:24px;border:1px solid var(--line);border-radius:18px;background:#fff;color:#233c34;font-size:.9rem;overflow-y:auto;overflow-wrap:anywhere;box-shadow:0 20px 80px #173c3433}
.ai-dialog::backdrop{background:#152e2866}
.ai-dialog-heading{display:flex;align-items:center;justify-content:space-between;gap:1rem}
.ai-dialog-heading h2{font-size:1.3rem}
.ai-dialog-heading button{flex-shrink:0;min-height:44px}
.ai-disclosure{display:grid;gap:.8rem;margin-top:1rem;overflow-wrap:anywhere}
.ai-disclosure p+p{margin:0}
</style>
