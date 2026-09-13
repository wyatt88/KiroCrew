import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Fire (design step 5): the drawer's fire panel. Two steps; the confirm says
 * what goes and what stays; purge is an explicit tick; the outcome is shown
 * -- including a thread the history path kept -- before the roster returns. */

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, api: { fireMember: vi.fn() } }
})
import { api, ApiError } from '../../api/client'
import FirePanel, { firedOutcomeText } from './FirePanel'

const onFired = vi.fn()

const MEMBER = {
  name: 'Pager-triage',
  slug: 'pager-triage',
  slot_key: '',
  running: false,
  kiro_agent: 'Pager-triage',
  workspace: 'default',
  memory_store: 'default',
  model: '',
  source: 'kirocrew',
  starred: false,
  display_name: 'Pager triage',
} as never

beforeEach(() => {
  vi.mocked(api.fireMember).mockReset()
  onFired.mockReset()
})

const render = () => renderWithProviders(<FirePanel member={MEMBER} label="Pager triage" onFired={onFired} />)
const t = (key: string, opts?: Record<string, unknown>) => {
  const table: Record<string, string> = {
    'pages.membersPage.fired_summary': '{{name}} has been fired.',
    'pages.membersPage.fired_thread_archived': 'Their thread stays in History; their activity and notes are archived.',
    'pages.membersPage.fired_thread_kept': 'Their activity and notes were deleted, but the thread could not be — it is still in History (a scheduled job may own it).',
    'pages.membersPage.fired_thread_purged': 'Their thread, activity and notes were deleted.',
    'pages.membersPage.fired_thread_none': 'They had no thread in History; their activity and notes are archived.',
    'pages.membersPage.fired_thread_none_purged': 'They had no thread in History; their activity and notes were deleted.',
  }
  return (table[key] ?? key).replace('{{name}}', String(opts?.name ?? ''))
}

describe('FirePanel', () => {
  it('fires only after the confirm step, archiving by default, and hands the outcome up', async () => {
    const result = {
      ok: true,
      thread: { history_key: 'dashboard:member-pager-triage', state: 'archived' as const },
      lived_state: 'archived' as const,
    }
    vi.mocked(api.fireMember).mockResolvedValue(result)
    render()
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('kept in History unless you say otherwise')
    fireEvent.click(screen.getByTestId('member-fire-start'))
    expect(api.fireMember).not.toHaveBeenCalled()
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('Fire Pager triage?')
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('archived, not deleted')
    expect(screen.getByTestId('confirm-fire-member')).toHaveTextContent('Yes, fire')
    fireEvent.click(screen.getByTestId('cancel-fire-member'))
    expect(screen.queryByTestId('confirm-fire-member')).toBeNull()
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await waitFor(() => expect(api.fireMember).toHaveBeenCalledWith('Pager-triage', { purge: false }))
    await waitFor(() => expect(onFired).toHaveBeenCalledWith(result))
    // The sentence the roster shows once the drawer is gone with the member.
    expect(firedOutcomeText('Pager triage', result, t)).toBe(
      'Pager triage has been fired. Their thread stays in History; their activity and notes are archived.',
    )
  })

  it('purge is an explicit tick, and a thread the history path kept is said, not hidden', async () => {
    vi.mocked(api.fireMember).mockResolvedValue({
      ok: true,
      thread: { history_key: 'dashboard:member-pager-triage', state: 'kept' },
      lived_state: 'purged',
    })
    render()
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('member-fire-purge'))
    expect(screen.getByTestId('confirm-fire-member')).toHaveTextContent('Yes, fire and delete')
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await waitFor(() => expect(api.fireMember).toHaveBeenCalledWith('Pager-triage', { purge: true }))
    await waitFor(() => expect(onFired).toHaveBeenCalledTimes(1))
    const kept = firedOutcomeText('Pager triage', onFired.mock.calls[0][0], t)
    expect(kept).toContain('the thread could not be')
    expect(kept).toContain('still in History')
  })

  it('a refused fire keeps the member in view with the error', async () => {
    vi.mocked(api.fireMember).mockRejectedValue(new ApiError(500, 'history save failed', JSON.stringify({ code: 'thread_close_failed' })))
    render()
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    expect(await screen.findByTestId('member-fire-error')).toHaveTextContent('history save failed')
    expect(onFired).not.toHaveBeenCalled()
    expect(screen.getByTestId('confirm-fire-member')).toBeInTheDocument()
  })
})
