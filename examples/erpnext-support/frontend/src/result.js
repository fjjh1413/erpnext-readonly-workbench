// Normalize only fields actually returned by the backend; no synthetic evidence.
import { limitationText } from './labels.js'
const list = (value) => Array.isArray(value) ? value : []
export function resultView(result) {
  const source = result && typeof result === 'object' ? result : {}
  return {
    summary: source.summary || source.conclusion || '',
    category: source.classification || source.category || source.problem_category || '',
    facts: list(source.confirmed_facts || source.facts),
    causes: [...list(source.candidate_causes || source.causes), ...list(source.model_hypotheses).map(item => ({...item, model_hypothesis: true}))],
    evidence: list(source.supporting_evidence || source.evidence),
    missing: list(source.missing_information || source.missing),
    limits: list(source.limits || source.limitations || source.scope_limitations),
    next: list(source.next_steps || source.manual_steps || source.recommendations || source.manual_recommendations),
    verification: list(source.verification || source.verification_steps || source.review_method),
    calls: list(source.tool_calls),
    mode: source.mode || source.execution_mode || source.engine || '',
    model: source.metrics?.model || source.model || source.model_name || '',
    modelTarget: source.model_target || '',
    modelUsage: source.model_usage || null,
    metrics: source.metrics || {},
    cases: list(source.approved_case_references),
  }
}
export const findingText = (value) => typeof value === 'string' ? limitationText(value) : value?.text || value?.statement || value?.description || value?.reason || JSON.stringify(value)
export const referencesOf = (value) => Array.isArray(value?.evidence_ids) ? value.evidence_ids : []
export const evidenceId = (value, index) => value?.id || value?.evidence_id || value?.tool_call_id || `未提供编号 ${index + 1}`
export const taskIsActive = (task) => ['queued', 'running'].includes(task?.state)
// API lists tasks in descending creation order.
export function latestReview(tasks) { return (tasks || []).find((task) => task.kind === 'review') || null }
export function reviewPassed(task) { return ['completed', 'partial'].includes(task?.state) && task.result?.review?.business_complete === true }
