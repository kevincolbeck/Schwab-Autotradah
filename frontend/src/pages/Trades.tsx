import { useEffect, useState } from 'react'
import type { Trade } from '../types'

function fmtPnl(n: number | null) {
  if (n === null) return <span className="text-slate-500">—</span>
  const color = n >= 0 ? 'text-green-400' : 'text-red-400'
  const sign = n >= 0 ? '+' : '-'
  return <span className={color}>{sign}${Math.abs(n).toFixed(2)}</span>
}

function fmtTs(ts: string | null) {
  if (!ts) return '—'
  return new Date(ts).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function dur(entry: string, exit: string | null) {
  if (!exit) return '—'
  const ms = new Date(exit).getTime() - new Date(entry).getTime()
  const min = Math.floor(ms / 60000)
  const sec = Math.floor((ms % 60000) / 1000)
  return `${min}m ${sec}s`
}

export default function Trades() {
  const [trades, setTrades] = useState<Trade[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/trades?limit=200')
      .then((r) => r.json())
      .then((d) => { setTrades(d.trades ?? []); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  const closed = trades.filter((t) => t.exit_ts !== null)
  const wins = closed.filter((t) => (t.net_pnl ?? 0) > 0).length
  const totalPnl = closed.reduce((s, t) => s + (t.net_pnl ?? 0), 0)

  return (
    <div className="p-4 flex flex-col gap-4">
      <div className="flex items-center gap-6 text-xs">
        <span className="text-slate-500">Trades: <span className="text-slate-300">{closed.length}</span></span>
        <span className="text-slate-500">Win rate:{' '}
          <span className={closed.length > 0 ? (wins / closed.length >= 0.5 ? 'text-green-400' : 'text-red-400') : 'text-slate-400'}>
            {closed.length > 0 ? `${((wins / closed.length) * 100).toFixed(1)}%` : '—'}
          </span>
        </span>
        <span className="text-slate-500">Total P&L:{' '}
          <span className={totalPnl >= 0 ? 'text-green-400' : 'text-red-400'}>
            {totalPnl >= 0 ? '+' : '-'}${Math.abs(totalPnl).toFixed(2)}
          </span>
        </span>
      </div>

      <div className="bg-[#1a1d27] border border-[#2a2d3a] rounded overflow-hidden">
        {loading ? (
          <div className="text-slate-600 text-xs text-center py-8">Loading…</div>
        ) : trades.length === 0 ? (
          <div className="text-slate-600 text-xs text-center py-8">No trades recorded yet</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-[9px] text-slate-500 uppercase tracking-wider border-b border-[#2a2d3a]">
                  <th className="px-3 py-2 text-left">Entry Time</th>
                  <th className="px-3 py-2 text-left">Ticker</th>
                  <th className="px-3 py-2 text-left">Dir</th>
                  <th className="px-3 py-2 text-left">Contract</th>
                  <th className="px-3 py-2 text-right">Cts</th>
                  <th className="px-3 py-2 text-right">Entry$</th>
                  <th className="px-3 py-2 text-right">Exit$</th>
                  <th className="px-3 py-2 text-left">Reason</th>
                  <th className="px-3 py-2 text-right">Duration</th>
                  <th className="px-3 py-2 text-right">Net P&L</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-b border-[#2a2d3a] hover:bg-[#0f1117]">
                    <td className="px-3 py-1.5 text-slate-400">{fmtTs(t.entry_ts)}</td>
                    <td className="px-3 py-1.5 font-semibold text-slate-200">{t.ticker}</td>
                    <td className="px-3 py-1.5">
                      <span className={`font-semibold ${t.direction === 'LONG' ? 'text-green-400' : 'text-red-400'}`}>
                        {t.direction}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-slate-400">
                      {t.option_type} ${t.option_strike} {t.option_expiry}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-300">{t.contracts}</td>
                    <td className="px-3 py-1.5 text-right text-slate-300">${t.entry_price?.toFixed(2)}</td>
                    <td className="px-3 py-1.5 text-right text-slate-300">
                      {t.exit_price !== null ? `$${t.exit_price.toFixed(2)}` : <span className="text-slate-600">open</span>}
                    </td>
                    <td className="px-3 py-1.5 text-slate-500">{t.exit_reason ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right text-slate-500">{dur(t.entry_ts, t.exit_ts)}</td>
                    <td className="px-3 py-1.5 text-right">{fmtPnl(t.net_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
