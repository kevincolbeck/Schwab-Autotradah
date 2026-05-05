import type { WsPayload } from '../types'
import StatusBar from '../components/StatusBar'
import BotControls from '../components/BotControls'
import SignalGrid from '../components/SignalGrid'
import OpenPositions from '../components/OpenPositions'
import PnlCurve from '../components/PnlCurve'

interface Props {
  payload: WsPayload | null
}

export default function Dashboard({ payload }: Props) {
  return (
    <div className="flex flex-col">
      <StatusBar payload={payload} />

      <div className="p-4 flex flex-col gap-4">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-1">
            <BotControls />
          </div>
          <div className="lg:col-span-2">
            <PnlCurve data={payload?.pnl_curve ?? []} />
          </div>
        </div>

        <OpenPositions positions={payload?.open_positions ?? []} />
        <SignalGrid tickers={payload?.tickers ?? {}} />
      </div>
    </div>
  )
}
