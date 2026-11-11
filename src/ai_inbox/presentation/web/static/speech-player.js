'use strict';
// Playback lifecycle is separate from DOM rendering so races can be tested offline.
window.InboxSpeech = {
  create({createAudio, onState, onReady, onError}) {
    const cache = new WeakMap();
    const requests = new WeakMap();
    let active = null;

    function stop() {
      const previous = active;
      active = null;
      if (!previous) return;
      if (previous.audio) {
        previous.audio.onended = previous.audio.onerror = null;
        previous.audio.pause();
        previous.audio.currentTime = 0;
        previous.audio.remove();
      }
      onState(previous.owner, 'idle');
    }

    function load(owner, key, request) {
      let entry = requests.get(owner);
      if (!entry || entry.key !== key) {
        entry = {key};
        // Keep an in-flight generation for reuse after Stop. It must never auto-play
        // for an obsolete session; cancelling fetch would discard already paid audio.
        entry.promise = Promise.resolve().then(request).then(result => {
          if (requests.get(owner) === entry) cache.set(owner, {key, result});
          return result;
        }).finally(() => {
          if (requests.get(owner) === entry) requests.delete(owner);
        });
        requests.set(owner, entry);
      }
      return entry.promise;
    }

    async function toggle(owner, key, request) {
      if (active?.owner === owner) {
        stop();
        return;
      }
      stop();
      const session = {owner, audio: null};
      active = session;
      onState(owner, 'loading');
      try {
        const saved = cache.get(owner);
        // On a cache hit play() runs inside the click gesture (autoplay fallback).
        const result = saved?.key === key ? saved.result : await load(owner, key, request);
        if (active !== session) return;
        onReady(owner, result);
        const audio = session.audio = createAudio(owner, result);
        audio.onended = () => {
          if (active === session) stop();
        };
        audio.onerror = () => {
          if (active !== session) return;
          cache.delete(owner);
          stop();
          onError(new Error('Не удалось воспроизвести аудио. Нажми «Озвучить», чтобы подготовить его заново.'));
        };
        await audio.play();
        if (active === session) onState(owner, 'playing');
      } catch (error) {
        if (active !== session) return;
        if (error.name !== 'NotAllowedError') cache.delete(owner);
        stop();
        onError(error.name === 'NotAllowedError'
          ? new Error('Браузер заблокировал автозапуск. Нажми «Озвучить» ещё раз — аудио уже готово.')
          : error);
      }
    }

    return {
      toggle,
      stop,
      stopWithin(container) {
        if (active && container?.contains(active.owner.button)) stop();
      }
    };
  }
};
