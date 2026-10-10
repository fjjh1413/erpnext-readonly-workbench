<script setup lang="ts">
import { statusLabel, statusType } from '../labels'
defineProps<{ value?: string }>()
</script>
<template><el-tag :type="statusType(value)" effect="light">{{ statusLabel(value) }}</el-tag></template>
