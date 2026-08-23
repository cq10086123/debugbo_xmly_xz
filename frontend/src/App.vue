<script setup>
import { useToast } from './utils/toast'
import AnnouncementModal from './components/AnnouncementModal.vue'

const { toasts } = useToast()
</script>

<template>
  <div class="toast-wrap">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="t.type">{{ t.msg }}</div>
  </div>
  <AnnouncementModal />
  <router-view />
</template>
