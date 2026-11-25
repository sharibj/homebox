<script setup lang="ts">
import { computed } from "vue";
import type { Draft, Location } from "../types";
const props = defineProps<{ modelValue: Draft; locationId: string; locations: Location[]; reviewDisabled?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: Draft]; "update:locationId": [value: string]; edit: [field: keyof Draft]; submit: [] }>();
const draft = computed({ get: () => props.modelValue, set: value => emit("update:modelValue", value) });
const tags = computed({ get: () => draft.value.tags.join(", "), set: value => { draft.value.tags = value.split(",").map(v => v.trim()).filter(Boolean); } });
function recordEdit(event: Event) {
  const field = (event.target as HTMLInputElement).name;
  if (Object.prototype.hasOwnProperty.call(props.modelValue, field)) emit("edit", field as keyof Draft);
}
</script>
<template>
  <form class="stack" @input.capture="recordEdit" @submit.prevent="$emit('submit')">
    <label>Item name<input v-model="draft.name" name="name" required maxlength="255" autocomplete="off"></label>
    <label>Location<select aria-label="Location" :value="locationId" @change="$emit('update:locationId', ($event.target as HTMLSelectElement).value)">
      <option value="">No location assigned</option>
      <option v-for="location in locations" :key="location.id" :value="location.id">{{ location.path || location.name }}</option>
    </select></label>
    <div class="form-grid">
      <label>Quantity<input v-model.number="draft.quantity" name="quantity" type="number" required min="1" max="2147483647" step="1"></label>
      <label>Category suggestion<input v-model="draft.category" name="category" maxlength="255"></label>
      <label>Manufacturer<input v-model="draft.manufacturer" name="manufacturer" maxlength="255"></label>
      <label>Model<input v-model="draft.model" name="model" maxlength="255"></label>
    </div>
    <label>Visible serial number<input v-model="draft.serialNumber" name="serialNumber" maxlength="255"></label>
    <label>Tags, separated by commas<input v-model="tags" name="tags" aria-label="Tags, separated by commas" maxlength="2000"><small>Review these tags. They are not created just by analyzing a photo.</small></label>
    <label>Notes<textarea v-model="draft.notes" name="notes" rows="3" maxlength="10000"></textarea></label>
    <p class="muted small">Model suggestions can be wrong. Review all details. Category stays a suggestion and does not create a Homebox category.</p>
    <button class="primary" type="submit" :disabled="reviewDisabled">Review new item</button>
  </form>
</template>
