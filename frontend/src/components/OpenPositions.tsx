import type { OpenPosition } from '../types'

interface Props {
  positions: OpenPosition[]
}

function fmt$(n: number | null, dec = 2) {
  if (n === null) return '—'
  return `$${Math.abs(n).toFixed(dec)}`
}

function elapsed(ms: number) {
  const min = Math.floor(ms / 60000)
  const sec = Math.floor((ms % 60000) / 1000)
  return `${min}m ${sec}s`
}

export default function OpenPositions({ positions }: Props) {
  const now = Date.now()

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
                <th className="px-3 py-1.5 text-left">Contract</th>
                <th className="px-3 py-1.5 text-right">Cts</th>
                <th className="px-3 py-1.5 text-right">Entry</th>
                <th className="px-3 py-1.5 text-right">Current</th>
                <th className="px-3 py-1.5 text-right">Unreal PnL</th>
                <th className="px-3 py-1.5 text-right">Unreal %</th>
                <th className="px-3 py-1.5 text-right">Stop</th>
                <th className="px-3 py-1.5 text-right">2R Target</th>
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

                return (
                  <tr key={p.trade_id} className="border-b border-[#2a2d3a] hover:bg-[#0f1117]">
                    <td className="px-3 py-1.5 font-semibold text-slate-200">{p.ticker}</td>
                    <td className="px-3 py-1.5">
                      <span className={`font-semibold ${p.direction === 'LONG' ? 'text-green-400' : 'text-red-400'}`}>
                        {p.direction}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-slate-400">
                      {p.option_type} ${p.option_strike} {p.option_expiry}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-300">{p.contracts}</td>
                    <td className="px-3 py-1.5 text-right text-slate-300">{fmt$(p.entry_price)}</td>
                    <td className="px-3 py-1.5 text-right text-slate-300">
                      {p.current_mid !== null ? fmt$(p.current_mid) : <span className="text-slate-600">—</span>}
                    </td>
                    <td className={`px-3 py-1.5 text-right ${pnlColor}`}>
                      {p.unrealized_pnl !== null
                        ? `${p.unrealized_pnl >= 0 ? '+' : '-'}$${Math.abs(p.unrealized_pnl).toFixed(2)}`
                        : '—'}
                    </td>
                    <td className={`px-3 py-1.5 text-right ${pnlColor}`}>
                      {p.unrealized_pct !== null
                        ? `${p.unrealized_pct >= 0 ? '+' : ''}${p.unrealized_pct.toFixed(1)}%`
                        : '—'}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-400">{fmt$(p.stop_price)}</td>
                    <td className="px-3 py-1.5 text-right text-slate-400">{fmt$(p.target_2r)}</td>
                    <td className="px-3 py-1.5 text-right text-slate-500">
                      {elapsed(now)}
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
