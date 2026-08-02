(function attachNaturalTts(global) {
  if (global.NaturalTTS) return;

  const preferredNames = ['Ava', 'Aria', 'Jenny', 'Guy', 'Samantha', 'Google US English'];
  let activeAudio = null;
  let activeAudioUrl = '';

  function voiceScore(voice, lang) {
    const name = String(voice?.name || '');
    const voiceLang = String(voice?.lang || '').toLowerCase();
    const requested = String(lang || 'en-US').toLowerCase();
    let score = 0;
    if (voiceLang === requested) score += 40;
    else if (voiceLang.startsWith(requested.split('-')[0])) score += 20;
    if (/online/i.test(name)) score += 40;
    if (/natural/i.test(name)) score += 60;
    const preferredIndex = preferredNames.findIndex(item => name.includes(item));
    if (preferredIndex >= 0) score += 20 - preferredIndex;
    return score;
  }

  function selectVoice(lang = 'en-US') {
    return global.speechSynthesis.getVoices()
      .filter(voice => String(voice.lang || '').toLowerCase().startsWith(lang.slice(0, 2).toLowerCase()))
      .sort((a, b) => voiceScore(b, lang) - voiceScore(a, lang))[0] || null;
  }

  function speakWithBrowser(text, options = {}) {
    if (!text || !global.speechSynthesis || !global.SpeechSynthesisUtterance) return null;
    const utterance = new global.SpeechSynthesisUtterance(text);
    utterance.lang = options.lang || 'en-US';
    utterance.rate = Math.max(0.6, Math.min(1.3, Number(options.rate) || 0.95));
    utterance.pitch = Math.max(0.8, Math.min(1.2, Number(options.pitch) || 1));
    utterance.voice = selectVoice(utterance.lang);
    if (typeof options.onEnd === 'function') {
      utterance.onend = utterance.onerror = options.onEnd;
    }
    global.speechSynthesis.cancel();
    global.setTimeout(() => global.speechSynthesis.speak(utterance), Number(options.delay) || 60);
    return utterance;
  }

  function stop() {
    global.speechSynthesis?.cancel();
    if (activeAudio) {
      activeAudio.pause();
      activeAudio.currentTime = 0;
      activeAudio = null;
    }
    if (activeAudioUrl) {
      global.URL.revokeObjectURL(activeAudioUrl);
      activeAudioUrl = '';
    }
  }

  async function speakNeural(text, options = {}) {
    stop();
    const response = await global.fetch(`/api/tts/natural?text=${encodeURIComponent(text)}`);
    if (!response.ok) throw new Error(`Natural TTS failed: ${response.status}`);
    const blob = await response.blob();
    const audioUrl = global.URL.createObjectURL(blob);
    const audio = new global.Audio(audioUrl);
    activeAudio = audio;
    activeAudioUrl = audioUrl;
    audio.playbackRate = Math.max(0.85, Math.min(1.15, Number(options.playbackRate) || 1));
    const cleanup = () => {
      if (activeAudio === audio) activeAudio = null;
      if (activeAudioUrl === audioUrl) activeAudioUrl = '';
      global.URL.revokeObjectURL(audioUrl);
      if (typeof options.onEnd === 'function') options.onEnd();
    };
    audio.onended = cleanup;
    audio.onerror = cleanup;
    try {
      await audio.play();
    } catch (error) {
      audio.onended = null;
      audio.onerror = null;
      cleanup();
      throw error;
    }
    return audio;
  }

  function speak(text, options = {}) {
    if (!text) return null;
    if (options.neural) {
      speakNeural(text, options).catch(() => speakWithBrowser(text, options));
      return null;
    }
    return speakWithBrowser(text, options);
  }

  global.NaturalTTS = {
    speak,
    stop,
    selectVoice,
  };
})(window);
