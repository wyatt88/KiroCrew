import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { renderWithProviders } from '../../test/helpers'

/* The hire gallery (design step 6): one listing for every template a member
 * can be hired from, scenario chips, a detail layer, and a zero-config Hire
 * that lands in the new member's thread. */

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberTemplates: vi.fn(),
      hireMember: vi.fn(),
      members: vi.fn(() => Promise.resolve({ members: [] })),
    },
  }
})

import { api, type HireTemplateCard } from '../../api/client'
import HireGalleryPage, { categoriesPresent, filterCards } from './HireGalleryPage'

function card(over: Partial<HireTemplateCard>): HireTemplateCard {
  return {
    id: 'local:reviewer',
    origin: 'local',
    source: { kind: 'local', agent: 'reviewer' },
    role: 'Reviewer',
    duty: 'Reviews pull requests.',
    description: '',
    tags: [],
    category: 'other',
    starter_prompts: [],
    avatar: null,
    team: 0,
    publisher: '',
    version: '',
    agent: 'reviewer',
    capabilities: [],
    hired_as: [],
    hireable: true,
    unavailable_code: '',
    unavailable_reason: '',
    ...over,
  }
}

const APP_CARD = card({
  id: 'app:oncall-pack/agents/triage.json',
  origin: 'app',
  source: { kind: 'store', app: 'oncall-pack', agent: 'agents/triage.json' },
  role: 'Oncall Triage Engineer',
  duty: 'Triages every page, correlates it with deploys.',
  description: 'Owns a paging queue end to end. Never rolls back without an ack.',
  tags: ['Incident triage', 'Deploy correlation', 'Rollback plans', 'Fourth tag'],
  category: 'ops',
  starter_prompts: [{ text: 'What paged overnight?' }, { text: 'Draft a rollback plan.', attachment: 'incident.md' }],
  avatar: { kind: 'ghost', traits: { eyes: 'visor' } },
  publisher: 'Oncall pack',
  version: '1.2.0',
  agent: 'triage',
  capabilities: [{ kind: 'mcp', name: 'pagerduty' }, { kind: 'skill', name: 'deployment-fixer' }],
})
const BUILTIN_CARD = card({
  id: 'builtin:pipeline-conductor',
  origin: 'builtin',
  source: { kind: 'local', agent: 'pipeline-conductor' },
  role: 'Pipeline Conductor',
  duty: 'Runs one issue-to-PR pipeline as a supervised fleet.',
  category: 'engineering',
  publisher: 'Release Desk',
  team: 3,
})
const HIRED_CARD = card({ id: 'local:scribe', source: { kind: 'local', agent: 'scribe' }, role: 'Scribe', hired_as: ['Scribe'] })
const OFF_CARD = card({
  id: 'app:ghost/agents/x.json',
  origin: 'app',
  source: { kind: 'store', app: 'ghost', agent: 'agents/x.json' },
  role: 'Ghost',
  hireable: false,
  unavailable_code: 'template_not_materialized',
  unavailable_reason: "App 'ghost' has not installed its agent 'x' yet; re-enable the app",
})

function LocationProbe() {
  const loc = useLocation()
  return <span data-testid="location">{loc.pathname + loc.search}</span>
}

function renderGallery() {
  return renderWithProviders(
    <>
      <Routes>
        <Route path="/members/hire" element={<HireGalleryPage />} />
        <Route path="/members" element={<div data-testid="roster">roster</div>} />
      </Routes>
      <LocationProbe />
    </>,
    { route: '/members/hire' },
  )
}

describe('filterCards / categoriesPresent', () => {
  it('files by category, searches role, duty, tags and publisher, and offers only present chips', () => {
    const all = [APP_CARD, BUILTIN_CARD, HIRED_CARD]
    expect(filterCards(all, 'all', '').map((c) => c.id)).toEqual(all.map((c) => c.id))
    expect(filterCards(all, 'ops', '').map((c) => c.id)).toEqual([APP_CARD.id])
    expect(filterCards(all, 'other', '').map((c) => c.id)).toEqual([HIRED_CARD.id])
    expect(filterCards(all, 'all', 'rollback').map((c) => c.id)).toEqual([APP_CARD.id])
    expect(filterCards(all, 'all', 'release desk').map((c) => c.id)).toEqual([BUILTIN_CARD.id])
    expect(filterCards(all, 'engineering', 'oncall')).toEqual([])
    expect(categoriesPresent(all)).toEqual(['engineering', 'ops', 'other'])
    expect(categoriesPresent([card({ category: '' })])).toEqual(['other'])
  })
})

describe('HireGalleryPage', () => {
  beforeEach(() => {
    vi.mocked(api.memberTemplates).mockReset()
    vi.mocked(api.hireMember).mockReset()
    vi.mocked(api.memberTemplates).mockResolvedValue({ templates: [APP_CARD, BUILTIN_CARD, HIRED_CARD, OFF_CARD] })
  })

  it('lists cards from all sources with role, duty, three tags and the right button', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    expect(cards).toHaveLength(4)
    const app = cards.find((c) => c.getAttribute('data-card-id') === APP_CARD.id)!
    expect(app).toHaveTextContent('Oncall Triage Engineer')
    expect(app).toHaveTextContent('Triages every page, correlates it with deploys.')
    // Three tags on the face, never the fourth; no publisher/version on the face.
    expect(app).toHaveTextContent('Rollback plans')
    expect(app).not.toHaveTextContent('Fourth tag')
    expect(app).not.toHaveTextContent('Oncall pack')
    expect(within(app).getByTestId('hire-button')).toHaveTextContent('Hire')
    // Already hired: Open chat. Fleet: Hire team ×3, not yet enabled. Off: disabled with why.
    const hired = cards.find((c) => c.getAttribute('data-card-id') === HIRED_CARD.id)!
    expect(within(hired).getByTestId('hire-open-chat')).toHaveTextContent('Open chat')
    const team = cards.find((c) => c.getAttribute('data-card-id') === BUILTIN_CARD.id)!
    expect(within(team).getByTestId('hire-button')).toHaveTextContent('Hire team ×3')
    expect(within(team).getByTestId('hire-button')).toBeDisabled()
    expect(within(team).getByTestId('hire-team-badge')).toHaveTextContent('×3')
    const off = cards.find((c) => c.getAttribute('data-card-id') === OFF_CARD.id)!
    expect(within(off).getByTestId('hire-button')).toBeDisabled()
    expect(within(off).getByTestId('hire-button')).toHaveAttribute('title', OFF_CARD.unavailable_reason)
    // The source line and the apps link.
    expect(screen.getByText('From installed apps, built-ins and your local agent files')).toBeInTheDocument()
    expect(screen.getByTestId('hire-browse-apps')).toHaveAttribute('href', '/apps')
  })

  it('scenario chips filter the grid and only present scenarios are offered', async () => {
    renderGallery()
    await screen.findAllByTestId('hire-template-card')
    const chips = within(screen.getByTestId('hire-category-chips')).getAllByRole('radio')
    expect(chips.map((c) => c.textContent)).toEqual(['All', 'Engineering', 'Ops & Incidents', 'Other'])
    fireEvent.click(screen.getByRole('radio', { name: 'Ops & Incidents' }))
    const shown = screen.getAllByTestId('hire-template-card')
    expect(shown).toHaveLength(1)
    expect(shown[0]).toHaveTextContent('Oncall Triage Engineer')
    fireEvent.change(screen.getByLabelText('Search roles'), { target: { value: 'zzz' } })
    expect(screen.getByTestId('hire-no-match')).toBeInTheDocument()
  })

  it('gate: Hire from a card is zero-config and lands in the new member’s thread', async () => {
    vi.mocked(api.hireMember).mockResolvedValue({ ok: true, id: 'Oncall-Triage-Engineer' })
    const { queryClient } = renderGallery()
    // The installed-agent list the drawer's Capabilities reads was fetched
    // before the hire: it must be marked stale by it, or the new copy's
    // capabilities read as "none" until a reload.
    queryClient.setQueryData(['agents-installed'], [])
    const cards = await screen.findAllByTestId('hire-template-card')
    const app = cards.find((c) => c.getAttribute('data-card-id') === APP_CARD.id)!
    fireEvent.click(within(app).getByTestId('hire-button'))
    await waitFor(() => expect(api.hireMember).toHaveBeenCalledTimes(1))
    // Only the source: no name, no role -- the server names it after the role.
    expect(vi.mocked(api.hireMember).mock.calls[0][0]).toEqual({ source: APP_CARD.source })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Oncall-Triage-Engineer'))
    expect(screen.getByTestId('roster')).toBeInTheDocument()
    expect(queryClient.getQueryState(['agents-installed'])?.isInvalidated).toBe(true)
  })

  it('Open chat goes to the hired member; a click on the card body opens the detail layer', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    const app = cards.find((c) => c.getAttribute('data-card-id') === APP_CARD.id)!
    fireEvent.click(app)
    const detail = await screen.findByTestId('hire-template-detail')
    expect(detail).toHaveTextContent('Owns a paging queue end to end. Never rolls back without an ack.')
    expect(detail).toHaveTextContent('Fourth tag')
    const starters = within(detail).getByTestId('hire-detail-starters')
    expect(within(starters).getAllByRole('button')).toHaveLength(2)
    expect(starters).toHaveTextContent('incident.md')
    expect(within(detail).getByTestId('hire-detail-caps-toggle')).toHaveTextContent('Built-in capabilities 2')
    expect(within(detail).getByTestId('hire-detail-caps')).toHaveTextContent('pagerduty')
    fireEvent.click(within(detail).getByTestId('hire-detail-caps-toggle'))
    expect(within(detail).queryByTestId('hire-detail-caps')).toBeNull()
    // The quiet meta line: publisher · version · origin.
    expect(within(detail).getByTestId('hire-detail-meta')).toHaveTextContent('Oncall pack')
    expect(within(detail).getByTestId('hire-detail-meta')).toHaveTextContent('v1.2.0')
    expect(within(detail).getByTestId('hire-detail-meta')).toHaveTextContent('Installed app')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('hire-template-detail')).toBeNull())
    const hired = screen.getAllByTestId('hire-template-card').find((c) => c.getAttribute('data-card-id') === HIRED_CARD.id)!
    fireEvent.click(within(hired).getByTestId('hire-open-chat'))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Scribe'))
    expect(api.hireMember).not.toHaveBeenCalled()
  })

  it('gate: a hired card still hires a second colleague through "Hire another"', async () => {
    vi.mocked(api.hireMember).mockResolvedValue({ ok: true, id: 'Scribe-2' })
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    const hired = cards.find((c) => c.getAttribute('data-card-id') === HIRED_CARD.id)!
    const again = within(hired).getByTestId('hire-again')
    expect(again).toHaveTextContent('Hire another')
    fireEvent.click(again)
    await waitFor(() => expect(api.hireMember).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.hireMember).mock.calls[0][0]).toEqual({ source: HIRED_CARD.source })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Scribe-2'))
    // A fleet card never offers it: the team hire is not enabled yet.
    vi.mocked(api.memberTemplates).mockResolvedValue({ templates: [{ ...BUILTIN_CARD, hired_as: ['Pipeline Conductor'] }] })
    renderGallery()
    await waitFor(() => expect(screen.getAllByTestId('hire-open-chat').length).toBeGreaterThan(0))
    expect(screen.queryByTestId('hire-again')).toBeNull()
  })

  it('a refused hire is said in place and nothing navigates', async () => {
    vi.mocked(api.hireMember).mockResolvedValue({ ok: false, error: "'Reviewer' would share its member space with 'reviewer'", code: 'slug_collision' })
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    const app = cards.find((c) => c.getAttribute('data-card-id') === APP_CARD.id)!
    fireEvent.click(within(app).getByTestId('hire-button'))
    expect(await screen.findByTestId('hire-error')).toHaveTextContent('would share its member space')
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
  })

  it('a catalog that cannot be read is an ErrorNotice, and an empty one says so', async () => {
    vi.mocked(api.memberTemplates).mockRejectedValue(new Error('boom'))
    renderGallery()
    expect(await screen.findByTestId('hire-catalog-error')).toBeInTheDocument()
  })

  it('an empty catalog says so', async () => {
    vi.mocked(api.memberTemplates).mockResolvedValue({ templates: [] })
    renderGallery()
    expect(await screen.findByTestId('hire-empty')).toBeInTheDocument()
  })
})
