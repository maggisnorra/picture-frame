import { useCallback, useEffect, useRef, useState } from 'react'

export const MESSAGE_DURATION_MS = 30_000

type FrameMessage = { id: number; text: string }

export function useMessages() {
  const [currentMessage, setCurrentMessage] = useState<FrameMessage | null>(null)
  const nextId = useRef(0)

  const showMessage = useCallback((text: string) => {
    const message = { id: nextId.current++, text: String(text ?? '').trim() || '❤️' }
    setCurrentMessage(message)
  }, [])

  useEffect(() => {
    if (!currentMessage) return
    const timer = window.setTimeout(() => {
      setCurrentMessage(null)
    }, MESSAGE_DURATION_MS)
    return () => window.clearTimeout(timer)
  }, [currentMessage])

  return { currentMessage, showMessage }
}
