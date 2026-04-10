import { useRef, useState } from "react";

function float32ToInt16(float32Array) {
  const out = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i += 1) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function MicIcon({ active = false }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={`mic-icon ${active ? "mic-icon-active" : ""}`}>
      <path d="M12 15.5a3.5 3.5 0 0 0 3.5-3.5V6.5a3.5 3.5 0 1 0-7 0V12a3.5 3.5 0 0 0 3.5 3.5Z" />
      <path d="M19 11a1 1 0 0 0-2 0 5 5 0 0 1-10 0 1 1 0 0 0-2 0 7.01 7.01 0 0 0 6 6.92V21H8a1 1 0 0 0 0 2h8a1 1 0 0 0 0-2h-3v-3.08A7.01 7.01 0 0 0 19 11Z" />
    </svg>
  );
}

export default function App() {
  const [status, setStatus] = useState("idle");
  const [logs, setLogs] = useState([]);
  const [assistantText, setAssistantText] = useState("");
  const [prompt, setPrompt] = useState(
    "You are Aura-Stream. Give clear and correct English voice replies. Keep answers concise unless I ask for details."
  );
  const [backendHost, setBackendHost] = useState("127.0.0.1:8000");
  const [showDebug, setShowDebug] = useState(false);

  const wsRef = useRef(null);
  const ctxRef = useRef(null);
  const playbackCtxRef = useRef(null);
  const playbackCursorRef = useRef(0);
  const suppressAssistantAudioRef = useRef(false);
  const callIdRef = useRef(0);
  const stopRequestedRef = useRef(false);
  const assistantSpeakingRef = useRef(false);
  const sourceRef = useRef(null);
  const processorRef = useRef(null);
  const gainRef = useRef(null);
  const streamRef = useRef(null);
  const lastInterruptAtRef = useRef(0);
  const interruptStreakRef = useRef(0);
  const lastAssistantAudioAtRef = useRef(0);

  const log = (msg) => {
    const time = new Date().toLocaleTimeString();
    setLogs((prev) => [...prev, `[${time}] ${msg}`]);
  };

  const base64ToArrayBuffer = (base64) => {
    const binaryString = window.atob(base64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i += 1) {
      bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes.buffer;
  };

  const playAssistantAudio = async (base64Audio) => {
    if (!base64Audio) {
      return;
    }
    if (suppressAssistantAudioRef.current) {
      return;
    }
    assistantSpeakingRef.current = true;
    if (!playbackCtxRef.current) {
      playbackCtxRef.current = new AudioContext({ sampleRate: 24000 });
      playbackCursorRef.current = playbackCtxRef.current.currentTime + 0.05;
    }

    if (playbackCtxRef.current.state === "closed") {
      playbackCtxRef.current = new AudioContext({ sampleRate: 24000 });
      playbackCursorRef.current = playbackCtxRef.current.currentTime + 0.05;
    }

    const ctx = playbackCtxRef.current;
    if (ctx.state === "suspended") {
      await ctx.resume();
    }

    const pcmBuffer = base64ToArrayBuffer(base64Audio);
    const int16 = new Int16Array(pcmBuffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i += 1) {
      float32[i] = int16[i] / 0x8000;
    }

    const audioBuffer = ctx.createBuffer(1, float32.length, 24000);
    audioBuffer.copyToChannel(float32, 0);

    const source = ctx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(ctx.destination);

    const startAt = Math.max(playbackCursorRef.current, ctx.currentTime + 0.02);
    source.start(startAt);
    playbackCursorRef.current = startAt + audioBuffer.duration;
  };

  const stopAssistantPlayback = async () => {
    suppressAssistantAudioRef.current = true;
    assistantSpeakingRef.current = false;
    if (playbackCtxRef.current) {
      await playbackCtxRef.current.close().catch(() => null);
      playbackCtxRef.current = null;
      playbackCursorRef.current = 0;
    }
  };

  const resumeAssistantPlayback = () => {
    suppressAssistantAudioRef.current = false;
  };

  const cleanupMedia = async () => {
    if (processorRef.current) {
      processorRef.current.disconnect();
      processorRef.current.onaudioprocess = null;
      processorRef.current = null;
    }
    if (sourceRef.current) {
      sourceRef.current.disconnect();
      sourceRef.current = null;
    }
    if (gainRef.current) {
      gainRef.current.disconnect();
      gainRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (ctxRef.current) {
      await ctxRef.current.close();
      ctxRef.current = null;
    }
    if (playbackCtxRef.current) {
      await playbackCtxRef.current.close().catch(() => null);
      playbackCtxRef.current = null;
      playbackCursorRef.current = 0;
    }
  };

  const startCall = async () => {
    const callId = callIdRef.current + 1;
    callIdRef.current = callId;
    stopRequestedRef.current = false;
    setStatus("connecting");
    try {
      const ws = new WebSocket(`ws://${backendHost}/ws/call/`);
      wsRef.current = ws;

      ws.onopen = async () => {
        if (stopRequestedRef.current || callIdRef.current !== callId) {
          ws.close();
          return;
        }
        setStatus("connected");
        log("socket connected");
        ws.send(
          JSON.stringify({
            event: "start",
            sample_rate: 16000,
            channels: 1,
            model: "gpt-4o-realtime-preview",
            prompt,
          })
        );

        const media = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            sampleRate: 16000,
            echoCancellation: true,
            noiseSuppression: true,
          },
        });

        if (stopRequestedRef.current || callIdRef.current !== callId) {
          media.getTracks().forEach((t) => t.stop());
          ws.close();
          return;
        }

        streamRef.current = media;

        const ctx = new AudioContext({ sampleRate: 16000 });
        ctxRef.current = ctx;

        const src = ctx.createMediaStreamSource(media);
        sourceRef.current = src;

        const proc = ctx.createScriptProcessor(4096, 1, 1);
        processorRef.current = proc;

        const gain = ctx.createGain();
        gain.gain.value = 0;
        gainRef.current = gain;

        proc.onaudioprocess = (event) => {
          if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
            return;
          }
          const input = event.inputBuffer.getChannelData(0);

          // Local interruption gate: stop assistant playback immediately when user starts speaking.
          let energy = 0;
          for (let i = 0; i < input.length; i += 1) {
            energy += input[i] * input[i];
          }
          const rms = Math.sqrt(energy / input.length) * 32768;
          const now = Date.now();
          const isRecentAssistantAudio = now - lastAssistantAudioAtRef.current < 1200;
          if (assistantSpeakingRef.current && isRecentAssistantAudio && rms > 1200) {
            interruptStreakRef.current += 1;
          } else {
            interruptStreakRef.current = 0;
          }

          if (interruptStreakRef.current >= 3 && now - lastInterruptAtRef.current > 1200) {
            lastInterruptAtRef.current = now;
            interruptStreakRef.current = 0;
            stopAssistantPlayback();
            wsRef.current.send(JSON.stringify({ event: "interrupt" }));
          }

          const int16 = float32ToInt16(input);
          wsRef.current.send(int16.buffer);
        };

        src.connect(proc);
        proc.connect(gain);
        gain.connect(ctx.destination);

        setStatus("streaming");
        log("microphone streaming started");
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.event === "assistant_text_delta") {
            setAssistantText((prev) => `${prev}${payload.text}`);
          } else if (payload.event === "quota_exceeded") {
            setAssistantText("OpenAI quota exceeded. Please update billing/plan, then reconnect.");
            setStatus("error");
            stopCall();
          } else if (payload.event === "response_started") {
            setAssistantText("");
            assistantSpeakingRef.current = false;
          } else if (payload.event === "assistant_done") {
            setAssistantText((prev) => (payload.text && payload.text.trim() ? payload.text : prev));
            assistantSpeakingRef.current = false;
            resumeAssistantPlayback();
          } else if (payload.event === "assistant_audio_delta") {
            lastAssistantAudioAtRef.current = Date.now();
            playAssistantAudio(payload.audio);
          } else if (payload.event === "barge_in") {
            stopAssistantPlayback();
          } else if (payload.event === "speech_stopped") {
            resumeAssistantPlayback();
          }

          if (payload.event === "assistant_audio_delta") {
            const chars = payload.audio ? payload.audio.length : 0;
            log(`server: {"event":"assistant_audio_delta","audio":"<base64 ${chars} chars>"}`);
          } else {
            log(`server: ${event.data}`);
          }
        } catch {
          log(`server: ${event.data}`);
        }
      };

      ws.onerror = () => {
        setStatus("error");
        log("socket error");
      };

      ws.onclose = () => {
        if (stopRequestedRef.current) {
          return;
        }
        assistantSpeakingRef.current = false;
        setStatus("idle");
        log("socket closed");
      };
    } catch (err) {
      setStatus("failed");
      log(`start failed: ${err.message}`);
    }
  };

  const stopCall = async () => {
    try {
      stopRequestedRef.current = true;
      callIdRef.current += 1;

      if (wsRef.current && wsRef.current.readyState !== WebSocket.CLOSED) {
        try {
          if (wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ event: "stop" }));
          }
        } finally {
          wsRef.current.close();
        }
        wsRef.current = null;
      }
      await cleanupMedia();
      log("stream stopped");
      setStatus("idle");
    } catch (err) {
      log(`stop error: ${err.message}`);
    }
  };

  const toggleCall = async () => {
    if (status === "streaming" || status === "connecting" || status === "connected") {
      await stopCall();
      return;
    }
    await startCall();
  };

  const isLive = status === "streaming";
  return (
    <div className="voice-page">
      <div className="noise" />
      <main className="voice-shell">
        <section className="orb-wrap">
          <div className={`orb ${isLive ? "orb-live" : ""}`}>
            <div className="orb-inner" />
            <div className="wave wave-a" />
            <div className="wave wave-b" />
          </div>
          <button className={`mic-btn ${isLive ? "mic-live" : ""}`} onClick={toggleCall}>
            <MicIcon active={isLive} />
            <span>{isLive ? "Stop" : "Mic"}</span>
          </button>
          {assistantText && <p className="assistant-text">{assistantText}</p>}
        </section>

        <nav className="bottom-nav">
          <button className="nav-item active">Home</button>
          <button className="nav-item" onClick={() => setShowDebug((v) => !v)}>
            Debug
          </button>
        </nav>

        {showDebug && (
          <section className="debug-panel">
            <label>Backend host</label>
            <input value={backendHost} onChange={(e) => setBackendHost(e.target.value)} />
            <label>Agent prompt</label>
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} />
            <pre className="log">{logs.join("\n")}</pre>
          </section>
        )}
      </main>
    </div>
  );
}
