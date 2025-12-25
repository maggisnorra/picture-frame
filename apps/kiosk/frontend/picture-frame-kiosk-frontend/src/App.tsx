import { useState, useEffect, useRef } from 'react'
import { FrameLayout, MediaSurface } from './components/FrameLayout'
import { VolumeToast, CallIncoming, CallOutgoing } from './components/Overlays'
import { useKioskEvents } from "./hooks/useKioskEvents";
import './App.css'
import type { CallPayload,/*, CallState*/} from './types/push';

// each state
export type Mode = "picture" | "call" | "ended" | "instructions"
export type CallMode = "caller" | "callee" | null

export const MAX_VOLUME = 100;

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api"; // "" for same-origin kiosk

export default function App() {
  const [mode, setMode] = useState<Mode>("picture")
  const [callMode, setCallMode] = useState<CallMode>(null)

  const [photoUrl, setPhotoUrl] = useState<string>("/vite.svg")
  const [volume, setVolume] = useState<number>(40)
  const [showVol, setShowVol] = useState(false)

  //const [callId, setCallId] = useState<string | null>(null);
  //const [callState, setCallState] = useState<CallState>("idle");

  // --- keyboard shortcuts for testing
  /*useEffect(() => {
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
  }, [volume])*/

  let reactionHideTimer: number | null | undefined = null;

  function ensureReactionOverlay() {
    let el = document.getElementById("reactionOverlay");
    if (el) return el;

    el = document.createElement("div");
    el.id = "reactionOverlay";
    el.innerHTML = `<div class="bubble" id="reactionOverlayText"></div>`;
    document.body.appendChild(el);
    return el;
  }

  function showReaction(message: string, ms = 1600) {
    const overlay = ensureReactionOverlay();
    const textEl = document.getElementById("reactionOverlayText");
    if (textEl) textEl.textContent = String(message ?? "").trim() || "❤️";

    overlay.classList.remove("show");
    void overlay.offsetHeight;
    overlay.classList.add("show");

    if (reactionHideTimer) clearTimeout(reactionHideTimer);
    reactionHideTimer = setTimeout(() => {
      overlay.classList.remove("show");
    }, ms);
  }


  const volTimer = useRef<number | null>(null);
  function flashVolume(v: number) {
    setVolume(v)
    // TODO: maybe add mute?
    setShowVol(true)
    if (volTimer.current) window.clearTimeout(volTimer.current);
    volTimer.current = window.setTimeout(() => setShowVol(false), 1200);
  }

  type PictureMeta = {
    filename: string;
    content_type: string;
    updated_at: number;
    url: string;
  }

  useEffect(() => {
    fetch(`${API_BASE}/picture/meta`, { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as PictureMeta;
      })
      .then((v) => {
        setPhotoUrl(`${v.url}?t=${v.updated_at}`);
      })
      .catch(() => {
        setPhotoUrl("/vite.svg");
      });

    fetch(`${API_BASE}/volume`)
      .then((r) => r.json() as Promise<{ volume_percent: number; muted: boolean }>)
      .then((v) => {
        flashVolume(v.volume_percent);
        setShowVol(false);
      })
      .catch(() => {});

    fetch(`${API_BASE}/call/state`)
      .then((r) => r.json() as Promise<CallPayload>)
      .then((s) => applyCallSnapshot(s))
      .catch(() => {});
  }, []);


  function applyCallSnapshot(p: CallPayload) {
    const st = p.state;
    //setCallState(st);
    //setCallId(p.call?.call_id ?? null);

    if (st === "idle" || st === "ended") {
      setMode("picture");
      setCallMode(null);
      return;
    }
    setMode("call");
    if (st === "incoming_ringing") setCallMode("callee");
    else if (st === "outgoing_ringing") setCallMode("caller");
    else setCallMode(null); // connecting / in_call → hide ringing overlays
  }

  useKioskEvents({
    apiBase: API_BASE,
    onMessage: (msg) => {
      if (msg.event === "volume") {
        flashVolume(msg.data.volume_percent);
      }

      if (msg.event === "picture") {
        // cache-bust so the <img> reloads even if URL is the same
        setPhotoUrl(`${msg.data.url}?t=${Date.now()}`);
        setMode((prev) => (prev === "call" ? prev : "picture"));
      }

      if (msg.event === "call") {
        // msg.data is the payload you send in push_call()
        applyCallSnapshot(msg.data);
      }

      if (msg.event === "reaction") {
        showReaction(msg.data.message);
      }
    },
  });

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
