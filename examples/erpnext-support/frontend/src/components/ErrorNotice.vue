<script setup lang="ts">
import type { ErrorState } from '../types'
defineProps<{ error?: ErrorState | null; title?: string }>()
</script>
<template>
  <el-alert v-if="error" :title="error.status === 403 ? '权限不足：' + error.message : title || error.message" type="error" :closable="false" show-icon class="space-bottom">
    <p v-if="title || error.status === 403">{{ error.message }}</p>
    <p v-if="error.requestId" class="request-id">请求编号：<code>{{ error.requestId }}</code> · 联系管理员时请提供此编号。</p>
  </el-alert>
</template>
