<script setup lang="ts">
import { computed, ref } from "vue";
import type { Entity } from "../types";
export type Destination = Entity & { path?: string; kind?: string; candidateId?: string };
const props = defineProps<{ options: Destination[]; selectable: boolean; disabled: boolean; selectionKind?: "record" | "destination" }>();
defineEmits<{ select: [entity: Destination]; open: [entity: Destination] }>();
const filter = ref("");
const path = (item: Destination) => item.path || item.locationPath || item.name;
const groups = computed(() => {
  const result = new Map<string, Destination[]>();
  for (const item of props.options) {
    const full = path(item);
    if (!`${item.name} ${full}`.toLocaleLowerCase().includes(filter.value.trim().toLocaleLowerCase())) continue;
    const parent = full.includes(" / ") ? full.slice(0, full.lastIndexOf(" / ")) : "Top level";
    const group = result.get(parent) || [];
    group.push(item); result.set(parent, group);
  }
  return [...result].map(([name, items]) => ({ name, items }));
});
</script>
<template>
  <div class="chat-destinations">
    <label v-if="options.length > 5" class="small">Filter this page's destinations<input v-model="filter" type="search" maxlength="255" placeholder="Search names or paths"></label>
    <ul v-if="selectable" class="plain-list destination-choices">
      <li v-for="item in groups.flatMap(group => group.items)" :key="item.candidateId || item.id" class="destination-choice">
        <strong>{{ path(item) }}</strong>
        <span class="small muted">{{ item.kind === "unassigned" ? "No parent" : item.isLocation || item.kind === "location" ? "Location" : selectionKind === "record" ? "Item" : "Item container" }}</span>
        <div class="button-row">
          <button type="button" class="secondary" :disabled="disabled" :aria-label="`${selectionKind === 'record' ? 'Choose' : 'Use'} ${path(item)}`" @click="$emit('select', item)">{{ item.kind === "unassigned" ? "Leave unassigned" : selectionKind === "record" ? "Choose this record" : item.isLocation || item.kind === "location" ? "Use this location" : "Use this container" }}</button>
          <button v-if="item.kind !== 'unassigned'" type="button" class="text-button" :aria-label="`View ${path(item)}`" @click="$emit('open', item)">View details</button>
        </div>
      </li>
    </ul>
    <details v-for="group in selectable ? [] : groups" :key="group.name" open class="destination-group">
      <summary>{{ group.name }}</summary>
      <ul class="plain-list">
        <li v-for="item in group.items" :key="item.id" class="destination-option">
          <strong>{{ item.name }}</strong><span class="small muted">{{ item.isLocation || item.kind === "location" ? "Location" : "Item container" }}</span>
          <p class="small location-path">{{ path(item) }}</p>
          <div class="button-row">
            <button v-if="item.kind !== 'unassigned'" type="button" class="text-button" :aria-label="`View ${path(item)}`" @click="$emit('open', item)">View details</button>
          </div>
        </li>
      </ul>
    </details>
    <p v-if="!groups.length" class="small muted">{{ filter ? "No matches on this page. Clear the filter or browse more destinations." : "No destinations to show." }}</p>
  </div>
</template>
