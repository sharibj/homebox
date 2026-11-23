<script setup lang="ts">
import { ref, watch } from "vue";
import QRCode from "qrcode";
import { safeTagUrl } from "../helpers";
import type { Entity, Mapping } from "../types";
const props = defineProps<{ mappings: Mapping[]; entity: Entity; locations: { id: string; name: string; path: string }[] }>();
const emit = defineEmits<{ create: [label: string]; update: [tag: Mapping, payload: Record<string, string>] }>();
const label = ref("");
const selected = ref<Mapping | null>(null);
const destination = ref("");
const targetType = ref<"item" | "location">("location");
const manualId = ref("");
const qr = ref("");
const notice = ref("");
watch(() => props.mappings, mappings => {
  if (selected.value) selected.value = mappings.find(mapping => mapping.tagId === selected.value!.tagId) || null;
});
watch(selected, async mapping => {
  qr.value = ""; notice.value = ""; destination.value = ""; manualId.value = "";
  const url = mapping && safeTagUrl(mapping.url);
  if (url) {
    try {
      const image = await QRCode.toDataURL(url, { width: 256, margin: 2, errorCorrectionLevel: "M" });
      if (selected.value?.tagId === mapping?.tagId) qr.value = image;
    } catch { notice.value = "Could not draw the QR code. You can still copy the stable URL."; }
  }
});
async function copy(value: string) {
  try { await navigator.clipboard.writeText(value); notice.value = "Tag URL copied."; }
  catch { notice.value = "Copy is unavailable here. Select and copy the URL above."; }
}
</script>
<template>
  <section class="stack">
    <div><h3>NFC &amp; QR labels</h3><p class="muted">The label keeps the same URL when you reassign it. Anyone opening it must sign in.</p></div>
    <form class="stack" @submit.prevent="$emit('create', label)">
      <label>New label name<input v-model="label" :placeholder="entity.name" maxlength="255"></label>
      <button type="submit" class="secondary">Create stable tag for {{ entity.isLocation ? "location" : "item" }}</button>
    </form>
    <ul class="plain-list"><li v-for="mapping in mappings" :key="mapping.tagId">
      <button type="button" class="text-button" @click="selected = mapping">{{ mapping.label || mapping.tagId }} · {{ mapping.status }}</button>
    </li></ul>
    <div v-if="selected" class="inset stack">
      <h3>{{ selected.label || selected.tagId }}</h3>
      <template v-if="safeTagUrl(selected.url)">
        <img v-if="qr" :src="qr" alt="QR code for this stable tag URL" class="qr-image" width="256" height="256">
        <a class="break-anywhere" :href="safeTagUrl(selected.url)!">{{ selected.url }}</a>
        <button type="button" class="secondary" @click="copy(selected!.url)">Copy tag URL</button>
      </template>
      <p v-else role="alert">This tag URL does not match the companion origin. Ask your administrator to check the public origin.</p>
      <p class="small muted">Write this URL as an NFC URL record with a phone app, or print the QR code. Keep the hostname working if you move servers.</p>
      <form class="stack" @submit.prevent="$emit('update', selected!, {entityId: targetType === 'location' ? destination : manualId, targetType})">
        <label>Reassign target type<select v-model="targetType" aria-label="Reassign target type"><option value="location">Location</option><option value="item">Item (exact ID)</option></select></label>
        <label v-if="targetType === 'location'">New location<select v-model="destination" aria-label="New location" required><option value="" disabled>Choose a location</option><option v-for="location in locations" :key="location.id" :value="location.id">{{ location.path || location.name }}</option></select></label>
        <label v-else>New item ID<input v-model="manualId" required pattern="[0-9a-fA-F-]{36}" placeholder="Exact Homebox entity UUID"><small>Open an item to see its exact ID. Names are never guessed.</small></label>
        <button class="secondary" type="submit">Confirm reassignment</button>
      </form>
      <button type="button" class="secondary" @click="$emit('update', selected!, {status: selected!.status === 'active' ? 'revoked' : 'active'}); selected = null">
        {{ selected.status === "active" ? "Revoke tag" : "Reactivate tag" }}
      </button>
      <p v-if="notice" role="status">{{ notice }}</p>
    </div>
  </section>
</template>
