'use strict';
window.InboxChatStream = {
  async consume(response, onEvent, onActivity = () => {}) {
    if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
      throw new Error('Сервер не открыл поток ответа.');
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '', kind = 'message', data = [];
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (done) throw new Error('Соединение закрылось до сохранения полного ответа.');
        onActivity();
        buffer += decoder.decode(value, {stream: true});
        if (buffer.length > 8 * 1024 * 1024) throw new Error('Слишком большое событие потока.');
        let newline;
        while ((newline = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, newline).replace(/\r$/, '');
          buffer = buffer.slice(newline + 1);
          if (line === '') {
            if (data.length) {
              let event;
              try {event = JSON.parse(data.join('\n'));}
              catch {throw new Error('Не удалось прочитать событие потока.');}
              if (!event || typeof event !== 'object') throw new Error('Некорректное событие потока.');
              if (kind === 'error') {
                const error = new Error(event.error?.message || 'Не удалось завершить запрос.');
                error.terminal = true;
                throw error;
              }
              if (kind === 'done') {
                if (!event.result?.request_id || !['completed', 'failed'].includes(event.result.status)) {
                  throw new Error('Сервер не вернул сохранённый результат.');
                }
                return event.result;
              }
              await onEvent({...event, type: kind});
            }
            kind = 'message';
            data = [];
          } else if (line.startsWith('event:')) kind = line.slice(6).trim();
          else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
          // id and keepalive comments are deliberately ignored: reconnect uses a snapshot.
        }
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
};
