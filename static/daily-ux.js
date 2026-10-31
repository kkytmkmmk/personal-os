/* Daily-use UX layer.  It deliberately keeps the local API and data model
   unchanged while making the existing screens behave as one coherent app. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  const announce = message => { const live = $('#ui-live'); if (live) live.textContent = message; };
  const visible = element => Boolean(element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
  const navigate = tab => window.personalOsNavigate?.(tab);
  const domainLabels = { finance: '資産', money: '資産', travel: '旅行', housing: '住居', relationship: '人間関係', people: '人間関係', work: '仕事', health: '健康', life: '生活', learning: '学習', hobby: '趣味', food: '食事', shopping: '買い物', technology: '技術', other: 'その他' };
  const domainLabel = domain => domainLabels[String(domain || '').toLowerCase()] || 'その他';
  window.personalOsDomainLabel = domainLabel;
  const sheetOpeners = new Map();
  let activeSheet = null;

  const focusableIn = sheet => $$('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', sheet)
    .filter(element => visible(element));

  function openSheet(id, opener) {
    const sheet = $(`#${id}`);
    if (!sheet) return;
    if (activeSheet && activeSheet !== sheet) closeSheet(activeSheet, false);
    if (opener) sheetOpeners.set(id, opener);
    sheet.hidden = false;
    sheet.classList.remove('hidden');
    document.body.classList.add('sheet-open');
    activeSheet = sheet;
    window.setTimeout(() => focusableIn(sheet)[0]?.focus(), 0);
  }

  function closeSheet(sheet, restoreFocus = true) {
    if (!sheet) return;
    sheet.hidden = true;
    sheet.classList.add('hidden');
    if (activeSheet === sheet) activeSheet = null;
    if (!$$('.ui-sheet:not([hidden])').length) document.body.classList.remove('sheet-open');
    const opener = sheetOpeners.get(sheet.id);
    if (restoreFocus && opener && document.contains(opener)) opener.focus();
  }

  function wireStaticSheets() {
    $$('[data-action="domains"]').forEach(button => button.addEventListener('click', () => openSheet('domains-sheet', button)));
    $$('[data-sheet-close]').forEach(button => button.addEventListener('click', () => closeSheet(button.closest('.ui-sheet'))));
    document.addEventListener('keydown', event => {
      if (!activeSheet) return;
      if (event.key === 'Escape') { event.preventDefault(); closeSheet(activeSheet); return; }
      if (event.key !== 'Tab') return;
      const targets = focusableIn(activeSheet);
      if (!targets.length) { event.preventDefault(); return; }
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    window.personalOsSheets = { open: openSheet, close: closeSheet };
  }

  function addDraft(field, key) {
    if (!field) return;
    const storageKey = `personal-os-draft-${key}`;
    const route = key === 'chat' ? 'chat' : key.startsWith('decision-') ? 'decisions' : key === 'ux-feedback' ? 'settings' : key === 'benchmark-import' ? 'explore' : 'home';
    const focus = key === 'chat' ? '#chat-message' : key === 'memo' ? '#record-text' : '';
    const read = () => {
      if (window.PersonalOSDraftStore) return window.PersonalOSDraftStore.read(storageKey);
      const raw = sessionStorage.getItem(storageKey); if (!raw) return null;
      try { const parsed = JSON.parse(raw); return parsed && typeof parsed.body === 'string' ? parsed : { body: raw }; }
      catch { return { body: raw }; }
    };
    const write = patch => {
      if (window.PersonalOSDraftStore) { const kind = ({ chat: 'chat', memo: 'memo', 'screenshot-context': 'image_context', 'benchmark-import': 'benchmark_import' })[key] || key; window.PersonalOSDraftStore.write(storageKey, { body: field.value, kind, route, focus, ...patch }); return; }
      const previous = read() || {};
      sessionStorage.setItem(storageKey, JSON.stringify({ version: 2, body: field.value,
        updated_at: new Date().toISOString(), save_failed: false, hidden_until: null, route, focus, ...previous, ...patch }));
    };
    const existing = read(); if (!field.value && existing?.body) field.value = existing.body;
    field.addEventListener('input', () => write({ body: field.value, updated_at: new Date().toISOString(), save_failed: false, hidden_until: null }));
    field.closest('form')?.addEventListener('submit', () => field.closest('form').dataset.draftKey = storageKey);
  }

  function clearDraftFor(form) {
    const key = form?.dataset.draftKey;
    if (key) { if (window.PersonalOSDraftStore) window.PersonalOSDraftStore.clear(key); else sessionStorage.removeItem(key); }
  }

  function markDraftFailed(form) {
    const key = form?.dataset.draftKey; if (!key) return;
    if (window.PersonalOSDraftStore) { window.PersonalOSDraftStore.markFailed(key); return; }
    const raw = sessionStorage.getItem(key); if (!raw) return;
    try {
      const draft = JSON.parse(raw); if (!draft || typeof draft.body !== 'string') return;
      draft.save_failed = true; draft.hidden_until = null; sessionStorage.setItem(key, JSON.stringify(draft));
    } catch { /* Legacy drafts have no reliable update time and are not promoted. */ }
  }

  function markStoredDraftFailed(storageKey) {
    if (window.PersonalOSDraftStore) { window.PersonalOSDraftStore.markFailed(storageKey); return; }
    const raw = sessionStorage.getItem(storageKey); if (!raw) return;
    try { const draft = JSON.parse(raw); if (draft && typeof draft.body === 'string') { draft.save_failed = true; draft.hidden_until = null; sessionStorage.setItem(storageKey, JSON.stringify(draft)); } }
    catch { /* Legacy drafts remain restorable but are not promoted. */ }
  }

  const mutationConfig = {
    'record-form': { path: '/api/ingest', method: 'POST', notice: '#record-notice', success: '保存しました。AIがバックグラウンドで整理します。' },
    'screenshot-form': { path: '/api/import/screenshot', method: 'POST', notice: '#screenshot-notice', success: '保存しました。AIがバックグラウンドで整理します。' },
    'benchmark-import-form': { path: '/api/benchmarks/import', method: 'POST', notice: '#benchmark-import-notice', success: '保存しました。' },
    'decision-outcome-form': { path: '/api/decisions/', method: 'PATCH', notice: '#decision-outcome-notice', success: '保存しました。' },
    'moneyforward-import-form': { path: '/api/integrations/moneyforward/import', method: 'POST', notice: '#moneyforward-import-notice', success: '更新データを反映しました。' },
  };

  function mutationFor(form) { return mutationConfig[form?.id] || null; }
  function setMutationState(form, state, message = '') {
    const config = mutationFor(form); if (!config) return;
    form.dataset.submitting = state === 'submitting' ? 'true' : 'false';
    form.dataset.mutationState = state;
    const submit = form.querySelector('button[type="submit"], button:not([type])');
    if (submit) {
      if (!submit.dataset.label) submit.dataset.label = submit.textContent || '保存する';
      submit.disabled = state === 'submitting';
      submit.textContent = state === 'submitting' ? '保存しています…' : submit.dataset.label;
    }
    const notice = $(config.notice);
    if (notice && message) notice.textContent = message;
  }
  function matchesMutation(form, detail) {
    const config = mutationFor(form);
    return Boolean(config && detail && detail.method === config.method
      && (config.path.endsWith('/') ? String(detail.path || '').startsWith(config.path) : detail.path === config.path));
  }

  function wireMutationState() {
    document.addEventListener('submit', event => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !mutationFor(form)) return;
      if (form.dataset.submitting === 'true') { event.preventDefault(); return; }
      setMutationState(form, 'submitting', '保存しています…');
      window.setTimeout(() => {
        if (form.dataset.submitting !== 'true') return;
        setMutationState(form, 'error', '保存できませんでした。入力内容は保持しています。再試行してください。');
      }, 45000);
    }, true);
    window.addEventListener('personal-os-api-response', event => {
      $$('#record-form,#screenshot-form,#benchmark-import-form,#decision-outcome-form').forEach(form => {
        if (form.dataset.submitting !== 'true' || !matchesMutation(form, event.detail)) return;
        if (!event.detail.ok) { markDraftFailed(form); setMutationState(form, 'error', '保存できませんでした。入力内容は保持しています。'); return; }
        setMutationState(form, 'success', mutationFor(form).success);
        clearDraftFor(form);
      });
    });
    window.addEventListener('personal-os-api-error', event => {
      if (event.detail?.path === '/api/chat') markStoredDraftFailed('personal-os-draft-chat');
      $$('#record-form,#screenshot-form,#benchmark-import-form,#decision-outcome-form').forEach(form => {
        if (form.dataset.submitting === 'true' && matchesMutation(form, event.detail)) {
          markDraftFailed(form);
          setMutationState(form, 'error', '保存できませんでした。入力内容は保持しています。');
        }
      });
    });
  }

  function unifyCapture() {
    const home = $('#home'), record = $('#record-card');
    if (!home || !record || record.dataset.dailyReady) return;
    record.dataset.dailyReady = 'true';
    $('h2', record).textContent = '覚えておいてほしいこと';
    $('.help', record).textContent = '今日あったこと、予定、買ったもの、気になったことを、そのまま残せます。AIがあとから整理します。';
    const form = $('#record-form', record), actions = $('.actions', form);
    if (actions && !$('#record-image-open')) actions.insertAdjacentHTML('afterbegin', '<button id="record-image-open" class="secondary" type="button">画像を追加</button>');
    const advanced = $('.record-advanced', record);
    if (advanced) {
      advanced.querySelectorAll('#ingest-form,#capture').forEach(form => form.closest('.card')?.remove());
      advanced.querySelector('p.help')?.remove();
      advanced.querySelector('summary').textContent = '詳細設定';
    }
    const screenshot = $('#screenshot-import');
    if (screenshot) {
      const attachment = document.createElement('details');
      attachment.id = 'record-image-details';
      attachment.className = 'record-advanced';
      attachment.innerHTML = '<summary>画像から記録する</summary>';
      attachment.append(screenshot);
      advanced?.after(attachment);
      $('#record-image-open')?.addEventListener('click', () => { attachment.open = true; attachment.scrollIntoView({ block: 'center' }); $('#screenshot-form input[type="file"]')?.focus(); });
    }
    form?.addEventListener('submit', () => {
      const notice = $('#record-notice');
      if (notice) notice.textContent = '保存しています…';
    });
    addDraft($('#record-text'), 'memo');
    addDraft($('#screenshot-form [name="context"]'), 'screenshot-context');
  }

  function positionToday() {
    const page = $('#today');
    const card = $('#today-daily-actions');
    if (!page || !card) return;
    const header = $('.page-header', page);
    if (header && header.nextElementSibling !== card) header.after(card);
    const digest = $('#today-digest'); if (digest && card.nextElementSibling !== digest) card.after(digest);
    const overview = $('#today-overview'); if (overview && (digest || card).nextElementSibling !== overview) (digest || card).after(overview);
    const cycle = $('#today-cycle-summary'); if (cycle && overview?.nextElementSibling !== cycle) overview?.after(cycle);
  }

  function prepareToday() {
    const page = $('#today');
    if (!page || $('#today-daily-actions')) { positionToday(); return; }
    const card = document.createElement('section');
    card.id = 'today-daily-actions'; card.className = 'card daily-actions-card';
    card.innerHTML = '<h2>いま、何をする？</h2><p class="help">相談するか、覚えておきたいことを記録します。</p><div class="actions"><button type="button" data-daily-action="chat">相談する</button><button type="button" class="secondary" data-daily-action="record">記録する</button></div>';
    page.prepend(card);
    $('[data-daily-action="chat"]', card).addEventListener('click', () => { navigate('chat'); window.setTimeout(() => $('#chat-message')?.focus(), 0); });
    $('[data-daily-action="record"]', card).addEventListener('click', () => { navigate('home'); window.setTimeout(() => $('#record-text')?.focus(), 0); });
    positionToday();
    new MutationObserver(positionToday).observe(page, { childList: true });
  }

  function digestEmpty() {
    return '<div class="empty-state"><p>まだ今日のダイジェストを作れる記録がありません。</p><span class="help">記録や相談を始めると、ここに今の状態が表示されます。</span><div class="actions"><button type="button" class="secondary" data-digest-record>記録する</button><button type="button" class="secondary" data-digest-chat>相談する</button></div></div>';
  }

  function digestBasis(item) {
    const basis = Array.isArray(item?.basis) ? item.basis : [];
    const first = basis[0] || {};
    return basis.length ? `<details class="digest-basis"><summary>根拠を見る</summary><p class="help">確認済みの記録・判断をもとに表示しています。</p><button type="button" class="secondary" data-digest-evidence-kind="${escapeHtml(first.kind || '')}">関連する記録を見る</button></details>` : '';
  }

  function bindDigestActions(card) {
    $$('[data-digest-record]', card).forEach(button => button.addEventListener('click', () => { navigate('home'); window.setTimeout(() => $('#record-text')?.focus(), 0); }));
    $$('[data-digest-chat]', card).forEach(button => button.addEventListener('click', () => { navigate('chat'); window.setTimeout(() => $('#chat-message')?.focus(), 0); }));
    $$('[data-digest-decision]', card).forEach(button => button.addEventListener('click', () => {
      const id = Number(button.dataset.digestDecision || 0);
      if (id && ['結果待ち', '後日評価待ち'].includes(button.dataset.digestDecisionState || '')) window.personalOsOpenDecisionReplay?.(id, button);
      else navigate('decisions');
    }));
    $$('[data-digest-evidence-kind]', card).forEach(button => button.addEventListener('click', () => navigate(button.dataset.digestEvidenceKind === 'decision' ? 'decisions' : 'home')));
    $$('[data-digest-timeline]', card).forEach(button => button.addEventListener('click', () => {
      navigate('explore');
      window.setTimeout(() => document.querySelector('[data-explore-mode="timeline"]')?.click(), 0);
    }));
    $$('[data-digest-prompt]', card).forEach(button => button.addEventListener('click', () => {
      const prompt = button.dataset.digestPrompt || '';
      navigate('chat');
      window.setTimeout(() => { const input = $('#chat-message'); if (input) { input.value = prompt; input.focus(); input.dispatchEvent(new Event('input', { bubbles: true })); } }, 0);
    }));
  }

  function refreshDailyDigest() { return window.refreshActionCenter?.(); }

  function streamlineConsultation() {
    const page = $('#chat'), result = $('#chat-result');
    if (!page || !result) return;
    const form = $('#chat-form');
    if (!form || form.dataset.consultationReady) return;
    form.dataset.consultationReady = 'true';
    const examples = document.createElement('div');
    examples.className = 'consultation-examples';
    examples.innerHTML = '<p class="help">例: 「次の旅行を決めたい」「この判断を整理したい」</p><p id="consultation-status" class="help" role="status" aria-live="polite"></p>';
    form.after(examples);
    const status = $('#consultation-status');
    const collapseEvidence = () => {
      const memories = $('#chat-memories');
      if (!memories || memories.closest('details')) return;
      const details = document.createElement('details'); details.className = 'chat-context';
      details.innerHTML = '<summary>参照した根拠を見る</summary>';
      memories.before(details); details.append(memories);
      result.querySelector('h3')?.remove();
    };
    const renderResponseContext = data => {
      window.setTimeout(() => {
        collapseEvidence();
        result.querySelector('#consultation-missing')?.remove();
        const missing = Array.isArray(data?.missing_context) ? data.missing_context.slice(0, 3) : [];
        if (missing.length) {
          const section = document.createElement('section');
          section.id = 'consultation-missing'; section.className = 'consultation-missing';
          section.innerHTML = `<h3>確認できると、より良く答えられること</h3><ul>${missing.map(item => `<li><b>${escapeHtml(item.label || '追加情報')}</b>${item.reason ? `<span>${escapeHtml(item.reason)}</span>` : ''}</li>`).join('')}</ul>`;
          const context = result.querySelector('.chat-context');
          (context || result.lastElementChild)?.before(section);
        }
        const responseType = { answer_only: '回答', recommendation: '提案', planning: '計画', decision_review: '判断の整理' }[data?.response_type] || '回答';
        if (status) status.textContent = `${responseType}を表示しました。根拠は必要なときだけ開けます。`;
        announce(`${responseType}を表示しました`);
      }, 0);
    };
    form.addEventListener('submit', () => {
      result.querySelector('#chat-recommendation-candidate')?.remove();
      result.querySelector('#consultation-missing')?.remove();
      if (status) status.textContent = '記憶と過去の判断を確認しています…';
      window.setTimeout(() => { if (form.dataset.actionState === 'submitting' && status) status.textContent = '関連する情報を選んでいます…'; }, 300);
    }, true);
    window.addEventListener('personal-os-chat-response', event => renderResponseContext(event.detail || {}));
    window.addEventListener('personal-os-api-error', event => {
      if (event.detail?.path === '/api/chat' && status) status.textContent = '相談を完了できませんでした。入力内容はそのままです。';
    });
    new MutationObserver(collapseEvidence).observe(result, { childList: true, subtree: true });
    collapseEvidence();
    addDraft($('#chat-message'), 'chat');
  }

  function outcomeSheet() {
    let sheet = $('#decision-outcome-sheet');
    if (sheet) return sheet;
    sheet = document.createElement('div');
    sheet.id = 'decision-outcome-sheet'; sheet.className = 'ui-sheet hidden'; sheet.hidden = true;
    sheet.setAttribute('role', 'dialog'); sheet.setAttribute('aria-modal', 'true');
    sheet.innerHTML = '<div class="ui-sheet-backdrop" data-sheet-close></div><section class="ui-sheet-panel"><div class="ui-sheet-grabber"></div><h2 id="decision-outcome-title">結果を記録する</h2><form id="decision-outcome-form"><label for="decision-outcome-text">どうだったか</label><textarea id="decision-outcome-text" required></textarea><label for="decision-outcome-good">良かった点（任意）</label><textarea id="decision-outcome-good"></textarea><label for="decision-outcome-next">次回に活かすこと（任意）</label><textarea id="decision-outcome-next"></textarea><label for="decision-outcome-score">満足度（任意）</label><select id="decision-outcome-score"><option value="">選ばない</option><option>良かった</option><option>微妙だった</option><option>悪かった</option></select><div class="actions"><button type="submit">保存する</button><button type="button" class="secondary" data-sheet-close>閉じる</button></div><p id="decision-outcome-notice" class="help" aria-live="polite"></p></form></section>';
    document.body.append(sheet);
    $$('[data-sheet-close]', sheet).forEach(button => button.addEventListener('click', () => closeSheet(sheet)));
    return sheet;
  }

  const replayStageLabels = {
    trigger: 'きっかけ', context: '検討した背景', options: '選択肢', recommendation: '相談で得た提案',
    decision: '決めたこと', rationale: '決めた理由', execution: '実行', result: '結果',
    later_evaluation: '後日評価', lesson: '次回に活かすこと'
  };
  const replayStatusLabels = { recorded: '記録あり', missing: '未記録', not_applicable: '対象外', inferred_candidate: '根拠付きの候補' };

  function replayStage(stage) {
    const items = Array.isArray(stage.items) && stage.items.length ? `<ul>${stage.items.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : '';
    const evidence = Array.isArray(stage.basis) && stage.basis.length
      ? `<details class="replay-evidence"><summary>根拠を見る（${stage.basis.length}件）</summary><ul>${stage.basis.map(item => `<li>${escapeHtml(item.kind === 'fact' ? (item.summary || '確認済みの関連情報') : `${item.kind} #${item.id}`)}</li>`).join('')}</ul></details>`
      : '';
    return `<article class="replay-stage replay-stage-${escapeHtml(stage.status || 'missing')}"><div class="replay-stage-head"><h3>${escapeHtml(replayStageLabels[stage.stage] || '記録')}</h3><span class="pill">${escapeHtml(replayStatusLabels[stage.status] || '未記録')}</span></div>${stage.occurred_at ? `<p class="meta">${escapeHtml(stage.occurred_at)}</p>` : ''}${stage.summary ? `<p>${escapeHtml(stage.summary)}</p>` : ''}${items}${evidence}</article>`;
  }

  function replayAction(action, decisionId) {
    if (!action) return '';
    return `<div class="replay-next-action"><h3>次にできること</h3><button type="button" data-replay-action="${escapeHtml(action.type)}" data-decision-id="${Number(decisionId)}">${escapeHtml(action.label || '判断を確認する')}</button></div>`;
  }

  async function openDecisionReplay(decisionId, opener) {
    const sheet = $('#decision-replay-sheet'), content = $('#decision-replay-content');
    if (!sheet || !content || !Number(decisionId)) return;
    content.innerHTML = '<p class="help">判断の流れを読み込んでいます…</p>';
    openSheet(sheet.id, opener || (document.activeElement instanceof HTMLElement ? document.activeElement : undefined));
    try {
      const response = await fetch(`/api/decisions/${Number(decisionId)}/replay`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || '判断を読み込めませんでした');
      const stages = Array.isArray(payload.stages) ? payload.stages : [];
      content.innerHTML = `<header class="replay-header"><p class="meta">${escapeHtml(domainLabel(payload.decision?.domain))} ・ ${escapeHtml(payload.decision?.decided_on || payload.decision?.created_at || '')}</p><h3>${escapeHtml(payload.decision?.title || '判断')}</h3>${payload.has_sensitive_content ? '<p class="help">機微情報はマスク中です。</p>' : ''}</header><div class="replay-stages">${stages.map(replayStage).join('')}</div>${replayAction(payload.next_action, payload.decision?.id)}</div><div class="actions"><button type="button" class="secondary" data-replay-consult>この判断について相談する</button></div>`;
      content.querySelector('[data-replay-consult]')?.addEventListener('click', () => {
        closeSheet(sheet); navigate('chat');
        window.setTimeout(() => { const field = $('#chat-message'); if (field) { field.value = `「${payload.decision?.title || 'この判断'}」を、次回の判断に活かせる形で整理してください。`; field.focus(); field.dispatchEvent(new Event('input', { bubbles: true })); } }, 0);
      });
      content.querySelector('[data-replay-action]')?.addEventListener('click', async event => {
        const button = event.currentTarget, type = button.dataset.replayAction, id = Number(button.dataset.decisionId);
        if (type === 'record_result') { closeSheet(sheet); window.personalOsOpenDecisionOutcome?.(id, 'result'); return; }
        if (type === 'record_evaluation') { closeSheet(sheet); window.personalOsOpenDecisionOutcome?.(id, 'evaluate'); return; }
        if (type === 'mark_executed') {
          button.disabled = true;
          const response = await fetch(`/api/decisions/${id}/execute`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: 'ユーザーが実行した' }) });
          if (!response.ok) { button.disabled = false; announce('実行済みとして記録できませんでした'); return; }
          await openDecisionReplay(id, opener); window.refreshDecisions?.(); window.refreshToday?.(); return;
        }
        closeSheet(sheet); navigate('decisions');
      });
    } catch (error) { content.innerHTML = `<p class="help">${escapeHtml(error.message || '判断を読み込めませんでした')}</p>`; }
  }

  function setupDecisionReplay() {
    window.personalOsOpenDecisionReplay = openDecisionReplay;
    document.addEventListener('click', event => {
      const button = event.target.closest('[data-decision-replay]');
      if (button) openDecisionReplay(button.dataset.decisionReplay, button);
    });
    const openFromHash = () => {
      const match = location.hash.match(/^#decisions\/replay\/(\d+)$/);
      if (!match) return;
      navigate('decisions');
      window.setTimeout(() => openDecisionReplay(Number(match[1])), 80);
    };
    window.addEventListener('hashchange', openFromHash);
    window.setTimeout(openFromHash, 100);
  }

  function setupUxFeedback() {
    const sheet = $('#ux-feedback-sheet'), form = $('#ux-feedback-form');
    if (!sheet || !form || form.dataset.ready) return;
    form.dataset.ready = 'true';
    const body = $('#ux-feedback-body', sheet), expected = $('#ux-feedback-expected', sheet);
    const draftKey = 'personal-os-draft-ux-feedback';
    const restoreDraft = () => { try {
      const saved = window.PersonalOSDraftStore?.read(draftKey) || JSON.parse(sessionStorage.getItem(draftKey) || '{}');
      if (saved?.body) body.value = saved.body;
      if (saved?.fields?.expected || saved?.expected) expected.value = saved.fields?.expected || saved.expected;
    } catch { /* ignore corrupt local draft */ } };
    restoreDraft();
    const saveDraft = () => {
      if (window.PersonalOSDraftStore) window.PersonalOSDraftStore.write(draftKey, { kind: 'ux_feedback', body: body.value, fields: { expected: expected.value }, save_failed: false, hidden_until: null, route: 'settings', focus: '#ux-feedback-body' });
      else sessionStorage.setItem(draftKey, JSON.stringify({ body: body.value, expected: expected.value }));
    };
    body.addEventListener('input', saveDraft); expected.addEventListener('input', saveDraft);
    const renderList = async () => {
      const list = $('#ux-feedback-list', sheet); if (!list) return;
      try {
        const rows = await fetch('/api/ux-feedback').then(response => response.ok ? response.json() : []);
        list.innerHTML = rows.length ? `<h3>最近の記録</h3>${rows.slice(0, 5).map(row => `<article class="feedback-item"><b>${escapeHtml({ improvement: '使いにくい', bug: '動作がおかしい', confusing: '分かりにくい', praise: '良かった' }[row.feedback_type] || '気づき')}</b><span class="pill">${escapeHtml({ low: '低', medium: '中', high: '高' }[row.severity] || '中')}</span><p>${escapeHtml(row.body)}</p><p class="meta">${escapeHtml(row.screen)} ・ ${escapeHtml(row.created_at || '')}</p></article>`).join('')}` : '<p class="help">まだ記録はありません。</p>';
      } catch { list.innerHTML = '<p class="help">記録を読み込めませんでした。</p>'; }
    };
    window.personalOsOpenFeedback = opener => { restoreDraft(); closeSheet(opener?.closest?.('.ui-sheet')); openSheet('ux-feedback-sheet', opener); renderList(); };
    document.querySelectorAll('[data-action="ux-feedback"]').forEach(button => button.addEventListener('click', () => window.personalOsOpenFeedback(button)));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = form.querySelector('button[type="submit"]'); submit.disabled = true;
      const notice = $('#ux-feedback-notice', sheet); notice.textContent = 'ローカルに記録しています…';
      try {
        const payload = Object.fromEntries(new FormData(form));
        const response = await fetch('/api/ux-feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || '記録できませんでした');
        form.reset(); if (window.PersonalOSDraftStore) window.PersonalOSDraftStore.clear(draftKey); else sessionStorage.removeItem(draftKey); notice.textContent = 'この端末のPersonal OSに記録しました。'; renderList();
      } catch (error) { if (window.PersonalOSDraftStore) window.PersonalOSDraftStore.markFailed(draftKey, { body: body.value, fields: { expected: expected.value } }); notice.textContent = error.message || '記録できませんでした。入力は保持しています。'; }
      finally { submit.disabled = false; }
    });
    $('#ux-feedback-copy', sheet)?.addEventListener('click', async () => {
      const payload = Object.fromEntries(new FormData(form));
      const text = `## Personal OS利用フィードバック\n\n- 画面: ${payload.screen}\n- 種類: ${payload.feedback_type}\n- 内容: ${payload.body || ''}\n- 期待していたこと: ${payload.expected_behavior || ''}\n- 重要度: ${payload.severity}`;
      try { await navigator.clipboard.writeText(text); $('#ux-feedback-notice', sheet).textContent = 'Markdownをコピーしました。'; }
      catch { $('#ux-feedback-notice', sheet).textContent = 'コピーできませんでした。内容は入力欄に残っています。'; }
    });
  }

  function setupProductionReadiness() {
    const settings = $('#settings');
    if (!settings || $('#production-readiness')) return;
    const card = document.createElement('section');
    card.id = 'production-readiness'; card.className = 'card production-readiness';
    card.innerHTML = '<h2>本人利用開始の確認</h2><p class="help">この端末の状態だけを確認します。会話本文や本人データを外部へ送信しません。</p><div id="production-readiness-status" class="help" aria-live="polite">状態を確認できます。</div><div class="actions"><button type="button" class="secondary" data-production-readiness-refresh>状態を更新</button><button type="button" class="secondary" data-action="ux-feedback">使い方について気づいたことを記録</button></div>';
    settings.append(card);
    const refresh = async () => {
      const status = $('#production-readiness-status', card); status.textContent = '状態を確認しています…';
      try {
        const response = await fetch('/api/health'); const health = await response.json();
        if (!response.ok) throw new Error(health.error || '状態を確認できませんでした');
        const database = health.integrity === 'ok' ? 'データベース: 正常' : 'データベース: 確認が必要';
        const backup = health.backup?.latest_at ? `最終バックアップ: ${health.backup.latest_at}` : '最終バックアップ: まだありません';
        const analysis = health.analysis?.stale_running ? '解析: 確認が必要な実行中Jobがあります' : '解析: 実行中Jobは正常です';
        status.textContent = `${database} ／ ${backup} ／ ${analysis}`;
      } catch (error) { status.textContent = error.message || '状態を確認できませんでした。'; }
    };
    card.querySelector('[data-production-readiness-refresh]')?.addEventListener('click', refresh);
    card.querySelector('[data-action="ux-feedback"]')?.addEventListener('click', event => { window.personalOsOpenFeedback?.(event.currentTarget); $('#ux-feedback-screen').value = 'settings'; });
    window.personalOsRefreshProductionReadiness = refresh;
    window.addEventListener('hashchange', () => { if (location.hash === '#settings') refresh(); });
    refresh();
  }

  function streamlineDecisions() {
    const page = $('#decisions');
    if (!page) return;
    const board = $('#cycle-board');
    if (board && board.parentElement !== page) page.append(board);
    const existing = $('#decisions-content');
    if (existing && !$('#decision-filters')) {
      const controls = document.createElement('div');
      controls.id = 'decision-filters'; controls.className = 'decision-filters';
      controls.innerHTML = '<label>領域<select id="decision-domain-filter"><option value="">すべて</option><option value="money">資産</option><option value="travel">旅行</option><option value="housing">住居</option><option value="people">人間関係</option><option value="other">その他</option></select></label><label>状態<select id="decision-state-filter"><option value="">すべて</option><option value="actionable">対応が必要</option><option value="candidate">候補</option><option value="decided">決定済み</option><option value="executed">実行済み</option><option value="result">結果待ち・振り返り</option></select></label>';
      existing.before(controls);
      $$('#decision-filters select').forEach(select => select.addEventListener('change', () => window.refreshDecisions?.()));
    }
    const openOutcome = (id, mode) => {
      const sheet = outcomeSheet(), form = $('#decision-outcome-form', sheet), title = $('#decision-outcome-title', sheet);
      title.textContent = mode === 'evaluate' ? '後日評価を記録する' : '結果を記録する';
      form.dataset.decisionId = String(id); form.dataset.mode = mode;
      const draftKey = `personal-os-draft-decision-${id}-${mode}`;
      const saved = sessionStorage.getItem(draftKey);
      if (saved) { try { const data = window.PersonalOSDraftStore?.read(draftKey) || JSON.parse(saved); const fields = data.fields || data; $('#decision-outcome-text', sheet).value = data.body || data.text || ''; $('#decision-outcome-good', sheet).value = fields.good || ''; $('#decision-outcome-next', sheet).value = fields.next || ''; $('#decision-outcome-score', sheet).value = fields.score || ''; } catch { /* ignore corrupt local draft */ } }
      form.dataset.draftKey = draftKey; openSheet(sheet.id, document.activeElement instanceof HTMLElement ? document.activeElement : undefined);
    };
    window.recordDecisionResult = id => openOutcome(id, 'result');
    window.evaluateDecision = id => openOutcome(id, 'evaluate');
    window.personalOsOpenDecisionOutcome = openOutcome;
    const sheet = outcomeSheet(), form = $('#decision-outcome-form', sheet);
    ['#decision-outcome-text','#decision-outcome-good','#decision-outcome-next','#decision-outcome-score'].forEach(selector => $(selector, sheet)?.addEventListener('input', () => {
      const key = form.dataset.draftKey || ''; if (!key) return;
      const fields = { good: $('#decision-outcome-good', sheet).value, next: $('#decision-outcome-next', sheet).value, score: $('#decision-outcome-score', sheet).value };
      if (window.PersonalOSDraftStore) window.PersonalOSDraftStore.write(key, { kind: form.dataset.mode === 'evaluate' ? 'decision_evaluation' : 'decision_result', body: $('#decision-outcome-text', sheet).value, fields, target_id: Number(form.dataset.decisionId), mode: form.dataset.mode, save_failed: false, hidden_until: null, route: 'decisions', focus: '#decision-outcome-text' });
      else sessionStorage.setItem(key, JSON.stringify({ text: $('#decision-outcome-text', sheet).value, ...fields }));
    }));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const id = form.dataset.decisionId, mode = form.dataset.mode, text = $('#decision-outcome-text', sheet).value.trim();
      const next = $('#decision-outcome-next', sheet).value.trim(), score = $('#decision-outcome-score', sheet).value;
      if (!id || !text) return;
      const value = [text, $('#decision-outcome-good', sheet).value.trim() && `良かった点: ${$('#decision-outcome-good', sheet).value.trim()}`, next && `次回: ${next}`, score && `満足度: ${score}`].filter(Boolean).join('\n');
      const payload = mode === 'evaluate' ? { later_evaluation: value, status: 'revisited' } : { result: value };
      const response = await fetch(`/api/decisions/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const notice = $('#decision-outcome-notice', sheet);
      if (!response.ok) { if (window.PersonalOSDraftStore) window.PersonalOSDraftStore.markFailed(form.dataset.draftKey || '', { body: text, fields: { good: $('#decision-outcome-good', sheet).value, next, score } }); notice.textContent = '保存できませんでした。入力内容は保持しています。'; return; }
      notice.textContent = '保存しました。'; if (window.PersonalOSDraftStore) window.PersonalOSDraftStore.clear(form.dataset.draftKey || ''); else sessionStorage.removeItem(form.dataset.draftKey || ''); form.reset(); closeSheet(sheet);
      window.refreshDecisions?.(); window.refreshToday?.(); announce('判断の結果を保存しました');
    });
    const decisionState = item => {
      if (item.later_evaluation) return '振り返り済み';
      return ({ candidate: '候補', considered: '検討中', decided: '決定済み', executed: '結果待ち', result: '後日評価待ち' }[item.decision_state] || (item.result ? '後日評価待ち' : '決定済み'));
    };
    const actionFor = item => {
      const id = Number(item.id);
      if (item.later_evaluation) return '<span class="pill">振り返り済み</span>';
      if (item.decision_state === 'candidate' || item.decision_state === 'considered') {
        let options = []; try { options = JSON.parse(item.options_json || '[]'); } catch { /* no options */ }
        const select = options.length ? `<select data-decision-option="${id}">${options.map(option => `<option value="${escapeHtml(option)}">${escapeHtml(option)}</option>`).join('')}</select>` : '';
        return `${select}<button type="button" data-decision-action="confirm" data-decision-id="${id}">この判断で決定する</button>`;
      }
      if (item.decision_state === 'decided') return `<button type="button" data-decision-action="execute" data-decision-id="${id}">実行した</button>`;
      if (item.decision_state === 'executed') return `<button type="button" data-decision-outcome="${id}" data-outcome-mode="result">結果を記録する</button>`;
      if (item.decision_state === 'result' || item.result) return `<button type="button" data-decision-outcome="${id}" data-outcome-mode="evaluate">後日評価を記録する</button>`;
      return `<button type="button" data-decision-outcome="${id}" data-outcome-mode="result">結果を記録する</button>`;
    };
    window.refreshDecisions = async () => {
      const target = $('#decisions-content');
      if (!target) return;
      try {
        const allItems = await fetch('/api/decisions').then(response => response.json());
        const domain = $('#decision-domain-filter')?.value || '', state = $('#decision-state-filter')?.value || '';
        const domainAliases = { money: ['money', 'finance'], people: ['people', 'relationship'] };
        const items = allItems.filter(item => (!domain || (domainAliases[domain] || [domain]).includes(item.domain)) && (!state || (state === 'actionable' ? ['candidate', 'considered', 'decided', 'executed', 'result'].includes(item.decision_state) && !item.later_evaluation : item.decision_state === state || (state === 'result' && Boolean(item.result)))));
        const priority = item => item.later_evaluation ? 5 : ({ executed: 0, decided: 1, considered: 2, result: 3, candidate: 4 }[item.decision_state] ?? 4);
        items.sort((left, right) => priority(left) - priority(right)
          || String(left.updated_at || left.created_at || '').localeCompare(String(right.updated_at || right.created_at || ''))
          || String(left.created_at || '').localeCompare(String(right.created_at || ''))
          || Number(left.id || 0) - Number(right.id || 0));
        target.innerHTML = items.length ? items.map(item => {
          const rationale = item.rationale || item.decision || '理由は未記録です。';
          return `<article class="cycle-card"><div class="entry-head"><div><h3>${escapeHtml(item.title || '判断')}</h3><p class="meta">${escapeHtml(decisionState(item))} · 更新 ${escapeHtml(item.updated_at || item.decided_on || item.created_at || '')}</p></div><span class="pill">${domainLabel(item.domain)}</span></div><p>${escapeHtml(rationale)}</p>${item.result ? `<p class="source">結果: ${escapeHtml(item.result)}</p>` : ''}${item.later_evaluation ? `<p class="source">後日評価: ${escapeHtml(item.later_evaluation)}</p>` : ''}<div class="cycle-actions">${actionFor(item)}<button type="button" class="secondary" data-decision-replay="${Number(item.id)}">振り返る</button></div></article>`;
        }).join('') : '<div class="empty-state"><p>条件に合う判断はありません。相談から提案を残すと、結果まで追跡できます。</p><button type="button" class="secondary" data-decision-empty-chat>相談する</button></div>';
        $$('[data-decision-outcome]', target).forEach(button => button.addEventListener('click', () => openOutcome(button.dataset.decisionOutcome, button.dataset.outcomeMode)));
        $$('[data-decision-action="confirm"]', target).forEach(button => button.addEventListener('click', async () => {
          const id = Number(button.dataset.decisionId), selected = $(`[data-decision-option="${id}"]`, target)?.value || '';
          button.disabled = true;
          const response = await fetch(`/api/decisions/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ selected_option: selected, decision_state: 'decided' }) });
          if (!response.ok) announce('判断を決定できませんでした。');
          await window.refreshDecisions(); window.refreshToday?.();
        }));
        $$('[data-decision-action="execute"]', target).forEach(button => button.addEventListener('click', async () => {
          button.disabled = true;
          const response = await fetch(`/api/decisions/${Number(button.dataset.decisionId)}/execute`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: 'ユーザーが実行した' }) });
          if (!response.ok) announce('実行済みに更新できませんでした。');
          await window.refreshDecisions(); window.refreshToday?.();
        }));
        $('[data-decision-empty-chat]', target)?.addEventListener('click', () => navigate('chat'));
      } catch { target.innerHTML = '<p class="help">判断を読み込めませんでした。再試行してください。</p>'; }
    };
    window.refreshDecisions();
  }

  function streamlineExplore() {
    const panel = $('#explore-benchmark'), firstCard = panel?.querySelector(':scope > section.card');
    const importCard = panel?.querySelectorAll(':scope > section.card')[1];
    if (!panel || !firstCard || !importCard || importCard.dataset.moved) return;
    importCard.dataset.moved = 'true';
    const open = document.createElement('button'); open.id = 'benchmark-import-open'; open.type = 'button'; open.className = 'secondary'; open.textContent = '比較データを追加する';
    $('.actions', firstCard)?.append(open);
    $('#benchmark-import-sheet-content')?.append(importCard);
    open.addEventListener('click', () => openSheet('benchmark-import-sheet', open));
    addDraft($('#benchmark-import-json'), 'benchmark-import');
  }

  function standardizeDomainViews() {
    if (window.refreshDomain?.dailyUxWrapped) return;
    const categoryAliases = { money: ['finance'], people: ['relationship'], travel: ['travel'], housing: ['housing'] };
    const labels = { money: '資産', travel: '旅行', housing: '住居', people: '人間関係' };
    const empty = (message, action = true) => `<div class="empty-state"><p>${escapeHtml(message)}</p>${action ? '<button type="button" class="secondary" data-domain-record>記録する</button>' : ''}</div>`;
    const value = fact => {
      try {
        const data = JSON.parse(fact.value_json || '{}');
        return [data.asset, data.amount !== undefined && data.amount !== null ? `${Number(data.amount).toLocaleString('ja-JP')} ${data.currency || ''}`.trim() : '', data.value].filter(Boolean).join(' / ');
      } catch { return ''; }
    };
    const factCard = (fact, compact = false) => {
      const amount = (() => { try { return JSON.parse(fact.value_json || '{}').amount ?? ''; } catch { return ''; } })();
      const evidence = Number(fact.evidence_count || 0) > 0
        ? `<p class="source">根拠あり・${Number(fact.evidence_count)}件</p>`
        : '<p class="help">確認できる根拠がまだありません。</p>';
      return `<article class="domain-fact ${fact.status === 'current' ? 'current-card' : ''}"><b>${escapeHtml(fact.summary || '記録')}</b>${compact ? '' : `<div>${escapeHtml(value(fact) || fact.fact_key || '')}</div>`}<div class="source">${fact.status === 'current' ? '現在の情報' : '過去の記録'} ・ 確からしさ ${Math.round((Number(fact.truth_confidence ?? fact.confidence) || 0) * 100)}%</div>${compact ? '' : `<div class="actions"><button type="button" class="secondary" data-domain-correct="${Number(fact.id)}" data-domain-summary="${encodeURIComponent(fact.summary || '')}" data-domain-amount="${encodeURIComponent(String(amount))}">訂正する</button></div>`}${evidence}</article>`;
    };
    const currentSummary = (domain, summary = {}) => {
      if (domain === 'money') return `<div class="summary-grid"><div><span>総資産</span><b>${summary.total_assets === null || summary.total_assets === undefined ? '未登録' : `${Number(summary.total_assets).toLocaleString('ja-JP')}円`}</b></div><div><span>月間積立</span><b>${summary.monthly_investment === null || summary.monthly_investment === undefined ? '未登録' : `${Number(summary.monthly_investment).toLocaleString('ja-JP')}円`}</b></div><div><span>構成</span><b>${Object.keys(summary.breakdown || {}).length}種</b></div><div><span>実取引</span><b>${Number(summary.transaction_count || 0)}件</b></div></div>`;
      if (domain === 'travel') return `<div class="summary-grid"><div><span>次の旅行</span><b>${escapeHtml((summary.upcoming || []).join('、') || '未定')}</b></div><div><span>訪問地</span><b>${Number((summary.visited_places || []).length)}件</b></div><div><span>行きたい場所</span><b>${Number((summary.wanted_places || []).length)}件</b></div><div><span>旅行傾向</span><b>${escapeHtml((summary.preferences || []).slice(0, 2).join(' / ') || '未登録')}</b></div></div>`;
      if (domain === 'housing') return `<div class="summary-grid"><div><span>現住居</span><b>${Number((summary.current || []).length) ? '登録済み' : '未登録'}</b></div><div><span>希望条件</span><b>${Number((summary.preferences || []).length)}件</b></div><div><span>更新時期</span><b>${escapeHtml(summary.renewal || '未登録')}</b></div><div><span>検討候補</span><b>${Number((summary.candidates || []).length)}件</b></div></div>`;
      return `<div class="summary-grid"><div><span>最近の人物</span><b>${Number(summary.people_count || 0)}人</b></div><div><span>次の予定</span><b>${Number(summary.next_plans || 0)}件</b></div><div><span>結果待ち</span><b>${Number(summary.pending_results || 0)}件</b></div><div><span>タイムライン</span><b>${Number(summary.timeline_count || 0)}件</b></div></div>`;
    };
    const moneyForwardSection = integration => {
      if (!integration) return '';
      const updateControls = '<details><summary>マネーフォワード情報を更新</summary><p class="help">このチャットで「マネーフォワードを更新して」と依頼すると最新データを反映できます。更新用JSONをお持ちの場合は、ここから取り込めます。</p><form id="moneyforward-import-form"><label for="moneyforward-update-file">更新データ（JSON）</label><input id="moneyforward-update-file" name="snapshot" type="file" accept="application/json,.json" required><button type="submit">更新データを取り込む</button><p id="moneyforward-import-notice" role="status" aria-live="polite"></p></form></details>';
      if (!integration.connected) {
        return `<section class="domain-integration"><h3>マネーフォワード連携</h3><div class="summary-card"><p class="help">${escapeHtml(integration.message || '連携状態を確認できませんでした。')}</p></div>${updateControls}</section>`;
      }
      const yen = value => `${Number(value || 0).toLocaleString('ja-JP')}円`;
      const latestFlow = (integration.monthly_cashflow || []).at(-1);
      const holdings = (integration.holdings || []).slice(0, 12).map(item => `<div class="summary-line"><span>${escapeHtml(item.name || item.category || '資産')}</span><b>${yen(item.amount)}</b></div>`).join('');
      const transactions = (integration.recent_transactions || []).slice(0, 12).map(item => `<div class="summary-line"><span>${escapeHtml(`${item.date || ''} ${item.category || item.description || '取引'}`.trim())}</span><b>${item.type === 'expense' ? '−' : item.type === 'income' ? '＋' : ''}${yen(item.amount)}</b></div>`).join('');
      return `<section class="domain-integration"><div class="entry-head"><h3>マネーフォワード連携</h3><span class="pill">読取専用</span></div><div class="summary-card"><div class="summary-grid"><div><span>残高基準日</span><b>${escapeHtml(integration.balance_date || integration.snapshot_date || integration.asset_history?.at(-1)?.date || '未取得')}</b></div><div><span>保有明細</span><b>${Number(integration.holdings_count || 0).toLocaleString('ja-JP')}件</b></div><div><span>${escapeHtml(latestFlow?.month || '')} 収入</span><b>${latestFlow ? yen(latestFlow.income) : '未取得'}</b></div><div><span>${escapeHtml(latestFlow?.month || '')} 支出</span><b>${latestFlow ? yen(latestFlow.expense) : '未取得'}</b></div></div>${integration.net_assets !== undefined ? `<p>資産（負債控除前） ${yen(integration.total_assets)} ／ 負債 ${yen(integration.liabilities)} ／ 純資産 ${yen(integration.net_assets)}</p>` : ''}<p class="source">${escapeHtml(integration.message || 'ローカルDBを読み取り専用で参照しています。')} 取得日時: ${escapeHtml(integration.last_synced_at || '不明')}</p>${integration.sync_error ? `<p role="alert">${escapeHtml(integration.sync_error)}</p>` : ''}</div>${updateControls}${holdings ? `<details><summary>主な保有明細（取得時点の最新情報）</summary>${holdings}</details>` : ''}${transactions ? `<details><summary>取得した最近の入出金</summary>${transactions}</details>` : ''}</section>`;
    };
    const wrapped = async domain => {
      const root = $(`#${domain}-content`); if (!root) return;
      root.setAttribute('aria-busy', 'true');
      try {
        const [projectionResponse, changesResponse] = await Promise.all([fetch(`/api/domains/${domain}`), fetch('/api/memory-changes')]);
        if (!projectionResponse.ok) throw new Error('domain fetch failed');
        const data = await projectionResponse.json();
        const changes = changesResponse.ok ? await changesResponse.json() : [];
        const aliases = categoryAliases[domain] || [domain];
        const recent = (changes || []).filter(change => aliases.includes(change.category)).slice(0, 3);
        const decisions = (data.decisions || []).slice(0, 8);
        const history = [...(data.history || [])];
        if (domain === 'money') history.push(...(data.transactions || []).map(transaction => ({ id: `transaction-${transaction.fact_id}`, summary: `${transaction.asset || transaction.summary || '実取引'} ${Number(transaction.normalized_amount ?? transaction.amount ?? 0).toLocaleString('ja-JP')} ${transaction.currency || ''}`, status: 'historical', confidence: transaction.confidence, evidence: transaction.evidence, document_title: transaction.document_title, extractor: '取引集計', value_json: '{}' })));
        const rootEvidence = [...(data.current || [])].filter((item, index, items) => items.findIndex(other => String(other.evidence_id || other.id) === String(item.evidence_id || item.id)) === index);
        root.innerHTML = `<section class="domain-current"><h3>現在の要約</h3><div class="summary-card">${currentSummary(domain, data.summary || {})}</div><div class="domain-grid">${(data.current || []).map(item => factCard(item)).join('') || empty('現在の情報がまだありません')}</div></section>${domain === 'money' ? moneyForwardSection(data.moneyforward) : ''}<section class="domain-recent"><h3>最近の変化</h3><div class="domain-recent-changes summary-card">${recent.length ? recent.map(change => `<div class="summary-line"><span>${escapeHtml(change.fact_summary || '記憶の更新')}</span><b>${escapeHtml(change.change_type || '更新')}</b></div>`).join('') : empty('最近の変化はまだありません', false)}</div></section><section class="domain-decisions"><h3>関連する判断</h3>${decisions.length ? decisions.map(decision => `<article class="domain-fact"><b>${escapeHtml(decision.title || decision.decision || '判断')}</b><div class="source">${domainLabel(decision.domain)} ・ ${escapeHtml(decision.status || decision.decision_state || '')}</div>${decision.result ? `<details><summary>結果を表示</summary><div class="body">${escapeHtml(decision.result)}</div></details>` : '<p class="help">結果待ちです</p>'}</article>`).join('') : empty('この領域の進行中の判断はありません', false)}</section><section class="domain-history"><h3>履歴</h3>${history.length ? history.map(item => factCard(item, true)).join('') : empty('まだ履歴はありません')}</section><section class="domain-evidence"><h3>根拠</h3>${rootEvidence.length ? rootEvidence.map(item => `<details class="technical-detail"><summary>${escapeHtml(item.summary || '記憶')} の根拠</summary><p class="source">${escapeHtml(item.document_title || '原文')} / ${escapeHtml(item.extractor || '')} ${escapeHtml(item.extractor_model || '')} / Evidence ${Number(item.evidence_count || 0)}件</p><div class="body">${escapeHtml(String(item.evidence || '確認できる根拠がまだありません。').slice(0, 1200))}</div></details>`).join('') : empty('確認できる根拠がまだありません')}</section>`;
        $$('[data-domain-record]', root).forEach(button => button.addEventListener('click', () => { navigate('home'); window.setTimeout(() => $('#record-text')?.focus(), 0); }));
        $('#moneyforward-import-form', root)?.addEventListener('submit', async event => {
          event.preventDefault();
          const form = event.currentTarget;
          if (event.defaultPrevented && form.dataset.importBusy === 'true') return;
          form.dataset.importBusy = 'true';
          setMutationState(form, 'submitting', '更新データを確認しています…');
          try {
            const file = form.elements.snapshot.files[0];
            if (!file || file.size > 8 * 1024 * 1024) throw new Error('8MB以内の更新JSONを選択してください。');
            const payload = JSON.parse(await file.text());
            const response = await fetch('/api/integrations/moneyforward/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || '更新できませんでした。');
            await wrapped('money');
            const notice = $('#moneyforward-import-notice', root);
            if (notice) { notice.closest('details').open = true; notice.textContent = result.message || '更新データを反映しました。'; }
          } catch (error) {
            setMutationState(form, 'error', error.message || '更新できませんでした。選択したファイルは保持しています。');
          } finally { form.dataset.importBusy = 'false'; }
        });
        $$('[data-domain-correct]', root).forEach(button => button.addEventListener('click', () => window.correctDomainFact?.(Number(button.dataset.domainCorrect), button.dataset.domainSummary || '', button.dataset.domainAmount || '')));
      } catch {
        root.innerHTML = `<section class="domain-current"><h3>${labels[domain] || '領域'}の情報</h3>${empty('情報を読み込めませんでした。入力内容は失われていません。')}</section>`;
      } finally { root.removeAttribute('aria-busy'); }
    };
    wrapped.dailyUxWrapped = true;
    window.personalOsRenderDomain = wrapped;
    window.refreshDomain = wrapped;
  }

  function simplifyDomainCopy() {
    const copy = {
      money: '現在の要約、最近の変化、関連する判断と履歴を確認できます。',
      travel: '現在の要約、最近の変化、関連する判断と履歴を確認できます。',
      housing: '現在の要約、最近の変化、関連する判断と履歴を確認できます。',
      people: '明示された人物との記録、予定、関連する判断を確認できます。'
    };
    Object.entries(copy).forEach(([id, text]) => { const page = $(`#${id}`); const intro = page?.querySelector(':scope > section.card > p.help'); if (intro) intro.textContent = text; });
  }

  function init() {
    wireStaticSheets();
    unifyCapture();
    prepareToday();
    refreshDailyDigest();
    streamlineConsultation();
    streamlineDecisions();
    setupDecisionReplay();
    setupUxFeedback();
    setupProductionReadiness();
    streamlineExplore();
    standardizeDomainViews();
    simplifyDomainCopy();
    wireMutationState();
    document.addEventListener('click', event => {
      const action = event.target.closest('[data-today-action]')?.dataset.todayAction;
      if (action === 'chat') navigate('chat');
      if (action === 'record') navigate('home');
    });
  }

  // Register before app.js handles the initial hash route so direct Domain
  // links never produce a legacy-only first render.
  standardizeDomainViews();
  // app.js has a few legacy DOMContentLoaded initialisers.  Defer this final
  // UX composition one task so its unified renderers remain the active ones.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => window.setTimeout(init, 0), { once: true });
  else init();
  // Legacy inline scripts can finish their own first render after the deferred
  // bundle. Re-apply the decision renderer once the document is fully ready.
  window.addEventListener('load', streamlineDecisions, { once: true });
  window.refreshTodayDigest = () => window.refreshActionCenter?.();
})();
