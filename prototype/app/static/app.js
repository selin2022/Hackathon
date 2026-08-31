/* 프런트엔드 (설계서 §12.1).
 *
 * 모든 동적 텍스트는 textContent로만 넣는다. innerHTML·eval·문자열 타이머·인라인 핸들러를
 * 쓰지 않는다. 문서 본문에 <script>가 섞여 들어와도 화면에는 글자로만 나타난다.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const QUICK_QUESTIONS = [
  '입사 서류로 무엇을 제출해야 하나요?',
  '필수 교육은 언제까지 들어야 하나요?',
  '사무용품은 어디에서 신청하나요?',
  '전자결재 문서가 반려되면 어떻게 하나요?',
  '올해 건강검진 대상인가요?',
  '연말정산은 어떻게 준비하나요?',
];

let currentUser = null;
let lastQuery = '';

/* --- DOM 헬퍼 (textContent 전용) --- */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function button(label, className, onClick) {
  const b = el('button', className, label);
  b.type = 'button';
  b.addEventListener('click', onClick);
  return b;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function toast(message) {
  const t = el('div', 'toast', message);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  }, options || {}));
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  return { status: res.status, data };
}

/* --- 로그인 --- */
async function loadUsers() {
  const { data } = await api('/api/users');
  let badge = data.mode_badge || '';
  if (data.storage_persistent === false) {
    badge += ' · 저장 항목은 이 서버 인스턴스에서만 유지됩니다';
  }
  $('mode-badge').textContent = badge;
  window.__storageEphemeral = data.storage_persistent === false;
  const list = $('user-list');
  clear(list);
  (data.users || []).forEach((u) => {
    const card = el('button', 'user-card');
    card.type = 'button';
    const left = el('div');
    left.appendChild(el('div', 'name', u.display_name));
    left.appendChild(el('div', 'meta',
      `${u.dept} · ${u.employee_no_masked} · 입사 ${u.hire_date}`));
    card.appendChild(left);
    card.appendChild(el('div', 'meta', u.role === 'hr_admin' ? '인사담당자' : '직원'));
    card.addEventListener('click', () => login(u.employee_no));
    list.appendChild(card);
  });
}

async function login(employeeNo) {
  const { status, data } = await api('/api/session', {
    method: 'POST', body: JSON.stringify({ employee_no: employeeNo }),
  });
  if (status !== 200) { toast(data.message || '로그인에 실패했습니다.'); return; }
  currentUser = data.user;
  $('login-view').classList.add('hidden');
  $('main-view').classList.remove('hidden');
  $('user-label').textContent =
    `${currentUser.display_name} (${currentUser.dept} · ${currentUser.employee_no_masked})`;
  renderQuick();
  clear($('messages'));
  const note = $('storage-note');
  if (note && window.__storageEphemeral) {
    note.textContent = '이 배포 환경에서는 저장 항목이 서버 인스턴스 수명 동안만 유지됩니다. '
      + '로컬 실행 시에는 파일로 영구 저장됩니다.';
    note.classList.remove('hidden');
  }
  await Promise.all([loadStorage(), loadDocuments()]);
}

async function logout() {
  await api('/api/session', { method: 'DELETE' });
  currentUser = null;
  $('main-view').classList.add('hidden');
  $('login-view').classList.remove('hidden');
}

/* --- 대화 --- */
function renderQuick() {
  const box = $('quick');
  clear(box);
  QUICK_QUESTIONS.forEach((q) => box.appendChild(button(q, null, () => send(q))));
}

function addUserMessage(text) {
  const wrap = el('div', 'msg user');
  wrap.appendChild(el('div', 'bubble', text));
  $('messages').appendChild(wrap);
}

function section(label, builder) {
  const box = el('div', 'answer-section');
  box.appendChild(el('div', 'answer-label', label));
  builder(box);
  return box;
}

function renderAnswer(answer, meta) {
  const wrap = el('div', 'msg bot');
  const bubble = el('div', 'bubble');

  if (answer.summary) {
    bubble.appendChild(section('한 줄 요약', (box) => {
      box.appendChild(el('div', 'summary', answer.summary));
    }));
  }

  if (answer.personalization_basis) {
    bubble.appendChild(el('div', 'basis', answer.personalization_basis));
  }

  if (answer.actions && answer.actions.length) {
    bubble.appendChild(section('해야 할 일', (box) => {
      const ol = el('ol');
      answer.actions.forEach((a) => {
        const li = el('li');
        li.appendChild(el('span', null, a));
        li.appendChild(button('+ 체크리스트', 'link-btn', () => saveChecklist(a, answer)));
        ol.appendChild(li);
      });
      box.appendChild(ol);
    }));
  }

  if (answer.citations && answer.citations.length) {
    bubble.appendChild(section('참고 문서', (box) => {
      answer.citations.forEach((c) => {
        const card = el('div', 'citation');
        const title = el('div', 'cite-title', `${c.title} v${c.version}`);
        if (c.demo_assumption) title.appendChild(el('span', 'badge', '데모용 가정'));
        card.appendChild(title);
        const sections = (c.sections || [c.section_path]).join(' / ');
        card.appendChild(el('div', 'cite-meta',
          `${c.doc_id} · ${c.published_at} · 관련 구간: ${sections}`));
        if (c.excerpt) card.appendChild(el('div', 'excerpt', c.excerpt));
        card.appendChild(button('북마크', 'link-btn', () => saveBookmark(c)));
        box.appendChild(card);
      });
    }));
  }

  if (answer.cautions && answer.cautions.length) {
    bubble.appendChild(section('주의 · 예외', (box) => {
      const ul = el('ul');
      answer.cautions.forEach((c) => ul.appendChild(el('li', null, c)));
      box.appendChild(ul);
    }));
  }

  (answer.notices || []).forEach((n) => bubble.appendChild(el('div', 'notice', n)));

  bubble.appendChild(section('담당 부서', (box) => {
    const c = answer.contact || {};
    let line = `${c.dept || '인사팀'} ${c.person || ''} (${c.email || ''})`;
    if (c.is_demo) line += ' — 데모용 가상 정보';
    box.appendChild(el('div', 'contact', line));
    if (answer.contact_message) box.appendChild(el('div', 'contact', answer.contact_message));
  }));

  if (answer.citations && answer.citations.length) {
    const actions = el('div', 'msg-actions');
    actions.appendChild(button('이 답변 저장', null, () => saveAnswer(answer)));
    if (meta && meta.rewritten_query) {
      actions.appendChild(el('span', 'contact',
        `'${meta.rewritten_query}'에 대한 질문으로 이해했습니다.`));
    }
    bubble.appendChild(actions);
  }

  wrap.appendChild(bubble);
  $('messages').appendChild(wrap);
  wrap.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

async function send(text) {
  const message = (text || $('chat-input').value || '').trim();
  if (!message) return;
  $('chat-input').value = '';
  lastQuery = message;
  addUserMessage(message);

  const { data } = await api('/api/chat', {
    method: 'POST', body: JSON.stringify({ message }),
  });
  if (data.answer) renderAnswer(data.answer, data.meta);
  else toast(data.message || '요청을 처리하지 못했습니다.');
}

/* --- 저장 --- */
async function save(kind, payload) {
  const { status, data } = await api('/api/storage', {
    method: 'POST', body: JSON.stringify({ kind, payload }),
  });
  if (status !== 200) { toast(data.message || '저장하지 못했습니다.'); return; }
  toast('저장했습니다.');
  loadStorage();
}

const saveBookmark = (c) => save('bookmarks', {
  doc_id: c.doc_id, title: c.title, version: c.version, section_path: c.section_path,
});

const saveAnswer = (a) => save('saved_answers', {
  query: lastQuery, summary: a.summary, actions: a.actions, citations: a.citations,
});

const saveChecklist = (text, a) => {
  const cite = (a.citations && a.citations[0]) || {};
  save('checklist', { text, doc_id: cite.doc_id || '', doc_version: cite.version || '' });
};

async function loadStorage() {
  const { data } = await api('/api/storage');
  const items = data.items || {};
  renderBookmarks(items.bookmarks || []);
  renderSavedAnswers(items.saved_answers || []);
  renderChecklist(items.checklist || []);
}

function emptyNote(container, text) {
  container.appendChild(el('div', 'empty', text));
}

function renderBookmarks(list) {
  const box = $('bookmarks');
  clear(box);
  if (!list.length) return emptyNote(box, '북마크가 없습니다.');
  list.forEach((b) => {
    const card = el('div', 'card');
    card.appendChild(el('div', 'card-title', `${b.title} v${b.version}`));
    card.appendChild(el('div', 'card-meta', `${b.doc_id} · ${b.section_path}`));
    if (b.stale) card.appendChild(el('div', 'stale', b.stale));
    card.appendChild(button('삭제', 'link-btn danger', () => remove('bookmarks', b.id)));
    box.appendChild(card);
  });
}

function renderSavedAnswers(list) {
  const box = $('saved-answers');
  clear(box);
  if (!list.length) return emptyNote(box, '저장한 답변이 없습니다.');
  list.forEach((a) => {
    const card = el('div', 'card');
    card.appendChild(el('div', 'card-title', a.query));
    card.appendChild(el('div', 'card-meta', a.summary));
    const docs = (a.citations || []).map((c) => `${c.doc_id} v${c.version}`).join(', ');
    if (docs) card.appendChild(el('div', 'card-meta', `근거: ${docs}`));
    card.appendChild(button('삭제', 'link-btn danger', () => remove('saved_answers', a.id)));
    box.appendChild(card);
  });
}

function renderChecklist(list) {
  const box = $('checklist');
  clear(box);
  if (!list.length) return emptyNote(box, '답변의 "해야 할 일" 옆 + 버튼으로 항목을 담을 수 있습니다.');
  list.forEach((item) => {
    const card = el('div', 'card' + (item.done ? ' done' : ''));
    const row = el('div', 'card-row');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!item.done;
    cb.addEventListener('change', () => toggle(item.id, cb.checked));
    row.appendChild(cb);
    const grow = el('div', 'grow');
    grow.appendChild(el('div', 'card-title', item.text));
    if (item.doc_id) grow.appendChild(el('div', 'card-meta', `출처: ${item.doc_id} v${item.doc_version}`));
    if (item.stale) grow.appendChild(el('div', 'stale', item.stale));
    row.appendChild(grow);
    row.appendChild(button('삭제', 'link-btn danger', () => remove('checklist', item.id)));
    card.appendChild(row);
    box.appendChild(card);
  });
}

async function toggle(itemId, done) {
  await api('/api/storage/checklist/toggle', {
    method: 'POST', body: JSON.stringify({ item_id: itemId, done }),
  });
  loadStorage();
}

async function remove(kind, itemId) {
  await api(`/api/storage/${kind}/${itemId}`, { method: 'DELETE' });
  loadStorage();
}

async function loadDocuments() {
  const { data } = await api('/api/documents');
  const box = $('docs');
  clear(box);
  (data.documents || []).forEach((d) => {
    const card = el('div', 'card');
    const title = el('div', 'card-title', `${d.title} v${d.version}`);
    if (d.demo_assumption) title.appendChild(el('span', 'badge', '데모용 가정'));
    card.appendChild(title);
    card.appendChild(el('div', 'card-meta',
      `${d.doc_id} · ${d.category} > ${d.subcategory} · ${d.owner_dept} · 유효기간 ${d.valid_until}`));
    box.appendChild(card);
  });
}

/* --- 탭 --- */
function setupTabs() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      document.querySelectorAll('.tab-panel').forEach((p) => p.classList.add('hidden'));
      $(`tab-${tab.dataset.tab}`).classList.remove('hidden');
      if (tab.dataset.tab !== 'chat') loadStorage();
    });
  });
  document.querySelectorAll('[data-clear]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await api(`/api/storage/${btn.dataset.clear}/all`, { method: 'DELETE' });
      loadStorage();
    });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  setupTabs();
  $('logout').addEventListener('click', logout);
  $('chat-form').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  loadUsers();
});
