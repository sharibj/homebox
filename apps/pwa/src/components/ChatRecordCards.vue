<script setup lang="ts">
import type { Entity } from "../types";
defineProps<{ items: Entity[] }>();
defineEmits<{ open: [entity: Entity] }>();
</script>
<template>
  <ul class="chat-records">
    <li v-for="item in items" :key="item.id" class="chat-record">
      <div><strong>{{ item.name }}</strong><span v-if="item.quantity !== undefined && !item.isLocation" class="quantity-badge">Qty {{ item.quantity }}</span></div>
      <p class="small location-path">{{ item.locationPath || (item.isLocation ? "Storage location" : "No location assigned") }}</p>
      <button type="button" class="text-button" :aria-label="`Open ${item.isLocation ? 'location' : 'item'} ${item.name}, ${item.locationPath || 'no location assigned'}`" @click="$emit('open', item)">Open {{ item.isLocation ? "location" : "item" }} ›</button>
    </li>
  </ul>
</template>
