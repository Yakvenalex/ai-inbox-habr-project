const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const window = {};
runInNewContext(readFileSync('src/ai_inbox/presentation/web/static/chat-stream.js', 'utf8'), {window, TextDecoder});
const result = {request_id: 'synthetic-request', status: 'completed', text: 'Привет', actions: []};
const frame = (type, data) => 'event: ' + type + '\ndata: ' + JSON.stringify(data) + '\n\n';
function response(text, split = 7) {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      if (offset >= bytes.length) return controller.close();
      controller.enqueue(bytes.slice(offset, offset + split));
      offset += split;
    }
  }), {headers: {'Content-Type': 'text/event-stream; charset=utf-8'}});
}

test('reads UTF-8 deltas across arbitrary byte boundaries and returns persisted done', async () => {
  const events = [];
  const text = frame('snapshot', {text: ''}) + frame('delta', {text: 'Привет 🌍'}) + frame('done', {result});
  const output = await window.InboxChatStream.consume(response(text, 1), e => events.push(e));
  assert.equal(output.request_id, result.request_id);
  assert.equal(events[1].text, 'Привет 🌍');
  assert.equal(events[1].type, 'delta');
});

test('supports CRLF and ignores keepalive/id fields', async () => {
  const events = [];
  const text = (': keepalive\n\nid: 7\n' + frame('tool_start', {name: 'read_email'}) + frame('done', {result})).replace(/\n/g, '\r\n');
  await window.InboxChatStream.consume(response(text, 3), e => events.push(e));
  assert.equal(events.length, 1);
  assert.equal(events[0].name, 'read_email');
});

test('snapshot is forwarded as a replacement and multiple events may share one chunk', async () => {
  const events = [];
  await window.InboxChatStream.consume(response(frame('snapshot', {text: 'Уже получено'}) + frame('delta', {text: ' ещё'}) + frame('done', {result}), 99999), e => events.push(e));
  assert.equal(events[0].type, 'snapshot');
  assert.equal(events[0].text, 'Уже получено');
  assert.equal(events[1].text, ' ещё');
});

test('provider failure in done remains a final saved failure', async () => {
  const failed = {...result, status: 'failed', error: 'provider_unavailable'};
  const output = await window.InboxChatStream.consume(response(frame('done', {result: failed})), () => {});
  assert.equal(output.status, 'failed');
  assert.equal(output.error, 'provider_unavailable');
});

test('network close before done rejects instead of accepting a partial answer', async () => {
  const events = [];
  await assert.rejects(() => window.InboxChatStream.consume(response(frame('delta', {text: 'Часть'})), e => events.push(e)), /закрылось/);
  assert.equal(events[0].text, 'Часть');
});

test('truncated done frame is not treated as a committed result', async () => {
  await assert.rejects(() => window.InboxChatStream.consume(response(frame('done', {result}).slice(0, -2)), () => {}), /закрылось/);
});

test('safe terminal stream error stops reconnect attempts', async () => {
  let error;
  try {await window.InboxChatStream.consume(response(frame('error', {error: {message: 'Проверь историю'}})), () => {});}
  catch (caught) {error = caught;}
  assert.equal(error.terminal, true);
  assert.equal(error.message, 'Проверь историю');
});

test('rejects unexpected JSON response and malformed event JSON', async () => {
  await assert.rejects(() => window.InboxChatStream.consume(new Response('{}'), () => {}), /не открыл поток/);
  await assert.rejects(() => window.InboxChatStream.consume(response('event: delta\ndata: broken\n\n'), () => {}), /прочитать/);
});

test('activity callback includes heartbeat traffic', async () => {
  let activity = 0;
  await window.InboxChatStream.consume(response(': keepalive\n\n' + frame('done', {result})), () => {}, () => activity++);
  assert.ok(activity > 0);
});
