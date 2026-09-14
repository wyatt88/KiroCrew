/**
 * Crew Members › Hire: the template gallery (design step 6), at `/members/hire`.
 *
 * The ONE place a crew member is hired from, whatever the template's origin:
 * the job cards enabled installed apps offer (`crew.templates`), the agent
 * files this package ships (built-ins) and the user's own agent files. The
 * catalog is `GET /api/members/templates`; a card's Hire is zero-config --
 * `POST /api/members {source}` names the member after its role -- and lands in
 * the new member's DM thread, where the header offers the rename.
 *
 * The rail stays on Crew Members: this is a view under the page, not a new
 * surface. Scenario chips file cards by the card's `category`; a card opens a
 * detail layer (full description, tags, Try asking, capabilities, a quiet
 * publisher line) with one primary Hire. A card already hired reads
 * "Open chat" and goes to that member; a fleet template ("Hire team ×N") is
 * shown but not yet hireable here.
 */
import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, ChevronDown, ChevronRight, FileText, Library, Link2, MessageCircle, RefreshCw, Sparkles, UserPlus, Users } from 'lucide-react'
import { api, type HireTemplateCard } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY, membersRosterQuery } from '../../api/membersQuery'
import { PageHeader, SearchInput, IconButton, Btn } from '../../components/ui'
import { Dialog, DialogContent, DialogTitle } from '../../components/ui/dialog'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'

export const HIRE_TEMPLATES_QUERY_KEY = ['members-hire-templates'] as const
/** The installed-agent list (`/api/agents/installed`), shared with the agents
 *  page and the drawer's Capabilities section: a hire adds a row to it. */
export const AGENTS_INSTALLED_QUERY_KEY = ['agents-installed'] as const

/** The scenario chips, in the order the design lists them; `other` last so a
 *  card without a category is never lost. Keyed to the server's
 *  `CREW_CATEGORIES` vocabulary. */
export const HIRE_CATEGORIES = ['engineering', 'ops', 'research', 'release', 'product', 'writing', 'other'] as const
export type HireCategory = (typeof HIRE_CATEGORIES)[number]

/** Cards matching the chip and the search, in catalog order. Pure, so the
 *  filter is testable without the page. */
export function filterCards(cards: readonly HireTemplateCard[], category: HireCategory | 'all', query: string): HireTemplateCard[] {
  const q = query.trim().toLowerCase()
  return cards.filter((c) => {
    if (category !== 'all' && (c.category || 'other') !== category) return false
    if (!q) return true
    return `${c.role} ${c.duty} ${c.tags.join(' ')} ${c.publisher}`.toLowerCase().includes(q)
  })
}

/** Which chips have at least one card: an empty scenario is not offered. */
export function categoriesPresent(cards: readonly HireTemplateCard[]): HireCategory[] {
  const present = new Set(cards.map((c) => (c.category || 'other') as HireCategory))
  return HIRE_CATEGORIES.filter((c) => present.has(c))
}

function memberPath(id: string) {
  return `/members?member=${encodeURIComponent(id)}`
}

/** The card's avatar seed: the card id, so two cards never share a default
 *  face by accident; a card with a ghost wears it. */
function cardAvatar(c: HireTemplateCard) {
  return c.avatar ?? undefined
}

type HireButtonProps = {
  card: HireTemplateCard
  pending: boolean
  onHire: () => void
  onOpen: () => void
  size?: 'sm' | 'lg'
}

function HireButton({ card, pending, onHire, onOpen, size = 'sm' }: HireButtonProps) {
  const { t } = useTranslation()
  const cls = size === 'lg' ? 'px-4 py-1.5 text-[13.5px]' : ''
  const team = card.team > 0
  if (card.hired_as.length > 0) {
    // Hired: the chat is the primary action. A second colleague from the same
    // card stays one quiet click away (two members from one file, one template
    // hired twice) rather than disappearing once the first is on the roster.
    const again = pending || !card.hireable || team
    return (
      <span className="inline-flex items-center gap-1.5">
        {!team && (
          <button
            type="button"
            className="text-[12px] text-muted hover:text-text underline-offset-2 hover:underline bg-transparent border-none cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
            disabled={again}
            title={!card.hireable ? card.unavailable_reason || t('pages.hireGallery.unavailable') : undefined}
            onClick={(e) => { e.stopPropagation(); if (!again) onHire() }}
            data-testid="hire-again"
          >
            {pending ? t('pages.hireGallery.hiring') : t('pages.hireGallery.hire_again')}
          </button>
        )}
        <Btn className={cls} onClick={(e) => { e.stopPropagation(); onOpen() }} data-testid="hire-open-chat">
          <MessageCircle size={13} className="lucide-inline" /> {t('pages.hireGallery.open_chat')}
        </Btn>
      </span>
    )
  }
  const disabled = pending || !card.hireable || team
  const title = !card.hireable
    ? card.unavailable_reason || t('pages.hireGallery.unavailable')
    : team
      ? t('pages.hireGallery.team_not_yet')
      : undefined
  return (
    <Btn
      primary
      className={cls}
      disabled={disabled}
      aria-disabled={disabled}
      title={title}
      onClick={(e) => { e.stopPropagation(); if (!disabled) onHire() }}
      data-testid="hire-button"
    >
      <UserPlus size={13} className="lucide-inline" />
      {pending
        ? t('pages.hireGallery.hiring')
        : team
          ? t('pages.hireGallery.hire_team', { count: card.team })
          : t('pages.hireGallery.hire')}
    </Btn>
  )
}

function TemplateCard({ card, pending, onOpen, onHire, onChat }: { card: HireTemplateCard; pending: boolean; onOpen: () => void; onHire: () => void; onChat: () => void }) {
  const { t } = useTranslation()
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } }}
      className="flex items-start gap-3.5 p-3.5 border border-border rounded-xl bg-card hover:border-border-strong transition-colors cursor-pointer focus-ring"
      data-testid="hire-template-card"
      data-card-id={card.id}
      aria-label={t('pages.hireGallery.card_aria', { role: card.role })}
    >
      <div className="relative shrink-0">
        <CrewAvatar seed={card.id} avatar={cardAvatar(card)} size={64} className="rounded-2xl" />
        {card.team > 0 && (
          <span className="absolute -right-1.5 -bottom-1 inline-flex items-center gap-0.5 rounded-full bg-accent text-accent-fg text-[10px] font-semibold px-1.5 h-[16px] leading-none border-2 border-card" data-testid="hire-team-badge">
            <Users size={9} /> ×{card.team}
          </span>
        )}
      </div>
      <div className="flex-1 min-w-0 flex flex-col gap-1">
        <div className="text-[14.5px] font-semibold text-text-strong truncate">{card.role}</div>
        {card.duty && <div className="text-[12.5px] text-muted truncate" title={card.duty}>{card.duty}</div>}
        {card.tags.length > 0 && (
          <div className="flex items-center gap-1 min-w-0 mt-0.5">
            {card.tags.slice(0, 3).map((tag) => (
              <span key={tag} className="text-[10.5px] text-muted bg-bg-elevated border border-border rounded-md px-1.5 h-[18px] inline-flex items-center whitespace-nowrap truncate">{tag}</span>
            ))}
          </div>
        )}
      </div>
      <div className="shrink-0 self-center" onKeyDown={(e) => e.stopPropagation()} role="presentation">
        <HireButton card={card} pending={pending} onHire={onHire} onOpen={onChat} />
      </div>
    </div>
  )
}

function CapIcon({ kind }: { kind: 'skill' | 'mcp' | 'knowledge' }) {
  const cls = 'inline-flex items-center justify-center w-6 h-6 rounded-md border border-border bg-bg-elevated shrink-0'
  if (kind === 'mcp') return <span className={cls}><Link2 size={12} className="text-accent" /></span>
  if (kind === 'knowledge') return <span className={cls}><Library size={12} className="text-accent" /></span>
  return <span className={cls}><Sparkles size={12} className="text-accent" /></span>
}

function TemplateDetail({ card, pending, onClose, onHire, onChat }: { card: HireTemplateCard | null; pending: boolean; onClose: () => void; onHire: (c: HireTemplateCard) => void; onChat: (c: HireTemplateCard) => void }) {
  const { t } = useTranslation()
  const [capsOpen, setCapsOpen] = useState(true)
  const originLabel: Record<HireTemplateCard['origin'], string> = {
    app: t('pages.hireGallery.origin_app'),
    builtin: t('pages.hireGallery.origin_builtin'),
    local: t('pages.hireGallery.origin_local'),
  }
  return (
    <Dialog open={!!card} onOpenChange={(o) => { if (!o) onClose() }}>
      {card && (
        <DialogContent maxWidth={640} className="p-0" data-testid="hire-template-detail">
          <div className="overflow-y-auto min-h-0 px-7 pt-7 pb-4">
            <CrewAvatar seed={card.id} avatar={cardAvatar(card)} size={72} className="rounded-2xl" />
            <DialogTitle className="mt-4 text-[20px] font-semibold text-text-strong leading-tight">{card.role}</DialogTitle>
            {(card.description || card.duty) && (
              <p className="mt-1.5 text-[13px] text-muted leading-relaxed">{card.description || card.duty}</p>
            )}
            {card.tags.length > 0 && (
              <div className="mt-4 flex items-center gap-1.5 flex-wrap">
                {card.tags.map((tag) => (
                  <span key={tag} className="text-[12px] text-text bg-bg-elevated border border-border rounded-full px-2.5 h-[24px] inline-flex items-center">{tag}</span>
                ))}
              </div>
            )}
            {!card.hireable && (
              <div className="mt-4">
                <ErrorNotice message={card.unavailable_reason || t('pages.hireGallery.unavailable')} variant="inline" askAgent testId="hire-detail-unavailable" />
              </div>
            )}

            {card.starter_prompts.length > 0 && (
              <>
                <div className="mt-6 text-[13px] font-semibold text-text-strong">{t('pages.hireGallery.try_asking')}</div>
                <ul className="list-none m-0 p-0 mt-2 space-y-2" data-testid="hire-detail-starters">
                  {card.starter_prompts.map((s) => (
                    <li key={s.text}>
                      <button
                        type="button"
                        onClick={() => (card.hired_as.length > 0 ? onChat(card) : onHire(card))}
                        disabled={pending || (!card.hireable && card.hired_as.length === 0)}
                        className="w-full flex items-center gap-3 text-left px-3.5 py-2.5 rounded-lg border border-border bg-bg-elevated hover:border-border-strong hover:bg-bg-hover transition-colors cursor-pointer focus-ring disabled:opacity-60 disabled:cursor-not-allowed"
                      >
                        <span className="flex-1 min-w-0 flex flex-col gap-1">
                          <span className="text-[13px] text-text leading-snug">{s.text}</span>
                          {s.attachment && (
                            <span className="inline-flex items-center gap-1 self-start text-[11px] text-muted border border-border rounded-md px-1.5 h-[20px] bg-card">
                              <FileText size={11} /> {s.attachment}
                            </span>
                          )}
                        </span>
                        <ArrowRight size={15} className="text-muted shrink-0" />
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}

            {card.capabilities.length > 0 && (
              <>
                <button
                  type="button"
                  onClick={() => setCapsOpen((v) => !v)}
                  aria-expanded={capsOpen}
                  className="mt-6 w-full flex items-center gap-1.5 text-[13px] font-semibold text-text-strong bg-transparent border-none px-0 cursor-pointer"
                  data-testid="hire-detail-caps-toggle"
                >
                  {capsOpen ? <ChevronDown size={14} className="text-muted" /> : <ChevronRight size={14} className="text-muted" />}
                  {t('pages.hireGallery.capabilities')} <span className="text-muted font-normal">{card.capabilities.length}</span>
                </button>
                {capsOpen && (
                  <ul className="list-none m-0 p-0 mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5" data-testid="hire-detail-caps">
                    {card.capabilities.map((c) => (
                      <li key={`${c.kind}:${c.name}`} className="flex items-center gap-2 text-[12.5px] min-w-0">
                        <CapIcon kind={c.kind} />
                        <span className="truncate">{c.name}</span>
                        <span className="text-[10px] uppercase tracking-wide text-muted ml-auto shrink-0">{c.kind}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}

            <div className="mt-6 text-[11.5px] text-muted flex items-center gap-1.5 flex-wrap" data-testid="hire-detail-meta">
              {card.publisher && <><span>{card.publisher}</span><span aria-hidden>·</span></>}
              {card.version && <><span>v{card.version}</span><span aria-hidden>·</span></>}
              <span>{originLabel[card.origin]}</span>
              {card.hired_as.length > 0 && (
                <><span aria-hidden>·</span><span>{t('pages.hireGallery.hired_as', { count: card.hired_as.length, name: card.hired_as[0] })}</span></>
              )}
            </div>
          </div>
          <div className="shrink-0 flex items-center justify-end gap-2 px-7 py-4 border-t border-border bg-card">
            <HireButton card={card} pending={pending} size="lg" onHire={() => onHire(card)} onOpen={() => onChat(card)} />
          </div>
        </DialogContent>
      )}
    </Dialog>
  )
}

export default function HireGalleryPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<HireCategory | 'all'>('all')
  const [detailId, setDetailId] = useState<string | null>(null)
  const [hireError, setHireError] = useState<string>('')

  const templates = useQuery({
    queryKey: HIRE_TEMPLATES_QUERY_KEY,
    queryFn: () => api.memberTemplates().then((r) => r.templates),
    staleTime: 30_000,
  })
  // The roster is what "Open chat" and the post-hire navigation land on; kept
  // warm so the thread mounts at once after a hire.
  useQuery(membersRosterQuery)

  const cards = useMemo(() => templates.data ?? [], [templates.data])
  const shown = useMemo(() => filterCards(cards, category, query), [cards, category, query])
  const chips = useMemo(() => categoriesPresent(cards), [cards])
  const detail = detailId ? cards.find((c) => c.id === detailId) ?? null : null

  const hire = useMutation({
    mutationFn: (card: HireTemplateCard) => api.hireMember({ source: card.source }),
    onMutate: () => setHireError(''),
    onSuccess: (r) => {
      // The new member's thread is the destination; the roster, the catalog
      // (hired_as) and the installed-agent list (the drawer's Capabilities
      // reads the new copy from it) are refetched so every surface the thread
      // opens already knows the hire.
      if (r.ok && r.id) {
        void qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
        void qc.invalidateQueries({ queryKey: HIRE_TEMPLATES_QUERY_KEY })
        void qc.invalidateQueries({ queryKey: AGENTS_INSTALLED_QUERY_KEY })
        navigate(memberPath(r.id))
      } else {
        setHireError(r.error || t('pages.hireGallery.hire_failed'))
      }
    },
    onError: (e: Error) => setHireError(e.message || t('pages.hireGallery.hire_failed')),
  })
  const pendingId = hire.isPending ? hire.variables?.id ?? null : null

  const openChat = useCallback((card: HireTemplateCard) => {
    if (card.hired_as[0]) navigate(memberPath(card.hired_as[0]))
  }, [navigate])

  return (
    <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden" data-testid="hire-gallery">
      <div className="pt-3">
        <PageHeader
          title={
            <span className="inline-flex items-center gap-2">
              <button type="button" onClick={() => navigate('/members')} className="inline-flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none" aria-label={t('pages.hireGallery.back')} data-testid="hire-back">
                <ArrowLeft size={16} />
              </button>
              {t('pages.hireGallery.title')}
            </span>
          }
          subtitle={t('pages.hireGallery.subtitle')}
          actions={<>
            <SearchInput placeholder={t('pages.hireGallery.search')} value={query} onChange={(e) => setQuery(e.target.value)} className="w-[220px]" aria-label={t('pages.hireGallery.search')} />
            <IconButton aria-label={t('pages.hireGallery.rescan')} title={t('pages.hireGallery.rescan')} onClick={() => void templates.refetch()} data-testid="hire-rescan">
              <RefreshCw size={15} className={templates.isFetching ? 'animate-spin' : ''} />
            </IconButton>
            <Link to="/apps" className="text-[13px] text-accent hover:underline inline-flex items-center gap-1 whitespace-nowrap" data-testid="hire-browse-apps">
              {t('pages.hireGallery.browse_apps')} <ArrowRight size={13} />
            </Link>
          </>}
        />
      </div>
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="max-w-[1200px] mx-auto">
          {hireError && (
            <div className="mb-3">
              <ErrorNotice message={hireError} variant="inline" askAgent testId="hire-error" />
            </div>
          )}
          {templates.isError ? (
            <ErrorNotice message={t('pages.hireGallery.catalog_failed')} variant="inline" askAgent testId="hire-catalog-error" />
          ) : (
            <>
              <div className="flex items-center gap-1.5 flex-wrap mb-4" role="radiogroup" aria-label={t('pages.hireGallery.scenario')} data-testid="hire-category-chips">
                {(['all', ...chips] as const).map((c) => {
                  const active = c === category
                  return (
                    <button
                      key={c}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => setCategory(c)}
                      className={`px-3 h-[28px] rounded-full text-[12.5px] border transition-colors cursor-pointer ${active ? 'bg-accent-subtle border-accent text-accent font-medium' : 'border-border text-muted hover:text-text hover:border-border-strong bg-transparent'}`}
                    >
                      {t(`pages.hireGallery.category_${c}`)}
                    </button>
                  )
                })}
              </div>
              {templates.isSuccess && cards.length === 0 && (
                <div className="text-[13px] text-muted" data-testid="hire-empty">{t('pages.hireGallery.empty')}</div>
              )}
              {templates.isSuccess && cards.length > 0 && shown.length === 0 && (
                <div className="text-[13px] text-muted" data-testid="hire-no-match">{t('pages.hireGallery.no_match')}</div>
              )}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3" data-testid="hire-template-grid">
                {shown.map((card) => (
                  <TemplateCard
                    key={card.id}
                    card={card}
                    pending={pendingId === card.id}
                    onOpen={() => setDetailId(card.id)}
                    onHire={() => hire.mutate(card)}
                    onChat={() => openChat(card)}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </div>
      <TemplateDetail card={detail} pending={pendingId !== null && pendingId === detail?.id} onClose={() => setDetailId(null)} onHire={(c) => hire.mutate(c)} onChat={openChat} />
    </div>
  )
}
