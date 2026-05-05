import type { WsPayload } from '../types'

interface Props {
  payload: WsPayload | null
}

export default function StatusBar({ payload }: Props) {
  const running = payload?.bot_status === 'RUNNING'
  const paused = payload?.bot_status === 'PAUSED'
  const cb = payload?.circuit_breaker

  const stateLabel = !payload
    ? 'OFFLINE'
    : cb
    ? 'CIRCUIT BREAKER'
    : paused
    ? 'PAUSED'
    : payload.time_window === 'PRE_OPEN_WATCH'
    ? 'PRE-OPEN WATCH'
    : running && payload.market_open
    ? `TRADING · ${payload.time_window}`
    : running
    ? 'DORMANT'
    : 'STOPPED'

  const stateColor = !payload
    ? 'text-slate-500'
    : cb
    ? 'text-red-400'
    : paused
    ? 'text-yellow-400'
    : payload.time_window === 'PRE_OPEN_WATCH'
    ? 'text-blue-400'
    : running && payload.market_open
    ? 'text-green-400'
    : running
    ? 'text-slate-400'
    : 'text-slate-500'

  const vixColor =
    !payload?.vix_regime
      ? 'text-slate-400'
      : payload.vix_regime === 'LOW' || payload.vix_regime === 'NORMAL'
      ? 'text-green-400'
      : payload.vix_regime === 'ELEVATED'
      ? 'text-yellow-400'
      : 'text-red-400'

  const pnlVal = payload ? payload.paper_balance - payload.paper_day_start : 0
  const pnlColor = pnlVal >= 0 ? 'text-green-400' : 'text-red-400'

  function fmtDollar(n: number) {
    const sign = n >= 0 ? '+' : '-'
    return `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 px-4 py-2 bg-[#1a1d27] border-b border-[#2a2d3a] text-xs">
      <Stat label="STATUS">
        <span className={`font-semibold ${stateColor}`}>{stateLabel}</span>
      </Stat>

      <Stat label="VIX">
        <span className={vixColor}>
          {payload?.vix?.toFixed(2) ?? '—'}
          {payload?.vix_regime ? ` · ${payload.vix_regime}` : ''}
        </span>
      </Stat>

      <Stat label="BALANCE">
        <span className="text-slate-300">
          ${payload?.paper_balance?.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) ?? '—'}
        </span>
      </Stat>

      <Stat label="DAILY P&L">
        <span className={pnlColor}>{payload ? fmtDollar(pnlVal) : '—'}</span>
      </Stat>

      <Stat label="TRADES">
        <span className="text-slate-300">{payload?.total_trades ?? '—'}</span>
      </Stat>

      <Stat label="WIN RATE">
        <span className="text-slate-300">
          {payload && payload.total_trades > 0
            ? `${((payload.winning_trades / payload.total_trades) * 100).toFixed(1)}%`
            : '—'}
        </span>
      </Stat>

      {cb && (
        <span className="ml-auto text-red-400 font-semibold animate-pulse text-[10px]">
          ⚠ CIRCUIT BREAKER — NO NEW ENTRIES
        </span>
      )}
    </div>
  )
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-slate-500 uppercase tracking-wider text-[9px]">{label}</span>
      {children}
    </div>
  )
}
