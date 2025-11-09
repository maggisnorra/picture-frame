import { MAX_VOLUME } from "../App"

export function VolumeToast({ value }: { value: number }) {
  return (
    <div style={{ position:"absolute", top:20, right:20 }} className="overlay-card">
      <div style={{ fontSize:14, opacity:.8 }}>Volume</div>
      <div style={{ fontSize:24, fontWeight:700 }}>{value}</div>
      <div style={{ width:180, height:6, background:"#333", borderRadius:999, marginTop:8 }}>
        <div style={{ width:`${(value / MAX_VOLUME) * 100}%`, height:"100%", background:"#fff", borderRadius:999 }} />
      </div>
    </div>
  )
}

export function CallIncoming() {
  return (
    <div style={{ position:"absolute", left:"50%", top:"50%", transform:"translate(-50%,-50%)" }} className="overlay-card">
      <div style={{ fontSize:18, marginBottom:8 }}>Incoming call...</div>
      <div style={{ display:"flex", justifyContent:"center" }}>
        <button className="btn" style={{ background:"#35c759", color:"#fff" }}>Accept</button>
        <button className="btn" style={{ background:"#ff3b30", color:"#fff" }}>Decline</button>
      </div>
    </div>
  )
}

export function CallOutgoing() {
  return (
    <div style={{ position:"absolute", left:"50%", top:"50%", transform:"translate(-50%,-50%)" }} className="overlay-card">
      <div style={{ fontSize:18, marginBottom:8 }}>Calling...</div>
      <div style={{ display:"flex", justifyContent:"center" }}>
        <button className="btn" style={{ background:"#ff3b30", color:"#fff" }}>End</button>
      </div>
    </div>
  )
}
