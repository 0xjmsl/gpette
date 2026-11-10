// --- State ---
let socket = null;
let connected = false;
let conversationId = null;
let senderInboxId = null;
let serverUrl = null;

// --- DOM refs ---
const $messages    = document.getElementById('messages');
const $msgInput    = document.getElementById('msgInput');
const $statusDot   = document.getElementById('statusDot');
const $statusText  = document.getElementById('statusText');
const $connectBtn  = document.getElementById('connectBtn');
const $authBanner  = document.getElementById('authBanner');

// --- Connection ---

function toggleConnection() {
  if (connected) disconnect();
  else connect();
}

function connect() {
  serverUrl      = document.getElementById('serverUrl').value.trim();
  conversationId = document.getElementById('conversationId').value.trim();
  senderInboxId  = document.getElementById('senderInboxId').value.trim();

  if (!serverUrl || !conversationId || !senderInboxId) {
    setStatus('error', 'Fill in all fields');
    return;
  }

  setStatus('connecting', 'Connecting...');
  $connectBtn.disabled = true;

  socket = io(serverUrl, { transports: ['websocket', 'polling'] });

  socket.on('connect', () => {
    connected = true;
    setStatus('connected', 'Connected');
    $connectBtn.textContent = 'Disconnect';
    $connectBtn.disabled = false;
    loadHistory();
    checkAuth();
  });

  socket.on('disconnect', () => {
    connected = false;
    setStatus('error', 'Disconnected');
    $connectBtn.textContent = 'Connect';
    $connectBtn.disabled = false;
  });

  socket.on('connect_error', (err) => {
    setStatus('error', 'Connection failed');
    $connectBtn.disabled = false;
  });

  // Listen for assistant responses
  socket.on('outgoing_message', (data) => {
    if (data.conversationId !== conversationId) return;
    // Render tool calls if present
    if (data.tool_calls) {
      renderToolCalls(data.tool_calls);
    }
    // Render content
    if (data.content) {
      renderMessage('assistant', data.content);
    }
  });
}

function disconnect() {
  if (socket) { socket.disconnect(); socket = null; }
  connected = false;
  setStatus('disconnected', 'Disconnected');
  $connectBtn.textContent = 'Connect';
  $connectBtn.disabled = false;
  $authBanner.style.display = 'none';
}

function setStatus(state, text) {
  $statusDot.className = 'status-dot';
  if (state === 'connected') $statusDot.classList.add('connected');
  else if (state === 'error') $statusDot.classList.add('error');
  $statusText.textContent = text;
}

// --- History ---

function loadHistory() {
  fetch(`${serverUrl}/conversations/${conversationId}/history?limit=100`)
    .then(r => r.json())
    .then(data => {
      $messages.innerHTML = '';
      if (!data.success) return;
      data.messages.forEach(msg => {
        const role = msg.role || 'user';

        // Skip tool-return messages (context is already shown via the tool_call)
        if (role === 'tool') return;

        // Skip empty assistant messages (fake tool_call carriers from proactive system)
        const content = msg.content;
        if (role === 'assistant' && msg.tool_calls) {
          const isEmpty = !content || (typeof content === 'object' && !content.value);
          if (isEmpty) {
            renderToolCalls(msg.tool_calls);
            return;
          }
        }

        // Render tool calls from history
        if (msg.tool_calls) {
          renderToolCalls(msg.tool_calls);
        }
        // Content from DB is {type, value} dict
        if (content) renderMessage(role, content);
      });
      scrollToBottom();
    })
    .catch(err => console.error('History load error:', err));
}

// --- Auth check ---

function checkAuth() {
  fetch(`${serverUrl}/authenticated_inboxes`)
    .then(r => r.json())
    .then(data => {
      if (!data.success) return;
      const found = data.inboxes.some(i => i.inbox_id === senderInboxId);
      $authBanner.style.display = found ? 'none' : 'block';
    })
    .catch(() => {});
}

// --- Send ---

function sendMessage() {
  const text = $msgInput.value.trim();
  if (!text || !connected) return;

  // Render optimistically (server doesn't echo user messages back)
  renderMessage('user', text);
  scrollToBottom();
  $msgInput.value = '';

  socket.emit('incoming_message', {
    conversationId: conversationId,
    senderInboxId: senderInboxId,
    content: text,
    role: 'user',
    timestamp: Date.now()
  });
}

// --- Rendering ---

function renderMessage(role, content) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;

  // Normalize content to string
  let text;
  if (typeof content === 'object' && content !== null) {
    if (content.type === 'attachment') {
      text = content.value || `[Attachment: ${content.filename || 'file'}]`;
    } else {
      text = content.value || JSON.stringify(content);
    }
  } else {
    text = String(content);
  }

  div.textContent = text;
  $messages.appendChild(div);
  scrollToBottom();
}

function renderToolCalls(toolCalls) {
  if (!toolCalls || !toolCalls.length) return;
  toolCalls.forEach(tc => {
    const fn = tc.function || {};
    const div = document.createElement('div');
    div.className = 'msg assistant';
    let argsDisplay;
    try {
      argsDisplay = JSON.stringify(JSON.parse(fn.arguments || '{}'), null, 2);
    } catch {
      argsDisplay = fn.arguments || '{}';
    }
    div.innerHTML = `<details><summary>Tool: ${fn.name || 'unknown'}</summary><pre>${escapeHtml(argsDisplay)}</pre></details>`;
    $messages.appendChild(div);
  });
}

function escapeHtml(str) {
  const el = document.createElement('span');
  el.textContent = str;
  return el.innerHTML;
}

function scrollToBottom() {
  $messages.scrollTop = $messages.scrollHeight;
}

// --- Tabs ---

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById(`tab-${name}`).classList.add('active');
  // Activate the correct tab button
  document.querySelectorAll('.tab-btn').forEach(b => {
    if (b.textContent.toLowerCase() === name) b.classList.add('active');
  });
  if (name === 'settings') loadSettings();
  if (name === 'timers') loadTimers();
}

// --- Timers ---

let timerInterval = null;

function loadTimers() {
  const url = serverUrl || document.getElementById('serverUrl').value.trim();
  const convId = conversationId || document.getElementById('conversationId').value.trim();
  const $list = document.getElementById('timersList');

  if (!url || !convId) {
    $list.innerHTML = '<p class="timers-empty">Connect to a conversation to see timers.</p>';
    return;
  }

  fetch(`${url}/api/timers/${convId}`)
    .then(r => r.json())
    .then(data => {
      if (!data.success) {
        $list.innerHTML = '<p class="timers-empty">Error loading timers.</p>';
        return;
      }
      renderTimers(data.timers);
      startTimerCountdown();
    })
    .catch(err => {
      console.error('Timer load error:', err);
      $list.innerHTML = '<p class="timers-empty">Could not reach server.</p>';
    });
}

function renderTimers(timers) {
  const $list = document.getElementById('timersList');

  if (!timers || !timers.length) {
    $list.innerHTML = '<p class="timers-empty">No active timers.</p>';
    return;
  }

  $list.innerHTML = '';
  const now = Math.floor(Date.now() / 1000);

  timers.forEach(t => {
    const fireAt = t.fire_at || 0;
    const remaining = fireAt - now;
    const isOverdue = remaining <= 0;
    const fireDate = new Date(fireAt * 1000);
    const isRecurring = t.timer_type === 'recurring';

    const card = document.createElement('div');
    card.className = `timer-card${isOverdue ? ' overdue' : ''}`;
    card.dataset.fireAt = fireAt;
    card.dataset.timerId = t.timer_id;

    card.innerHTML = `
      <div class="timer-card-top">
        <div>
          <div class="timer-desc">${escapeHtml(t.description || 'Timer')}</div>
          <div class="timer-id">${t.timer_id}</div>
        </div>
        <span class="timer-badge${isRecurring ? ' recurring' : ''}">${isRecurring ? 'recurring' : 'one-shot'}</span>
      </div>
      <div class="timer-meta">
        <span class="timer-meta-label">Fires at</span>
        <span class="timer-meta-value">${formatDateTime(fireDate)}</span>
        <span class="timer-meta-label">Remaining</span>
        <span class="timer-meta-value countdown${isOverdue ? ' overdue' : ''}" data-countdown="${fireAt}">
          ${formatRemaining(remaining)}
        </span>
        ${isRecurring ? `
        <span class="timer-meta-label">Interval</span>
        <span class="timer-meta-value">${formatInterval(t.interval_seconds)}</span>
        ` : ''}
        <span class="timer-meta-label">Created</span>
        <span class="timer-meta-value">${formatDateTime(new Date(t.created_at * 1000))}</span>
      </div>
      <div class="timer-instructions">${escapeHtml(t.instructions || 'No instructions')}</div>
      <div class="timer-actions">
        <button class="btn-cancel" onclick="cancelTimerUI('${t.timer_id}')">Cancel</button>
      </div>
    `;

    $list.appendChild(card);
  });
}

function formatDateTime(date) {
  const h = String(date.getHours()).padStart(2, '0');
  const m = String(date.getMinutes()).padStart(2, '0');
  const mon = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${h}:${m} — ${date.getFullYear()}-${mon}-${d}`;
}

function formatRemaining(seconds) {
  if (seconds <= 0) return 'overdue';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatInterval(seconds) {
  if (!seconds) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0 && m > 0) return `${h}h ${m}m`;
  if (h > 0) return `${h}h`;
  return `${m}m`;
}

function startTimerCountdown() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    const now = Math.floor(Date.now() / 1000);
    document.querySelectorAll('[data-countdown]').forEach(el => {
      const fireAt = parseInt(el.dataset.countdown);
      const remaining = fireAt - now;
      el.textContent = formatRemaining(remaining);
      if (remaining <= 0) {
        el.classList.add('overdue');
        el.closest('.timer-card')?.classList.add('overdue');
      }
    });
  }, 1000);
}

function cancelTimerUI(timerId) {
  const url = serverUrl || document.getElementById('serverUrl').value.trim();
  const convId = conversationId || document.getElementById('conversationId').value.trim();
  if (!url || !convId) return;

  fetch(`${url}/api/timers/${convId}/cancel/${timerId}`, { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        const card = document.querySelector(`.timer-card[data-timer-id="${timerId}"]`);
        if (card) card.remove();
        // Check if list is now empty
        const $list = document.getElementById('timersList');
        if (!$list.querySelector('.timer-card')) {
          $list.innerHTML = '<p class="timers-empty">No active timers.</p>';
        }
      } else {
        alert(data.error || 'Failed to cancel timer');
      }
    })
    .catch(err => {
      console.error('Cancel error:', err);
      alert('Network error');
    });
}

// --- Settings ---

const SETTING_LABELS = {
  LLM_SERVICE: 'LLM Service',
  LLM_TEMPERATURE: 'Temperature',
  LLM_MAX_TOKENS: 'Max Tokens',
  LLM_REQUEST_TIMEOUT: 'Request Timeout (s)',
  OLLAMA_HOST: 'Ollama Host',
  LM_STUDIO_HOST: 'LM Studio Host',
  COMPACTION_PROVIDER: 'Compaction Model',
  COMPACTION_AFTER: 'Fold After (messages)',
  COMPACTION_KEEP: 'Keep Word for Word',
  COMPACTION_SUMMARY_TOKENS: 'Summary Tokens',
  PROACTIVE_CHECK_INTERVAL: 'Check Interval (s)',
  PROACTIVE_DEFAULT_TIMER: 'Default Timer (s)',
  PROACTIVE_QUIET_START: 'Quiet Start (h)',
  PROACTIVE_QUIET_END: 'Quiet End (h)',
};

const SELECT_OPTIONS = {
  LLM_SERVICE: ['gemini', 'openai', 'claude', 'lmstudio', 'ollama'],
  COMPACTION_PROVIDER: ['same', 'lm_studio', 'claude', 'openai', 'gemini', 'ollama', 'disabled'],
};

function loadSettings() {
  const url = serverUrl || document.getElementById('serverUrl').value.trim();
  if (!url) return;

  fetch(`${url}/api/config`)
    .then(r => r.json())
    .then(data => {
      if (!data.success) return;
      const grid = document.getElementById('settingsGrid');
      grid.innerHTML = '';
      const cfg = data.config;
      for (const key of Object.keys(SETTING_LABELS)) {
        if (!(key in cfg)) continue;
        const label = document.createElement('label');
        label.textContent = SETTING_LABELS[key];
        label.setAttribute('for', `cfg-${key}`);

        let input;
        if (SELECT_OPTIONS[key]) {
          input = document.createElement('select');
          SELECT_OPTIONS[key].forEach(opt => {
            const o = document.createElement('option');
            o.value = opt;
            o.textContent = opt;
            if (String(cfg[key]) === opt) o.selected = true;
            input.appendChild(o);
          });
        } else if (typeof cfg[key] === 'boolean') {
          input = document.createElement('select');
          ['true', 'false'].forEach(v => {
            const o = document.createElement('option');
            o.value = v;
            o.textContent = v;
            if (String(cfg[key]) === v) o.selected = true;
            input.appendChild(o);
          });
        } else {
          input = document.createElement('input');
          input.type = typeof cfg[key] === 'number' ? 'number' : 'text';
          input.value = cfg[key];
          if (key === 'LLM_TEMPERATURE') { input.step = '0.1'; input.min = '0'; input.max = '2'; }
        }
        input.id = `cfg-${key}`;
        input.dataset.key = key;

        grid.appendChild(label);
        grid.appendChild(input);
      }
    })
    .catch(err => console.error('Settings load error:', err));
}

function saveSettings() {
  const url = serverUrl || document.getElementById('serverUrl').value.trim();
  if (!url) return;

  const payload = {};
  document.querySelectorAll('#settingsGrid [data-key]').forEach(el => {
    payload[el.dataset.key] = el.value;
  });

  const $status = document.getElementById('saveStatus');
  $status.textContent = 'Saving...';
  $status.style.color = '#aaa';

  fetch(`${url}/api/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        $status.textContent = `Saved: ${data.updated.join(', ')}`;
        $status.style.color = '#4caf50';
        if (data.errors.length) {
          $status.textContent += ` | Errors: ${data.errors.join(', ')}`;
          $status.style.color = '#e94560';
        }
      } else {
        $status.textContent = data.error || 'Save failed';
        $status.style.color = '#e94560';
      }
    })
    .catch(err => {
      $status.textContent = 'Network error';
      $status.style.color = '#e94560';
    });
}
