import { createPortal } from 'react-dom'
import type { BrightnessSnapshot } from '../hooks/useBrightness'

export function ScreenDimmer({ brightness }: { brightness: BrightnessSnapshot }) {
  // Mount above body-level reactions as well as photo/video and call controls.
  return createPortal(
    <div className="screen-dimmer" aria-hidden="true" style={{
      opacity: 1 - brightness.level_percent / 100,
      transitionProperty: brightness.transition_seconds === 0 ? 'none' : 'opacity',
      transitionDuration: `${brightness.transition_seconds}s`,
    }} />,
    document.body,
  )
}
