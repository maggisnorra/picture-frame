import { useState, useEffect, useRef } from 'react'
import { FrameLayout, MediaSurface } from './components/FrameLayout'
import { VolumeToast, CallIncoming, CallOutgoing } from './components/Overlays'
import './App.css'

// each state
export type Mode = "picture" | "call" | "ended" | "instructions"
export type CallMode = "caller" | "callee" | null

export const MAX_VOLUME = 15;

export default function App() {
  const [mode, setMode] = useState<Mode>("picture")
  const [callMode, setCallMode] = useState<CallMode>(null)

  const [photoUrl, setPhotoUrl] = useState<string>("/vite.svg")
  const [volume, setVolume] = useState<number>(7)
  const [showVol, setShowVol] = useState(false)

  // --- keyboard shortcuts for testing
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowUp") flashVolume(Math.min(MAX_VOLUME, volume + 1))
      if (e.key === "ArrowDown") flashVolume(Math.max(0, volume - 1))
      if (e.key === "p") { setMode("picture"); setPhotoUrl("/vite.svg") }
      if (e.key === "c") { setMode("call") }
      if (e.key === "e") { setMode("ended") }
      if (e.key === "s") { setCallMode("caller") } // start
      if (e.key === "g") { setCallMode("callee") } // get
      if (e.key === "i") { setMode("instructions") }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [volume])

  const volTimer = useRef<number | null>(null);

  function flashVolume(v: number) {
    setVolume(v)
    setShowVol(true)
    if (volTimer.current) window.clearTimeout(volTimer.current);
    volTimer.current = window.setTimeout(() => setShowVol(false), 1200);
  }

  // --- Example SSE hook (point to your FastAPI endpoint)
  // useSSE("/events", (msg: PushMsg) => {
  //   if (msg.type === "showPhoto") { setPhotoUrl(msg.url); setMode("photo") }
  //   if (msg.type === "setVolume") flashVolume(msg.value)
  //   if (msg.type === "incomingCall") { setIncomingFrom(msg.from); setMode("call") }
  //   if (msg.type === "endCall") { setIncomingFrom(null); setInCall(false); setMode("photo") }
  // })

  return (
    <FrameLayout media={<MediaSurface mode={mode} photoUrl={photoUrl} />}>
      {showVol && <VolumeToast value={volume} />}
      {callMode === "callee" && (
        <CallIncoming />
      )}
      {callMode === "caller" && (
        <CallOutgoing />
      )}
    </FrameLayout>
  )
}
