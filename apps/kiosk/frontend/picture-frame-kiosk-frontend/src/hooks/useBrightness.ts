import { useCallback, useEffect, useRef, useState } from 'react'

export type BrightnessSnapshot = { level_percent: number; transition_seconds: number }

function valid(value: unknown): value is BrightnessSnapshot {
  if (!value || typeof value !== 'object') return false
  const v = value as BrightnessSnapshot
  return Number.isFinite(v.level_percent) && v.level_percent >= 5 && v.level_percent <= 100
    && Number.isFinite(v.transition_seconds) && v.transition_seconds >= 0 && v.transition_seconds <= 30
}

function initialBrightness(): BrightnessSnapshot {
  try {
    const seed: unknown = JSON.parse(document.getElementById('brightness-bootstrap')?.textContent || 'null')
    if (valid(seed)) return { ...seed, transition_seconds: 0 }
  } catch { /* The API/SSE snapshot will repair a missing or invalid bootstrap. */ }
  return { level_percent: 100, transition_seconds: 0 }
}

export function useBrightness(apiBase: string) {
  const [brightness, setBrightness] = useState(initialBrightness)
  const generation = useRef(0)
  const applyBrightness = useCallback((snapshot: unknown) => {
    if (!valid(snapshot)) return
    generation.current += 1
    // Repeated snapshots must not restart a fade already in progress.
    setBrightness((previous) => {
      if (previous.level_percent !== snapshot.level_percent) return snapshot
      // A manual command/initial resync can cancel a running fade at the same target.
      if (snapshot.transition_seconds === 0 && previous.transition_seconds !== 0) return snapshot
      return previous
    })
  }, [])

  useEffect(() => {
    let stopped = false
    let active: AbortController | null = null
    async function sync() {
      if (active) return
      const started = generation.current
      const controller = new AbortController()
      active = controller
      const timeout = window.setTimeout(() => controller.abort(), 5000)
      try {
        const response = await fetch(`${apiBase}/brightness`, { cache: 'no-store', signal: controller.signal })
        if (!response.ok) return
        const snapshot: unknown = await response.json()
        if (!stopped && started === generation.current) applyBrightness(snapshot)
      } catch { /* Retain the last level if the API is temporarily offline. */ }
      finally {
        window.clearTimeout(timeout)
        active = null
      }
    }
    void sync()
    // SSE gives immediate updates; polling also repairs a missed event/stream outage.
    const timer = window.setInterval(() => { void sync() }, 30000)
    return () => {
      stopped = true
      active?.abort()
      window.clearInterval(timer)
    }
  }, [apiBase, applyBrightness])

  return { brightness, applyBrightness }
}
