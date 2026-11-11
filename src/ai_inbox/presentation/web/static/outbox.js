'use strict';

// Uses the same authenticated API helper and notifications as the rest of the workspace.
(() => {
  if (!$('#compose-form')) return;
  const form = $('#compose-form');
  const states = {
    draft: 'Черновик',
    sending: 'Отправляется',
    sent: 'Принято сервером',
    failed: 'Не отправлено',
    unknown: 'Результат неизвестен'
  };
  let letterId = null,
    attachments = [],
    locked = false,
    active = false,
    uploadActive = false;
  let confirmation = null;

  function status(message, error = false) {
    const target = $('#outgoing-state');
    target.textContent = message;
    target.classList.remove('hidden');
    target.classList.toggle('alert-warning', error);
  }

  function controls() {
    for (const control of form.querySelectorAll('input, textarea, button')) {
      control.disabled = locked || active || uploadActive;
    }
    $('#outgoing-files-label').classList.toggle('opacity-40', locked || active || uploadActive);
    $('#reset-outgoing').disabled = active || uploadActive;
    renderAttachments();
  }

  function renderAttachments() {
    const target = $('#outgoing-attachments');
    target.replaceChildren();
    for (const file of attachments) {
      const row = node('div', 'attachment-row flex items-center gap-3 rounded-lg border border-base-300 p-3 text-xs');
      const link = node('a', 'link link-hover flex-1 min-w-0 break-all', file.name);
      link.href = file.url;
      link.download = file.name;
      const remove = button('✕', 'btn btn-ghost btn-xs', async () => {
        attachments = attachments.filter(item => item.file_id !== file.file_id);
        confirmation = null;
        renderAttachments();
      });
      remove.setAttribute('aria-label', 'Убрать вложение: ' + file.name);
      remove.disabled = locked || active || uploadActive;
      row.append(link, node('span', 'opacity-50 shrink-0', Math.ceil(file.size / 1024) + ' КБ'), remove);
      target.append(row);
    }
  }

  function reset() {
    letterId = null;
    attachments = [];
    locked = false;
    confirmation = null;
    form.reset();
    $('#outgoing-state').classList.add('hidden');
    const url = new URL(location);
    url.searchParams.delete('outgoing');
    url.searchParams.delete('draft');
    history.replaceState(null, '', url);
    controls();
  }

  function stateText(letter) {
    if (letter.status === 'sent') return 'Почтовый сервер принял письмо. Доставка получателю пока не подтверждена.';
    if (letter.status === 'unknown') return letter.error || 'Результат неизвестен. Повтор заблокирован.';
    if (letter.status === 'sending') return 'Отправка выполняется. Обнови статус в «Исходящих», если соединение прервалось.';
    return letter.error || 'Черновик сохранён. Письмо ещё не отправлялось.';
  }

  function fill(letter) {
    letterId = letter.id;
    attachments = letter.attachments;
    form.elements.recipient.value = letter.recipient;
    form.elements.subject.value = letter.subject;
    form.elements.body.value = letter.body;
    locked = !['draft', 'failed'].includes(letter.status);
    status(stateText(letter), ['unknown', 'failed'].includes(letter.status));
    controls();
    const url = new URL(location);
    url.searchParams.set('outgoing', letter.id);
    url.searchParams.delete('draft');
    history.replaceState(null, '', url);
  }
  async function save() {
    const payload = {
      recipient: form.elements.recipient.value,
      subject: form.elements.subject.value,
      body: form.elements.body.value,
      attachment_ids: attachments.map(file => file.file_id)
    };
    const letter = await api('/outbox' + (letterId ? '/' + letterId : ''), {
      method: letterId ? 'PATCH' : 'POST',
      body: JSON.stringify(payload)
    });
    fill(letter);
    return letter;
  }
  async function perform(action) {
    if (active || uploadActive || locked) return;
    active = true;
    controls();
    try {
      await action();
    } finally {
      active = false;
      controls();
    }
  }
  async function openLetter(id) {
    if (active || uploadActive) throw new Error('Дождись завершения текущей операции.');
    const letter = await api('/outbox/' + encodeURIComponent(id));
    confirmation = null;
    fill(letter);
    await view('compose');
  }
  async function loadOutbox() {
    const items = await api('/outbox');
    const target = $('#outbox-list');
    target.replaceChildren();
    if (!items.length) return empty(target, 'Здесь будут твои письма', 'Сохрани первый черновик в разделе «Написать письмо».');
    for (const item of items) {
      const card = button('', 'mail-card card bg-base-100 border border-base-300 p-5 text-left w-full', () => openLetter(item.id));
      const head = node('div', 'flex justify-between items-start gap-3 w-full');
      head.append(node('h3', 'font-semibold text-sm break-words', item.subject), node('span', 'badge badge-sm shrink-0 ' + (item.status === 'sent' ? 'badge-success' : ['unknown', 'failed'].includes(item.status) ? 'badge-warning' : 'badge-outline'), states[item.status]));
      card.append(head, node('p', 'text-xs opacity-60 mt-2 break-all', 'Кому: ' + item.recipient), node('p', 'text-xs opacity-40 mt-3', fmt(item.updated_at) + ' · Вложений: ' + item.attachment_count));
      if (item.error) card.append(node('p', 'text-xs mt-3 text-warning leading-6', item.error));
      target.append(card);
    }
  }
  $('#reset-outgoing').onclick = () => {
    reset();
    form.elements.recipient.focus();
  };
  form.addEventListener('input', () => {
    confirmation = null;
  });
  form.onsubmit = event => {
    event.preventDefault();
    guard(() => perform(async () => {
      await save();
      toast('Исходящий черновик сохранён');
    }));
  };
  $('#compose-with-ai').onclick = () => guard(() => perform(async () => {
    const outline = $('#outgoing-outline').value.trim();
    if (!outline) throw new Error('Сначала напиши набросок для ИИ.');
    status('ИИ готовит тему и текст…');
    try {
      const result = await post('/outbox/compose', {
        outline
      });
      form.elements.subject.value = result.subject;
      form.elements.body.value = result.body;
      confirmation = null;
      status('Текст подготовлен. Проверь получателя, тему и содержание перед отправкой.');
    } catch (error) {
      status('Не удалось подготовить текст. Текущие поля редактора сохранены.', true);
      throw error;
    }
  }));
  $('#outgoing-files').onchange = () => guard(async () => {
    const files = [...$('#outgoing-files').files];
    $('#outgoing-files').value = '';
    if (!files.length || active || uploadActive || locked) return;
    if (files.length + attachments.length > 10) throw new Error('Можно приложить не более 10 файлов.');
    if (files.reduce((sum, file) => sum + file.size, 0) + attachments.reduce((sum, file) => sum + file.size, 0) > 15 * 1024 * 1024) throw new Error('Суммарный размер вложений — не больше 15 МБ.');
    uploadActive = true;
    controls();
    try {
      for (const file of files) {
        const data = new FormData();
        data.append('file', file);
        attachments.push(await api('/files/attachments', {
          method: 'POST',
          body: data
        }));
        renderAttachments();
      }
      confirmation = null;
      toast('Файлы прикреплены');
    } finally {
      uploadActive = false;
      controls();
    }
  });
  $('#review-outgoing').onclick = () => {
    if (!form.reportValidity()) return;
    guard(() => perform(async () => {
      const letter = await save();
      confirmation = {
        id: letter.id,
        content_token: letter.content_token,
        request_id: crypto.randomUUID()
      };
      $('#review-recipient').textContent = letter.recipient;
      $('#review-subject').textContent = letter.subject;
      $('#review-body').textContent = letter.body;
      $('#review-attachments').replaceChildren(...attachments.map(file => node('p', '', file.name + ' · ' + Math.ceil(file.size / 1024) + ' КБ')));
      $('#send-email-dialog').showModal();
    }));
  };
  $('#cancel-send-email').onclick = () => $('#send-email-dialog').close();
  $('#confirm-send-email').onclick = () => guard(async () => {
    if (!confirmation || active) return;
    const current = confirmation;
    const confirmButton = $('#confirm-send-email');
    active = true;
    controls();
    $('#cancel-send-email').disabled = true;
    try {
      await busy(confirmButton, async () => {
        const receipt = await post('/outbox/' + current.id + '/send', {
          request_id: current.request_id,
          confirmed: true,
          content_token: current.content_token
        });
        $('#send-email-dialog').close();
        fill(await api('/outbox/' + current.id));
        confirmation = null;
        toast(receipt.status === 'sent' ? 'Почтовый сервер принял письмо' : receipt.error || states[receipt.status], receipt.status !== 'sent');
      });
    } catch (error) {
      // Keep the request ID: retrying this same confirmation cannot send a second copy.
      if (error.status) {
        $('#send-email-dialog').close();
        confirmation = null;
        fill(await api('/outbox/' + current.id));
        status(error.message, true);
      } else {
        status('Соединение прервалось. Обнови статус в «Исходящих»; повтор этой кнопки использует тот же запрос.', true);
      }
      throw error;
    } finally {
      active = false;
      $('#cancel-send-email').disabled = false;
      controls();
    }
  });
  $('#refresh-outbox').onclick = () => guard(() => busy($('#refresh-outbox'), loadOutbox));
  document.addEventListener('inbox:view', event => {
    if (event.detail === 'outbox') guard(loadOutbox);
  });
  guard(async () => {
    const params = new URL(location).searchParams;
    if (params.get('outgoing')) await openLetter(params.get('outgoing'));
    else if (params.get('draft')) {
      const draft = await api('/drafts/' + encodeURIComponent(params.get('draft')));
      form.elements.subject.value = draft.subject;
      form.elements.body.value = draft.body;
      await view('compose');
      status('Текст черновика перенесён. Укажи получателя и проверь письмо.');
    }
    if (!$('[data-panel="outbox"]').hidden) await loadOutbox();
  });
})();