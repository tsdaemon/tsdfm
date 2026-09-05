// Read-only analysis of the existing player. No second stream request, microphone,
// or connection to AudioContext.destination: the media element keeps playing normally.
window.createAudioSpectrum = function createAudioSpectrum(audio, onSample) {
  const bands = 22;
  const silence = () => ({levels: Array(bands).fill(0), energy: 0});
  const capture = audio.captureStream || audio.mozCaptureStream;
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!capture || !Context) return {setEnabled() {}};

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let enabled = false, context, analyser, source, stream;
  let frequencies, waveform, frame = 0, lastSample = 0;

  function stopFrames() {
    cancelAnimationFrame(frame);
    frame = 0;
    onSample(silence());
  }

  function detach() {
    source?.disconnect();
    source = null;
    if (stream) {
      stream.removeEventListener('addtrack', attach);
      stream.removeEventListener('removetrack', attach);
      stream.getTracks().forEach(track => track.stop());
    }
    stream = null;
  }

  function attach() {
    source?.disconnect();
    source = null;
    if (stream?.getAudioTracks().some(track => track.readyState === 'live')) {
      source = context.createMediaStreamSource(stream);
      source.connect(analyser);
    }
  }

  function initialize() {
    try {
      if (!context) {
        context = new Context();
        analyser = context.createAnalyser();
        analyser.fftSize = 4096;
        analyser.smoothingTimeConstant = 0.65;
        analyser.minDecibels = -85;
        analyser.maxDecibels = -20;
        frequencies = new Uint8Array(analyser.frequencyBinCount);
        waveform = new Float32Array(analyser.fftSize);
      }
      if (!stream) {
        stream = capture.call(audio);
        stream.addEventListener('addtrack', attach);
        stream.addEventListener('removetrack', attach);
        attach();
      }
      if (context.state === 'suspended') context.resume().catch(() => {});
    } catch {
      // Cross-origin capture may be denied. Leave the player and bars alone.
      detach();
    }
  }

  function canSample() {
    return enabled && !document.hidden && !reducedMotion.matches &&
      !audio.paused && !audio.ended && !audio.muted && audio.volume > 0 && audio.readyState >= 2;
  }

  function sample(time) {
    frame = 0;
    if (!canSample()) { stopFrames(); return; }
    if (time - lastSample >= 1000 / 30) {
      lastSample = time;
      if (source && context.state === 'running') {
        analyser.getByteFrequencyData(frequencies);
        analyser.getFloatTimeDomainData(waveform);
        const hzPerBin = context.sampleRate / analyser.fftSize;
        const upperHz = Math.min(16000, context.sampleRate / 2);
        const levels = Array.from({length: bands}, (_, index) => {
          const start = Math.max(1, Math.floor(40 * (upperHz / 40) ** (index / bands) / hzPerBin));
          const end = Math.min(frequencies.length, Math.max(start + 1,
            Math.ceil(40 * (upperHz / 40) ** ((index + 1) / bands) / hzPerBin)));
          // Peak per band keeps narrow high-frequency notes visible even though
          // logarithmic treble bands span more FFT bins than the bass bands.
          let peak = 0;
          for (let bin = start; bin < end; bin++) peak = Math.max(peak, frequencies[bin]);
          return peak / 255;
        });
        const rms = Math.sqrt(waveform.reduce((sum, value) => sum + value * value, 0) / waveform.length);
        // Do not leave smoothed FFT history glowing after measured silence.
        if (rms < 0.0001) levels.fill(0);
        onSample({levels, energy: Math.min(1, rms * 3)});
      } else {
        onSample(silence());
      }
    }
    frame = requestAnimationFrame(sample);
  }

  function update() {
    if (!canSample()) { stopFrames(); return; }
    initialize();
    if (!frame && stream) frame = requestAnimationFrame(sample);
  }

  for (const event of ['playing', 'pause', 'ended', 'volumechange', 'waiting']) {
    audio.addEventListener(event, update);
  }
  audio.addEventListener('emptied', () => { detach(); stopFrames(); });
  document.addEventListener('visibilitychange', update);
  reducedMotion.addEventListener('change', update);
  // Resumes only the silent analyser context, never unmutes or plays the radio.
  document.addEventListener('pointerdown', update);
  document.addEventListener('keydown', update);
  return {
    setEnabled(value) {
      if (enabled === value) return;
      enabled = value;
      update();
    },
  };
};
