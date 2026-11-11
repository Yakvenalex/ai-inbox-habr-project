const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const window = {};
runInNewContext(readFileSync('src/ai_inbox/presentation/web/static/speech-player.js', 'utf8'), {window});
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
function harness(play = () => Promise.resolve()) {
  const events = [], audios = [], errors = [];
  const player = window.InboxSpeech.create({
    createAudio(owner, result) {
      const audio = {owner, result, currentTime: 19, paused: true, removed: false,
        play() {this.paused = false; return play();},
        pause() {this.paused = true;}, remove() {this.removed = true;}};
      audios.push(audio);
      return audio;
    },
    onState(owner, state) {events.push([owner.id, state]);},
    onReady(owner, result) {owner.result = result;},
    onError(error) {errors.push(error);}
  });
  return {player, events, audios, errors};
}
const owner = id => ({id, button: {id}});
const result = {file_id: 'synthetic', url: '/api/v1/media/synthetic'};

test('starts playing automatically; Stop pauses and rewinds, replay reuses prepared audio', async () => {
  const h = harness(), b = owner('reply');
  let loads = 0;
  const request = async () => {loads++; return result;};
  await h.player.toggle(b, 'text', request);
  assert.equal(h.audios.length, 1);
  assert.equal(h.audios[0].paused, false);
  assert.equal(h.events.at(-1)[1], 'playing');
  await h.player.toggle(b, 'text', request);
  assert.equal(h.audios[0].paused, true);
  assert.equal(h.audios[0].currentTime, 0);
  assert.equal(h.audios[0].removed, true);
  assert.equal(h.events.at(-1)[1], 'idle');
  await h.player.toggle(b, 'text', request);
  assert.equal(loads, 1);
  assert.equal(h.audios.length, 2);
});

test('Stop during generation prevents late playback and retains ready audio for replay', async () => {
  const h = harness(), b = owner('reply'), wait = deferred();
  let loads = 0;
  const request = () => {loads++; return wait.promise;};
  const running = h.player.toggle(b, 'text', request);
  await flush();
  await h.player.toggle(b, 'text', request);
  wait.resolve(result);
  await running;
  assert.equal(h.audios.length, 0);
  await h.player.toggle(b, 'text', request);
  assert.equal(loads, 1);
  assert.equal(h.audios.length, 1);
});

test('restarting during in-flight generation makes one synthesis request', async () => {
  const h = harness(), b = owner('reply'), wait = deferred();
  let loads = 0;
  const request = () => {loads++; return wait.promise;};
  const first = h.player.toggle(b, 'text', request);
  await flush();
  h.player.stop();
  const second = h.player.toggle(b, 'text', request);
  wait.resolve(result);
  await Promise.all([first, second]);
  assert.equal(loads, 1);
  assert.equal(h.audios.length, 1);
});

test('starting another reply stops existing sound', async () => {
  const h = harness(), a = owner('a'), b = owner('b');
  await h.player.toggle(a, 'first', async () => result);
  await h.player.toggle(b, 'second', async () => result);
  assert.equal(h.audios[0].paused, true);
  assert.equal(h.audios[1].paused, false);
});

test('late result from a different reply cannot interrupt current audio', async () => {
  const h = harness(), a = owner('a'), b = owner('b'), wait = deferred();
  const old = h.player.toggle(a, 'old', () => wait.promise);
  await h.player.toggle(b, 'new', async () => result);
  wait.resolve(result);
  await old;
  assert.equal(h.audios.length, 1);
  assert.equal(h.audios[0].owner, b);
  assert.equal(h.audios[0].paused, false);
});

test('changing the text invalidates the prepared audio', async () => {
  const h = harness(), b = owner('summary');
  let loads = 0;
  const request = async () => {loads++; return result;};
  await h.player.toggle(b, 'old summary', request);
  h.player.stop();
  await h.player.toggle(b, 'new summary', request);
  assert.equal(loads, 2);
});

test('audio completion restores the original button state', async () => {
  const h = harness(), b = owner('reply');
  await h.player.toggle(b, 'text', async () => result);
  h.audios[0].onended();
  assert.equal(h.events.at(-1)[1], 'idle');
  assert.equal(h.audios[0].paused, true);
});

test('autoplay rejection preserves the result for the next user click', async () => {
  let plays = 0, loads = 0;
  const h = harness(() => ++plays === 1 ? Promise.reject(Object.assign(new Error(), {name: 'NotAllowedError'})) : Promise.resolve());
  const b = owner('reply'), request = async () => {loads++; return result;};
  await h.player.toggle(b, 'text', request);
  assert.equal(h.events.at(-1)[1], 'idle');
  assert.equal(h.errors.length, 1);
  await h.player.toggle(b, 'text', request);
  assert.equal(loads, 1);
  assert.equal(h.events.at(-1)[1], 'playing');
});

test('media error invalidates an expired URL so retry can synthesize again', async () => {
  const h = harness(), b = owner('reply');
  let loads = 0;
  const request = async () => {loads++; return result;};
  await h.player.toggle(b, 'text', request);
  h.audios[0].onerror();
  assert.equal(h.errors.length, 1);
  await h.player.toggle(b, 'text', request);
  assert.equal(loads, 2);
});

test('failed generation returns to idle; cancelled failure does not show a stale error', async () => {
  const h = harness(), b = owner('reply');
  await h.player.toggle(b, 'text', async () => {throw new Error('provider unavailable');});
  assert.equal(h.events.at(-1)[1], 'idle');
  assert.equal(h.errors.length, 1);
  const wait = deferred();
  const running = h.player.toggle(b, 'text', () => wait.promise);
  await flush();
  h.player.stop();
  wait.reject(new Error('cancelled request failed'));
  await running;
  assert.equal(h.errors.length, 1);
});

test('Stop during buffering ignores a late play resolution', async () => {
  const wait = deferred(), h = harness(() => wait.promise), b = owner('reply');
  const running = h.player.toggle(b, 'text', async () => result);
  await flush();
  h.player.stop();
  wait.resolve();
  await running;
  assert.equal(h.events.at(-1)[1], 'idle');
  assert.equal(h.audios[0].paused, true);
});

test('changing conversations stops only speech inside the old conversation', async () => {
  const h = harness(), a = owner('chat'), b = owner('summary');
  const chat = {contains(button) {return button === a.button;}};
  await h.player.toggle(b, 'summary', async () => result);
  h.player.stopWithin(chat);
  assert.equal(h.audios[0].paused, false);
  await h.player.toggle(a, 'chat', async () => result);
  h.player.stopWithin(chat);
  assert.equal(h.audios[1].paused, true);
});
