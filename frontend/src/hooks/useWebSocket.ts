import { useEffect, useRef, useState, useCallback } from 'react'
import type { WsPayload } from '../types'

type ConnState = 'connecting' | 'connected' | 'disconnected'

export function useWebSocket() {
  const [payload, setPayload] = useState<WsPayload | null>(null)
  const [connState, setConnState] = useState<ConnState>('connecting')
  const wsRef = useRef<WebSocket | null>(null)
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const retryDelay = useRef(1000)

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/live`)
    wsRef.current = ws

    ws.onopen = () => {
      setConnState('connected')
      retryDelay.current = 1000
    }

    ws.onmessage = (ev) => {
      try {
        setPayload(JSON.parse(ev.data) as WsPayload)
      } catch {
        // ignore malformed frames
      }
    }

    ws.onclose = () => {
      setConnState('disconnected')
      retryTimer.current = setTimeout(() => {
        retryDelay.current = Math.min(retryDelay.current * 1.5, 15000)
        connect()
      }, retryDelay.current)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (retryTimer.current) clearTimeout(retryTimer.current)
      wsRef.current?.close()
    }
  }, [connect])

  return { payload, connState }
}
