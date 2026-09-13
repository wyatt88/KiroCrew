/* Fire (design: Crew Member = Custom Agent + Wrapper, rollout step 5).
 *
 * The reverse of hire, from the member's own drawer. Two steps, like the crew
 * editor's delete -- a misclick in a drawer is likelier than in a table -- and
 * the confirm step says exactly what goes and what stays: the row, the agent
 * file and the pristine copy go; the private memory is archived, never erased;
 * the thread, activity, briefing and rules are ARCHIVED (the transcript stays
 * in History, the space moves to members/.retired) unless the user ticks the
 * purge box, which is the design's "destroy only on explicit request".
 *
 * The outcome -- where the thread went, and whether a purge actually reached it
 * (the history path can refuse) -- is handed UP to the page: the roster
 * refreshes the moment the row is gone and this drawer goes with the member, so
 * a notice that must be read cannot live here. */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation } from '@tanstack/react-query'
import { api, type FireMemberResult, type MemberRosterRow } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'

/** The one sentence the roster shows once the member is gone. */
export function firedOutcomeText(
  label: string,
  result: FireMemberResult,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const thread = result.thread.state
  const where = thread === 'archived'
    ? t('pages.membersPage.fired_thread_archived')
    : thread === 'purged'
      ? t('pages.membersPage.fired_thread_purged')
      : thread === 'kept'
        ? t('pages.membersPage.fired_thread_kept')
        : result.lived_state === 'purged'
          ? t('pages.membersPage.fired_thread_none_purged')
          : t('pages.membersPage.fired_thread_none')
  return `${t('pages.membersPage.fired_summary', { name: label })} ${where}`
}

export default function FirePanel({
  member,
  label,
  onFired,
}: {
  member: MemberRosterRow
  label: string
  onFired: (result: FireMemberResult) => void
}) {
  const { t } = useTranslation()
  const [confirming, setConfirming] = useState(false)
  const [purge, setPurge] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    setConfirming(false)
    setPurge(false)
    setError(null)
  }, [member.name])

  const fireMut = useMutation({
    mutationFn: () => api.fireMember(member.name, { purge }),
    onSuccess: (result) => { setError(null); onFired(result) },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : t('pages.membersPage.fire_failed')),
  })

  return (
    <div className="mt-3 flex flex-col gap-2 rounded-md border border-danger-subtle bg-danger-subtle px-2.5 py-2 text-[11px] text-muted" data-testid="member-fire">
      <p className="m-0 leading-relaxed" data-testid="member-fire-explain">
        {confirming
          ? t('pages.membersPage.fire_confirm_explain', { name: label })
          : t('pages.membersPage.fire_explain')}
      </p>
      {confirming && (
        <label className="flex items-start gap-2 text-[11px]">
          <input
            type="checkbox"
            checked={purge}
            onChange={(e) => setPurge(e.target.checked)}
            className="mt-0.5"
            aria-label={t('pages.membersPage.fire_purge_label')}
            data-testid="member-fire-purge"
          />
          <span>{t('pages.membersPage.fire_purge_label')}</span>
        </label>
      )}
      {/* No hand-off: the member is in view and the retry is the same button. */}
      <ErrorNotice message={error} variant="inline" testId="member-fire-error" />
      <div className="flex items-center justify-end gap-2">
        {confirming ? (
          <>
            <Btn onClick={() => setConfirming(false)} disabled={fireMut.isPending} data-testid="cancel-fire-member">
              {t('components.confirmDialog.cancel')}
            </Btn>
            <Btn danger disabled={fireMut.isPending} onClick={() => fireMut.mutate()} data-testid="confirm-fire-member">
              {fireMut.isPending ? t('pages.membersPage.firing') : purge ? t('pages.membersPage.fire_confirm_purge') : t('pages.membersPage.fire_confirm')}
            </Btn>
          </>
        ) : (
          <Btn danger onClick={() => setConfirming(true)} data-testid="member-fire-start">
            {t('pages.membersPage.fire', { name: label })}
          </Btn>
        )}
      </div>
    </div>
  )
}
