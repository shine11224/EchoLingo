(function attachNaturalTts(global) {
  if (global.NaturalTTS) return;

  const preferredNames = ['Ava', 'Aria', 'Jenny', 'Guy', 'Samantha', 'Google US English'];
  let activeAudio = null;
  let activeAudioUrl = '';
  let sharedAudio = null;
  let sharedAudioUnlocked = false;

  const TTS_VERSION = '20260805-3';

  // On-screen diagnostics for mobile browsers we cannot attach a debugger to.
  // Opt-in only: localStorage.ttsDebug = '1' (or open /static/tts-diag.html).
  function ttsToast(msg) {
    try {
      if (global.localStorage?.getItem('ttsDebug') !== '1') return;
      const doc = global.document;
      if (!doc || !doc.body) return;
      let el = doc.getElementById('__tts_toast');
      if (!el) {
        el = doc.createElement('div');
        el.id = '__tts_toast';
        el.style.cssText = 'position:fixed;left:8px;right:8px;bottom:8px;z-index:99999;'
          + 'background:rgba(0,0,0,.85);color:#0f0;font:12px/1.5 monospace;'
          + 'padding:8px 10px;border-radius:8px;white-space:pre-wrap;';
        doc.body.appendChild(el);
      }
      el.textContent = `[TTS ${TTS_VERSION}] ${msg}`;
      clearTimeout(el.__ttsTimer);
      el.__ttsTimer = global.setTimeout(() => { el.textContent = ''; }, 10000);
    } catch (_) { /* diagnostics must never break playback */ }
  }

  // 1-second silent WAV: keeps the shared <audio> element "unlocked" after a user
  // gesture, so async fetches (server TTS ~seconds later) may still call play().
  const SILENT_WAV =
    'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA=';

  function getSharedAudio() {
    if (!sharedAudio) {
      sharedAudio = new global.Audio();
      sharedAudio.preload = 'auto';
    }
    return sharedAudio;
  }

  // Must be called synchronously inside a user-gesture handler (click/tap).
  function unlockSharedAudio() {
    if (sharedAudioUnlocked) return;
    const audio = getSharedAudio();
    try {
      audio.muted = true;
      audio.src = SILENT_WAV;
      const attempt = audio.play();
      if (attempt && typeof attempt.then === 'function') {
        attempt
          .then(() => { sharedAudioUnlocked = true; })
          .catch(() => {});
      } else {
        sharedAudioUnlocked = true;
      }
    } catch (_) { /* gesture unlock best-effort */ }
  }

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
    speakSession++;
    global.speechSynthesis?.cancel();
    if (activeAudio) {
      activeAudio.pause();
      activeAudio = null;
    }
    if (activeAudioUrl) {
      global.URL.revokeObjectURL(activeAudioUrl);
      activeAudioUrl = '';
    }
  }

  async function fetchTtsBlob(text) {
    // Task 8：用户主动点击 → 先 POST prepare（缓存优先、只对新合成计费），
    // 再 GET 播放缓存；单用户/公开库 prepare 同样可用（后端计费 no-op）。
    const credits = global.eltCredits;
    if (credits && typeof credits.billableFetch === 'function') {
      const prepResp = await credits.billableFetch('sentence_tts', '/api/tts/natural/prepare', {
        method: 'POST',
        body: JSON.stringify({ text }),
      });
      if (!prepResp.ok) throw new Error(`Natural TTS prepare failed: ${prepResp.status}`);
      const prep = await prepResp.json();
      const audioUrl = (prep && prep.audio_url) || `/api/tts/natural?text=${encodeURIComponent(text)}`;
      const cached = await global.fetch(audioUrl);
      if (!cached.ok) throw new Error(`Natural TTS playback failed: ${cached.status}`);
      return cached.blob();
    }
    // 未加载 elt_credits.js 的页面（旧缓存 HTML）：保持旧 GET 直通行为
    const response = await global.fetch(`/api/tts/natural?text=${encodeURIComponent(text)}`);
    if (!response.ok) throw new Error(`Natural TTS failed: ${response.status}`);
    return response.blob();
  }

  const TTS_CHUNK_LIMIT = 450; // backend /api/tts/natural rejects text > 500 chars

  function splitTtsChunks(text) {
    const normalized = String(text || '').trim();
    if (normalized.length <= TTS_CHUNK_LIMIT) return [normalized];
    const pieces = normalized.match(/[^.!?;:\n]+[.!?;:\n]+["'”’)\]]*\s*|[^.!?;:\n]+$/g) || [normalized];
    const chunks = [];
    let current = '';
    for (const piece of pieces) {
      let rest = piece;
      while (rest.length > TTS_CHUNK_LIMIT) {
        if (current.trim()) { chunks.push(current.trim()); current = ''; }
        chunks.push(rest.slice(0, TTS_CHUNK_LIMIT).trim());
        rest = rest.slice(TTS_CHUNK_LIMIT);
      }
      if (current && (current + rest).length > TTS_CHUNK_LIMIT) {
        chunks.push(current.trim());
        current = rest;
      } else {
        current += rest;
      }
    }
    if (current.trim()) chunks.push(current.trim());
    return chunks.filter(Boolean);
  }

  let speakSession = 0;

  function playTtsBlob(blob, options, session) {
    return new Promise((resolve, reject) => {
      if (session !== speakSession) { resolve(); return; }
      const audioUrl = global.URL.createObjectURL(blob);
      const audio = getSharedAudio();
      activeAudio = audio;
      activeAudioUrl = audioUrl;
      audio.muted = false;
      audio.src = audioUrl;
      audio.playbackRate = Math.max(0.85, Math.min(1.15, Number(options.playbackRate || options.rate) || 1));
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        audio.onended = null;
        audio.onerror = null;
        if (activeAudio === audio) activeAudio = null;
        if (activeAudioUrl === audioUrl) activeAudioUrl = '';
        global.URL.revokeObjectURL(audioUrl);
        resolve();
      };
      audio.onended = finish;
      audio.onerror = finish;
      const attempt = audio.play();
      if (attempt && typeof attempt.then === 'function') {
        attempt
          .then(() => ttsToast(`播放中 (rate=${audio.playbackRate})`))
          .catch((error) => {
            ttsToast(`play() 被拒: ${error.name} ${error.message}`);
            if (!settled) {
              settled = true;
              audio.onended = null;
              audio.onerror = null;
              global.URL.revokeObjectURL(audioUrl);
              reject(error);
            }
          });
      }
    });
  }

  async function speakNeural(text, options = {}) {
    stop();
    const session = ++speakSession;
    const chunks = splitTtsChunks(text);
    ttsToast(`请求服务端 TTS: "${String(text).slice(0, 24)}"${chunks.length > 1 ? `（${chunks.length} 段）` : ''}`);
    const blobs = await Promise.all(chunks.map(fetchTtsBlob));
    if (session !== speakSession) return null;
    ttsToast(`TTS 合成完成（${chunks.length} 段，共 ${Math.round(blobs.reduce((sum, b) => sum + b.size, 0) / 1024)}KB）`);
    for (const blob of blobs) {
      if (session !== speakSession) return null;
      await playTtsBlob(blob, options, session);
    }
    if (session === speakSession && typeof options.onEnd === 'function') options.onEnd();
    return activeAudio;
  }

  function speak(text, options = {}) {
    if (!text) return null;
    if (options.browser) {
      return speakWithBrowser(text, options);
    }
    // Neural-first for every caller: on many Android browsers (MIUI, Chrome
    // without voice packs) speechSynthesis exists but has zero voices and
    // fails silently, so browser TTS is only a fallback.
    ttsToast(`speak(): "${String(text).slice(0, 24)}"`);
    unlockSharedAudio();
    speakNeural(text, options).catch((error) => {
      const voices = global.speechSynthesis ? global.speechSynthesis.getVoices().length : -1;
      ttsToast(`服务端 TTS 失败(${error.message})，回退浏览器 TTS，voices=${voices}`);
      speakWithBrowser(text, options);
    });
    return null;
  }

  global.NaturalTTS = {
    speak,
    stop,
    selectVoice,
  };
})(window);
