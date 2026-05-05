import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
} from 'recharts'
import type { PnlPoint } from '../types'

interface Props {
  data: PnlPoint[]
}

function fmtTime(ts: string) {
  return new Date(ts).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function fmtDollar(v: number) {
  const sign = v >= 0 ? '+' : ''
  return `${sign}$${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function PnlCurve({ data }: Props) {
  const latest = data.length > 0 ? data[data.length - 1].cumulative_pnl : null
  const isPos = latest !== null && latest >= 0
  const lineColor = data.length === 0 ? '#6b7280' : isPos ? '#00d084' : '#ff4d4f'

  return (
    <div className="bg-[#1a1d27] border border-[#2a2d3a] rounded p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-[10px] text-slate-500 uppercase tracking-widest">Daily P&L Curve</h2>
        {latest !== null && (
          <span className={`text-sm font-semibold ${isPos ? 'text-green-400' : 'text-red-400'}`}>
            {fmtDollar(latest)}
          </span>
        )}
      </div>

      {data.length < 2 ? (
        <div className="flex items-center justify-center h-32 text-slate-600 text-xs">
          {data.length === 0 ? 'No trades today' : 'Need at least 2 data points…'}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={160}>
          <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <XAxis
              dataKey="ts"
              tickFormatter={fmtTime}
              tick={{ fill: '#6b7280', fontSize: 9 }}
              axisLine={false}
              tickLine={false}
              minTickGap={40}
            />
            <YAxis
              tickFormatter={(v) => `$${v}`}
              tick={{ fill: '#6b7280', fontSize: 9 }}
              axisLine={false}
              tickLine={false}
              width={55}
            />
            <Tooltip
              contentStyle={{
                background: '#1a1d27',
                border: '1px solid #2a2d3a',
                borderRadius: 4,
                fontSize: 11,
                color: '#e2e8f0',
              }}
              formatter={(v: number) => [fmtDollar(v), 'Cumulative P&L']}
              labelFormatter={fmtTime}
            />
            <ReferenceLine y={0} stroke="#2a2d3a" strokeDasharray="3 3" />
            <Line
              type="monotone"
              dataKey="cumulative_pnl"
              stroke={lineColor}
              strokeWidth={1.5}
              dot={false}
              activeDot={{ r: 3, fill: lineColor }}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
