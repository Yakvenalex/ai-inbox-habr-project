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
  const contents = [...element.childNodes];
  element.disabled = true;
  element.textContent = 'Подожди…';
  try {
    return await action();
  } finally {
    element.disabled = false;
    element.replaceChildren(...contents);
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
  if (!response.ok) {
    const error = new Error(data.error?.message || 'Не удалось выполнить запрос');
    error.status = response.status;
    throw error;
  }
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
const mailChanges = new Map();
let mailListRequest = 0,
  emailOpenRequest = 0,
  mailBatchBusy = false;
const views = {
  compose: 'Написать письмо',
  outbox: 'Исходящие',
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
  document.dispatchEvent(new CustomEvent('inbox:view', {
    detail: name
  }));
}

function updateMailSelection() {
  $('#mail-selected-count').textContent = selectedEmails.size ? 'Выбрано: ' + selectedEmails.size : 'Письма не выбраны';
  $('#mark-selected-read').disabled = !selectedEmails.size || mailBatchBusy;
  $('#mark-selected-unread').disabled = !selectedEmails.size || mailBatchBusy;
}
async function loadMail(preserveSelection = false) {
  const request = ++mailListRequest;
  const target = $('#mail-list');
  target.replaceChildren(node('p', 'text-sm opacity-50', 'Получаем письма…'));
  try {
    const [emails, counts] = await Promise.all([
      api('/emails?unread=' + unread + '&limit=20'),
      api('/emails/status').catch(() => null)
    ]);
    if (request !== mailListRequest) return;
    target.replaceChildren();
    if (!preserveSelection) selectedEmails.clear();
    const available = new Set(emails.map(email => email.id));
    for (const id of selectedEmails)
      if (!available.has(id)) selectedEmails.delete(id);
    $('#mail-count').textContent = counts ? counts.unread : '—';
    $('#mail-count').title = counts ? 'Непрочитанных в INBOX: ' + counts.unread : 'Счётчик временно недоступен';
    updateMailSelection();
    if (!emails.length) return empty(target, unread ? 'Всё прочитано' : 'Писем пока нет', unread ? 'Непрочитанных писем нет. Все письма доступны в соседней вкладке.' : 'Обнови список позже.');
    for (const email of emails) {
      const card = node('article', 'mail-card card bg-base-100 border border-base-300 p-4' + (email.unread ? ' mail-unread' : ''));
      const row = node('div', 'flex gap-3 items-start');
      const select = document.createElement('input');
      select.type = 'checkbox';
      select.className = 'checkbox checkbox-sm mt-1';
      select.checked = selectedEmails.has(email.id);
      select.setAttribute('aria-label', 'Выбрать письмо: ' + email.subject);
      select.onchange = () => {
        select.checked ? selectedEmails.add(email.id) : selectedEmails.delete(email.id);
        updateMailSelection();
      };
      const content = node('button', 'text-left flex-1 min-w-0');
      content.onclick = () => guard(() => openEmail(email.id));
      const head = node('div', 'flex justify-between gap-2 items-start');
      head.append(node('span', (email.unread ? 'font-semibold' : 'font-medium opacity-70') + ' text-sm truncate', email.sender), node('span', 'text-[11px] opacity-40 shrink-0', email.date ? new Date(email.date).toLocaleDateString('ru-RU', {
        timeZone: timezone
      }) : ''));
      const subject = node('div', 'flex items-center gap-2 mt-2');
      if (email.unread) subject.append(node('span', 'mail-unread-dot shrink-0'));
      subject.append(node('p', 'text-sm truncate', email.subject), node('span', 'text-[10px] opacity-45 shrink-0 ml-auto', email.unread ? 'Новое' : 'Прочитано'));
      content.append(head, subject);
      row.append(select, content);
      card.append(row);
      target.append(card);
    }
  } catch (error) {
    if (request === mailListRequest) empty(target, 'Почта недоступна', error.message);
    throw error;
  }
}

function renderEmailReadState() {
  if (!selectedEmail) return;
  const pending = mailChanges.has(selectedEmail.id);
  const failed = !$('#email-read-error').classList.contains('hidden');
  $('#email-read-badge').textContent = pending ? 'Сохраняем статус…' : failed ? 'Статус не подтверждён' : selectedEmail.unread ? 'Непрочитано' : 'Прочитано';
  $('#email-read-toggle').textContent = selectedEmail.unread ? 'Отметить прочитанным' : 'Отметить непрочитанным';
  $('#email-read-toggle').disabled = pending;
}
async function changeEmailReadState(email, unreadState) {
  const previous = mailChanges.get(email.id) || Promise.resolve();
  const operation = previous.then(async () => {
    try {
      const result = await post('/emails/' + encodeURIComponent(email.id) + '/read-state', {
        unread: unreadState
      });
      email.unread = result.unread;
      if (selectedEmail?.id === email.id) {
        selectedEmail.unread = result.unread;
        $('#email-read-error').classList.add('hidden');
      }
      try {
        await loadMail(true);
      } catch {
        toast('Статус сохранён, но список не удалось обновить.', true);
      }
    } catch (error) {
      if (selectedEmail?.id === email.id) {
        $('#email-read-error').textContent = 'Не удалось подтвердить статус в почте. ' + error.message;
        $('#email-read-error').classList.remove('hidden');
      }
    }
  });
  mailChanges.set(email.id, operation);
  if (selectedEmail?.id === email.id) renderEmailReadState();
  try {
    await operation;
  } finally {
    if (mailChanges.get(email.id) === operation) mailChanges.delete(email.id);
    if (selectedEmail?.id === email.id) renderEmailReadState();
  }
}
async function openEmail(id) {
  const request = ++emailOpenRequest;
  const email = await api('/emails/' + encodeURIComponent(id));
  if (request !== emailOpenRequest) return;
  selectedEmail = email;
  $('#email-subject').textContent = email.subject;
  $('#email-meta').textContent = email.sender + ' · ' + email.date;
  $('#email-body').textContent = email.text || 'Текстовая часть письма отсутствует.';
  $('#email-attachments').textContent = email.attachments.length ? 'Вложения (только метаданные): ' + email.attachments.map(a => a.filename + ' · ' + Math.ceil(a.size / 1024) + ' КБ').join(', ') : '';
  $('#email-read-error').classList.add('hidden');
  renderEmailReadState();
  $('#email-dialog').showModal();
  await changeEmailReadState(email, false);
}
async function changeSelectedReadStates(unreadState, trigger) {
  if (mailBatchBusy || !selectedEmails.size) return;
  mailBatchBusy = true;
  updateMailSelection();
  for (const control of $('#mail-list').querySelectorAll('button, input')) control.disabled = true;
  try {
    await busy(trigger, async () => {
      const {
        results
      } = await post('/emails/read-state', {
        ids: [...selectedEmails],
        unread: unreadState
      });
      const failed = results.filter(result => result.error);
      if (selectedEmail) {
        const current = results.find(result => result.id === selectedEmail.id && !result.error);
        if (current) {
          selectedEmail.unread = current.unread;
          renderEmailReadState();
        }
      }
      let refreshFailed = false;
      try {
        await loadMail(true);
      } catch {
        refreshFailed = true;
      }
      const outcome = failed.length ? 'Обновлено ' + (results.length - failed.length) + ' из ' + results.length + '. ' + failed[0].error.message : 'Статус выбранных писем сохранён в почте.';
      toast(outcome + (refreshFailed ? ' Список пока не удалось обновить.' : ''), !!failed.length || refreshFailed);
    });
  } finally {
    mailBatchBusy = false;
    for (const control of $('#mail-list').querySelectorAll('button, input')) control.disabled = false;
    updateMailSelection();
  }
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
    card.append(node('h3', 'font-semibold', draft.subject), node('p', 'text-sm opacity-60 mt-3 whitespace-pre-wrap', InboxMarkdown.preview(draft.body, 180)), node('p', 'text-xs opacity-40 mt-4', 'Локальный черновик · ' + fmt(draft.updated_at)));
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
    card.append(header, node('p', 'text-sm opacity-60 mt-3 whitespace-pre-wrap', InboxMarkdown.preview(digest.error || digest.text || 'Генерация…')));
    target.append(card);
  }
}
let conversationItems = [],
  conversationLoadRequest = 0;
let deletionCandidate = null,
  conversationDeleting = false;

function closeConversationMenu() {
  $('#conversation-list').hidden = true;
  $('#conversation-select').setAttribute('aria-expanded', 'false');
}

function renderConversationList() {
  const list = $('#conversation-list');
  const current = conversationItems.find(item => item.id === currentConversation);
  $('#conversation-label').textContent = current?.title || 'Новый диалог';
  list.replaceChildren(button('＋ Новый диалог', 'btn btn-ghost btn-sm w-full justify-start', () => loadConversation('')));
  for (const item of conversationItems) {
    const row = node('div', 'conversation-row' + (item.id === currentConversation ? ' conversation-current' : ''));
    row.setAttribute('role', 'listitem');
    const open = button('', 'conversation-open', () => loadConversation(item.id));
    open.append(node('span', 'block truncate text-sm', item.title), node('span', 'block text-[10px] opacity-40 mt-1', fmt(item.created_at)));
    open.title = item.title;
    const remove = button('✕', 'btn btn-ghost btn-xs btn-square conversation-remove', async () => {
      closeConversationMenu();
      deletionCandidate = item;
      $('#delete-conversation-title').textContent = item.title;
      $('#delete-conversation-error').classList.add('hidden');
      $('#delete-conversation-dialog').showModal();
    });
    remove.title = 'Удалить диалог';
    remove.setAttribute('aria-label', 'Удалить диалог: ' + item.title);
    row.append(open, remove);
    list.append(row);
  }
}
async function refreshConversations() {
  conversationItems = await api('/conversations');
  renderConversationList();
}
async function confirmConversationDeletion() {
  if (!deletionCandidate || conversationDeleting) return;
  const item = deletionCandidate;
  conversationDeleting = true;
  $('#cancel-delete-conversation').disabled = true;
  $('#close-delete-conversation').disabled = true;
  try {
    await busy($('#confirm-delete-conversation'), async () => {
      try {
        await api('/conversations/' + encodeURIComponent(item.id), {
          method: 'DELETE'
        });
      } catch (error) {
        if (error.status !== 404) throw error;
      }
      if (currentConversation === item.id) {
        await loadConversation('');
        $('#chat-text').value = '';
        clearImage();
      }
      // Remove locally before refreshing: a failed refresh must not restore a deleted row.
      conversationItems = conversationItems.filter(conversation => conversation.id !== item.id);
      renderConversationList();
      $('#delete-conversation-dialog').close();
      deletionCandidate = null;
      toast('Диалог удалён');
      try {
        await refreshConversations();
      } catch {
        toast('Диалог удалён. Список пока не удалось обновить.', true);
      }
    });
  } catch (error) {
    $('#delete-conversation-error').textContent = error.message;
    $('#delete-conversation-error').classList.remove('hidden');
  } finally {
    conversationDeleting = false;
    $('#cancel-delete-conversation').disabled = false;
    $('#close-delete-conversation').disabled = false;
  }
}

function renderMessage(message) {
  $('#chat-welcome')?.remove();
  if (message.role === 'user') $$('#chat-messages .chat-email-confirmation').forEach(el => el.remove());
  const wrap = node('article', message.role === 'user' ? 'rounded-xl bg-base-200 p-3' : 'py-1');
  const content = node('div', 'message-text');
  if (message.role === 'assistant') InboxMarkdown.render(content, message.text);
  else content.textContent = message.text;
  wrap.append(node('p', 'text-[10px] font-semibold uppercase tracking-wider opacity-40 mb-2', message.role === 'user' ? 'Ты' : '✦ AI Inbox'), content);
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
      const receipt = node('div', 'action-receipt ' + (action.result.error || action.result.status === 'failed' ? 'text-error' : ''));
      const result = action.result;
      const outcome = action.name === 'send_outgoing_email' && result.status === 'unknown' ? ' · результат неизвестен' : result.error ? ' · не выполнено' : ' · результат получен';
      receipt.append(node('strong', '', action.name + outcome));
      const info = (typeof result.error === 'string' ? result.error : result.error?.message) || (result.id ? 'ID: ' + result.id : result.tasks ? 'Задач: ' + result.tasks.length : result.emails ? 'Писем: ' + result.emails.length : result.subject || 'Готово');
      receipt.append(node('p', 'source-id mt-1', info));
      if (result.id && action.name === 'create_draft') {
        const a = node('a', 'link', 'Открыть черновик');
        a.href = '/drafts/' + result.id;
        receipt.append(a);
      }
      if (result.id && ['prepare_outgoing_email', 'review_outgoing_email'].includes(action.name) && !result.error) {
        receipt.append(node('p', 'mt-3 break-all', 'Кому: ' + result.recipient));
        receipt.append(node('p', 'mt-2 font-semibold break-words', 'Тема: ' + result.subject));
        receipt.append(node('p', 'mt-2 whitespace-pre-wrap break-words text-sm', result.body));
        receipt.append(node('p', 'mt-2 opacity-70', result.attachments?.length ? 'Вложения: ' + result.attachments.map(file => file.name + ' (' + file.size + ' байт)').join(', ') : 'Без вложений'));
        if (result.confirmation_available) receipt.append(node('p', 'chat-email-confirmation mt-3 font-medium', 'Для отправки ответь: «Всё ок, отправляй».'));
        const a = node('a', 'link', 'Открыть письмо для проверки');
        a.href = '/?view=compose&outgoing=' + encodeURIComponent(result.id);
        receipt.append(a);
      }
      if (action.name === 'send_outgoing_email' && result.outbox_id) {
        const statuses = {sent: 'SMTP-сервер принял письмо', failed: 'Отправка отклонена', unknown: 'Результат неизвестен — повтор запрещён', sending: 'Отправляется'};
        receipt.append(node('p', 'mt-2 font-semibold', statuses[result.status] || result.status));
        if (result.message_id) receipt.append(node('p', 'source-id mt-1', 'Message-ID: ' + result.message_id));
        const link = node('a', 'link', 'Открыть исходящее письмо');
        link.href = '/?view=compose&outgoing=' + encodeURIComponent(result.outbox_id);
        receipt.append(link);
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
  const request = ++conversationLoadRequest;
  closeConversationMenu();
  let messages = [];
  if (id) {
    try {
      messages = await api('/conversations/' + encodeURIComponent(id) + '/messages');
    } catch (error) {
      if (request !== conversationLoadRequest) return;
      if (error.status !== 404) throw error;
      id = '';
      toast('Этот диалог уже удалён. Можно начать новый.');
      conversationItems = await api('/conversations');
    }
  }
  if (request !== conversationLoadRequest) return;
  currentConversation = id;
  pendingRequest = null;
  for (const audio of $('#chat-messages').querySelectorAll('audio')) audio.pause();
  $('#chat-messages').replaceChildren();
  if (id) messages.forEach(renderMessage);
  else $('#chat-messages').append(node('p', 'text-sm opacity-50 text-center mt-10', 'Новый диалог. Напиши, что нужно сделать.'));
  renderConversationList();
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
  closeConversationMenu();
  $('#conversation-select').disabled = true;
  $('#new-chat').disabled = true;
  let waiting;
  try {
    if (!currentConversation) {
      const conversation = await post('/conversations', {
        title: text.slice(0, 80) || 'Диалог'
      });
      currentConversation = conversation.id;
      const url = new URL(location);
      url.searchParams.set('conversation', currentConversation);
      history.replaceState(null, '', url);
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
    $('#conversation-select').disabled = false;
    $('#new-chat').disabled = false;
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
  $('#email-read-toggle').onclick = () => {
    if (selectedEmail) changeEmailReadState(selectedEmail, !selectedEmail.unread);
  };
  $('#mark-selected-read').onclick = () => guard(() => changeSelectedReadStates(false, $('#mark-selected-read')));
  $('#mark-selected-unread').onclick = () => guard(() => changeSelectedReadStates(true, $('#mark-selected-unread')));
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
    InboxMarkdown.render($('#summary-text'), result.text);
    $('#summary-audio').replaceChildren();
    $('#summary').classList.remove('hidden');
    $('#summary-sources').replaceChildren(...result.sources.map(id => {
      const b = button('Письмо ↗', 'btn btn-ghost btn-xs', () => openEmail(id));
      return b;
    }));
  }));
  $('#close-summary').onclick = () => {
    $('#summary-audio audio')?.pause();
    $('#summary').classList.add('hidden');
    summaryText = '';
    for (const selector of ['#summary-text', '#summary-sources', '#summary-audio']) {
      $(selector).replaceChildren();
    }
    $('#summarize').focus();
  };
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
  $('#conversation-select').onclick = () => {
    const list = $('#conversation-list');
    list.hidden = !list.hidden;
    $('#conversation-select').setAttribute('aria-expanded', String(!list.hidden));
  };
  document.addEventListener('click', event => {
    if (!$('#conversation-picker').contains(event.target)) closeConversationMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeConversationMenu();
  });
  $('#cancel-delete-conversation').onclick = $('#close-delete-conversation').onclick = () => $('#delete-conversation-dialog').close();
  $('#delete-conversation-dialog').addEventListener('cancel', event => {
    if (conversationDeleting) event.preventDefault();
  });
  $('#delete-conversation-dialog').addEventListener('close', () => $('#conversation-select').focus());
  $('#confirm-delete-conversation').onclick = confirmConversationDeletion;
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
  $('#attach-image').onclick = () => $('#image-file').click();
  $('#upload-audio').onclick = () => $('#audio-file').click();
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
  function setRecording(active) {
    const control = $('#record');
    control.querySelector('[data-record-idle]').hidden = active;
    control.querySelector('[data-record-stop]').hidden = !active;
    control.classList.toggle('btn-error', active);
    control.setAttribute('aria-pressed', String(active));
    control.title = active ? 'Остановить запись' : 'Записать голос';
    control.setAttribute('aria-label', control.title);
  }
  $('#record').onclick = () => guard(async () => {
    if (recorder?.state === 'recording') {
      recorder.stop();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw new Error('Запись недоступна. Загрузи аудиофайл кнопкой с говорящим смайлом.');
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
      setRecording(false);
      const mime = recorder.mimeType;
      guard(() => transcribe(new File(chunks, 'recording.' + (mime.includes('ogg') ? 'ogg' : 'webm'), {
        type: mime
      }), $('#record')));
    };
    recorder.start();
    setRecording(true);
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
    InboxMarkdown.render($('#digest-body'), digest.error || digest.text);
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