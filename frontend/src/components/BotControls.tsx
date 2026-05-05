import { useState } from 'react'

interface ControlDef {
  key: string
  label: string
  definition: string
  endpoint: string
  variant: 'green' | 'yellow' | 'orange' | 'red'
  confirm?: string
}

const CONTROLS: ControlDef[] = [
  {
    key: 'start',
    label: 'START',
    definition:
      'Enables the bot engine. Pre-market prep runs at 9:00 AM ET, pre-open watch at 9:25 AM, trading begins at 9:30 AM. Safe to click at any time.',
    endpoint: '/api/bot/start',
    variant: 'green',
  },
  {
    key: 'stop',
    label: 'STOP',
    definition:
      'Pauses new signal evaluation and entries. Open positions continue to be monitored for stop-loss, take-profit, and EOD close. Does not close anything.',
    endpoint: '/api/bot/stop',
    variant: 'yellow',
  },
  {
    key: 'emergency',
    label: 'EMERGENCY CLOSE ALL',
    definition:
      'Immediately closes every open position at current market price (paper). Use when you want out of everything right now. Bot continues running after.',
    endpoint: '/api/bot/emergency-close',
    variant: 'orange',
    confirm: 'Close ALL open positions immediately?',
  },
  {
    key: 'kill',
    label: 'KILL',
    definition:
      'Fully shuts down the bot engine. No evaluation, no position monitoring. Positions are NOT closed — run Emergency Close first if needed.',
    endpoint: '/api/bot/kill',
    variant: 'red',
    confirm: 'Kill the bot? Position monitoring will stop.',
  },
]

const variantCls: Record<ControlDef['variant'], string> = {
  green: 'border-green-400 text-green-400 hover:bg-green-400 hover:text-[#0f1117]',
  yellow: 'border-yellow-400 text-yellow-400 hover:bg-yellow-400 hover:text-[#0f1117]',
  orange: 'border-orange-400 text-orange-400 hover:bg-orange-400 hover:text-[#0f1117]',
  red: 'border-red-400 text-red-400 hover:bg-red-400 hover:text-[#0f1117]',
}

export default function BotControls() {
  const [loading, setLoading] = useState<string | null>(null)
  const [lastMsg, setLastMsg] = useState<{ key: string; text: string } | null>(null)

  async function handle(ctrl: ControlDef) {
    if (ctrl.confirm && !window.confirm(ctrl.confirm)) return
    setLoading(ctrl.key)
    setLastMsg(null)
    try {
      const res = await fetch(ctrl.endpoint, { method: 'POST' })
      const data = await res.json()
      setLastMsg({ key: ctrl.key, text: data.definition ?? data.message ?? 'OK' })
    } catch {
      setLastMsg({ key: ctrl.key, text: 'Request failed' })
    } finally {
      setLoading(null)
    }
  }

  return (
    <div className="bg-[#1a1d27] border border-[#2a2d3a] rounded p-4">
      <h2 className="text-[10px] text-slate-500 uppercase tracking-widest mb-3">Bot Controls</h2>
      <div className="flex flex-col gap-3">
        {CONTROLS.map((ctrl) => (
          <div key={ctrl.key} className="flex items-start gap-3">
            <button
              onClick={() => handle(ctrl)}
              disabled={loading === ctrl.key}
              className={`flex-none px-3 py-1.5 text-[10px] font-semibold border rounded transition-colors min-w-[160px] text-center disabled:opacity-40 ${variantCls[ctrl.variant]}`}
            >
              {loading === ctrl.key ? '…' : ctrl.label}
            </button>
            <div className="flex flex-col gap-0.5 min-w-0">
              <span className="text-[10px] text-slate-400 leading-snug">{ctrl.definition}</span>
              {lastMsg?.key === ctrl.key && (
                <span className="text-[9px] text-slate-600 italic truncate">{lastMsg.text}</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
