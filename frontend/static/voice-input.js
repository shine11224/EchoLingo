(function attachVoiceInput(global) {
  'use strict';

  function createWebSpeechAdapter({onInterim, onFinal, onStateChange, onError}) {
    const Recognition = global.SpeechRecognition || global.webkitSpeechRecognition;
    let recognition = null;

    function isSupported() {
      return Boolean(Recognition);
    }

    function stop() {
      if (recognition) recognition.stop();
    }

    function destroy() {
      if (!recognition) return;
      recognition.onstart = null;
      recognition.onresult = null;
      recognition.onerror = null;
      recognition.onend = null;
      recognition.abort();
      recognition = null;
    }

    function start(language) {
      if (!isSupported()) {
        onError({code: 'unsupported', message: '当前浏览器不支持语音输入。'});
        return false;
      }
      if (recognition) stop();

      recognition = new Recognition();
      recognition.lang = language;
      recognition.continuous = true;
      recognition.interimResults = true;

      recognition.onstart = () => onStateChange('listening');
      recognition.onresult = event => {
        let interim = '';
        let final = '';
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          const transcript = event.results[index][0].transcript;
          if (event.results[index].isFinal) final += transcript;
          else interim += transcript;
        }
        onInterim(interim.trim());
        if (final.trim()) onFinal(final.trim());
      };
      recognition.onerror = event => {
        if (event.error === 'aborted') return;
        const messages = {
          'not-allowed': '麦克风权限未开启。',
          'service-not-allowed': '浏览器未允许语音识别服务。',
          'audio-capture': '没有检测到可用麦克风。',
          'no-speech': '没有听到语音，请重试。',
          network: '语音识别网络不可用。',
        };
        onError({code: event.error, message: messages[event.error] || `语音识别失败：${event.error}`});
      };
      recognition.onend = () => {
        recognition = null;
        onInterim('');
        onStateChange('idle');
      };
      try {
        recognition.start();
        return true;
      } catch (error) {
        recognition = null;
        onError({code: 'start-failed', message: `无法启动语音输入：${error.message}`});
        return false;
      }
    }

    return {isSupported, start, stop, destroy};
  }

  function createVoiceInput({
    createAdapter = createWebSpeechAdapter,
    onInterim = () => {},
    onFinal = () => {},
    onStateChange = () => {},
    onError = () => {},
  } = {}) {
    let state = 'idle';
    const emitState = nextState => {
      state = nextState;
      onStateChange(nextState);
    };
    const adapter = createAdapter({
      onInterim,
      onFinal,
      onStateChange: emitState,
      onError: error => {
        emitState('error');
        onError(error);
      },
    });

    function isSupported() {
      return adapter.isSupported();
    }

    function start(language) {
      if (state === 'listening') return true;
      return adapter.start(language);
    }

    function stop() {
      adapter.stop();
    }

    function toggle(language) {
      if (state === 'listening') stop();
      else start(language);
    }

    function destroy() {
      adapter.destroy();
      state = 'idle';
    }

    return {isSupported, start, stop, toggle, destroy, getState: () => state};
  }

  global.createWebSpeechAdapter = createWebSpeechAdapter;
  global.createVoiceInput = createVoiceInput;
})(typeof window !== 'undefined' ? window : globalThis);
