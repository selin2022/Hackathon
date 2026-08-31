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
let storageEphemeral = false;

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
  storageEphemeral = data.storage_persistent === false;
  let badge = data.mode_badge || '';
  if (storageEphemeral) badge += ' · 저장 항목은 이 서버 인스턴스에서만 유지됩니다';
  $('mode-badge').textContent = badge;

  const select = $('employee-no');
  clear(select);
  (data.users || []).forEach((u) => {
    const role = u.role === 'hr_admin' ? '인사담당자' : '직원';
    const option = el('option', null,
      `${u.employee_no} — ${u.display_name} (${u.dept} · ${role})`);
    option.value = u.employee_no;
    select.appendChild(option);
  });

  if (data.demo_password_hint) {
    const hint = $('password-hint');
    hint.textContent = `비밀번호 초기값은 ${data.demo_password_hint} 입니다.`;
    hint.classList.remove('hidden');
  }
}

async function login(event) {
  event.preventDefault();
  const errorBox = $('login-error');
  errorBox.classList.add('hidden');

  const { status, data } = await api('/api/session', {
    method: 'POST',
    body: JSON.stringify({
      employee_no: $('employee-no').value,
      password: $('password').value,
    }),
  });

  if (status !== 200) {
    errorBox.textContent = data.message || '로그인에 실패했습니다.';
    errorBox.classList.remove('hidden');
    return;
  }

  $('password').value = '';
  currentUser = data.user;
  $('login-view').classList.add('hidden');
  $('main-view').classList.remove('hidden');
  $('user-label').textContent =
    `${currentUser.display_name} (${currentUser.dept} · ${currentUser.employee_no_masked})`;

  if (storageEphemeral) {
    ['storage-note-todo', 'storage-note-saved'].forEach((id) => {
      const note = $(id);
      note.textContent = '이 배포 환경에서는 저장 항목이 서버 인스턴스 수명 동안만 유지됩니다.';
      note.classList.remove('hidden');
    });
  }

  renderQuick();
  clear($('messages'));
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

/* 답변 본문을 그린다. 대화 화면과 저장한 답변 화면이 같은 함수를 쓴다. */
function buildAnswerBody(answer, options) {
  const opts = options || {};
  const frag = document.createDocumentFragment();

  if (answer.summary) {
    frag.appendChild(section('한 줄 요약', (box) => {
      box.appendChild(el('div', 'summary', answer.summary));
    }));
  }
  if (answer.personalization_basis) {
    frag.appendChild(el('div', 'basis', answer.personalization_basis));
  }

  if (answer.actions && answer.actions.length) {
    frag.appendChild(section('해야 할 일', (box) => {
      const ol = el('ol', 'action-list');
      answer.actions.forEach((a) => {
        const li = el('li');
        const row = el('div', 'action-row');
        row.appendChild(el('span', 'action-text', a));
        if (opts.allowSave) {
          row.appendChild(button('할 일로 담기', 'chip-btn', () => saveTodo(a, answer)));
        }
        li.appendChild(row);
        ol.appendChild(li);
      });
      box.appendChild(ol);
    }));
  }

  if (answer.citations && answer.citations.length) {
    frag.appendChild(section('참고 문서', (box) => {
      answer.citations.forEach((c) => {
        const card = el('div', 'citation');
        const title = el('div', 'cite-title', `${c.title} v${c.version}`);
        if (c.demo_assumption) title.appendChild(el('span', 'badge', '데모용 가정'));
        card.appendChild(title);
        const sections = (c.sections || [c.section_path] || []).join(' / ');
        card.appendChild(el('div', 'cite-meta',
          `${c.doc_id} · ${c.published_at} · 관련 구간: ${sections}`));
        if (c.excerpt) card.appendChild(el('div', 'excerpt', c.excerpt));
        box.appendChild(card);
      });
    }));
  }

  if (answer.cautions && answer.cautions.length) {
    frag.appendChild(section('주의 · 예외', (box) => {
      const ul = el('ul');
      answer.cautions.forEach((c) => ul.appendChild(el('li', null, c)));
      box.appendChild(ul);
    }));
  }

  (answer.notices || []).forEach((n) => frag.appendChild(el('div', 'notice', n)));

  if (answer.contact) {
    frag.appendChild(section('담당 부서', (box) => {
      const c = answer.contact;
      let line = `${c.dept || '인사팀'} ${c.person || ''} (${c.email || ''})`;
      if (c.is_demo) line += ' — 데모용 가상 정보';
      box.appendChild(el('div', 'contact', line));
      if (answer.contact_message) box.appendChild(el('div', 'contact', answer.contact_message));
    }));
  }
  return frag;
}

function renderAnswer(answer, meta) {
  const wrap = el('div', 'msg bot');
  const bubble = el('div', 'bubble');
  bubble.appendChild(buildAnswerBody(answer, { allowSave: true }));

  if (answer.citations && answer.citations.length) {
    const actions = el('div', 'msg-actions');
    actions.appendChild(button('이 답변 저장', 'chip-btn', () => saveAnswer(answer)));
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
async function save(kind, payload, successMessage) {
  const { status, data } = await api('/api/storage', {
    method: 'POST', body: JSON.stringify({ kind, payload }),
  });
  if (status !== 200) { toast(data.message || '저장하지 못했습니다.'); return; }
  toast(successMessage);
  loadStorage();
}

const saveAnswer = (a) => save('saved_answers', {
  query: lastQuery,
  summary: a.summary,
  actions: a.actions,
  citations: a.citations,
  cautions: a.cautions,
  notices: a.notices,
  personalization_basis: a.personalization_basis,
}, '저장한 답변에 담았습니다.');

const saveTodo = (text, a) => {
  const cite = (a.citations && a.citations[0]) || {};
  save('checklist', { text, doc_id: cite.doc_id || '', doc_version: cite.version || '' },
       '내 할 일에 담았습니다.');
};

async function loadStorage() {
  const { data } = await api('/api/storage');
  const items = data.items || {};
  renderSavedAnswers(items.saved_answers || []);
  renderTodos(items.checklist || []);
  updateTabBadges(items.counts || {}, items.new_counts || {});
}

function updateTabBadges(counts, newCounts) {
  document.querySelectorAll('[data-badge]').forEach((node) => {
    const kind = node.dataset.badge;
    const count = counts[kind] || 0;
    const fresh = newCounts[kind] || 0;
    clear(node);
    if (!count && !fresh) { node.classList.add('hidden'); return; }
    node.classList.remove('hidden');
    if (count) node.appendChild(el('span', 'count', count));
    if (fresh) node.appendChild(el('span', 'new', 'New'));
  });
}

function emptyNote(container, text) {
  container.appendChild(el('div', 'empty', text));
}

function renderSavedAnswers(list) {
  const box = $('saved-answers');
  clear(box);
  if (!list.length) {
    return emptyNote(box, '답변 아래 "이 답변 저장"을 누르면 여기에 담깁니다.');
  }
  list.forEach((a) => {
    const card = el('div', 'card');
    const head = el('div', 'card-head');
    const titleWrap = el('div', 'grow');
    const title = el('div', 'card-title', a.query || '(질문 없음)');
    if (a.is_new) title.appendChild(el('span', 'badge new-badge', 'New'));
    titleWrap.appendChild(title);
    titleWrap.appendChild(el('div', 'card-meta', a.summary || ''));
    head.appendChild(titleWrap);

    const body = el('div', 'card-body hidden');
    const toggle = button('펼치기', 'chip-btn', () => {
      const hidden = body.classList.toggle('hidden');
      toggle.textContent = hidden ? '펼치기' : '접기';
      if (!hidden && !body.firstChild) {
        body.appendChild(buildAnswerBody(a, { allowSave: false }));
      }
    });
    head.appendChild(toggle);
    head.appendChild(button('삭제', 'link-btn danger', () => remove('saved_answers', a.id)));
    card.appendChild(head);
    if (a.stale) card.appendChild(el('div', 'stale', a.stale));
    card.appendChild(body);
    box.appendChild(card);
  });
}

function renderTodos(list) {
  const box = $('checklist');
  clear(box);
  if (!list.length) {
    return emptyNote(box, '답변의 "해야 할 일" 옆 [할 일로 담기] 버튼으로 항목을 담을 수 있습니다.');
  }
  list.forEach((item) => {
    const card = el('div', 'card' + (item.done ? ' done' : ''));
    const row = el('div', 'card-row');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!item.done;
    cb.addEventListener('change', () => toggle(item.id, cb.checked));
    row.appendChild(cb);

    const grow = el('div', 'grow');
    const title = el('div', 'card-title', item.text);
    if (item.is_new && !item.done) title.appendChild(el('span', 'badge new-badge', 'New'));
    grow.appendChild(title);
    if (item.doc_id) {
      grow.appendChild(el('div', 'card-meta', `출처: ${item.doc_id} v${item.doc_version}`));
    }
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
const TAB_KIND = { todo: 'checklist', saved: 'saved_answers' };

function setupTabs() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', async () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      document.querySelectorAll('.tab-panel').forEach((p) => p.classList.add('hidden'));
      $(`tab-${tab.dataset.tab}`).classList.remove('hidden');

      const kind = TAB_KIND[tab.dataset.tab];
      if (kind) {
        await api('/api/storage/seen', { method: 'POST', body: JSON.stringify({ kind }) });
        loadStorage();
      }
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
  $('login-form').addEventListener('submit', login);
  $('chat-form').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  loadUsers();
});
