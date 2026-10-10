<script setup lang="ts">
defineProps<{ page: number; limit: number; total: number; loading?: boolean }>()
const emit = defineEmits<{ 'update:page': [value: number]; 'update:limit': [value: number]; change: [] }>()
function changePage(value: number) { emit('update:page', value); emit('change') }
function changeLimit(value: number) { emit('update:limit', value); emit('update:page', 1); emit('change') }
</script>
<template>
  <el-pagination class="page-navigation" :current-page="page" :page-size="limit" :total="total" :disabled="loading" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" @update:current-page="changePage" @update:page-size="changeLimit" />
</template>
