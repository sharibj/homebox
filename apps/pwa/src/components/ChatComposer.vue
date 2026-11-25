<script setup lang="ts">
import { ref } from "vue";
defineProps<{ modelValue: string; sending: boolean; disabled: boolean; blocked?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: string]; send: [] }>();
const input = ref<HTMLTextAreaElement | null>(null);
function keydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    (event.currentTarget as HTMLTextAreaElement).form?.requestSubmit();
  }
}
defineExpose({ focus: () => input.value?.focus() });
</script>
<template>
  <form class="conversation-composer" @submit.prevent="!sending && !disabled && !blocked && modelValue.trim() && emit('send')">
    <label class="grow" for="chat-message">Ask your inventory
      <textarea id="chat-message" ref="input" :value="modelValue" :disabled="disabled" rows="2" maxlength="4000"
        placeholder="Ask a question, or tell me what to add…" aria-describedby="chat-keyboard-help"
        @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" @keydown="keydown"/>
    </label>
    <button class="primary" type="submit" :disabled="disabled || sending || blocked || !modelValue.trim()" aria-label="Ask Homebox">{{ sending ? "Waiting…" : "Send" }}</button>
    <p id="chat-keyboard-help" class="small muted">Enter to send · Shift+Enter for a new line. Changes always need explicit confirmation.</p>
  </form>
</template>
