import { memo } from 'react'
import { Bot, ChevronRight, MessageSquare, Users } from 'lucide-react'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { renderUserContent } from './ChatPageMessageContent'
import { formatTs } from '../../app-sdk/messageRenderers'
import { fmtMessageTimeFull } from './messageTime'

/**
 * The gateway's provenance record on a user row another SESSION authored
 * (`session_control.sent_by_meta`). Written by the gateway from the caller's
 * resolved slot, never from the message body, so the fields are trustworthy
 * enough to drive who the row says it is from.
 */
export interface SentBy {
  session_key: string
  via: string
  title?: string
  agent?: string
  member_slug?: string
}

/** Who spoke, in the three shapes the row draws differently. */
export type SentByKind = 'member' | 'worker' | 'session'

/**
 * The provenance line the gateway prepends for the MODEL's benefit
 * (`[sent by session X via session_send]` / `via send_message`). The row hides
 * it from DISPLAY only -- the persisted content, and what any history re-feed
 * shows the model, keep it verbatim.
 */
const SENT_BY_PREFIX_RE = /^\[sent by session [^\]\n]* via [^\]\n]*\]\n*/

export function parseSentBy(message: ChatMessage): SentBy | null {
  const meta = message.meta as Record<string, unknown> | undefined
  const raw = meta?.sent_by
  if (!raw || typeof raw !== 'object') return null
  const rec = raw as Record<string, unknown>
  if (typeof rec.session_key !== 'string' || typeof rec.via !== 'string') return null
  return {
    session_key: rec.session_key,
    via: rec.via,
    title: typeof rec.title === 'string' ? rec.title : undefined,
    agent: typeof rec.agent === 'string' ? rec.agent : undefined,
    member_slug: typeof rec.member_slug === 'string' && rec.member_slug ? rec.member_slug : undefined,
  }
}

/** A peer member first; a worker reporting to its creator second; any other
 *  session (an ordinary `session_send` from a non-member) last. */
export function sentByKind(sentBy: SentBy): SentByKind {
  if (sentBy.member_slug) return 'member'
  if (sentBy.via === 'send_message_origin') return 'worker'
  return 'session'
}

/** The name the header shows after "From". A member is named by its slug, the
 *  thing the roster shows; anything else by its title, falling back to the key. */
export function sentByName(sentBy: SentBy): string {
  if (sentBy.member_slug) return sentBy.member_slug
  return sentBy.title?.trim() || sentBy.session_key
}

/** Display body: the row's content minus the model-facing prefix line. */
export function sentByBody(message: ChatMessage): string {
  return (message.content ?? '').replace(SENT_BY_PREFIX_RE, '')
}

/** Peer-member rows open by default -- a colleague's message is the point of
 *  the thread. Worker reports and other sessions' rows start folded: they are
 *  status the member acts on, not something the person needs to re-read. */
export function sentByDefaultExpanded(kind: SentByKind): boolean {
  return kind === 'member'
}

const KIND_ICON = { member: Users, worker: Bot, session: MessageSquare } as const

const KIND_LABEL_KEY = {
  member: 'pages.chat.sentByCard.kind_member',
  worker: 'pages.chat.sentByCard.kind_worker',
  session: 'pages.chat.sentByCard.kind_session',
} as const

const KIND_TIP_KEY = {
  member: 'pages.chat.sentByCard.kind_member_tip',
  worker: 'pages.chat.sentByCard.kind_worker_tip',
  session: 'pages.chat.sentByCard.kind_session_tip',
} as const

const PREVIEW_CHARS = 140

function previewLine(body: string): string {
  const firstLine = body.split('\n').find(l => l.trim()) ?? ''
  return firstLine.length > PREVIEW_CHARS ? `${firstLine.slice(0, PREVIEW_CHARS)}…` : firstLine
}

/**
 * A user row another session authored, drawn as a distinct collapsible
 * "From <name>" row rather than as the person's own bubble.
 *
 * Header: chevron · kind badge · "From <name>" · (collapsed) one-line preview ·
 * time. The badge names what kind of sender this is -- a peer member, a
 * worker reporting back, another session -- with a tooltip spelling the door
 * the message came through. The body renders through the same content path a
 * user bubble uses (paste chips, inline images, file cards), minus the
 * provenance prefix line the model reads.
 */
export default memo(function SentByCard({
  message,
  sentBy,
  disclosureKey,
  onFileOpen,
}: {
  message: ChatMessage
  sentBy: SentBy
  disclosureKey?: string
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const kind = sentByKind(sentBy)
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, sentByDefaultExpanded(kind))
  const name = sentByName(sentBy)
  const body = sentByBody(message)
  const Icon = KIND_ICON[kind]
  const ts = formatTs(message.ts)
  const toggleTitle = expanded
    ? i18nT('pages.chat.sentByCard.hide_message')
    : i18nT('pages.chat.sentByCard.show_message')

  return (
    <div
      className="w-full max-w-full min-w-0 animate-scale-in"
      data-testid="sent-by-card"
      data-kind={kind}
      data-expanded={expanded ? 'true' : 'false'}
    >
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
        title={toggleTitle}
        className="flex items-center gap-2 w-full min-w-0 text-left text-[12px] leading-5 text-muted hover:text-text transition-colors rounded px-1 -mx-1"
        data-testid="sent-by-card-toggle"
      >
        <ChevronRight
          size={12}
          className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
        <span
          className="inline-flex items-center gap-1 shrink-0 rounded-full border border-border px-1.5 py-0 text-[10px] uppercase tracking-wide"
          title={i18nT(KIND_TIP_KEY[kind])}
          data-testid="sent-by-kind-badge"
        >
          <Icon size={10} className="lucide-inline shrink-0" aria-hidden="true" />
          {i18nT(KIND_LABEL_KEY[kind])}
        </span>
        <span className="shrink-0 text-text font-medium" data-testid="sent-by-name">
          {i18nT('pages.chat.sentByCard.from', { name })}
        </span>
        {!expanded && (
          <span className="truncate min-w-0" data-testid="sent-by-preview">
            {previewLine(body)}
          </span>
        )}
        {ts && (
          <span className="ml-auto shrink-0 tabular-nums" title={fmtMessageTimeFull(message.ts)}>
            {ts}
          </span>
        )}
      </button>
      {expanded && (
        <div
          className="mt-1 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card px-3 py-2 text-[13px] leading-5 overflow-hidden animate-rise motion-reduce:animate-none"
          style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
          data-testid="sent-by-body"
        >
          {renderUserContent({ content: body, meta: message.meta, onFileOpen })}
        </div>
      )}
    </div>
  )
})
