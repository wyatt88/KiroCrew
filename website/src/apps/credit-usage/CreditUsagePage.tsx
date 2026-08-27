// Credit Usage — dashboard page (/credit-usage).
//
// A real-time view of the credits Kiro Crew agents consume. The gateway writes
// one row per agent turn to <data home>/usage/tokens/<date>.jsonl; the app's
// backend rolls those rows up and this page polls the rollup on a short interval
// (there is no usage push channel on the dashboard ws, so we poll — the backend
// memoizes reads by shard signature, so a poll between turns is essentially
// free). Credits are provider-reported, not derived from tokens, so everything
// here is a straight sum of the `credits` field.
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, Coins, Cpu, Layers, RefreshCw, Users } from 'lucide-react'

import { Badge, Card, CardTitle, StatCard } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { i18nT } from '../../i18n/t'

import { creditUsageApi } from './api'
import { DEFAULT_WINDOW_DAYS, POLL_MS, STORAGE_KEY, WINDOW_CHOICES } from './constants'
import type { AlertConfig, BreakdownRow, SummaryResponse, TopSession } from './types'

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmtCredits(n: number | null | undefined): string {
  const v = typeof n === 'number' && isFinite(n) ? n : 0
  if (v >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (v >= 100) return v.toFixed(1)
  return v.toFixed(2)
}

function fmtInt(n: number | null | undefined): string {
  const v = typeof n === 'number' && isFinite(n) ? n : 0
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function pct(part: number, whole: number): number {
  if (!whole) return 0
  return Math.round((part / whole) * 100)
}

function surfaceVariant(surface: string): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  switch (surface) {
    case 'dashboard':
      return 'aim'
    case 'subagent':
      return 'ok'
    case 'cron':
      return 'warn'
    case 'webhook':
      return 'warn'
    default:
      return 'muted'
  }
}

function shortSlot(slot: string): string {
  // dashboard:chat-69-1785905004 -> chat-69 ; subagent:5e240c42 -> subagent 5e240c42
  const s = slot.replace(/^dashboard:/, '')
  const m = s.match(/^(chat-\d+)/)
  if (m) return m[1]
  return s.length > 28 ? s.slice(0, 27) + '…' : s
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function TrendChart({ data }: { data: SummaryResponse['trend'] }) {
  if (!data.length) return null
  const max = Math.max(1, ...data.map((d) => d.credits))
  const H = 160
  const AX = 46 // width of the fixed Y-axis column (does NOT scroll)
  const XLABEL_H = 24 // space below the plot for date labels
  const BAR_W = 26
  const bw = BAR_W
  const plotW = data.length * bw
  // Label at most ~8 x-ticks so the axis stays readable across window sizes.
  const step = Math.max(1, Math.ceil(data.length / 8))
  // 5 horizontal gridlines / Y ticks: 0, 25%, 50%, 75%, 100% of max.
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => ({ f, v: max * f, y: H - f * H }))
  return (
    <div style={{ display: 'flex', width: '100%', alignItems: 'stretch' }}>
      {/* Fixed Y axis — pinned, never scrolls */}
      <svg
        width={AX}
        height={H + XLABEL_H}
        viewBox={`0 0 ${AX} ${H + XLABEL_H}`}
        style={{ flex: `0 0 ${AX}px`, display: 'block' }}
        aria-hidden="true"
      >
        {yTicks.map((t) => (
          <text
            key={t.f}
            x={AX - 6}
            y={t.y + 3}
            textAnchor="end"
            fontSize="9"
            fill="var(--text-muted, #888)"
          >
            {fmtCredits(t.v)}
          </text>
        ))}
      </svg>
      {/* Scrollable plot. Few days → fills the card width (bars widen, no blank
          gap); many days → overflows and scrolls, Y axis stays pinned. Bars &
          gridlines live in a preserveAspectRatio=none SVG (rects/lines scale
          cleanly); the date labels are an aligned HTML row so text never
          distorts when the plot is stretched to fill. */}
      <div style={{ flex: 1, minWidth: 0, overflowX: 'auto' }}>
        <div style={{ minWidth: plotW }}>
          <svg
            viewBox={`0 0 ${plotW} ${H}`}
            height={H}
            width="100%"
            preserveAspectRatio="none"
            style={{ display: 'block' }}
            role="img"
            aria-label={i18nT('creditUsage.trendTitle')}
          >
            {yTicks.map((t) => (
              <line
                key={t.f}
                x1={0}
                y1={t.y}
                x2={plotW}
                y2={t.y}
                stroke="var(--border, rgba(127,127,127,0.2))"
                strokeWidth={1}
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {data.map((d, i) => {
              const h = Math.max(d.credits > 0 ? 1 : 0, (d.credits / max) * H)
              const x = i * bw
              const y = H - h
              return (
                <rect
                  key={d.date}
                  x={x + 1}
                  y={y}
                  width={Math.max(1, bw - 2)}
                  height={h}
                  rx={2}
                  fill="var(--accent, #6366f1)"
                  opacity={d.credits > 0 ? 0.85 : 0.15}
                >
                  <title>{`${d.date} — ${fmtCredits(d.credits)} credits`}</title>
                </rect>
              )
            })}
          </svg>
          {/* Date labels: one equal-flex cell per day, so they stay aligned
              with the bars whether the plot is stretched (fill) or scrolled. */}
          <div style={{ display: 'flex', height: XLABEL_H, alignItems: 'center' }}>
            {data.map((d, i) => (
              <div
                key={d.date}
                style={{
                  flex: '1 1 0',
                  textAlign: 'center',
                  fontSize: 9,
                  color: 'var(--text-muted, #888)',
                  whiteSpace: 'nowrap',
                  overflow: 'visible',
                }}
              >
                {i % step === 0 ? d.date.slice(5) : ''}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function BreakdownBars({ rows, total }: { rows: BreakdownRow[]; total: number }) {
  if (!rows.length) {
    return <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>{i18nT('creditUsage.noData')}</div>
  }
  const top = rows.slice(0, 8)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {top.map((r) => {
        const p = pct(r.credits, total)
        return (
          <div key={r.name} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
              <span style={{ fontWeight: 600 }}>{r.name}</span>
              <span style={{ color: 'var(--text-muted, #888)' }}>
                {fmtCredits(r.credits)} · {p}% · {fmtInt(r.turns)}
                {' '}
                {i18nT('creditUsage.turnsShort')}
              </span>
            </div>
            <div
              style={{
                height: 8,
                borderRadius: 4,
                background: 'var(--surface-2, rgba(127,127,127,0.15))',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${p}%`,
                  height: '100%',
                  background: 'var(--accent, #6366f1)',
                  borderRadius: 4,
                }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function TopSessionsTable({ rows }: { rows: TopSession[] }) {
  if (!rows.length) {
    return <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>{i18nT('creditUsage.noData')}</div>
  }
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ textAlign: 'left', color: 'var(--text-muted, #888)' }}>
            <th style={{ padding: '6px 8px' }}>{i18nT('creditUsage.colSession')}</th>
            <th style={{ padding: '6px 8px' }}>{i18nT('creditUsage.colSurface')}</th>
            <th style={{ padding: '6px 8px', textAlign: 'right' }}>{i18nT('creditUsage.colCredits')}</th>
            <th style={{ padding: '6px 8px', textAlign: 'right' }}>{i18nT('creditUsage.colTurns')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.slot} style={{ borderTop: '1px solid var(--border, rgba(127,127,127,0.2))' }}>
              <td style={{ padding: '6px 8px' }} title={r.slot}>
                <div style={{ fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 340 }}>
                  {r.title && r.title !== r.slot ? r.title : shortSlot(r.slot)}
                </div>
                {r.title && r.title !== r.slot && (
                  <div style={{ fontSize: 11, color: 'var(--text-muted, #888)', fontFamily: 'var(--font-mono, monospace)' }}>
                    {shortSlot(r.slot)}
                  </div>
                )}
              </td>
              <td style={{ padding: '6px 8px' }}>
                <Badge variant={surfaceVariant(r.surface)}>{r.surface}</Badge>
              </td>
              <td style={{ padding: '6px 8px', textAlign: 'right', fontWeight: 600 }}>{fmtCredits(r.credits)}</td>
              <td style={{ padding: '6px 8px', textAlign: 'right', color: 'var(--text-muted, #888)' }}>
                {fmtInt(r.turns)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Alert settings — daily threshold + notify ratio (checked by a background cron)
// ---------------------------------------------------------------------------

function AlertSettings() {
  const qc = useQueryClient()
  const cfgQ = useQuery({
    queryKey: ['credit-usage', 'alert-config'],
    queryFn: () => creditUsageApi.getAlertConfig(),
    staleTime: 0,
  })
  const todayQ = useQuery({
    queryKey: ['credit-usage', 'today'],
    queryFn: () => creditUsageApi.today(),
    refetchInterval: POLL_MS,
    staleTime: 0,
  })

  const [enabled, setEnabled] = useState(false)
  const [threshold, setThreshold] = useState('')
  const [ratio, setRatio] = useState(80)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (cfgQ.data && !dirty) {
      setEnabled(cfgQ.data.enabled)
      setThreshold(cfgQ.data.threshold ? String(cfgQ.data.threshold) : '')
      setRatio(Math.round((cfgQ.data.ratio || 0.8) * 100))
    }
  }, [cfgQ.data, dirty])

  const save = useMutation({
    mutationFn: async (cfg: AlertConfig) => {
      await creditUsageApi.saveAlertConfig(cfg)
      // Enable creates/refreshes the hourly Schedule job; disable removes it.
      await creditUsageApi.setAlertSchedule(cfg.enabled)
      return cfg
    },
    onSuccess: () => {
      setDirty(false)
      void qc.invalidateQueries({ queryKey: ['credit-usage', 'alert-config'] })
    },
  })

  const thr = parseFloat(threshold) || 0
  const trigger = thr * (ratio / 100)
  const todayCredits = todayQ.data?.credits ?? 0
  const pctOfTrigger = trigger > 0 ? Math.min(100, Math.round((todayCredits / trigger) * 100)) : 0
  const over = enabled && trigger > 0 && todayCredits >= trigger

  return (
    <Card>
      <CardTitle>
        <Bell size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
        {i18nT('creditUsage.alertTitle')}
      </CardTitle>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', gap: 16 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => {
              setEnabled(e.target.checked)
              setDirty(true)
            }}
          />
          {i18nT('creditUsage.alertEnable')}
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 12 }}>
          <span style={{ color: 'var(--text-muted, #888)' }}>{i18nT('creditUsage.alertThreshold')}</span>
          <input
            type="number"
            min={0}
            step={10}
            value={threshold}
            onChange={(e) => {
              setThreshold(e.target.value)
              setDirty(true)
            }}
            placeholder="e.g. 500"
            style={{
              width: 110,
              padding: '4px 8px',
              borderRadius: 6,
              border: '1px solid var(--border, rgba(127,127,127,0.3))',
              background: 'transparent',
              color: 'inherit',
              fontSize: 13,
            }}
          />
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 12 }}>
          <span style={{ color: 'var(--text-muted, #888)' }}>{i18nT('creditUsage.alertRatio')}</span>
          <SimpleSelect
            aria-label={i18nT('creditUsage.alertRatio')}
            options={['50', '60', '70', '80', '90', '100']}
            optionLabels={['50%', '60%', '70%', '80%', '90%', '100%']}
            value={String(ratio)}
            onChange={(v) => {
              setRatio(Number(v))
              setDirty(true)
            }}
          />
        </label>
        <button
          type="button"
          onClick={() => save.mutate({ enabled, threshold: thr, ratio: ratio / 100 })}
          disabled={save.isPending || !dirty}
          style={{
            padding: '6px 14px',
            borderRadius: 6,
            border: '1px solid var(--accent, #6366f1)',
            background: dirty ? 'var(--accent, #6366f1)' : 'transparent',
            color: dirty ? '#fff' : 'var(--text-muted, #888)',
            cursor: dirty && !save.isPending ? 'pointer' : 'default',
            fontSize: 13,
          }}
        >
          {save.isPending ? '…' : i18nT('creditUsage.alertSave')}
        </button>
      </div>
      {enabled && trigger > 0 && (
        <div style={{ marginTop: 12, fontSize: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
            <span style={{ color: 'var(--text-muted, #888)' }}>
              {i18nT('creditUsage.alertTodayVsTrigger', {
                today: fmtCredits(todayCredits),
                trigger: fmtCredits(trigger),
              })}
            </span>
            <span style={{ color: over ? 'var(--danger, #e11d48)' : 'var(--text-muted, #888)', fontWeight: 600 }}>
              {over ? i18nT('creditUsage.alertOver') : i18nT('creditUsage.alertPctOfTrigger', { pct: pctOfTrigger })}
            </span>
          </div>
          <div
            style={{
              height: 8,
              borderRadius: 4,
              background: 'var(--surface-2, rgba(127,127,127,0.15))',
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${pctOfTrigger}%`,
                height: '100%',
                background: over ? 'var(--danger, #e11d48)' : 'var(--accent, #6366f1)',
              }}
            />
          </div>
          <div style={{ marginTop: 6, color: 'var(--text-muted, #888)' }}>
            {i18nT('creditUsage.alertHint')}
          </div>
        </div>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function CreditUsagePage() {
  const [days, setDays] = useState<number>(() => {
    try {
      const v = parseInt(localStorage.getItem(STORAGE_KEY) || '', 10)
      return WINDOW_CHOICES.includes(v as (typeof WINDOW_CHOICES)[number]) ? v : DEFAULT_WINDOW_DAYS
    } catch {
      return DEFAULT_WINDOW_DAYS
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, String(days))
    } catch {
      /* ignore quota / private-mode failures */
    }
  }, [days])

  const summaryQ = useQuery({
    queryKey: ['credit-usage', 'summary', days],
    queryFn: () => creditUsageApi.summary(days),
    // The global QueryClient sets staleTime: Infinity (freshness is push-driven
    // app-wide). Credit usage has no ws push, so opt this query back into
    // interval polling + focus/mount refetch by making it always stale.
    staleTime: 0,
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
    refetchOnMount: 'always',
  })

  const s = summaryQ.data
  const totals = s?.totals
  const windowTotal = totals?.windowCredits ?? 0

  const [spin, setSpin] = useState(false)
  const refreshNow = () => {
    setSpin(true)
    void summaryQ.refetch().finally(() => setTimeout(() => setSpin(false), 500))
  }

  const busy = spin || summaryQ.isFetching

  return (
    <div style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1100, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Coins size={22} />
        <h2 style={{ margin: 0, fontSize: 20 }}>{i18nT('creditUsage.title')}</h2>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          onClick={refreshNow}
          title={i18nT('creditUsage.refresh')}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 10px',
            borderRadius: 6,
            border: '1px solid var(--border, rgba(127,127,127,0.3))',
            background: 'transparent',
            color: 'inherit',
            cursor: 'pointer',
            fontSize: 12,
          }}
        >
          <RefreshCw size={13} className={busy ? 'animate-spin' : undefined} />
          {i18nT('creditUsage.refresh')}
        </button>
        <div style={{ display: 'flex', gap: 4 }}>
          {WINDOW_CHOICES.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setDays(c)}
              style={{
                padding: '4px 10px',
                borderRadius: 6,
                border: '1px solid var(--border, rgba(127,127,127,0.3))',
                background: c === days ? 'var(--accent, #6366f1)' : 'transparent',
                color: c === days ? '#fff' : 'inherit',
                cursor: 'pointer',
                fontSize: 12,
              }}
            >
              {i18nT('creditUsage.daysChoice', { n: c })}
            </button>
          ))}
        </div>
      </div>

      {summaryQ.isError && (
        <Card>
          <div style={{ color: 'var(--danger, #e11d48)', fontSize: 13 }}>
            {i18nT('creditUsage.loadError')}{' '}
            {summaryQ.error instanceof Error ? summaryQ.error.message : ''}
          </div>
        </Card>
      )}

      {/* KPI cards */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
          gap: 12,
        }}
      >
        <StatCard label={i18nT('creditUsage.statToday')} value={fmtCredits(totals?.today)} accent />
        <StatCard label={i18nT('creditUsage.statWeek')} value={fmtCredits(totals?.thisWeek)} />
        <StatCard label={i18nT('creditUsage.statMonth')} value={fmtCredits(totals?.thisMonth)} />
        <StatCard
          label={i18nT('creditUsage.statWindow', { n: days })}
          value={fmtCredits(totals?.windowCredits)}
        />
        <StatCard label={i18nT('creditUsage.statAllTime')} value={fmtCredits(totals?.allTimeCredits)} />
        <StatCard
          label={i18nT('creditUsage.statTurns')}
          value={fmtInt(totals?.allTimeTurns)}
          title={i18nT('creditUsage.statTurnsTip')}
        />
      </div>

      {/* Alert settings */}
      <AlertSettings />

      {/* Trend */}
      <Card>
        <CardTitle>{i18nT('creditUsage.trendTitle')}</CardTitle>
        {/* Fixed-height viewport: if the chart is taller than this, scroll it
            vertically inside the card instead of growing the card / page. */}
        <div style={{ maxHeight: 240, overflowY: 'auto' }}>
          <TrendChart data={s?.trend ?? []} />
        </div>
      </Card>

      {/* Breakdowns */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
        <Card>
          <CardTitle>
            <Cpu size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            {i18nT('creditUsage.byModel')}
          </CardTitle>
          <BreakdownBars rows={s?.byModel ?? []} total={windowTotal} />
        </Card>
        <Card>
          <CardTitle>
            <Layers size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            {i18nT('creditUsage.bySurface')}
          </CardTitle>
          <BreakdownBars rows={s?.bySurface ?? []} total={windowTotal} />
        </Card>
        <Card>
          <CardTitle>
            <Users size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            {i18nT('creditUsage.byAgent')}
          </CardTitle>
          <BreakdownBars rows={s?.byAgent ?? []} total={windowTotal} />
        </Card>
      </div>

      {/* Top sessions */}
      <Card>
        <CardTitle>
          {i18nT('creditUsage.topSessions')}
          {s?.sessionCount ? (
            <span style={{ color: 'var(--text-muted, #888)', fontWeight: 400, marginLeft: 8, fontSize: 13 }}>
              {i18nT('creditUsage.ofSessions', { n: s.sessionCount })}
            </span>
          ) : null}
        </CardTitle>
        <TopSessionsTable rows={s?.topSessions ?? []} />
      </Card>
    </div>
  )
}
