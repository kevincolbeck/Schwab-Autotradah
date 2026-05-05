import { useEffect, useState } from 'react'
import type { TickerStat } from '../types'

interface StatsResponse {
  stats: Record<string, TickerStat>
  period_days: number
}

function pct(n: number) {
  const color = n >= 50 ? 'text-green-400' : n >= 40 ? 'text-yellow-400' : 'text-red-400'
  return <span className={color}>{n.toFixed(1)}%</span>
}

function pnl(n: number) {
  const color = n >= 0 ? 'text-green-400' : 'text-red-400'
  const sign = n >= 0 ? '+' : '-'
  return <span className={color}>{sign}${Math.abs(n).toFixed(2)}</span>
}

const TICKER_ORDER = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'GOOGL', 'META', 'AMZN', 'TSLA']

export default function Stats() {
  const [data, setData] = useState<StatsResponse | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/stats?days=30')
      .then((r) => r.json())
      .then((d) => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  if (loading) return <div className="text-slate-600 text-xs text-center py-12">Loading…</div>
  if (!data) return <div className="text-slate-600 text-xs text-center py-12">Failed to load</div>

  const tickers = TICKER_ORDER.filter((t) => t in data.stats)

  // Aggregate totals
  const totTrades = tickers.reduce((s, t) => s + data.stats[t].trades, 0)
  const totWins   = tickers.reduce((s, t) => s + data.stats[t].wins, 0)
  const totPnl    = tickers.reduce((s, t) => s + data.stats[t].total_pnl, 0)

  return (
    <div className="p-4 flex flex-col gap-4">
      {/* Aggregate header */}
      <div className="flex gap-6 text-xs">
        <span className="text-slate-500">Trades: <span className="text-slate-300">{totTrades}</span></span>
        <span className="text-slate-500">Win rate: {totTrades > 0 ? pct(totWins / totTrades * 100) : <span className="text-slate-400">—</span>}</span>
        <span className="text-slate-500">Total P&L: {pnl(totPnl)}</span>
        <span className="text-slate-500">Period: <span className="text-slate-300">{data.period_days}d</span></span>
      </div>

      {/* Per-ticker table */}
      <div className="bg-[#1a1d27] border border-[#2a2d3a] rounded overflow-hidden">
        <div className="px-4 py-2 border-b border-[#2a2d3a]">
          <h3 className="text-[10px] text-slate-500 uppercase tracking-widest">Per-Ticker Performance</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-[9px] text-slate-500 uppercase tracking-wider border-b border-[#2a2d3a]">
                <th className="px-4 py-2 text-left">Ticker</th>
                <th className="px-4 py-2 text-right">Trades</th>
                <th className="px-4 py-2 text-right">Wins</th>
                <th className="px-4 py-2 text-right">Win %</th>
                <th className="px-4 py-2 text-right">Total P&L</th>
                <th className="px-4 py-2 text-right">Avg P&L</th>
                <th className="px-4 py-2 text-right">Sig Acc 15m</th>
                <th className="px-4 py-2 text-right">Sig Acc 30m</th>
                <th className="px-4 py-2 text-right">Sig Acc 1h</th>
              </tr>
            </thead>
            <tbody>
              {tickers.length === 0 ? (
                <tr>
                  <td colSpan={9} className="text-center text-slate-600 py-8">No data yet</td>
                </tr>
              ) : (
                tickers.map((ticker) => {
                  const s = data.stats[ticker]
                  return (
                    <tr key={ticker} className="border-b border-[#2a2d3a] hover:bg-[#0f1117]">
                      <td className="px-4 py-2 font-semibold text-slate-200">{ticker}</td>
                      <td className="px-4 py-2 text-right text-slate-300">{s.trades}</td>
                      <td className="px-4 py-2 text-right text-slate-300">{s.wins}</td>
                      <td className="px-4 py-2 text-right">{s.trades > 0 ? pct(s.win_rate) : <span className="text-slate-600">—</span>}</td>
                      <td className="px-4 py-2 text-right">{s.trades > 0 ? pnl(s.total_pnl) : <span className="text-slate-600">—</span>}</td>
                      <td className="px-4 py-2 text-right">{s.trades > 0 ? pnl(s.avg_pnl) : <span className="text-slate-600">—</span>}</td>
                      <td className="px-4 py-2 text-right">{pct(s.signal_accuracy_15m)}</td>
                      <td className="px-4 py-2 text-right">{pct(s.signal_accuracy_30m)}</td>
                      <td className="px-4 py-2 text-right">{pct(s.signal_accuracy_1h)}</td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
