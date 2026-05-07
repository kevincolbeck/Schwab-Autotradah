import type { OpenPosition } from '../types'

interface Props {
  positions: OpenPosition[]
}

function fmt$(n: number | null, dec = 2) {
  if (n === null) return '—'
  return `$${Math.abs(n).toFixed(dec)}`
}

function elapsed(entryTs: string) {
  const ms = Date.now() - new Date(entryTs).getTime()
  const min = Math.floor(ms / 60000)
  const sec = Math.floor((ms % 60000) / 1000)
  return `${min}m ${sec}s`
}

function StageTag({ stage }: { stage: OpenPosition['tp_stage'] }) {
  if (stage === 'TP2') return <span className="text-[9px] px-1 py-0.5 rounded bg-purple-500/20 text-purple-300 font-semibold">TP2</span>
  if (stage === 'TP1') return <span className="text-[9px] px-1 py-0.5 rounded bg-blue-500/20 text-blue-300 font-semibold">TP1</span>
  return <span className="text-[9px] px-1 py-0.5 rounded bg-slate-700 text-slate-500">ENTRY</span>
}

function StopCell({ pos }: { pos: OpenPosition }) {
  if (pos.tp_stage === 'TP2' && pos.trailing_stop_price !== null) {
    return (
      <span className="text-yellow-400 text-[10px]">
        {fmt$(pos.trailing_stop_price)} <span className="text-slate-500">trail</span>
      </span>
    )
  }
  if (pos.stop_at_breakeven) {
    return <span className="text-blue-300 text-[10px]">{fmt$(pos.stop_price)} <span className="text-slate-500">BE</span></span>
  }
  return <span className="text-slate-400">{fmt$(pos.stop_price)}</span>
}

export default function OpenPositions({ positions }: Props) {
  return (
    <div className="bg-[#1a1d27] border border-[#2a2d3a] rounded overflow-hidden">
      <div className="px-4 py-2 border-b border-[#2a2d3a] flex items-center justify-between">
        <h2 className="text-[10px] text-slate-500 uppercase tracking-widest">Open Positions</h2>
        <span className="text-xs text-slate-400">{positions.length} open</span>
      </div>

      {positions.length === 0 ? (
        <div className="text-slate-600 text-xs text-center py-6">No open positions</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-[9px] text-slate-500 uppercase tracking-wider border-b border-[#2a2d3a]">
                <th className="px-3 py-1.5 text-left">Ticker</th>
                <th className="px-3 py-1.5 text-left">Dir</th>
                <th className="px-3 py-1.5 text-left">Stage</th>
                <th className="px-3 py-1.5 text-left">Contract</th>
                <th className="px-3 py-1.5 text-right">Cts</th>
                <th className="px-3 py-1.5 text-right">Entry</th>
                <th className="px-3 py-1.5 text-right">Current</th>
                <th className="px-3 py-1.5 text-right">Unreal PnL</th>
                <th className="px-3 py-1.5 text-right">Unreal %</th>
                <th className="px-3 py-1.5 text-right">Peak %</th>
                <th className="px-3 py-1.5 text-right">Realized</th>
                <th className="px-3 py-1.5 text-right">Stop</th>
                <th className="px-3 py-1.5 text-right">Age</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const pnlColor =
                  p.unrealized_pnl === null
                    ? 'text-slate-500'
                    : p.unrealized_pnl >= 0
                    ? 'text-green-400'
                    : 'text-red-400'

                const realizedColor = p.realized_partials_usd > 0
                  ? 'text-green-400'
                  : p.realized_partials_usd < 0
                  ? 'text-red-400'
                  : 'text-slate-600'

                return (
                  <tr key={p.trade_id} className="border-b border-[#2a2d3a] hover:bg-[#0f1117]">
                    <td className="px-3 py-1.5 font-semibold text-slate-200">{p.ticker}</td>
                    <td className="px-3 py-1.5">
                      <span className={`font-semibold ${p.direction === 'LONG' ? 'text-green-400' : 'text-red-400'}`}>
                        {p.direction}
                      </span>
                    </td>
                    <td className="px-3 py-1.5"><StageTag stage={p.tp_stage} /></td>
                    <td className="px-3 py-1.5 text-slate-400">
                      {p.option_type} ${p.option_strike} {p.option_expiry}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-300">
                      {p.contracts_remaining}
                      {p.contracts_remaining < p.contracts && (
                        <span className="text-slate-600 text-[9px]">/{p.contracts}</span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-300">{fmt$(p.entry_price)}</td>
                    <td className="px-3 py-1.5 text-right text-slate-300">
                      {p.current_mid !== null ? fmt$(p.current_mid) : <span className="text-slate-600">—</span>}
                    </td>
                    <td className={`px-3 py-1.5 text-right ${pnlColor}`}>
                      {p.unrealized_pnl !== null
                        ? `${p.unrealized_pnl >= 0 ? '+' : '-'}$${Math.abs(p.unrealized_pnl).toFixed(0)}`
                        : '—'}
                    </td>
                    <td className={`px-3 py-1.5 text-right ${pnlColor}`}>
                      {p.unrealized_pct !== null
                        ? `${p.unrealized_pct >= 0 ? '+' : ''}${p.unrealized_pct.toFixed(1)}%`
                        : '—'}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-400">
                      {p.peak_unrealized_pct > 0
                        ? <span className="text-green-400/60">+{p.peak_unrealized_pct.toFixed(1)}%</span>
                        : '—'}
                    </td>
                    <td className={`px-3 py-1.5 text-right ${realizedColor}`}>
                      {p.realized_partials_usd !== 0
                        ? `${p.realized_partials_usd >= 0 ? '+' : '-'}$${Math.abs(p.realized_partials_usd).toFixed(0)}`
                        : '—'}
                    </td>
                    <td className="px-3 py-1.5 text-right"><StopCell pos={p} /></td>
                    <td className="px-3 py-1.5 text-right text-slate-500">
                      {elapsed(p.entry_ts ?? '')}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
