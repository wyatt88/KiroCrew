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
  /**
   * Submit the current turn (same path as pressing Enter): it stops the live
   * capture and sends the composed transcript. Call mode drives this itself on a
   * short post-speech silence rather than waiting for the backend semantic
   * endpointer, whose COMPLETE verdict is unreliable (English-oriented, and
   * invalidated by the on-device recognizer's trailing correction partials).
   */
  submit: () => void
  /**
   * Live streaming transcript. Every change is treated as "the caller is still
   * speaking" and resets the submit-silence timer; once it is non-empty the
   * turn is armed to auto-submit after `submitSilenceMs` of no change.
   */
  partial: string
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
   * Milliseconds of no transcript change, AFTER the caller has said something,
   * that count as "done speaking" and trigger auto-submit. This is the primary
   * turn-end signal in call mode. Default 2000.
   */
  submitSilenceMs?: number
  /**
   * Whether entering the call should force spoken replies. When true, the hook
   * synchronously turns auto-speak ON at call start (before any reply chunk can
   * stream), so the first clause is spoken mid-stream rather than only after the
   * whole reply lands. ChatPage's callOwned effect restores the real setting on
   * hang-up. Off means the call runs with the user's existing auto-speak.
   */
  forceVoiceReply?: boolean
  /**
   * Called on hang-up when forceVoiceReply is set: restores auto-speak to the
   * user's PERSISTED setting. start() turns auto-speak ON synchronously (so the
   * first clause is spoken mid-stream); this is its guaranteed OFF counterpart,
   * living on the hangup path itself rather than in a caller-side effect whose
   * cleanup may not run (owner slot already left screen, forceVoiceReply toggled).
   * Kept as a callback so the hook stays api-agnostic; ChatPage wires it to a
   * fresh api.voiceConfig() read (persisted truth, unpolluted by the runtime force).
   */
  restoreAutoSpeak?: () => void
  /**
   * Seconds of silence with the mic open but NOTHING yet spoken before the call
   * auto-hangs-up. 0 disables. Distinct from submitSilenceMs: this reaps an
   * unattended open mic that never heard speech; that one ends a turn the caller
   * did speak.
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

// Default post-speech silence before auto-submit. The caller has said something;
// this much quiet means "your turn is over". Independent of the backend endpointer.
const DEFAULT_SUBMIT_SILENCE_MS = 2000

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
  const submitTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevPlayingRef = useRef(false)
  // Whether the caller has produced any transcript since the mic last armed.
  // The submit-silence timer only arms once this is true, so opening the mic and
  // saying nothing hangs up (via the silence watchdog) rather than sending empty.
  const spokeThisTurnRef = useRef(false)
  const lastPartialRef = useRef('')
  // One submit per armed turn: the falling edge into recording=false clears it.
  const submittedThisTurnRef = useRef(false)

  const clearRearm = useCallback(() => {
    if (rearmTimerRef.current) { clearTimeout(rearmTimerRef.current); rearmTimerRef.current = null }
  }, [])
  const clearSilence = useCallback(() => {
    if (silenceTimerRef.current) { clearTimeout(silenceTimerRef.current); silenceTimerRef.current = null }
  }, [])
  const clearSubmit = useCallback(() => {
    if (submitTimerRef.current) { clearTimeout(submitTimerRef.current); submitTimerRef.current = null }
  }, [])

  const armMic = useCallback(() => {
    if (!activeRef.current) return
    const o = optsRef.current
    if (o.recording || o.transcribing) return
    setState('listening')
    // Fresh turn: nothing spoken yet, no pending submit.
    spokeThisTurnRef.current = false
    lastPartialRef.current = ''
    submittedThisTurnRef.current = false
    clearSubmit()
    if (o.chime) playChime('listen')
    void Promise.resolve(o.startVoice({ silent: true })).catch(() => {})
    // Silence watchdog: if NOTHING is spoken within the window, hang up so an
    // unattended open mic does not run indefinitely. (Post-speech silence is a
    // different timer — see the partial-driven effect below — that submits.)
    clearSilence()
    const secs = o.silenceTimeoutSecs ?? 0
    if (secs > 0) {
      silenceTimerRef.current = setTimeout(() => {
        if (activeRef.current && optsRef.current.recording && !spokeThisTurnRef.current) hangUpRef.current()
      }, secs * 1000)
    }
  }, [clearSilence, clearSubmit])

  const hangUp = useCallback(() => {
    clearRearm()
    clearSilence()
    clearSubmit()
    const o = optsRef.current
    setActive(false)
    setState('idle')
    if (o.recording) o.stopVoice()
    stopTts()
    // start() forced auto-speak ON synchronously; this is its guaranteed OFF
    // counterpart. Restoring here (on the hangup path that ALWAYS runs) instead
    // of in a caller-side effect cleanup means the user's persisted setting is
    // always restored — no lingering ON that speaks non-call replies.
    if (o.forceVoiceReply) o.restoreAutoSpeak?.()
    if (o.chime) playChime('end')
  }, [clearRearm, clearSilence, clearSubmit])

  // hangUp is referenced from armMic's timer before it is declared; keep a ref.
  const hangUpRef = useRef(hangUp)
  useEffect(() => { hangUpRef.current = hangUp }, [hangUp])

  const start = useCallback(() => {
    const o = optsRef.current
    if (!o.available) return
    setActive(true)
    activeRef.current = true
    // Turn auto-speak ON synchronously, at the exact moment the call begins —
    // NOT via a post-paint effect. A fast reply's first chunk can flush before
    // a React effect runs, and the flush reads autoSpeakRef synchronously; if it
    // is still false the early clauses are skipped and only the end-of-turn tail
    // speaks, which reads as "TTS waits for the whole reply". Dispatching here
    // (the same voice-config-changed seam useWebSocket listens on) guarantees
    // the flag is true before any chunk lands. ChatPage's callOwned effect owns
    // the restore-on-hangup.
    if (o.forceVoiceReply) {
      try { window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { autoSpeak: true } })) } catch { /* no-op */ }
    }
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

  // PRIMARY turn-end signal: post-speech silence auto-submit.
  //
  // On every transcript change while the mic is recording, treat it as "still
  // speaking" — mark that the caller has spoken and (re)start a timer. When the
  // transcript then stays unchanged for `submitSilenceMs`, the turn is over:
  // call submit() (which stops capture and sends, same as pressing Enter). This
  // deliberately does NOT depend on the backend semantic endpointer, which never
  // fires a COMPLETE verdict reliably for on-device zh-CN dictation.
  useEffect(() => {
    if (!activeRef.current) return
    const o = optsRef.current
    if (!o.recording) return
    const p = (o.partial ?? '').trim()
    if (!p) return
    if (p === lastPartialRef.current) return  // no real change (re-render)
    lastPartialRef.current = p
    spokeThisTurnRef.current = true
    clearSilence()  // the caller spoke — the no-speech hang-up watchdog is moot
    clearSubmit()
    const ms = o.submitSilenceMs ?? DEFAULT_SUBMIT_SILENCE_MS
    submitTimerRef.current = setTimeout(() => {
      if (!activeRef.current) return
      const cur = optsRef.current
      if (!cur.recording || submittedThisTurnRef.current) return
      submittedThisTurnRef.current = true
      setState('thinking')
      cur.submit()
    }, ms)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.partial, opts.recording])

  // Clear the per-turn submit guard once capture actually ends, so the next
  // armed turn can submit again.
  useEffect(() => {
    if (!opts.recording) { submittedThisTurnRef.current = false; clearSubmit() }
  }, [opts.recording, clearSubmit])

  // If the voice stack becomes unavailable mid-call, hang up cleanly.
  useEffect(() => {
    if (active && !opts.available) hangUp()
  }, [active, opts.available, hangUp])

  // Tear down timers on unmount.
  useEffect(() => () => { clearRearm(); clearSilence(); clearSubmit() }, [clearRearm, clearSilence, clearSubmit])

  return { active, state, start, hangUp, toggle, onUserSpeechDuringPlayback }
}
