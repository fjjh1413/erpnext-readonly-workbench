import { describe, expect, it } from 'vitest'
import { resultView, evidenceId, latestReview, reviewPassed, taskIsActive } from './result'
import { canReview, parseRoute } from './labels'

describe('diagnostic result and workflow boundaries', () => {
  it('never creates evidence or facts when no result was returned', () => { const view = resultView(null); expect(view.facts).toEqual([]); expect(view.evidence).toEqual([]); expect(view.causes).toEqual([]); expect(view.summary).toBe('') })
  it('preserves evidence IDs and missing information returned by the API', () => { const view = resultView({ confirmed_facts: [{ statement: '单据处于草稿', evidence_ids: ['e1'] }], supporting_evidence: [{ id: 'e1', snippet: '<script>x</script>' }], missing_information: ['无用户权限数据'] }); expect(view.evidence[0].snippet).toBe('<script>x</script>'); expect(evidenceId(view.evidence[0], 0)).toBe('e1'); expect(view.missing).toEqual(['无用户权限数据']) })
  it('uses the latest review task rather than an earlier passing task', () => { const tasks = [{ id: '2', kind: 'review', state: 'partial', result: { review: { business_complete: false } } }, { id: '1', kind: 'review', state: 'completed', result: { review: { business_complete: true } } }]; expect(latestReview(tasks).id).toBe('2'); expect(reviewPassed(latestReview(tasks))).toBe(false) })
  it('requires explicit passing business results for closure', () => { expect(reviewPassed({ state: 'completed', result: { reachable: true } })).toBe(false); expect(reviewPassed({ state: 'completed', result: { review: { business_complete: true } } })).toBe(true); expect(reviewPassed({ state: 'interrupted', result: { review: { business_complete: true } } })).toBe(false); expect(taskIsActive({ state: 'running' })).toBe(true); expect(taskIsActive({ state: 'interrupted' })).toBe(false) })
  it('limits reviewer controls to reviewers and admins', () => { expect(canReview({ role: 'analyst' })).toBe(false); expect(canReview({ role: 'admin' })).toBe(true); expect(canReview({ role: 'reviewer' })).toBe(true) })
  it('handles malformed and encoded route IDs', () => { expect(parseRoute('#/tickets/%E0%A4%A')).toEqual({ page: 'tickets', id: '' }); expect(parseRoute('#/tickets/a%2Fb')).toEqual({ page: 'detail', id: 'a/b' }) })
})
