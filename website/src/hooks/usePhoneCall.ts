import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Half-duplex hands-free voice conversation ("call mode").
 *
 * The STT/TTS building blocks already exist independently: streaming dictation
 * with a semantic endpointer that auto-submits the turn, and auto-spoken replies
 * with barge-in. What is missing is the closed loop that ties "the reply finished
 * speaking" back to "re-open the microphone", so a user never has to touch a
 * button between turns. This hook is that coordinator — a small state machine
 * over the existing signals; it owns no audio itself.
 *
 * Half-duplex: exactly one side speaks at a time. While the assistant is
 * speaking the mic is closed; the loop re-arms it only after playback drains.
 * A deliberate exception is barge-in — a caller starting to speak mid-reply
 * cuts the playback (see `onUserSpeechDuringPlayback`) and the loop returns to
 * listening early.
 */

export type CallState = 'idle' | 'listening' | 'thinking' | 'speaking'

export interface PhoneCallOptions {
  /** Open the mic (silent = no setup modal on a passive trigger). Resolves when capture is live. */
  startVoice: (opts?: { silent?: boolean }) => Promise<void> | void
  /** Stop the current dictation capture. */
  stopVoice: () => void
  /** True while dictation capture is live. */
  recording: boolean
  /** True while a transcript is being finalized (mic busy, cannot re-arm yet). */
  transcribing: boolean
  /** True while the assistant reply is being spoken. */
  voicePlaying: boolean
  /** True while the assistant turn is running (streaming a reply). */
  assistantBusy: boolean
  /** Whether the voice stack is even usable in this session (STT enabled+available). */
  available: boolean
  /**
   * Seconds of silence in LISTENING before the call auto-hangs-up. 0 disables.
   * A closed loop that opened the mic and heard nothing should not sit open
   * forever draining the device.
   */
  silenceTimeoutSecs?: number
  /** Play a short cue when the mic re-arms, so the caller knows it is their turn. */
  chime?: boolean
}

/**
 * Cut assistant playback immediately (barge-in). Dispatches the `voice-stop`
 * window event, the same seam the WebSocket hook already listens on to stop TTS
 * when a user message lands — reused here so the loop needs no stopTts prop.
 */
function stopTts(): void {
  try { window.dispatchEvent(new CustomEvent('voice-stop')) } catch { /* no-op */ }
}

/**
 * WebAudio blip — no asset, no network. Two short tones: a rising pair when the
 * mic opens ("your turn"), a single low tone on hang-up. Best-effort; a missing
 * AudioContext (or an autoplay block before the first gesture) is swallowed.
 */
function playChime(kind: 'listen' | 'end'): void {
  try {
    const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Ctor) return
    const ctx = new Ctor()
    const now = ctx.currentTime
    const tones = kind === 'listen' ? [660, 880] : [440]
    tones.forEach((freq, i) => {
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.type = 'sine'
      osc.frequency.value = freq
      const t0 = now + i * 0.09
      gain.gain.setValueAtTime(0.0001, t0)
      gain.gain.exponentialRampToValueAtTime(0.12, t0 + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.08)
      osc.connect(gain).connect(ctx.destination)
      osc.start(t0)
      osc.stop(t0 + 0.1)
    })
    // Release the context shortly after the last tone so we don't leak one per re-arm.
    window.setTimeout(() => { void ctx.close().catch(() => {}) }, 400)
  } catch { /* best-effort cue */ }
}

export interface PhoneCall {
  active: boolean
  state: CallState
  start: () => void
  hangUp: () => void
  toggle: () => void
  /** Call from the dictation partial handler so a caller speaking mid-reply cuts playback. */
  onUserSpeechDuringPlayback: () => void
}

// A short settle after playback ends before the mic re-arms — long enough that
// the tail of the assistant's audio device teardown does not get captured as
// the opening of the caller's turn, short enough to feel immediate.
const REARM_DELAY_MS = 350

export function usePhoneCall(opts: PhoneCallOptions): PhoneCall {
  const [active, setActive] = useState(false)
  const [state, setState] = useState<CallState>('idle')

  // Latest option values, read from timers/effects without re-subscribing.
  const optsRef = useRef(opts)
  useEffect(() => { optsRef.current = opts }, [opts])

  const activeRef = useRef(false)
  useEffect(() => { activeRef.current = active }, [active])

  const rearmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevPlayingRef = useRef(false)

  const clearRearm = useCallback(() => {
    if (rearmTimerRef.current) { clearTimeout(rearmTimerRef.current); rearmTimerRef.current = null }
  }, [])
  const clearSilence = useCallback(() => {
    if (silenceTimerRef.current) { clearTimeout(silenceTimerRef.current); silenceTimerRef.current = null }
  }, [])

  const armMic = useCallback(() => {
    if (!activeRef.current) return
    const o = optsRef.current
    if (o.recording || o.transcribing) return
    setState('listening')
    if (o.chime) playChime('listen')
    void Promise.resolve(o.startVoice({ silent: true })).catch(() => {})
    // Silence watchdog: if nothing is spoken within the window, hang up so an
    // unattended open mic does not run indefinitely.
    clearSilence()
    const secs = o.silenceTimeoutSecs ?? 0
    if (secs > 0) {
      silenceTimerRef.current = setTimeout(() => {
        if (activeRef.current && optsRef.current.recording) hangUpRef.current()
      }, secs * 1000)
    }
  }, [clearSilence])

  const hangUp = useCallback(() => {
    clearRearm()
    clearSilence()
    const o = optsRef.current
    setActive(false)
    setState('idle')
    if (o.recording) o.stopVoice()
    stopTts()
    if (o.chime) playChime('end')
  }, [clearRearm, clearSilence])

  // hangUp is referenced from armMic's timer before it is declared; keep a ref.
  const hangUpRef = useRef(hangUp)
  useEffect(() => { hangUpRef.current = hangUp }, [hangUp])

  const start = useCallback(() => {
    const o = optsRef.current
    if (!o.available) return
    setActive(true)
    activeRef.current = true
    armMic()
  }, [armMic])

  const toggle = useCallback(() => {
    if (activeRef.current) hangUp()
    else start()
  }, [hangUp, start])

  const onUserSpeechDuringPlayback = useCallback(() => {
    if (!activeRef.current) return
    // Caller barged in while the assistant was speaking: cut playback and let
    // the loop fall back to listening on the next state settle.
    if (optsRef.current.voicePlaying) stopTts()
  }, [])

  // Drive the state machine off the assistant-busy / playing / recording signals.
  useEffect(() => {
    if (!active) return
    if (opts.assistantBusy) {
      setState('thinking')
      clearSilence()
      return
    }
    if (opts.voicePlaying) {
      setState('speaking')
      clearSilence()
      return
    }
    if (opts.recording || opts.transcribing) {
      // Capture in flight: a caller is (or just finished) speaking.
      if (opts.recording) setState('listening')
      return
    }
    // Idle: nothing playing, nothing thinking, mic closed. This is the moment
    // to re-arm — but only on the falling edge of playback (or a fresh start),
    // and after a short settle so device teardown is not captured as speech.
    const wasPlaying = prevPlayingRef.current
    if (state === 'speaking' || state === 'thinking' || wasPlaying || state === 'idle') {
      clearRearm()
      rearmTimerRef.current = setTimeout(() => { armMic() }, REARM_DELAY_MS)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, opts.assistantBusy, opts.voicePlaying, opts.recording, opts.transcribing])

  // Track the previous playing value for falling-edge detection above.
  useEffect(() => { prevPlayingRef.current = opts.voicePlaying }, [opts.voicePlaying])

  // If the voice stack becomes unavailable mid-call, hang up cleanly.
  useEffect(() => {
    if (active && !opts.available) hangUp()
  }, [active, opts.available, hangUp])

  // Tear down timers on unmount.
  useEffect(() => () => { clearRearm(); clearSilence() }, [clearRearm, clearSilence])

  return { active, state, start, hangUp, toggle, onUserSpeechDuringPlayback }
}
