<script setup lang="ts">
import type { Entity } from "../types";
defineProps<{ items: Entity[] }>();
defineEmits<{ open: [entity: Entity] }>();
</script>
<template>
  <ul class="entity-list">
    <li v-for="item in items" :key="item.id">
      <button type="button" class="entity-button" @click="$emit('open', item)">
        <span class="entity-icon" aria-hidden="true">{{ item.isLocation ? "▤" : "◇" }}</span>
        <span><strong>{{ item.name }}</strong><small>{{ item.locationPath || (item.isLocation ? "Storage location" : "No location assigned") }}</small></span>
        <span class="chevron" aria-hidden="true">›</span>
      </button>
    </li>
  </ul>
</template>
