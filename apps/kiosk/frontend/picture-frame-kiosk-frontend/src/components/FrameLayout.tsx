import { useEffect, useRef } from "react";
import type { Mode } from "../App";

export function FrameLayout({ media, children }: { media: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="kiosk-root">
      <div className="media-layer">{media}</div>
      <div className="overlay-root">{children}</div>
    </div>
  )
}

export function MediaSurface({ mode, photoUrl }: { mode: Mode; photoUrl?: string }) {
  const remoteRef = useRef<HTMLVideoElement>(null)
  const localRef  = useRef<HTMLVideoElement>(null)

  // Skeleton for wiring WebRTC later
  useEffect(() => {
    if (mode !== "call") return
    // TODO: hook up RTCPeerConnection and attach streams:
    // remoteRef.current!.srcObject = remoteStream
    // localRef.current!.srcObject  = localStream
  }, [mode])

  return (
    <>
      {mode === "picture" && (
        <img src={photoUrl} alt="" style={{ position: "absolute", inset: 0, width:"100%", height:"100%", objectFit:"cover" }} />
      )}

      {mode === "call" && (
        <>
          <video ref={remoteRef} autoPlay playsInline
            style={{ position:"absolute", inset:0, width:"100%", height:"100%", objectFit:"cover", background:"#000" }} />
          {/* PiP for local preview */}
          <video ref={localRef} autoPlay muted playsInline
            style={{ position:"absolute", right:20, bottom:20, width:200, height:120, objectFit:"cover", borderRadius:12, boxShadow:"0 8px 24px rgba(0,0,0,.5)" }} />
        </>
      )}
      {(mode === "ended" || mode === "instructions") && ( // TODO: something something
        <div style={{position:"absolute", inset:0, background:"#000"}}/>
      )}
    </>
  )
}
