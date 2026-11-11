'use strict';
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const csrf = () => document.cookie.split('; ').find(x => x.startsWith('ai_inbox_csrf='))?.split('=')[1] || '';
const timezone = $('[data-timezone]')?.dataset.timezone || 'Europe/Moscow';
const fmt = value => value ? new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: timezone
}).format(new Date(value)) : 'Без срока';
const node = (tag, classes = '', text = '') => {
  const el = document.createElement(tag);
  el.className = classes;
  el.textContent = text;
  return el;
};
const button = (text, classes, action) => {
  const b = node('button', classes, text);
  b.type = 'button';
  b.onclick = () => guard(action);
  return b;
};

function toast(message, error = false) {
  const item = node('div', 'alert shadow-lg ' + (error ? 'alert-error' : 'alert-success'), message);
  $('#notifications')?.append(item);
  setTimeout(() => item.remove(), error ? 12000 : 4500);
}
async function guard(action) {
  try {
    return await action();
  } catch (error) {
    toast(error.message || 'Ошибка запроса', true);
  }
}
async function busy(element, action) {
  const label = element.textContent;
  element.disabled = true;
  element.textContent = 'Подожди…';
  try {
    return await action();
  } finally {
    element.disabled = false;
    element.textContent = label;
  }
}
async function api(path, options = {}) {
  const response = await fetch('/api/v1' + path, {
    ...options,
    credentials: 'same-origin',
    headers: {
      'X-CSRF-Token': csrf(),
      ...(options.body instanceof FormData ? {} : {
        'Content-Type': 'application/json'
      }),
      ...options.headers
    }
  });
  if (response.status === 401) {
    location.assign('/login');
    throw new Error('Сессия истекла. Войди снова.');
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('Сервер вернул неожиданный ответ. Проверь соединение.');
  }
  if (!response.ok) throw new Error(data.error?.message || 'Не удалось выполнить запрос');
  return data;
}
const post = (path, data) => api(path, {
  method: 'POST',
  body: data === undefined ? undefined : JSON.stringify(data)
});

function empty(target, title, description) {
  target.replaceChildren();
  const card = node('div', 'card bg-base-100 border border-base-300 p-9 text-center');
  card.append(node('div', 'text-3xl opacity-30 mb-4', '◇'), node('h3', 'font-semibold', title), node('p', 'text-sm opacity-50 mt-2', description));
  target.append(card);
}

function audioPlayer(target, result) {
  target.replaceChildren();
  const audio = document.createElement('audio');
  audio.controls = true;
  audio.src = result.url;
  audio.className = 'w-full my-3';
  const link = node('a', 'link text-xs', 'Скачать MP3');
  link.href = result.url;
  link.download = result.file_id + '.mp3';
  target.append(audio, link);
}
async function speak(text, target, b) {
  await busy(b, async () => audioPlayer(target, await post('/speech/synthesis', {
    text
  })));
}

function sourceLink(source) {
  const [kind, ...rest] = source.split(':');
  const id = rest.join(':');
  const a = node('a', 'link text-xs break-all', (kind === 'email' ? 'Письмо ' : kind === 'task' ? 'Задача ' : 'Источник ') + id);
  a.href = kind === 'email' ? '/?email=' + encodeURIComponent(id) : '/?view=tasks&task=' + encodeURIComponent(id);
  return a;
}

function zonedIso(value) {
  if (!value) return null;
  const target = new Date(value + 'Z').getTime();
  let stamp = target;
  for (let i = 0; i < 3; i++) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23'
    }).formatToParts(new Date(stamp));
    const get = t => parts.find(p => p.type === t).value;
    const wall = Date.UTC(+get('year'), +get('month') - 1, +get('day'), +get('hour'), +get('minute'), +get('second'));
    stamp += target - wall;
  }
  return new Date(stamp).toISOString();
}

function localInput(value) {
  if (!value) return '';
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23'
  }).format(new Date(value));
  return parts.replace(' ', 'T');
}

let selectedEmail = null,
  currentConversation = '',
  attachedImage = null,
  editingTask = null,
  taskSource = 'manual';
let unread = true,
  summaryText = '',
  pendingRequest = null,
  sending = false;
const selectedEmails = new Set();
const views = {
  mail: 'Входящие',
  tasks: 'Задачи',
  drafts: 'Черновики',
  digests: 'Дайджесты'
};
async function view(name) {
  if (!views[name]) name = 'mail';
  $$('[data-panel]').forEach(el => el.hidden = el.dataset.panel !== name);
  $$('[data-view]').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  $('#breadcrumb').textContent = views[name];
  const url = new URL(location);
  url.searchParams.set('view', name);
  history.replaceState(null, '', url);
  if (name === 'mail') await loadMail();
  if (name === 'tasks') await loadTasks();
  if (name === 'drafts') await loadDrafts();
  if (name === 'digests') await loadDigests();
}
async function loadMail() {
  const target = $('#mail-list');
  target.replaceChildren(node('p', 'text-sm opacity-50', 'Получаем письма…'));
  try {
    const emails = await api('/emails?unread=' + unread + '&limit=20');
    target.replaceChildren();
    selectedEmails.clear();
    if (unread) $('#mail-count').textContent = emails.length === 20 ? '20+' : emails.length;
    if (!emails.length) return empty(target, 'Всё спокойно', 'Новых писем нет. Можно заняться задачами.');
    for (const email of emails) {
      const card = node('article', 'mail-card card bg-base-100 border border-base-300 p-4');
      const row = node('div', 'flex gap-3 items-start');
      const select = document.createElement('input');
      select.type = 'checkbox';
      select.className = 'checkbox checkbox-sm mt-1';
      select.setAttribute('aria-label', 'Выбрать письмо: ' + email.subject);
      select.onchange = () => select.checked ? selectedEmails.add(email.id) : selectedEmails.delete(email.id);
      const content = node('button', 'text-left flex-1 min-w-0');
      content.onclick = () => guard(() => openEmail(email.id));
      const head = node('div', 'flex justify-between gap-2 items-start');
      head.append(node('span', 'font-semibold text-sm truncate', email.sender), node('span', 'text-[11px] opacity-40 shrink-0', email.date ? new Date(email.date).toLocaleDateString('ru-RU', {
        timeZone: timezone
      }) : ''));
      content.append(head, node('p', 'text-sm mt-2 truncate', email.subject));
      row.append(select, content);
      card.append(row);
      target.append(card);
    }
  } catch (error) {
    empty(target, 'Почта недоступна', error.message);
    throw error;
  }
}
async function openEmail(id) {
  const email = await api('/emails/' + encodeURIComponent(id));
  selectedEmail = email;
  $('#email-subject').textContent = email.subject;
  $('#email-meta').textContent = email.sender + ' · ' + email.date;
  $('#email-body').textContent = email.text || 'Текстовая часть письма отсутствует.';
  $('#email-attachments').textContent = email.attachments.length ? 'Вложения (только метаданные): ' + email.attachments.map(a => a.filename + ' · ' + Math.ceil(a.size / 1024) + ' КБ').join(', ') : '';
  $('#email-dialog').showModal();
}
async function loadTasks() {
  const query = new URLSearchParams();
  if ($('#task-status').value) query.set('status', $('#task-status').value);
  const due = $('#task-due').value;
  if (due === 'overdue') query.set('due_before', new Date().toISOString());
  if (due === 'today') {
    const day = localInput(new Date()).slice(0, 10);
    query.set('due_before', zonedIso(day + 'T23:59'));
  }
  const tasks = await api('/tasks?' + query);
  const allOpen = await api('/tasks?status=open');
  $('#task-count').textContent = allOpen.length;
  const target = $('#task-list');
  target.replaceChildren();
  if (!tasks.length) return empty(target, 'Здесь пока пусто', 'Добавь задачу вручную или попроси ассистента.');
  for (const task of tasks) {
    const card = node('article', 'card bg-base-100 border border-base-300 p-5');
    card.id = 'task-' + task.id;
    const row = node('div', 'flex gap-3');
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.className = 'checkbox checkbox-sm mt-1';
    check.checked = task.status === 'completed';
    check.setAttribute('aria-label', 'Завершить задачу: ' + task.title);
    check.onchange = () => guard(async () => {
      await api('/tasks/' + task.id, {
        method: 'PATCH',
        body: JSON.stringify({
          status: check.checked ? 'completed' : 'open'
        })
      });
      await loadTasks();
    });
    const content = node('div', 'flex-1 min-w-0');
    content.append(node('h3', 'font-semibold text-sm ' + (check.checked ? 'line-through opacity-50' : ''), task.title));
    if (task.notes) content.append(node('p', 'text-xs opacity-60 whitespace-pre-wrap mt-2 leading-6', task.notes));
    const overdue = task.due_at && new Date(task.due_at) < new Date() && !check.checked;
    content.append(node('p', 'text-xs mt-3 ' + (overdue ? 'text-error' : 'opacity-50'), (overdue ? 'Просрочено · ' : '') + fmt(task.due_at)));
    const meta = node('div', 'flex flex-wrap gap-2 items-center mt-2');
    meta.append(node('span', 'source-id opacity-40', task.id));
    if (task.source.startsWith('email:')) {
      const source = sourceLink(task.source);
      meta.append(source);
    }
    content.append(meta);
    row.append(check, content, button('✎', 'btn btn-ghost btn-xs', () => taskModal(task)));
    card.append(row);
    target.append(card);
  }
  const id = new URL(location).searchParams.get('task');
  if (id) document.getElementById('task-' + id)?.scrollIntoView({
    behavior: 'smooth'
  });
}

function taskModal(task = null, source = 'manual') {
  editingTask = task?.id || null;
  taskSource = task?.source || source;
  const form = $('#task-form');
  form.reset();
  form.elements.title.value = task?.title || '';
  form.elements.notes.value = task?.notes || '';
  form.elements.due_at.value = localInput(task?.due_at);
  $('#task-dialog-title').textContent = task ? 'Изменить задачу' : 'Новая задача';
  $('#task-dialog').showModal();
  form.elements.title.focus();
}
async function loadDrafts() {
  const drafts = await api('/drafts');
  const target = $('#draft-list');
  target.replaceChildren();
  if (!drafts.length) return empty(target, 'Ответы под твоим контролем', 'Открой письмо и сохрани черновик или попроси ассистента.');
  for (const draft of drafts) {
    const card = node('a', 'card bg-base-100 border border-base-300 p-5 mail-card');
    card.href = '/drafts/' + draft.id;
    card.append(node('h3', 'font-semibold', draft.subject), node('p', 'text-sm opacity-60 mt-3 whitespace-pre-wrap', draft.body.slice(0, 180)), node('p', 'text-xs opacity-40 mt-4', 'Локальный черновик · ' + fmt(draft.updated_at)));
    target.append(card);
  }
}
async function loadDigests() {
  const digests = await api('/digests');
  const target = $('#digest-list');
  target.replaceChildren();
  if (!digests.length) return empty(target, 'Пока нет дайджестов', 'Создай первый вручную или дождись запуска по расписанию.');
  for (const digest of digests) {
    const card = node('a', 'card bg-base-100 border border-base-300 p-5 mail-card');
    card.href = '/digests/' + digest.id;
    const header = node('div', 'flex justify-between');
    header.append(node('h3', 'font-semibold', fmt(digest.created_at)), node('span', 'badge badge-sm ' + (digest.status === 'ready' ? 'badge-success' : 'badge-warning'), {
      ready: 'Готов',
      failed: 'Ошибка',
      generating: 'Готовится'
    } [digest.status] || digest.status));
    card.append(header, node('p', 'text-sm opacity-60 mt-3 whitespace-pre-wrap', (digest.error || digest.text || 'Генерация…').slice(0, 240)));
    target.append(card);
  }
}
async function refreshConversations() {
  const items = await api('/conversations');
  const select = $('#conversation-select');
  select.replaceChildren(new Option('Новый диалог', ''));
  items.forEach(c => select.add(new Option(c.title + ' · ' + fmt(c.created_at), c.id)));
  select.value = currentConversation;
}

function renderMessage(message) {
  $('#chat-welcome')?.remove();
  const wrap = node('article', message.role === 'user' ? 'rounded-xl bg-base-200 p-3' : 'py-1');
  wrap.append(node('p', 'text-[10px] font-semibold uppercase tracking-wider opacity-40 mb-2', message.role === 'user' ? 'Ты' : '✦ AI Inbox'), node('div', 'message-text', message.text));
  if (message.image_id) {
    const img = document.createElement('img');
    img.src = '/api/v1/media/' + message.image_id;
    img.alt = 'Изображение из сообщения';
    img.className = 'rounded-lg mt-3 max-h-44';
    wrap.append(img);
  }
  if (message.role === 'assistant') {
    const actions = message.actions || [];
    wrap.append(node('p', 'text-[10px] opacity-40 mt-3', actions.length ? 'Подтверждённые результаты инструментов:' : 'Действия инструментами не выполнялись.'));
    for (const action of actions) {
      const receipt = node('div', 'action-receipt ' + (action.result.error ? 'text-error' : ''));
      const result = action.result;
      receipt.append(node('strong', '', action.name + (result.error ? ' · не выполнено' : ' · результат получен')));
      const info = result.error?.message || (result.id ? 'ID: ' + result.id : result.tasks ? 'Задач: ' + result.tasks.length : result.emails ? 'Писем: ' + result.emails.length : result.subject || 'Готово');
      receipt.append(node('p', 'source-id mt-1', info));
      if (result.id && action.name === 'create_draft') {
        const a = node('a', 'link', 'Открыть черновик');
        a.href = '/drafts/' + result.id;
        receipt.append(a);
      }
      if (result.id && action.name === 'generate_digest') {
        const a = node('a', 'link', 'Открыть дайджест');
        a.href = '/digests/' + result.id;
        receipt.append(a);
      }
      wrap.append(receipt);
    }
    const audio = node('div');
    const speakButton = button('♫ Озвучить', 'btn btn-ghost btn-xs mt-2', () => speak(message.text, audio, speakButton));
    wrap.append(speakButton, audio);
  }
  $('#chat-messages').append(wrap);
  $('#chat-messages').scrollTop = $('#chat-messages').scrollHeight;
}
async function loadConversation(id) {
  currentConversation = id;
  pendingRequest = null;
  $('#conversation-select').value = id;
  $('#chat-messages').replaceChildren();
  if (id) {
    for (const message of await api('/conversations/' + id + '/messages')) renderMessage(message);
  } else {
    $('#chat-messages').append(node('p', 'text-sm opacity-50 text-center mt-10', 'Новый диалог. Напиши, что нужно сделать.'));
  }
  const url = new URL(location);
  if (id) url.searchParams.set('conversation', id);
  else url.searchParams.delete('conversation');
  history.replaceState(null, '', url);
}

function openChat(text) {
  const workspace = $('.workspace');
  workspace.classList.remove('chat-hidden');
  workspace.classList.add('chat-open');
  $('#chat-text').value = text;
  $('#chat-text').focus();
}

function clearImage() {
  attachedImage = null;
  $('#image-preview').classList.add('hidden');
  $('#image-file').value = '';
}
async function sendMessage() {
  if (sending) return;
  let text = $('#chat-text').value.trim();
  if (!text && attachedImage) text = 'Прочитай изображение и извлеки данные. Ничего не создавай без моей просьбы.';
  if (!text && !pendingRequest) return;
  sending = true;
  const send = $('#send-chat');
  send.disabled = true;
  let waiting;
  try {
    if (!currentConversation) {
      const conversation = await post('/conversations', {
        title: text.slice(0, 80) || 'Диалог'
      });
      currentConversation = conversation.id;
      await refreshConversations();
    }
    if (!pendingRequest) {
      pendingRequest = {
        text,
        image_id: attachedImage,
        request_id: crypto.randomUUID()
      };
      renderMessage({
        role: 'user',
        text,
        image_id: attachedImage
      });
      $('#chat-text').value = '';
      clearImage();
    }
    waiting = node('p', 'text-xs opacity-50 animate-pulse', 'Ассистент работает. Вызовы внешнего сервиса могут занять некоторое время…');
    $('#chat-messages').append(waiting);
    const result = await post('/conversations/' + currentConversation + '/messages', pendingRequest);
    renderMessage({
      role: 'assistant',
      ...result
    });
    pendingRequest = null;
    send.textContent = '↑';
    if (result.status === 'failed') toast('Ответ не завершён. Проверь сохранённые действия.', true);
    await loadTasks();
  } catch (error) {
    send.textContent = '↻';
    toast(error.message + ' Повторная отправка использует тот же ID запроса.', true);
  } finally {
    waiting?.remove();
    sending = false;
    send.disabled = false;
  }
}
async function transcribe(file, trigger) {
  if (!file) return;
  await busy(trigger, async () => {
    const form = new FormData();
    form.append('file', file);
    const result = await api('/speech/transcriptions', {
      method: 'POST',
      body: form
    });
    $('#transcript-text').value = result.text;
    $('#transcript-dialog').showModal();
  });
}

function initializeWorkspace() {
  $$('[data-view]').forEach(b => b.onclick = () => guard(() => view(b.dataset.view)));
  $('#logout').onclick = () => guard(async () => {
    const response = await fetch('/auth/logout', {
      method: 'POST',
      headers: {
        'X-CSRF-Token': csrf()
      }
    });
    if (response.ok) location.assign('/login');
    else throw new Error('Не удалось выйти. Обнови страницу.');
  });
  $('#chat-toggle').onclick = () => {
    const workspace = $('.workspace');
    if (innerWidth >= 1200) workspace.classList.toggle('chat-hidden');
    else workspace.classList.toggle('chat-open');
  };
  $('#refresh-mail').onclick = () => guard(() => busy($('#refresh-mail'), loadMail));
  $$('[data-unread]').forEach(b => b.onclick = () => guard(async () => {
    unread = b.dataset.unread === 'true';
    $$('[data-unread]').forEach(x => x.classList.toggle('tab-active', x === b));
    await loadMail();
  }));
  $('#summarize').onclick = () => guard(() => busy($('#summarize'), async () => {
    const result = await post('/emails/summarize', selectedEmails.size ? {
      ids: [...selectedEmails]
    } : {
      limit: 10
    });
    summaryText = result.text;
    $('#summary-text').textContent = result.text;
    $('#summary').classList.remove('hidden');
    $('#summary-sources').replaceChildren(...result.sources.map(id => {
      const b = button('Письмо ↗', 'btn btn-ghost btn-xs', () => openEmail(id));
      return b;
    }));
  }));
  $('#speak-summary').onclick = () => guard(() => speak(summaryText, $('#summary-audio'), $('#speak-summary')));
  $('#new-task').onclick = () => taskModal();
  $('#task-status').onchange = () => guard(loadTasks);
  $('#task-due').onchange = () => guard(loadTasks);
  $('#task-form').onsubmit = event => {
    event.preventDefault();
    guard(() => busy(event.submitter, async () => {
      const form = event.target;
      const data = {
        title: form.elements.title.value,
        notes: form.elements.notes.value,
        due_at: zonedIso(form.elements.due_at.value)
      };
      await api('/tasks' + (editingTask ? '/' + editingTask : ''), {
        method: editingTask ? 'PATCH' : 'POST',
        body: JSON.stringify(editingTask ? data : {
          ...data,
          source: taskSource
        })
      });
      $('#task-dialog').close();
      toast('Задача сохранена');
      await loadTasks();
    }));
  };
  $('#email-task').onclick = () => {
    const email = selectedEmail;
    $('#email-dialog').close();
    taskModal({
      title: email.subject,
      notes: email.text.slice(0, 2000),
      source: 'email:' + email.id
    });
  };
  $('#email-draft').onclick = () => {
    $('#draft-form').elements.subject.value = 'Re: ' + selectedEmail.subject;
    $('#draft-form').elements.body.value = '';
    $('#email-dialog').close();
    $('#draft-dialog').showModal();
  };
  $('#email-ask').onclick = () => {
    $('#email-dialog').close();
    openChat('Прочитай письмо ' + selectedEmail.id + ' и кратко объясни, что от меня требуется.');
  };
  $('#draft-form').onsubmit = event => {
    event.preventDefault();
    guard(() => busy(event.submitter, async () => {
      const result = await post('/drafts', {
        email_id: selectedEmail.id,
        subject: event.target.elements.subject.value,
        body: event.target.elements.body.value
      });
      $('#draft-dialog').close();
      toast('Черновик сохранён: ' + result.id);
      await view('drafts');
    }));
  };
  $('#new-digest').onclick = () => guard(() => busy($('#new-digest'), async () => {
    const digest = await post('/digests');
    await loadDigests();
    if (digest.status === 'failed') toast(digest.error, true);
    else toast('Дайджест готов');
  }));
  $('#new-chat').onclick = () => guard(() => loadConversation(''));
  $('#conversation-select').onchange = event => guard(() => loadConversation(event.target.value));
  $$('.suggestion').forEach(b => b.onclick = () => openChat(b.dataset.prompt));
  $('#chat-form').onsubmit = event => {
    event.preventDefault();
    sendMessage();
  };
  $('#chat-text').onkeydown = event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      sendMessage();
    }
  };
  $('#image-file').onchange = () => guard(async () => {
    const file = $('#image-file').files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    $('#send-chat').disabled = true;
    try {
      const result = await api('/files/images', {
        method: 'POST',
        body: form
      });
      attachedImage = result.file_id;
      $('#image-preview img').src = result.url;
      $('#image-preview').classList.remove('hidden');
      $('#image-preview').classList.add('flex');
      toast('Изображение прикреплено');
    } finally {
      $('#send-chat').disabled = false;
    }
  });
  $('#remove-image').onclick = clearImage;
  $('#audio-file').onchange = () => guard(async () => {
    await transcribe($('#audio-file').files[0], $('#record'));
    $('#audio-file').value = '';
  });
  $('#confirm-transcript').onclick = () => {
    openChat($('#transcript-text').value);
    $('#transcript-dialog').close();
  };
  let recorder = null,
    stream = null,
    chunks = [],
    timer = null;
  $('#record').onclick = () => guard(async () => {
    if (recorder?.state === 'recording') {
      recorder.stop();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw new Error('Запись недоступна. Загрузи аудиофайл кнопкой «Аудио».');
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: true
      });
    } catch {
      throw new Error('Нет доступа к микрофону. Разреши запись в браузере или загрузи аудиофайл.');
    }
    const type = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm'].find(t => MediaRecorder.isTypeSupported(t));
    recorder = new MediaRecorder(stream, type ? {
      mimeType: type
    } : {});
    chunks = [];
    recorder.ondataavailable = e => {
      if (e.data.size) chunks.push(e.data);
    };
    recorder.onstop = () => {
      clearTimeout(timer);
      stream.getTracks().forEach(t => t.stop());
      $('#record').textContent = '♩ Запись';
      $('#record').classList.remove('btn-error');
      const mime = recorder.mimeType;
      guard(() => transcribe(new File(chunks, 'recording.' + (mime.includes('ogg') ? 'ogg' : 'webm'), {
        type: mime
      }), $('#record')));
    };
    recorder.start();
    $('#record').textContent = '■ Стоп';
    $('#record').classList.add('btn-error');
    timer = setTimeout(() => recorder.state === 'recording' && recorder.stop(), 295000);
  });
  guard(async () => {
    await refreshConversations();
    const params = new URL(location).searchParams;
    const id = params.get('conversation');
    if (id) await loadConversation(id);
    await view(params.get('view') || 'mail');
    await loadTasks();
    if (params.get('email')) await openEmail(params.get('email'));
  });
}
async function initializeDetail() {
  const root = $('[data-detail]'),
    id = root.dataset.id;
  if (root.dataset.detail === 'draft') {
    const draft = await api('/drafts/' + id);
    $('#detail-title').textContent = draft.subject;
    $('#detail-meta').textContent = 'Источник: ' + draft.email_id + ' · ' + fmt(draft.updated_at);
    const form = $('#edit-draft-form');
    form.elements.subject.value = draft.subject;
    form.elements.body.value = draft.body;
    form.onsubmit = event => {
      event.preventDefault();
      guard(() => busy(event.submitter, async () => {
        const result = await api('/drafts/' + id, {
          method: 'PATCH',
          body: JSON.stringify({
            subject: form.elements.subject.value,
            body: form.elements.body.value
          })
        });
        $('#detail-title').textContent = result.subject;
        $('#saved-state').textContent = 'Сохранено · ' + fmt(result.updated_at);
        toast('Черновик обновлён');
      }));
    };
  } else {
    const digest = await api('/digests/' + id);
    $('#detail-title').textContent = 'Дайджест · ' + fmt(digest.created_at);
    $('#detail-meta').textContent = ({
      ready: 'Готов',
      failed: 'Ошибка генерации',
      generating: 'Готовится'
    } [digest.status]) + ' · ' + digest.key;
    $('#digest-body').textContent = digest.error || digest.text;
    $('#digest-sources').replaceChildren(...digest.sources.map(sourceLink));
    $('#digest-speak').disabled = digest.status !== 'ready';
    $('#digest-speak').onclick = () => guard(() => busy($('#digest-speak'), async () => audioPlayer($('#digest-audio'), await post('/digests/' + id + '/audio'))));
    if (digest.audio_id) audioPlayer($('#digest-audio'), {
      url: '/api/v1/media/' + digest.audio_id,
      file_id: digest.audio_id
    });
  }
}
if ($('.workspace')) initializeWorkspace();
if ($('[data-detail]')) guard(initializeDetail);