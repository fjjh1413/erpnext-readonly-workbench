<script setup>
import { statusLabel, statusType } from '../labels.js'
defineProps({ value: String })
</script>
<template><el-tag :type="statusType(value)" effect="light">{{ statusLabel(value) }}</el-tag></template>
