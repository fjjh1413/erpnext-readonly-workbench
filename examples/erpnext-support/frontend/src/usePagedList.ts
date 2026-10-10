import { reactive, ref } from 'vue'
import { pageOf, toError } from './api'
import type { ErrorState, ListQuery } from './types'

/** Filters are applied by the server before pagination; stale replies never replace a newer page. */
export function usePagedList<T>(fetchPage: (query: ListQuery) => Promise<unknown>, filters: () => ListQuery = () => ({})) {
  const items = ref<T[]>([])
  const loading = ref(false)
  const error = ref<ErrorState | null>(null)
  const pagination = reactive({ page: 1, limit: 20, total: 0 })
  let version = 0
  async function load() {
    const current = ++version
    loading.value = true; error.value = null
    try {
      const result = pageOf<T>(await fetchPage({ ...filters(), limit: pagination.limit, offset: (pagination.page - 1) * pagination.limit }), pagination.limit)
      if (current !== version) return
      items.value = result.items as typeof items.value
      pagination.total = result.total
      if (pagination.page > 1 && (pagination.page - 1) * pagination.limit >= result.total) { pagination.page = 1; await load() }
    } catch (failure) { if (current === version) { items.value = []; error.value = toError(failure) } }
    finally { if (current === version) loading.value = false }
  }
  function reset() { pagination.page = 1; void load() }
  return { items, loading, error, pagination, load, reset }
}
