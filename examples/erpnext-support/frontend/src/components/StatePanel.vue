<script setup lang="ts">
import type { ErrorState } from '../types'
import ErrorNotice from './ErrorNotice.vue'
withDefaults(defineProps<{ loading?: boolean; error?: ErrorState | null; empty?: boolean; emptyText?: string }>(), { emptyText: '暂无数据' })
defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="loading" class="state-panel" aria-live="polite" aria-busy="true">
    <p class="muted">正在读取实际数据…</p><el-skeleton :rows="4" animated />
  </div>
  <div v-else-if="error" class="state-panel" role="alert">
    <ErrorNotice :error="error" title="读取失败" />
    <p v-if="error.status === 403">请联系管理员核对当前账号的环境授权。</p>
    <el-button class="space-top" @click="$emit('retry')">重新读取</el-button>
  </div>
  <el-empty v-else-if="empty" :description="emptyText" />
  <slot v-else />
</template>
