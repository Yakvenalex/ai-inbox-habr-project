'use strict';

// Mail and model output are untrusted. Parse Markdown, then allow only document markup.
(() => {
  let parser;
  const escapeHtml = text => String(text).replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[character]);

  function render(target, text) {
    target.classList.add('markdown-body');
    // The app remains usable if a CDN is unavailable; never insert unsanitized HTML.
    if (!window.marked || !window.DOMPurify) {
      target.textContent = text;
      target.classList.add('markdown-fallback');
      return;
    }
    parser ||= new marked.Marked({
      gfm: true,
      breaks: true,
      async: false,
      renderer: {
        html: ({
          text
        }) => escapeHtml(text),
        // Remote images in summaries must not trigger tracking requests.
        image: ({
          text
        }) => escapeHtml(text || 'Изображение')
      }
    });
    const fragment = DOMPurify.sanitize(parser.parse(text), {
      ALLOWED_TAGS: [
        'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 's',
        'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'hr',
        'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'
      ],
      ALLOWED_ATTR: ['href', 'title', 'start', 'class'],
      ALLOW_DATA_ATTR: false,
      ALLOW_ARIA_ATTR: false,
      RETURN_DOM_FRAGMENT: true
    });
    for (const link of fragment.querySelectorAll('a')) {
      const href = link.getAttribute('href') || '';
      if (!/^(https?:\/\/|mailto:|\/(?!\/)|#)/i.test(href)) {
        link.removeAttribute('href');
      } else if (/^https?:\/\//i.test(href)) {
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
      }
    }
    target.classList.remove('markdown-fallback');
    target.replaceChildren(fragment);
  }

  function preview(text, limit = 240) {
    const container = document.createElement('div');
    render(container, text);
    for (const lineBreak of container.querySelectorAll('br')) {
      lineBreak.replaceWith(document.createTextNode(' '));
    }
    const plainText = container.textContent.replace(/\s+/g, ' ').trim();
    return plainText.length > limit ? plainText.slice(0, limit).trimEnd() + '…' : plainText;
  }

  window.InboxMarkdown = {
    render,
    preview
  };
})();