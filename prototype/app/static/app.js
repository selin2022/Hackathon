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
  '사내 계정은 언제 발급되나요?',
  '복지 포인트는 얼마나 지급되나요?',
  '비밀번호 재설정은 어디서 하나요?',
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

/* 아이콘. innerHTML을 쓰지 않으므로 DOM API로 <use>를 만든다. */
const SVG_NS = 'http://www.w3.org/2000/svg';
function icon(id) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#${id}`);
  svg.appendChild(use);
  return svg;
}

function iconButton(iconId, className, label, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = className;
  b.setAttribute('aria-label', label);
  b.title = label;
  b.appendChild(icon(iconId));
  b.addEventListener('click', onClick);
  return b;
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
  $('user-name').textContent = `${currentUser.display_name}님`;
  $('user-role').textContent = `${currentUser.dept} · ${currentUser.employee_no_masked}`;
  $('greeting').textContent = `${currentUser.display_name}님, 무엇을 도와드릴까요?`;

  if (storageEphemeral) {
    ['storage-note-todo', 'storage-note-saved'].forEach((id) => {
      const note = $(id);
      note.textContent = '이 배포 환경에서는 저장 항목이 서버 인스턴스 수명 동안만 유지됩니다.';
      note.classList.remove('hidden');
    });
  }

  renderQuick();
  $('chat-intro').classList.remove('hidden');
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
  QUICK_QUESTIONS.slice(0, 5).forEach((q) => {
    const b = button('', null, () => send(q));
    b.appendChild(el('span', null, q));
    b.appendChild(icon('i-arrow-ur'));
    box.appendChild(b);
  });
}

function addUserMessage(text) {
  // 첫 질문이 들어오면 시작 안내는 물러난다. 대화가 화면의 주인공이어야 한다.
  $('chat-intro').classList.add('hidden');
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

/* 답변 본문을 그린다. 대화 화면과 저장한 답변 화면이 같은 함수를 쓴다.
 *
 * 5단 구조(요약·해야 할 일·참고 문서·주의·담당)는 유지하되, 시안의 표현을 따른다.
 * 참고 문서는 문서 칩으로, 해야 할 일과 근거 발췌는 회색 보조 카드로 담는다.
 */
function buildAnswerBody(answer, options) {
  const opts = options || {};
  const frag = document.createDocumentFragment();

  if (answer.summary) {
    frag.appendChild(el('div', 'summary', answer.summary));
  }
  if (answer.personalization_basis) {
    frag.appendChild(el('div', 'basis', answer.personalization_basis));
  }

  if (answer.citations && answer.citations.length) {
    frag.appendChild(section('참고 문서', (box) => {
      const chips = el('div', 'doc-chips');
      answer.citations.forEach((c) => {
        const chip = el('div', 'doc-chip');
        chip.appendChild(icon('i-doc'));
        chip.appendChild(el('span', null, c.title));
        chips.appendChild(chip);
      });
      box.appendChild(chips);
    }));
  }

  if (answer.actions && answer.actions.length) {
    const card = el('div', 'subcard');
    card.appendChild(el('div', 'answer-label', '해야 할 일'));
    const ol = el('ol', 'action-list');
    answer.actions.forEach((a) => {
      const li = el('li');
      const row = el('div', 'action-row');
      row.appendChild(el('span', 'action-text', a));
      if (opts.allowSave) {
        row.appendChild(button('담기', 'chip-btn', () => saveTodo(a, answer)));
      }
      li.appendChild(row);
      ol.appendChild(li);
    });
    card.appendChild(ol);
    frag.appendChild(card);
  }

  // 근거 발췌 — 인용한 조문·구간의 원문. 답을 검증할 수 있어야 한다.
  (answer.citations || []).forEach((c) => {
    if (!c.excerpt) return;
    const card = el('div', 'subcard');
    const refs = c.article_refs || [];
    const where = refs.length
      ? refs.join(' / ')
      : (c.sections || [c.section_path] || []).join(' / ');
    let meta = `${c.doc_id} v${c.version} · ${refs.length ? '조문' : '관련 구간'}: ${where}`;
    if (c.authority_level && c.authority_level !== '안내문') {
      meta += ` · ${c.authority_level}`;
      if (c.effective_from) meta += ` (${c.effective_from} 시행)`;
    }
    const head = el('div', 'answer-label', c.title);
    if (c.demo_assumption) head.appendChild(el('span', 'badge', '데모용 가정'));
    card.appendChild(head);
    card.appendChild(el('div', 'cite-meta', meta));
    card.appendChild(el('div', 'excerpt', c.excerpt));
    frag.appendChild(card);
  });

  if (answer.cautions && answer.cautions.length) {
    frag.appendChild(section('주의 · 예외', (box) => {
      const ul = el('ul', 'action-list');
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

  if (meta && meta.rewritten_query) {
    bubble.appendChild(el('div', 'basis',
      `'${meta.rewritten_query}'에 대한 질문으로 이해했어요.`));
  }
  wrap.appendChild(bubble);

  // 시안의 떠 있는 원형 버튼. 답변 옆에 붙어 화면을 어지럽히지 않는다.
  if (answer.citations && answer.citations.length) {
    const actions = el('div', 'msg-actions');
    actions.appendChild(iconButton('i-bookmark', 'round-btn', '이 답변 저장',
      () => saveAnswer(answer)));
    wrap.appendChild(actions);
  }

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
    if (!count) { node.classList.add('hidden'); return; }
    node.classList.remove('hidden');
    node.appendChild(el('span', fresh ? 'count is-new' : 'count', count));
  });
}

function emptyCard(container, iconId, text) {
  const card = el('div', 'empty-card');
  card.appendChild(icon(iconId));
  card.appendChild(el('p', null, text));
  container.appendChild(card);
}

function renderSavedAnswers(list) {
  const box = $('saved-answers');
  clear(box);
  if (!list.length) {
    return emptyCard(box, 'i-bookmark', '아직 저장한 답변이 없어요.');
  }
  const card = el('div', 'row-card');
  list.forEach((a) => {
    const row = el('div', 'row');
    const grow = el('div', 'grow');
    const title = el('div', 'row-title', a.query || '(질문 없음)');
    if (a.is_new) title.appendChild(el('span', 'badge', 'New'));
    grow.appendChild(title);
    grow.appendChild(el('div', 'row-meta', a.summary || ''));
    if (a.stale) grow.appendChild(el('div', 'stale', a.stale));

    const body = el('div', 'card-body hidden');
    const openBtn = button('펼치기', 'btn-ghost', () => {
      const hidden = body.classList.toggle('hidden');
      openBtn.textContent = hidden ? '펼치기' : '접기';
      if (!hidden && !body.firstChild) {
        body.appendChild(buildAnswerBody(a, { allowSave: false }));
      }
    });
    grow.appendChild(body);

    row.appendChild(grow);
    row.appendChild(openBtn);
    row.appendChild(button('삭제', 'btn-ghost danger', () => remove('saved_answers', a.id)));
    card.appendChild(row);
  });
  box.appendChild(card);
}

function renderTodos(list) {
  const box = $('checklist');
  clear(box);
  const done = list.filter((i) => i.done).length;
  $('todo-progress').textContent = list.length
    ? `${list.length}개 중 ${done}개 완료했어요.`
    : '답변의 "해야 할 일"에서 담은 항목이에요.';
  if (!list.length) {
    return emptyCard(box, 'i-todo', '아직 담은 할 일이 없어요.');
  }
  const card = el('div', 'row-card');
  list.forEach((item) => {
    const row = el('div', 'row' + (item.done ? ' done' : ''));
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'check';
    cb.checked = !!item.done;
    cb.addEventListener('change', () => toggle(item.id, cb.checked));
    row.appendChild(cb);

    const grow = el('div', 'grow');
    const title = el('div', 'row-title', item.text);
    if (item.is_new && !item.done) title.appendChild(el('span', 'badge', 'New'));
    grow.appendChild(title);
    grow.appendChild(el('div', 'row-meta',
      item.doc_id ? `출처: ${item.doc_id} v${item.doc_version}` : '온보딩 체크리스트'));
    if (item.stale) grow.appendChild(el('div', 'stale', item.stale));
    row.appendChild(grow);
    row.appendChild(button('삭제', 'btn-ghost danger', () => remove('checklist', item.id)));
    card.appendChild(row);
  });
  box.appendChild(card);
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

/* 문서 목록은 평면 나열하지 않는다.
 *
 * 먼저 "전 직원 공통"과 "나에게만 열린 문서"로 가른다. 신규 입사자가 목록을 처음 열었을 때
 * 알아야 하는 첫 번째 사실이 그것이고, 이 서비스의 권한 통제가 화면에서 보이는 지점이기도 하다.
 * 그 안에서 카테고리별로 묶어 찾아보기 쉽게 한다. */
const SCOPE_SECTIONS = [
  { key: '공통', title: '전 직원 공통', note: '회사 구성원 누구나 볼 수 있어요.' },
  { key: '소속', title: '내 소속 부서 문서', note: '소속 부서 구성원에게만 열려 있어요.' },
  { key: '역할', title: '내 역할 전용 문서', note: '담당 역할에만 열려 있어요.' },
];

function documentRow(d) {
  const row = el('div', 'row');
  const badge = el('div', 'doc-icon');
  badge.appendChild(icon('i-doc'));
  row.appendChild(badge);

  const grow = el('div', 'grow');
  const title = el('div', 'row-title', `${d.title} v${d.version}`);
  if (d.doc_type === '규정') title.appendChild(el('span', 'badge', d.authority_level));
  if (d.demo_assumption) title.appendChild(el('span', 'badge', '데모용 가정'));
  grow.appendChild(title);
  grow.appendChild(el('div', 'row-meta',
    `${d.doc_id} · ${d.owner_dept} · 유효기간 ${d.valid_until}`));
  row.appendChild(grow);
  row.appendChild(el('span', d.scope === '공통' ? 'tag' : 'tag accent',
    d.scope === '공통' ? '열람 가능' : '나에게만 열림'));
  return row;
}

async function loadDocuments() {
  const { data } = await api('/api/documents');
  const box = $('docs');
  clear(box);
  const docs = data.documents || [];
  $('docs-count').textContent = docs.length
    ? `열람 권한이 있는 문서 ${docs.length}건이에요.`
    : '열람 권한이 있는 문서만 표시돼요.';
  if (!docs.length) {
    return emptyCard(box, 'i-doc', '열람 가능한 문서가 없어요.');
  }

  SCOPE_SECTIONS.forEach((sectionDef) => {
    const inScope = docs.filter((d) => d.scope === sectionDef.key);
    if (!inScope.length) return;

    const head = el('div', 'scope-head');
    head.appendChild(el('h2', 'scope-title', `${sectionDef.title} (${inScope.length})`));
    head.appendChild(el('div', 'scope-note', sectionDef.note));
    box.appendChild(head);

    // 카테고리 순서는 서버 정렬을 그대로 따른다.
    const byCategory = new Map();
    inScope.forEach((d) => {
      if (!byCategory.has(d.category)) byCategory.set(d.category, []);
      byCategory.get(d.category).push(d);
    });
    byCategory.forEach((items, category) => {
      box.appendChild(el('div', 'category-label', category));
      const card = el('div', 'row-card');
      items.forEach((d) => card.appendChild(documentRow(d)));
      box.appendChild(card);
    });
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
      // 탭을 바꾸면 맨 위부터 본다. 이전 탭의 스크롤 위치가 남으면 새 탭의 제목이 가려진다.
      window.scrollTo({ top: 0 });

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
