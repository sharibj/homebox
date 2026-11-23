<script setup lang="ts">
import { computed } from "vue";
import type { Action, Entity } from "../types";
const props = defineProps<{ action: Action; busy: boolean; photoPreview?: string }>();
defineEmits<{ confirm: []; cancel: []; refresh: []; dismiss: []; open: [entity: Entity] }>();
const savedEntity = computed(() => {
  const result = props.action.result;
  if (!result) return null;
  return "id" in result ? result as Entity : result.entity || null;
});
// Never let a destination-only `after` hide approved fields. Show any changed
// resolved value separately rather than silently overwriting the reviewed value.
const resolvedEntries = computed(() => Object.entries(props.action.after || {}).filter(([key, value]) =>
  !(key in props.action.payload) || JSON.stringify(value) !== JSON.stringify(props.action.payload[key])));
const labels: Record<Action["status"], string> = {
  proposed: "Review before saving", running: "Change in progress", succeeded: "Saved to Homebox",
  failed: "Change needs attention", uncertain: "Check before trying again", cancelled: "Change cancelled",
};
function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not set";
  if (Array.isArray(value)) return value.map(valueText).join(", ") || "None";
  if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${k}: ${valueText(v)}`).join(", ");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}
const fieldLabels: Record<string, string> = {
  entityId: "Exact item ID", locationId: "Exact destination ID", name: "Name", quantity: "Quantity",
  manufacturer: "Manufacturer", model: "Model", serialNumber: "Serial number", notes: "Notes",
  description: "Description", tags: "Tags", category: "Category suggestion", photoIncluded: "Photo included",
  destination: "Destination", parentId: "Exact parent ID", locationPath: "Location path",
};
</script>
<template>
  <section class="card action-card" aria-label="Inventory change confirmation" aria-live="polite">
    <p class="eyebrow">{{ labels[action.status] }}</p>
    <h2>{{ action.summary }}</h2>
    <p v-if="action.status === 'proposed'" class="muted">Nothing has changed yet. Confirm only if these details are right.</p>
    <div v-if="action.before && Object.keys(action.before).length" class="change-snapshot">
      <h3>Before</h3>
      <dl><template v-for="(value, key) in action.before" :key="key"><dt>{{ fieldLabels[key] || key }}</dt><dd>{{ valueText(value) }}</dd></template></dl>
    </div>
    <div class="change-snapshot">
      <h3>Approved values</h3>
      <dl><template v-for="(value, key) in action.payload" :key="key"><dt>{{ fieldLabels[key] || key }}</dt><dd>{{ valueText(value) }}</dd></template></dl>
    </div>
    <div v-if="resolvedEntries.length" class="change-snapshot">
      <h3>Resolved change</h3>
      <dl><template v-for="[key, value] in resolvedEntries" :key="key"><dt>{{ fieldLabels[key] || key }}</dt><dd>{{ valueText(value) }}</dd></template></dl>
    </div>
    <img v-if="photoPreview" :src="photoPreview" class="photo-preview" alt="Reviewed photo to attach">
    <p v-if="action.error" role="alert">{{ typeof action.error === "string" ? action.error : action.error.message }}</p>
    <p v-if="action.status === 'uncertain'">Homebox may have saved this change. Do not create it again. Check its status or inspect the item in Homebox.</p>
    <p v-if="action.status === 'running'">Keep this screen open. Use “Check status” if the connection was interrupted.</p>
    <p v-if="action.status === 'failed' && action.retryable">The item already exists. Retry only the remaining photo or final-read step, not item creation.</p>
    <div class="button-row" v-if="action.status === 'proposed'">
      <button type="button" class="primary" :disabled="busy" @click="$emit('confirm')">Confirm change</button>
      <button type="button" class="secondary" :disabled="busy" @click="$emit('cancel')">Cancel change</button>
    </div>
    <div class="button-row" v-else>
      <button v-if="savedEntity" type="button" class="primary" :disabled="busy" @click="$emit('open', savedEntity)">Open saved record</button>
      <button v-if="action.status === 'failed' && action.retryable === true" type="button" class="primary" :disabled="busy" @click="$emit('confirm')">Retry remaining step</button>
      <button v-if="['running','uncertain','failed'].includes(action.status)" type="button" class="secondary" :disabled="busy" @click="$emit('refresh')">Check status</button>
      <button v-if="['succeeded','cancelled','failed'].includes(action.status)" type="button" class="secondary" :disabled="busy" @click="$emit('dismiss')">Done</button>
    </div>
  </section>
</template>
