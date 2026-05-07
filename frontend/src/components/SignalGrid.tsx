import { clsx } from 'clsx'
import type { TickerState } from '../types'

interface Props {
  tickers: Record<string, TickerState>
}

function Dim({ children }: { children: React.ReactNode }) {
  return <span className="text-slate-600">{children}</span>
}

function PctCell({ v, invert = false }: { v: number | null; invert?: boolean }) {
  if (v === null) return <Dim>—</Dim>
  const pct = v * 100
  const pos = invert ? pct <= 0 : pct >= 0
  const sign = pct >= 0 ? '+' : ''
  return <span className={pos ? 'text-green-400' : 'text-red-400'}>{sign}{pct.toFixed(2)}%</span>
}

function ImbalanceCell({ v, dir }: { v: number | null; dir: string | null }) {
  if (v === null) return <Dim>—</Dim>
  const color = dir === 'LONG' ? 'text-green-400' : dir === 'SHORT' ? 'text-red-400' : 'text-slate-400'
  return <span className={color}>{dir ?? '—'} {(v * 100).toFixed(0)}%</span>
}

function FlowCell({ detected, dir }: { detected: boolean; dir: string | null }) {
  if (!detected || !dir) return <Dim>—</Dim>
  const color = dir === 'LONG' ? 'text-green-400' : 'text-red-400'
  return <span className={`${color} text-[10px] font-semibold`}>{dir}</span>
}

function OpeningPlayCell({ play }: { play: TickerState['opening_play'] }) {
  if (!play || !play.direction) return <Dim>—</Dim>
  const color = play.direction === 'LONG' ? 'text-green-400' : 'text-red-400'
  const statusIcon = play.fired ? '✓' : play.queued ? '⏳' : ''
  return (
    <span className={`text-[10px] ${color}`}>
      {statusIcon}{play.direction} <span className="text-slate-500">{play.conviction}</span>
      {play.gex_blocking && <span className="text-yellow-400"> ⚠GEX</span>}
    </span>
  )
}

function ConvCell({ sig }: { sig: TickerState['last_signal'] }) {
  if (!sig) return <Dim>—</Dim>
  const fired = sig.signal_fired
  const score = sig.conviction_score ?? 0
  const color = fired
    ? 'text-green-400 font-bold'
    : score >= 45
    ? 'text-yellow-400'
    : 'text-slate-400'
  return (
    <span className={`text-[10px] ${color}`}>
      {score}{fired ? ' ▶' : ''}
    </span>
  )
}

function GateCell({ sig }: { sig: TickerState['last_signal'] }) {
  if (!sig) return <Dim>—</Dim>
  if (sig.gate_passed) return <span className="text-green-400 text-[9px]">PASS</span>
  if (sig.gated_by) return <span className="text-red-400 text-[9px]">{sig.gated_by.slice(0, 12)}</span>
  return <Dim>—</Dim>
}

const TICKERS_ORDER = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'GOOGL', 'META', 'AMZN', 'TSLA']

export default function SignalGrid({ tickers }: Props) {
  const rows = TICKERS_ORDER.filter((t) => t in tickers).map((t) => ({ ticker: t, state: tickers[t] }))

  return (
    <div className="bg-[#1a1d27] border border-[#2a2d3a] rounded overflow-hidden">
      <div className="px-4 py-2 border-b border-[#2a2d3a]">
        <h2 className="text-[10px] text-slate-500 uppercase tracking-widest">Live Signal Grid</h2>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-[9px] text-slate-500 uppercase tracking-wider border-b border-[#2a2d3a]">
              <Th>Ticker</Th>
              <Th>Price</Th>
              <Th>Call Wall</Th>
              <Th>Put Wall</Th>
              <Th>γ-Flip</Th>
              <Th>PDH/PDL</Th>
              <Th>OR H/L</Th>
              <Th>Open Play</Th>
              <Th>Δ 1m</Th>
              <Th>Δ 5m</Th>
              <Th>Absorb</Th>
              <Th>L2 Imb</Th>
              <Th>Sweep</Th>
              <Th>Print</Th>
              <Th>IVR</Th>
              <Th>RVOL</Th>
              <Th>Conv</Th>
              <Th>Gate</Th>
              <Th>Dir</Th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={19} className="text-center text-slate-600 py-6 text-xs">
                  Waiting for data…
                </td>
              </tr>
            )}
            {rows.map(({ ticker, state: s }) => (
              <tr
                key={ticker}
                className={clsx(
                  'border-b border-[#2a2d3a] hover:bg-[#0f1117] transition-colors',
                  s.last_signal?.signal_fired && 'bg-green-400/5'
                )}
              >
                <Td><span className="font-semibold text-slate-200">{ticker}</span></Td>
                <Td>
                  {s.price > 0 ? (
                    <span className="text-slate-300">${s.price.toFixed(2)}</span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td>{s.gex_call_wall ? <span className="text-red-400">${s.gex_call_wall}</span> : <Dim>—</Dim>}</Td>
                <Td>{s.gex_put_wall ? <span className="text-green-400">${s.gex_put_wall}</span> : <Dim>—</Dim>}</Td>
                <Td>{s.gex_gamma_flip ? <span className="text-yellow-400">${s.gex_gamma_flip}</span> : <Dim>—</Dim>}</Td>
                <Td>
                  {s.prev_day_high && s.prev_day_low ? (
                    <span className="text-slate-400">
                      <span className="text-green-400/70">{s.prev_day_high.toFixed(0)}</span>
                      {'/'}
                      <span className="text-red-400/70">{s.prev_day_low.toFixed(0)}</span>
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td>
                  {s.opening_range_high && s.opening_range_low ? (
                    <span className="text-slate-400">
                      {s.opening_range_high.toFixed(1)}/{s.opening_range_low.toFixed(1)}
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td><OpeningPlayCell play={s.opening_play} /></Td>
                <Td>
                  {s.footprint_delta_1m !== null ? (
                    <span className={s.footprint_delta_1m >= 0 ? 'text-green-400' : 'text-red-400'}>
                      {s.footprint_delta_1m > 0 ? '+' : ''}{Math.round(s.footprint_delta_1m)}
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td>
                  {s.footprint_delta_5m !== null ? (
                    <span className={s.footprint_delta_5m >= 0 ? 'text-green-400' : 'text-red-400'}>
                      {s.footprint_delta_5m > 0 ? '+' : ''}{Math.round(s.footprint_delta_5m)}
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td>
                  {s.footprint_absorption ? (
                    <span className={s.footprint_absorption_side === 'BULLISH' ? 'text-green-400' : 'text-red-400'}>
                      {s.footprint_absorption_side?.slice(0, 4) ?? '—'}
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td><ImbalanceCell v={s.l2_imbalance} dir={s.l2_imbalance_direction} /></Td>
                <Td><FlowCell detected={s.sweep_detected} dir={s.sweep_direction} /></Td>
                <Td><FlowCell detected={s.large_print_detected} dir={s.large_print_direction} /></Td>
                <Td>
                  {s.iv_rank !== null ? (
                    <span className={s.iv_rank > 60 ? 'text-red-400' : 'text-slate-300'}>
                      {s.iv_rank.toFixed(0)}
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td>
                  {s.rvol !== null ? (
                    <span className={s.rvol >= 1.3 ? 'text-green-400' : 'text-slate-500'}>
                      {s.rvol.toFixed(2)}x
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
                <Td><ConvCell sig={s.last_signal} /></Td>
                <Td><GateCell sig={s.last_signal} /></Td>
                <Td>
                  {s.last_signal?.direction ? (
                    <span className={`font-semibold ${s.last_signal.direction === 'LONG' ? 'text-green-400' : 'text-red-400'}`}>
                      {s.last_signal.direction}
                    </span>
                  ) : <Dim>—</Dim>}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-2 py-1.5 text-left whitespace-nowrap font-normal">{children}</th>
}
function Td({ children }: { children: React.ReactNode }) {
  return <td className="px-2 py-1.5 whitespace-nowrap">{children}</td>
}
