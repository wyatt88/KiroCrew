/* Role update (design: Crew Member = Custom Agent + Wrapper, rollout step 4).
 *
 * A member hired from a store template carries `template` / `template_version`.
 * This panel, under the drawer's Source row, reads the merge plan for such a
 * member — BASE the pristine copy the hire recorded, MINE the member's own agent
 * file and card fields, THEIRS the template as the app ships it now — and offers
 * the update when applying it would change anything. Every field is one of
 * unchanged / apply (only the template changed) / keep (only the member changed)
 * / agree / conflict (both changed apart — the user picks). Nothing is written
 * until Apply, and a conflict without a choice keeps Apply disabled: the server
 * refuses a half-resolved merge too, this only says so before the round trip.
 *
 * Detach is the other verb: provenance cleared, everything else kept, one-way.
 * Two-step inline, like the crew editor's delete — a misclick in a drawer is
 * likelier than in a table. */
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type MemberRosterRow, type RoleUpdateField } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { Btn } from '../../components/ui'
import {
  Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '../../components/ui/dialog'
import ErrorNotice from '../../components/ErrorNotice'

export const roleUpdateQueryKey = (member: string) => ['member-role-update', member] as const

/** A field's value for the review table: strings as they are, everything else
 *  as compact JSON, an absent side as an em dash. Bounded so a long prompt
 *  reads as a clamped block, never a wall. */
export function fieldValueText(value: unknown, absent: string): string {
  if (value === undefined) return absent
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 1)
  } catch {
    return String(value)
  }
}

/** `spec.prompt` → `prompt`; the two card fields read as the drawer's own rows. */
export function fieldLabel(field: string, t: (k: string) => string): string {
  if (field === 'card.role') return t('pages.membersPage.role')
  if (field === 'card.triggers') return t('pages.membersPage.role_update_triggers')
  return field.replace(/^spec\./, '')
}

/** The fields the review shows: everything that would change or needs a
 *  decision, plus the member's own customizations so the reader sees what is
 *  being kept. `unchanged` and `agree` rows say nothing and are left out. */
export function reviewableFields(fields: RoleUpdateField[]): RoleUpdateField[] {
  return fields.filter((f) => f.state === 'apply' || f.state === 'keep' || f.state === 'conflict')
}

/** Every conflict has a side chosen. */
export function conflictsResolved(fields: RoleUpdateField[], resolutions: Record<string, 'mine' | 'theirs'>): boolean {
  return fields.every((f) => f.state !== 'conflict' || resolutions[f.field] === 'mine' || resolutions[f.field] === 'theirs')
}

function errorCode(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.body) as { code?: unknown }
      if (typeof parsed.code === 'string') return parsed.code
    } catch {
      /* not a JSON body */
    }
  }
  return ''
}

export default function RoleUpdatePanel({ member, appLabel }: { member: MemberRosterRow; appLabel: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const plan = useQuery({
    queryKey: roleUpdateQueryKey(member.name),
    queryFn: () => api.memberRoleUpdatePlan(member.name),
    // The plan reads the app tree and the member's file; nothing here changes
    // under the drawer without a mutation this page makes, so no interval.
    retry: false,
  })
  const [reviewing, setReviewing] = useState(false)
  const [resolutions, setResolutions] = useState<Record<string, 'mine' | 'theirs'>>({})
  const [confirmDetach, setConfirmDetach] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)
  const [detachError, setDetachError] = useState<string | null>(null)
  // A different member in the drawer is a different plan: no stale choice may
  // carry over and land on a same-named field of another template.
  useEffect(() => {
    setResolutions({})
    setReviewing(false)
    setConfirmDetach(false)
    setApplyError(null)
    setDetachError(null)
  }, [member.name])

  const refetchAll = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: roleUpdateQueryKey(member.name) }),
    ])
  }
  const applyMut = useMutation({
    mutationFn: () =>
      api.applyMemberRoleUpdate(member.name, {
        resolutions,
        expected_version: plan.data?.installed_version ?? '',
        member_fingerprint: plan.data?.member_fingerprint ?? '',
        template_fingerprint: plan.data?.template_fingerprint ?? '',
      }),
    onSuccess: () => {
      setReviewing(false)
      setResolutions({})
      setApplyError(null)
    },
    onError: (err: unknown) => {
      const code = errorCode(err)
      setApplyError(
        code === 'template_changed'
          ? t('pages.membersPage.role_update_moved')
          : code === 'member_changed_since_plan'
            ? t('pages.membersPage.role_update_member_moved')
            : err instanceof Error ? err.message : t('pages.membersPage.role_update_failed'),
      )
    },
    // Returned so isPending spans the refetch: the dialog's Apply stays
    // disabled until the drawer reads the new version, error paths included
    // (a request the server applied whose response was lost still needs the
    // roster reconciled).
    onSettled: () => refetchAll(),
  })
  const detachMut = useMutation({
    mutationFn: () => api.detachMember(member.name),
    onSuccess: () => { setConfirmDetach(false); setDetachError(null) },
    onError: (err: unknown) => setDetachError(err instanceof Error ? err.message : t('pages.membersPage.detach_failed')),
    onSettled: () => refetchAll(),
  })

  const fields = useMemo(() => reviewableFields(plan.data?.fields ?? []), [plan.data])
  const conflicts = fields.filter((f) => f.state === 'conflict').length
  const canApply = !!plan.data && conflictsResolved(fields, resolutions) && !applyMut.isPending

  const unavailable = plan.isError
    ? (() => {
        const code = errorCode(plan.error)
        if (code === 'pristine_copy_missing') return t('pages.membersPage.role_update_no_base')
        if (code === 'not_private_copy') return t('pages.membersPage.role_update_not_own_copy')
        if (code === 'app_not_installed') return t('pages.membersPage.role_update_app_gone', { app: appLabel })
        if (code === 'app_disabled') return t('pages.membersPage.role_update_app_disabled', { app: appLabel })
        return plan.error instanceof Error ? plan.error.message : t('pages.membersPage.role_update_unavailable')
      })()
    : null

  return (
    <div
      className="mt-3 flex flex-col gap-2 text-[11px] text-muted border border-border rounded-md px-2.5 py-2"
      data-testid="member-role-update"
    >
      {/* Every refusal on this surface is an ErrorNotice, never bare text, so the
          user gets the same treatment a failed save gets. No hand-off to an agent
          from any of the three: the plan is a READ the drawer re-runs on its own,
          and apply/detach act on a member whose state the user is looking at --
          the fix is to review again or to detach, both one click away here. */}
      {plan.isPending ? (
        <span>{t('pages.membersPage.role_update_checking')}</span>
      ) : unavailable ? (
        <ErrorNotice message={unavailable} variant="inline" testId="member-role-update-unavailable" />
      ) : plan.data?.update_available ? (
        <span data-testid="member-role-update-available">
          {t('pages.membersPage.role_update_available', {
            app: appLabel,
            version: plan.data.installed_version,
            from: plan.data.member_version || '?',
          })}
        </span>
      ) : (
        <span data-testid="member-role-update-current">
          {t('pages.membersPage.role_update_current', { version: plan.data?.installed_version ?? member.template_version ?? '?' })}
        </span>
      )}
      <ErrorNotice message={detachError} variant="inline" testId="member-detach-error" />
      {/* Two actions per row, never three: Review update and Detach share the
          first row; the detach confirm (Cancel / Yes, detach) replaces Detach's
          row rather than joining it. */}
      {!confirmDetach && (
        <div className="flex items-center gap-2">
          {plan.data?.update_available && (
            <Btn primary onClick={() => setReviewing(true)} data-testid="member-role-update-review">
              {t('pages.membersPage.role_update_review')}
            </Btn>
          )}
          <div className="flex-1" />
          <Btn onClick={() => setConfirmDetach(true)} disabled={detachMut.isPending} data-testid="member-detach">
            {t('pages.membersPage.detach')}
          </Btn>
        </div>
      )}
      {confirmDetach && (
        <>
          <p className="m-0 leading-relaxed" data-testid="member-detach-explain">
            {t('pages.membersPage.detach_explain', { app: appLabel })}
          </p>
          <div className="flex items-center justify-end gap-2" data-testid="member-detach-confirm-row">
            <Btn onClick={() => setConfirmDetach(false)} data-testid="cancel-detach-member">{t('components.confirmDialog.cancel')}</Btn>
            <Btn danger disabled={detachMut.isPending} onClick={() => detachMut.mutate()} data-testid="confirm-detach-member">
              {t('pages.membersPage.detach_confirm')}
            </Btn>
          </div>
        </>
      )}

      <Dialog open={reviewing} onOpenChange={(next) => { if (!next && !applyMut.isPending) setReviewing(false) }}>
        <DialogContent maxWidth={720} aria-label={t('pages.membersPage.role_update_title')}>
          <DialogHeader>
            <DialogTitle>{t('pages.membersPage.role_update_title')}</DialogTitle>
          </DialogHeader>
          <DialogBody>
            <p className="m-0 mb-3 text-[12px] text-muted">
              {t('pages.membersPage.role_update_intro', {
                app: appLabel,
                from: plan.data?.member_version || '?',
                to: plan.data?.installed_version ?? '?',
              })}
              {conflicts > 0 && ' ' + t('pages.membersPage.role_update_conflicts', { count: conflicts })}
            </p>
            {fields.length === 0 ? (
              <p className="m-0 text-[12px] text-muted" data-testid="role-update-version-only">
                {t('pages.membersPage.role_update_version_only')}
              </p>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-2 p-0" data-testid="role-update-fields">
                {fields.map((f) => (
                  <li key={f.field} className="rounded-md border border-border p-2" data-testid={`role-update-field-${f.field}`}>
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[12px]">{fieldLabel(f.field, t)}</span>
                      <span className={`text-[11px] ${f.state === 'conflict' ? 'text-warn' : 'text-muted'}`} data-testid="role-update-field-state">
                        {f.state === 'apply'
                          ? t('pages.membersPage.role_update_state_apply')
                          : f.state === 'keep'
                            ? t('pages.membersPage.role_update_state_keep')
                            : t('pages.membersPage.role_update_state_conflict')}
                      </span>
                    </div>
                    {f.state === 'conflict' ? (
                      <fieldset className="mt-2 flex flex-col gap-2 border-0 p-0">
                        <legend className="sr-only">{fieldLabel(f.field, t)}</legend>
                        {(['mine', 'theirs'] as const).map((side) => {
                          const sideLabel = side === 'mine'
                            ? t('pages.membersPage.role_update_keep_mine')
                            : t('pages.membersPage.role_update_take_theirs')
                          return (
                          <label key={side} className="flex items-start gap-2 text-[12px]">
                            <input
                              type="radio"
                              name={`resolution-${f.field}`}
                              value={side}
                              aria-label={`${fieldLabel(f.field, t)}: ${sideLabel}`}
                              checked={resolutions[f.field] === side}
                              onChange={() => setResolutions((prev) => ({ ...prev, [f.field]: side }))}
                              className="mt-0.5"
                            />
                            <span className="min-w-0 flex-1">
                              <span className="block text-muted">{sideLabel}</span>
                              <pre className="m-0 mt-0.5 max-h-24 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px]">
                                {fieldValueText(side === 'mine' ? f.mine : f.theirs, t('pages.membersPage.role_update_absent'))}
                              </pre>
                            </span>
                          </label>
                          )
                        })}
                      </fieldset>
                    ) : (
                      <pre className="m-0 mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-muted">
                        {fieldValueText(f.state === 'apply' ? f.theirs : f.mine, t('pages.membersPage.role_update_absent'))}
                      </pre>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {/* No hand-off: the plan re-reads on its own and the choice is the
                user's to make again in this dialog. */}
            <ErrorNotice message={applyError} variant="inline" testId="role-update-error" />
          </DialogBody>
          <DialogFooter>
            <Btn onClick={() => setReviewing(false)} disabled={applyMut.isPending}>{t('components.confirmDialog.cancel')}</Btn>
            <Btn primary disabled={!canApply} onClick={() => applyMut.mutate()} data-testid="role-update-apply">
              {applyMut.isPending
                ? t('pages.membersPage.role_update_applying')
                : t('pages.membersPage.role_update_apply', { version: plan.data?.installed_version ?? '' })}
            </Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
