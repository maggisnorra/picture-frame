export type VolumeMsg = {
  event: "volume";
  data: { volume_percent: number; muted: boolean };
}

export type PictureMsg = {
  event: "picture";
  data: { url: string };
}

export type CallState =
  | "idle"
  | "outgoing_ringing"
  | "incoming_ringing"
  | "connecting"
  | "in_call"
  | "ended"

export type CallSession = {
  call_id: string;
  state: CallState;
  created_at: number;
}

export type CallPayload =
  | { state: "idle"; call: null; reason?: string; ended_call_id?: string }
  | { state: Exclude<CallState, "idle">; call: CallSession; reason?: string; ended_call_id?: string }

export type CallMsg = {
  event: "call";
  data: CallPayload;
}

export type ReactionMsg = {
  event: "reaction";
  data: { message: string };
}

export type PushMsg = VolumeMsg | PictureMsg | CallMsg | ReactionMsg
