import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Role update (design step 4): the drawer panel for a template-hired member.
 * Reads the merge plan, offers the update only when applying would change
 * anything, keeps Apply disabled until every conflict has a side, sends the
 * chosen sides with the version the plan was made against, and detaches in
 * two steps. */

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberRoleUpdatePlan: vi.fn(),
      applyMemberRoleUpdate: vi.fn(),
      detachMember: vi.fn(),
    },
  }
})

import { api, ApiError } from '../../api/client'
import RoleUpdatePanel, { conflictsResolved, fieldValueText, reviewableFields } from './RoleUpdatePanel'

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
  role: 'Oncall Triage Engineer',
  template: 'oncall-pack/triage',
  template_version: '1.2.0',
} as never

const PLAN = {
  member: 'Pager-triage',
  template: 'oncall-pack/triage',
  member_version: '1.2.0',
  installed_version: '1.3.0',
  update_available: true,
  member_fingerprint: 'abc123',
  template_fingerprint: 'tpl456',
  fields: [
    { field: 'spec.description', state: 'unchanged', base: 'd', mine: 'd', theirs: 'd' },
    { field: 'spec.prompt', state: 'conflict', base: 'p0', mine: 'mine prompt', theirs: 'their prompt' },
    { field: 'spec.tools', state: 'keep', base: ['A'], mine: ['A', 'B'], theirs: ['A'] },
    { field: 'spec.hooks', state: 'apply', theirs: { agentSpawn: [] } },
    { field: 'card.role', state: 'agree', base: 'R', mine: 'R2', theirs: 'R2' },
    { field: 'card.triggers', state: 'apply', base: 'incident', mine: 'incident', theirs: 'incident, sev2' },
  ],
}

beforeEach(() => {
  vi.mocked(api.memberRoleUpdatePlan).mockReset()
  vi.mocked(api.applyMemberRoleUpdate).mockReset()
  vi.mocked(api.detachMember).mockReset()
})

function render() {
  return renderWithProviders(<RoleUpdatePanel member={MEMBER} appLabel="Oncall pack" />)
}

describe('RoleUpdatePanel helpers', () => {
  it('reviews only what changes or needs a decision, and knows when every conflict has a side', () => {
    const fields = reviewableFields(PLAN.fields as never)
    expect(fields.map((f) => f.field)).toEqual(['spec.prompt', 'spec.tools', 'spec.hooks', 'card.triggers'])
    expect(conflictsResolved(fields, {})).toBe(false)
    expect(conflictsResolved(fields, { 'spec.prompt': 'mine' })).toBe(true)
    expect(fieldValueText(undefined, 'absent')).toBe('absent')
    expect(fieldValueText('text', 'absent')).toBe('text')
    expect(fieldValueText(['A', 'B'], 'absent')).toBe('[\n "A",\n "B"\n]')
  })
})

describe('RoleUpdatePanel', () => {
  it('says the template is current when nothing would change', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue({ ...PLAN, update_available: false, installed_version: '1.2.0' } as never)
    render()
    expect(await screen.findByTestId('member-role-update-current')).toHaveTextContent('Template up to date (v1.2.0).')
    expect(screen.queryByTestId('member-role-update-review')).toBeNull()
  })

  it('offers the update, blocks Apply until the conflict is decided, and sends the decision with the version it saw', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue(PLAN as never)
    vi.mocked(api.applyMemberRoleUpdate).mockResolvedValue({ ok: true, version: '1.3.0' })
    render()
    expect(await screen.findByTestId('member-role-update-available')).toHaveTextContent(
      'Oncall pack v1.3.0 is available (this member is on v1.2.0).',
    )
    fireEvent.click(screen.getByTestId('member-role-update-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Update role from template' })
    // unchanged and agree rows are not shown; the rest carry their state.
    const list = within(dialog).getByTestId('role-update-fields')
    expect(within(list).getAllByRole('listitem')).toHaveLength(4)
    expect(within(dialog).getByTestId('role-update-field-spec.prompt')).toHaveTextContent('Both changed — choose')
    expect(within(dialog).getByTestId('role-update-field-spec.tools')).toHaveTextContent('You changed — kept')
    expect(within(dialog).getByTestId('role-update-field-card.triggers')).toHaveTextContent('Template changed — will apply')
    expect(within(dialog).getByTestId('role-update-field-card.triggers')).toHaveTextContent('incident, sev2')
    expect(within(dialog).queryByTestId('role-update-field-spec.description')).toBeNull()
    expect(within(dialog).queryByTestId('role-update-field-card.role')).toBeNull()
    const apply = within(dialog).getByTestId('role-update-apply')
    expect(apply).toBeDisabled()
    expect(apply).toHaveTextContent('Apply v1.3.0')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'prompt: Keep mine' }))
    expect(apply).not.toBeDisabled()
    fireEvent.click(apply)
    await waitFor(() => expect(api.applyMemberRoleUpdate).toHaveBeenCalledTimes(1))
    expect(api.applyMemberRoleUpdate).toHaveBeenCalledWith('Pager-triage', {
      resolutions: { 'spec.prompt': 'mine' },
      expected_version: '1.3.0',
      member_fingerprint: 'abc123',
      template_fingerprint: 'tpl456',
    })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update role from template' })).toBeNull())
  })

  it('says so when the template moved since the plan, and keeps the dialog open', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue({
      ...PLAN,
      fields: PLAN.fields.filter((f) => f.state !== 'conflict'),
    } as never)
    vi.mocked(api.applyMemberRoleUpdate).mockRejectedValue(
      new ApiError(409, 'moved', JSON.stringify({ code: 'template_changed', installed_version: '1.4.0' })),
    )
    render()
    fireEvent.click(await screen.findByTestId('member-role-update-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Update role from template' })
    fireEvent.click(within(dialog).getByTestId('role-update-apply'))
    expect(await within(dialog).findByTestId('role-update-error')).toHaveTextContent(
      'The template moved since this plan was made. Review it again.',
    )
  })

  it('says so when the member was edited since the plan', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue({
      ...PLAN,
      fields: PLAN.fields.filter((f) => f.state !== 'conflict'),
    } as never)
    vi.mocked(api.applyMemberRoleUpdate).mockRejectedValue(
      new ApiError(409, 'changed', JSON.stringify({ code: 'member_changed_since_plan' })),
    )
    render()
    fireEvent.click(await screen.findByTestId('member-role-update-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Update role from template' })
    fireEvent.click(within(dialog).getByTestId('role-update-apply'))
    expect(await within(dialog).findByTestId('role-update-error')).toHaveTextContent(
      'This member was edited since this plan was made. Review it again.',
    )
  })

  it('explains an unavailable template instead of offering nothing', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockRejectedValue(
      new ApiError(409, 'disabled', JSON.stringify({ code: 'app_disabled' })),
    )
    render()
    expect(await screen.findByTestId('member-role-update-unavailable')).toHaveTextContent(
      'Oncall pack is disabled; enable it to update from it.',
    )
    expect(screen.queryByTestId('member-role-update-review')).toBeNull()
    // Detach stays available: severing is how a member leaves a broken template.
    expect(screen.getByTestId('member-detach')).toBeInTheDocument()
  })

  it('detaches only after a confirm step, explaining what stays', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue(PLAN as never)
    vi.mocked(api.detachMember).mockResolvedValue({ ok: true })
    render()
    fireEvent.click(await screen.findByTestId('member-detach'))
    expect(api.detachMember).not.toHaveBeenCalled()
    expect(screen.getByTestId('member-detach-explain')).toHaveTextContent('stops following Oncall pack')
    // The confirm pair is its own row: Review update does not sit beside it.
    const row = screen.getByTestId('member-detach-confirm-row')
    expect(within(row).getAllByRole('button')).toHaveLength(2)
    expect(screen.queryByTestId('member-role-update-review')).toBeNull()
    fireEvent.click(screen.getByTestId('cancel-detach-member'))
    expect(screen.queryByTestId('confirm-detach-member')).toBeNull()
    fireEvent.click(screen.getByTestId('member-detach'))
    fireEvent.click(screen.getByTestId('confirm-detach-member'))
    await waitFor(() => expect(api.detachMember).toHaveBeenCalledWith('Pager-triage'))
  })
})
