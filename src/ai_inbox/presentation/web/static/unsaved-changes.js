'use strict';
// Snapshots live only in this tab's memory. No mail content is stored in browser storage.
(() => {
  const trackers = new Set();
  let navigationApproved = false, pendingPrompt = null;
  function showDiscardDialog() {
    if (pendingPrompt) return pendingPrompt;
    let dialog = document.querySelector('#unsaved-dialog');
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.id = 'unsaved-dialog';
      dialog.className = 'modal';
      dialog.setAttribute('aria-labelledby', 'unsaved-heading');
      dialog.innerHTML = '<div class="modal-box max-w-md"><h2 id="unsaved-heading" class="text-xl font-semibold">Есть несохранённые изменения</h2><p class="text-sm opacity-60 mt-4 leading-6">Изменения письма ещё не сохранены. Если продолжить, они будут потеряны.</p><div class="modal-action"><button data-stay class="btn btn-ghost" type="button" autofocus>Остаться</button><button data-discard class="btn btn-error" type="button">Продолжить без сохранения</button></div></div>';
      document.body.append(dialog);
    }
    pendingPrompt = new Promise(resolve => {
      const finish = accepted => {
        dialog.close();
        pendingPrompt = null;
        resolve(accepted);
      };
      dialog.querySelector('[data-stay]').onclick = () => finish(false);
      dialog.querySelector('[data-discard]').onclick = () => finish(true);
      dialog.oncancel = event => {event.preventDefault(); finish(false);};
      dialog.showModal();
      dialog.querySelector('[data-stay]').focus();
    });
    return pendingPrompt;
  }
  const api = {
    track(read, onChange = () => {}) {
      let saved = JSON.stringify(read());
      const tracker = {
        snapshot: () => JSON.stringify(read()),
        dirty: () => tracker.snapshot() !== saved,
        check: () => onChange(tracker.dirty()),
        saved(snapshot = tracker.snapshot()) {saved = snapshot; tracker.check();}
      };
      trackers.add(tracker);
      return tracker;
    },
    hasChanges: () => [...trackers].some(tracker => tracker.dirty()),
    ask: showDiscardDialog,
    confirmLeave: async () => !api.hasChanges() || await api.ask(),
    allowNavigation: () => {navigationApproved = true;}
  };
  window.InboxUnsaved = api;
  window.addEventListener('beforeunload', event => {
    if (navigationApproved) {navigationApproved = false; return;}
    if (!api.hasChanges()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  document.addEventListener('click', async event => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download') || link.target === '_blank' || link.getAttribute('href').startsWith('#')) return;
    if (!['http:', 'https:'].includes(link.protocol)) return;
    if (!api.hasChanges()) return;
    event.preventDefault();
    if (await api.confirmLeave()) {api.allowNavigation(); window.location.assign(link.href);}
  });
})();
