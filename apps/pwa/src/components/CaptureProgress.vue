<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import type { CaptureJob } from "../types";
const props = defineProps<{ job: CaptureJob | null; pending: boolean; issue: string; hasLocalPhoto: boolean }>();
defineEmits<{ open: []; dismiss: [] }>();
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); });
onUnmounted(() => { if (timer) clearInterval(timer); });
const elapsed = computed(() => {
  if (!props.job) return "";
  const end = props.job.finishedAt ? Date.parse(props.job.finishedAt) : now.value;
  const seconds = Math.max(0, Math.floor((end - Date.parse(props.job.createdAt)) / 1000));
  return Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}m ${seconds % 60}s elapsed` : "";
});
const label = computed(() => props.job ? ({
  queued: "Photo queued", running: "Analyzing photo", succeeded: "Photo ready to review",
  manual: "Photo saved for manual review", failed: "Photo job needs attention", expired: "Photo expired",
}[props.job.status]) : props.pending ? "Checking submitted photo" : "Photo progress unavailable");
</script>
<template>
  <section class="card stack" aria-label="Photo progress">
    <div role="status" aria-live="polite"><p class="eyebrow">Photo analysis</p><h3>{{ label }}</h3><p v-if="elapsed" class="small muted">{{ elapsed }}</p></div>
    <p v-if="job?.status === 'queued'" class="muted">Waiting for the home server’s analysis slot. You can edit details while you wait.</p>
    <p v-if="job?.status === 'running'" class="muted">Photo analysis is running. It can take several minutes. This screen checks the same saved job, not a new upload.</p>
    <p v-if="!hasLocalPhoto && job && !['expired', 'failed'].includes(job.status)" class="small muted">Recovered server upload. The local preview is not available after a reload.</p>
    <p v-if="issue" class="banner warning">{{ issue }}</p>
    <p class="small muted">No inventory item is created by photo analysis. Saving still needs your review and confirmation.</p>
    <div class="button-row"><button type="button" class="secondary" @click="$emit('open')">Open photo details</button><button type="button" class="text-button" @click="$emit('dismiss')">{{ pending ? "Stop checking and remove photo" : "Dismiss photo progress" }}</button></div>
  </section>
</template>
