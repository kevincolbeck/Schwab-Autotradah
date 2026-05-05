import { useState } from 'react'
import { useWebSocket } from './hooks/useWebSocket'
import Dashboard from './pages/Dashboard'
import Trades from './pages/Trades'
import Stats from './pages/Stats'

type Tab = 'dashboard' | 'trades' | 'stats'

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard')
  const { payload, connState } = useWebSocket()

  const connColor =
    connState === 'connected'
      ? 'text-green-400'
      : connState === 'connecting'
      ? 'text-yellow-400'
      : 'text-red-400'

  const connLabel =
    connState === 'connected'
      ? '● LIVE'
      : connState === 'connecting'
      ? '◌ CONNECTING'
      : '○ DISCONNECTED'

  return (
    <div className="min-h-screen bg-[#0f1117] text-slate-200 font-mono flex flex-col">
      <header className="flex items-center justify-between px-4 py-2 bg-[#1a1d27] border-b border-[#2a2d3a]">
        <div className="flex items-center gap-6">
          <span className="text-sm font-semibold tracking-widest text-slate-300">EQUITIES BOT</span>
          <nav className="flex gap-1">
            {(['dashboard', 'trades', 'stats'] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-3 py-1 text-xs rounded uppercase tracking-wider transition-colors ${
                  tab === t
                    ? 'bg-[#40a9ff] text-[#0f1117] font-semibold'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {t}
              </button>
            ))}
          </nav>
        </div>
        <span className={`text-xs font-semibold ${connColor}`}>{connLabel}</span>
      </header>

      <main className="flex-1 overflow-auto">
        {tab === 'dashboard' && <Dashboard payload={payload} />}
        {tab === 'trades' && <Trades />}
        {tab === 'stats' && <Stats />}
      </main>
    </div>
  )
}
