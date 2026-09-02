'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const token = $('meta[name="miniagent-token"]').content;
const activeStatuses = new Set(['running', 'awaiting_approval', 'stopping']);
const statusLabels = {running:'正在运行',awaiting_approval:'等待审批',stopping:'正在停止',finished:'已结束',blocked:'审批已拒绝',cancelled:'已停止',error:'运行出错',iteration_limit:'达到循环上限',repeated_invalid_call:'无效调用已停止'};
const welcomeMarkup = $('#feed').innerHTML;
let selectedId = '', currentView = 'timeline', currentFolder = '', previewPath = '';
let state = null, renderedSignature = '', approvalSignature = '', busy = false, polling = false;
let initial = true, toastTimer, seenRunSequence = 0;

const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const pretty = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const basename = path => path.split(/[\\/]/).filter(Boolean).pop() || 'workspace';

async function api(path, data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST',
      headers: {'X-Miniagent-Token': token, ...(data === undefined ? {} : {'Content-Type':'application/json'})},
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: controller.signal,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求失败');
    return result;
  } finally { clearTimeout(timeout); }
}

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').hidden = true, 4500);
}

function setConnection(connected) {
  $('#connection-dot').className = 'status-dot' + (connected ? '' : ' offline');
  $('#connection-label').textContent = connected ? '本地服务已连接' : '连接中断 · 正在重试';
}

async function refresh() {
  if (polling) return;
  polling = true;
  const requestedId = selectedId;
  try {
    const next = await api('/api/state?run=' + encodeURIComponent(requestedId));
    if (requestedId !== selectedId) return;
    state = next;
    setConnection(true);
    if (initial) {
      initial = false;
      if (state.active_id) { selectedId = state.active_id; setTimeout(refresh, 0); }
      refreshFiles();
    }
    renderState();
  } catch (error) {
    setConnection(false);
    $('#send-button').disabled = true;
    $('#notice').textContent = '无法连接本地服务。请确认启动终端仍在运行；服务重启后请刷新网页。';
    $('#notice').hidden = false;
  } finally { polling = false; }
}

function renderState() {
  const run = state.run;
  $('#workspace-label').textContent = state.workspace;
  $('#workspace-label').title = state.workspace;
  $('#model-label').textContent = state.model || '尚未配置模型';
  $('#model-label').title = state.model || '';
  $('#model-dot').className = 'status-dot' + (state.configured ? '' : ' waiting');
  $('#notice').hidden = state.configured;
  if (!state.configured) $('#notice').textContent = '模型尚未配置。请在项目 .env 中填写 OPENAI_API_KEY 与 OPENAI_MODEL，再重启本地服务。可以先浏览界面。';
  $('#history-count').textContent = state.runs.length;
  const history = state.runs.map(item => `<button class="history-item ${item.id === selectedId ? 'selected' : ''}" data-run="${escapeHTML(item.id)}" title="${escapeHTML(item.title)}"><span class="status-dot ${activeStatuses.has(item.status) ? 'running' : item.status === 'error' ? 'offline' : 'neutral'}"></span><span class="history-text">${escapeHTML(item.title)}</span></button>`).join('');
  if ($('#history').innerHTML !== history) $('#history').innerHTML = history || '<div class="history-empty">让第一个想法开始运行。</div>';
  $('#breadcrumb-title').textContent = run ? run.title : '新任务';
  $('#change-workspace').disabled = !!state.active_id;
  $('#send-button').disabled = busy || !!state.active_id || !state.configured;
  $('#stop-button').hidden = !state.active_id;
  $('#stop-button').disabled = busy || (run?.status === 'stopping');
  for (const id of ['mode-select','iteration-limit','save-session']) $('#' + id).disabled = !!state.active_id;
  $('#export-button').disabled = !run;
  $('#return-active').hidden = !state.active_id || selectedId === state.active_id;
  $('#run-status').innerHTML = `<span class="status-dot ${run?.status === 'awaiting_approval' ? 'waiting' : activeStatuses.has(run?.status) ? 'running' : 'neutral'}"></span>${escapeHTML(run ? (statusLabels[run.status] || run.status) : '准备就绪')}`;
  $('#log-count').textContent = run?.events.length || 0;
  renderMetrics();
  const signature = [selectedId,currentView,run?.events.at(-1)?.seq,run?.status].join(':');
  if (signature !== renderedSignature) {
    renderedSignature = signature;
    renderFeed(run);
    if (run && seenRunSequence !== run.events.at(-1)?.seq) {
      seenRunSequence = run.events.at(-1)?.seq;
      if (run.events.some(e => e.type === 'tool_end' && e.data.ok && ['write','edit'].includes(e.data.name))) refreshFiles();
    }
  }
  renderApproval(run);
}

function renderMetrics() {
  const run = state?.run;
  $('#metric-iterations').innerHTML = `${run?.iteration || 0}<span>/${run?.limit || $('#iteration-limit').value}</span>`;
  $('#metric-tools').textContent = run?.events.filter(e => e.type === 'tool_start').length || 0;
  const paths = new Set((run?.events || []).filter(e => e.type === 'tool_end' && e.data.ok && ['write','edit'].includes(e.data.name)).map(e => e.data.data?.path));
  paths.delete(undefined);
  $('#metric-files').textContent = paths.size;
  if (run) {
    const seconds = Math.max(0, Math.floor((run.ended || Date.now()/1000) - run.created));
    $('#elapsed-label').textContent = `${Math.floor(seconds/60).toString().padStart(2,'0')}:${(seconds%60).toString().padStart(2,'0')}`;
  } else $('#elapsed-label').textContent = '—';
}

function renderFeed(run) {
  const feed = $('#feed');
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
  const opened = new Set($$('details[open]', feed).map(item => item.dataset.key));
  if (!run) { feed.innerHTML = welcomeMarkup; return; }
  if (currentView === 'logs') {
    feed.innerHTML = '<pre class="raw-logs"></pre>';
    $('.raw-logs', feed).textContent = run.events.map(e => `[${new Date(e.time*1000).toLocaleTimeString()}] ${e.type}\n${pretty(e.data)}`).join('\n\n');
    return;
  }
  const results = new Map(run.events.filter(e => e.type === 'tool_end').map(e => [e.data.call_id, e]));
  let html = `<article class="message"><div class="message-header"><span class="avatar user">你</span>任务指令</div><div class="message-text">${escapeHTML(run.prompt)}</div></article>`;
  for (const event of run.events) {
    const data = event.data;
    if (event.type === 'iteration') html += `<div class="iteration-line">ROUND ${String(data.iteration).padStart(2,'0')} · 模型决策</div>`;
    if (event.type === 'tool_start') {
      const result = results.get(data.call_id)?.data;
      const target = data.arguments?.path || data.arguments?.command || '';
      const resultLabel = result ? (result.ok ? '✓ 已执行' : '× 未成功') : run.status === 'cancelled' ? '已停止' : run.status === 'awaiting_approval' ? '等待确认' : '执行中';
      html += `<details class="tool-card" data-key="${event.seq}" ${opened.has(String(event.seq)) ? 'open' : ''}><summary><span class="tool-name">${escapeHTML(data.name)}</span><span class="tool-target" title="${escapeHTML(target)}">${escapeHTML(target)}</span><span class="tool-status ${result && !result.ok ? 'error' : ''}">${resultLabel}</span></summary><div class="tool-details"><h4>调用参数</h4><pre>${escapeHTML(pretty(data.arguments))}</pre>${result ? `<h4>执行结果${result.data?.exit_code !== undefined ? ' · EXIT '+escapeHTML(result.data.exit_code) : ''}</h4><pre>${escapeHTML(pretty(result.data || result.error || '操作完成'))}</pre>${result.error ? `<p class="error-text">${escapeHTML(result.error)}</p>` : ''}` : '<p class="event-note">结果返回后会显示在这里。</p>'}</div></details>`;
    }
    if (event.type === 'approval_resolved') html += `<div class="event-note">${data.allowed ? '✓ 你已允许本次操作' : '◇ 你拒绝了本次操作，任务将停止'}</div>`;
    if (event.type === 'retry') html += `<div class="event-note">连接模型遇到问题，正在进行第 ${data.attempt} 次重试…</div>`;
  }
  if (run.result) html += `<article class="message"><div class="message-header"><span class="avatar">✦</span>Mini Agent · ${escapeHTML(statusLabels[run.status] || run.status)}</div><div class="message-text final-text">${escapeHTML(run.result)}</div><div class="result-footnote">“已结束”表示模型停止回答。请结合上方命令退出码与输出核对实际结果。</div></article>`;
  else if (run.status === 'running') html += '<div class="event-note">✦ 正在等待模型或工具返回结果…</div>';
  feed.innerHTML = html;
  if (nearBottom) feed.scrollTop = feed.scrollHeight;
}

function renderApproval(run) {
  const pending = run?.pending;
  const signature = pending?.id || '';
  const card = $('#approval-card');
  card.hidden = !pending;
  if (!pending) { approvalSignature = ''; return; }
  if (signature === approvalSignature) return;
  approvalSignature = signature;
  const description = escapeHTML(pending.description || pending.summary || '执行下方所示操作，请核对完整参数。');
  card.innerHTML = `<div class="approval-title">◇ 等待你的确认<span>${escapeHTML(pending.tool)} · 本次操作</span></div><p>${description}</p><pre id="approval-preview"></pre><details><summary class="event-note">查看完整调用参数</summary><pre id="approval-arguments"></pre></details><div class="approval-actions"><button class="button danger-outline" id="deny-button">拒绝并停止</button><button class="button primary" id="allow-button">✓ 允许本次操作</button></div>`;
  const preview = $('#approval-preview');
  if (pending.diff) {
    for (const line of pending.diff.split('\n')) {
      const span = document.createElement('span');
      span.textContent = line + '\n';
      span.className = line.startsWith('+') && !line.startsWith('+++') ? 'diff-add' : line.startsWith('-') && !line.startsWith('---') ? 'diff-remove' : '';
      preview.append(span);
    }
  } else preview.textContent = pending.arguments.command || pretty(pending.arguments);
  $('#approval-arguments').textContent = pretty(pending.arguments);
  $('#allow-button').onclick = () => approve(true);
  $('#deny-button').onclick = () => approve(false);
}

async function approve(allow) {
  const run = state?.run, pending = run?.pending;
  if (!pending || busy) return;
  busy = true;
  $$('.approval-actions button').forEach(button => button.disabled = true);
  try {
    await api('/api/approval', {run_id:run.id, approval_id:pending.id, allow});
    toast(allow ? '已允许，继续执行。' : '已拒绝，任务将停止。');
    approvalSignature = '';
    await refresh();
  } catch (error) { toast(error.message); approvalSignature = ''; }
  finally { busy = false; }
}

async function refreshFiles() {
  const folder = currentFolder;
  try {
    const response = await api('/api/files?path=' + encodeURIComponent(folder));
    if (folder !== currentFolder) return;
    $('#folder-label').textContent = (currentFolder || basename(state?.workspace || 'workspace')) + ' /';
    $('#folder-label').title = currentFolder;
    $('#parent-folder').disabled = !currentFolder;
    $('#file-list').innerHTML = response.files.map(file => `<button class="file-row" data-file="${escapeHTML(file.path)}" data-directory="${file.directory}"><span>${file.directory ? '▱' : file.name.endsWith('.py') ? 'py' : /\.(cpp|h|hpp)$/.test(file.name) ? 'C' : '▤'}</span><span>${escapeHTML(file.name)}</span></button>`).join('') || '<p class="muted">这个文件夹没有可展示的文件。</p>';
  } catch (error) { $('#file-list').textContent = error.message; }
}

async function previewFile(path) {
  try {
    const result = await api('/api/file?path=' + encodeURIComponent(path));
    previewPath = path;
    $('#preview-title').textContent = result.path;
    $('#preview-content').textContent = result.content + (result.truncated ? '\n\n… 内容过长，预览已截断。' : '');
    $('#file-dialog').showModal();
  } catch (error) { toast(error.message); }
}

async function sendTask() {
  const prompt = $('#task-input').value.trim();
  if (!prompt) { $('#task-input').focus(); return; }
  if (!state?.configured) { toast('请先配置模型并重启本地服务。'); return; }
  if (busy || state.active_id) { toast('请先完成或停止当前任务。'); return; }
  const limit = Number($('#iteration-limit').value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) { toast('最大循环次数应为 1–50。'); return; }
  busy = true;
  $('#send-button').disabled = true;
  try {
    const result = await api('/api/run', {prompt, mode:$('#mode-select').value, limit, save_session:$('#save-session').checked});
    selectedId = result.id;
    renderedSignature = '';
    $('#task-input').value = '';
    updateComposer();
    currentView = 'timeline';
    updateTabs();
    await refresh();
  } catch (error) { toast(error.message); }
  finally { busy = false; if (state) renderState(); }
}

function newTask() {
  selectedId = '';
  renderedSignature = '';
  currentView = 'timeline';
  updateTabs();
  refresh();
  $('#task-input').focus();
}

function updateComposer() { $('#character-count').textContent = `${$('#task-input').value.length.toLocaleString()} / 30,000`; }
function updateTabs() { $$('.tab').forEach(tab => {const selected = tab.dataset.view === currentView; tab.classList.toggle('selected',selected);tab.setAttribute('aria-selected',String(selected));}); }
function insertTemplate(name) {
  const templates = {
    zero:'请根据我提供的算法要求，先阅读相关代码，分析实现思路与复杂度，再完成实现并验证结果。若缺少题目描述或目标文件，请先向我确认。',
    test:'请查看项目结构与 README，运行测试并定位失败原因。进行最小必要修复，不要修改测试以掩盖问题。修复后再次运行完整测试，说明修改内容、实际验证结果和剩余限制。',
    review:'请分析当前项目的目录结构和核心逻辑，解释各模块如何协作，并指出值得改进的地方。只读取文件，不修改文件，不执行任何命令。如果需要具体文件路径，请告诉我。',
  };
  $('#task-input').value = templates[name] || '';
  if (name === 'review') { $('#mode-select').value = 'read_only'; updateMode(); }
  updateComposer();
  $('#task-input').focus();
}

function updateMode() {
  const readOnly = $('#mode-select').value === 'read_only';
  $('#composer-mode').textContent = readOnly ? '◈ 只读分析' : '◈ 逐次审批';
  $('#mode-help').textContent = readOnly ? '仅允许读取；写入、编辑与所有 Shell 命令均被拒绝。' : '写入文件与执行命令前，等待你的确认。';
}

$('#new-task').onclick = newTask;
$('#workspace-nav').onclick = () => { currentView = 'timeline'; updateTabs(); renderedSignature=''; if(state) renderState(); };
$('#send-button').onclick = sendTask;
$('#task-input').addEventListener('input', updateComposer);
$('#task-input').addEventListener('keydown', event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !event.isComposing) {event.preventDefault();sendTask();} });
document.addEventListener('keydown', event => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !document.querySelector('dialog[open]')) {event.preventDefault();newTask();} });
$('#feed').addEventListener('click', event => { const button = event.target.closest('[data-template]'); if(button) insertTemplate(button.dataset.template); });
$('#history').addEventListener('click', event => { const button = event.target.closest('[data-run]'); if(button) {selectedId=button.dataset.run;renderedSignature='';refresh();} });
$('#return-active').onclick = () => {selectedId=state.active_id;renderedSignature='';refresh();};
$$('.tab').forEach(tab => tab.onclick = () => {currentView=tab.dataset.view;updateTabs();renderedSignature='';if(state) renderState();});
$('#mode-select').onchange = updateMode;
$('#iteration-limit').onchange = renderMetrics;
$('#refresh-files').onclick = refreshFiles;
$('#parent-folder').onclick = () => {currentFolder=currentFolder.split('/').slice(0,-1).join('/');refreshFiles();};
$('#file-list').onclick = event => {const row=event.target.closest('[data-file]');if(row){if(row.dataset.directory==='true'){currentFolder=row.dataset.file;refreshFiles();}else previewFile(row.dataset.file);}};
$('#mention-file').onclick = () => {$('#task-input').value += `${$('#task-input').value ? '\n' : ''}请关注文件：${previewPath}`;updateComposer();$('#file-dialog').close();$('#task-input').focus();};
$('#change-workspace').onclick = () => {$('#workspace-input').value=state?.workspace || '';$('#workspace-error').textContent='';$('#workspace-dialog').showModal();};
$('#workspace-form').onsubmit = async event => {
  event.preventDefault();
  const button = $('#workspace-form button[type=submit]'); button.disabled=true;
  try {await api('/api/workspace',{path:$('#workspace-input').value.trim()});currentFolder='';selectedId='';renderedSignature='';$('#workspace-dialog').close();await refresh();await refreshFiles();toast('工作区已切换。');}
  catch(error){$('#workspace-error').textContent=error.message;}
  finally{button.disabled=false;}
};
$('#help-button').onclick = () => $('#help-dialog').showModal();
$('#toggle-inspector').onclick = () => {
  const open = document.body.classList.toggle('inspector-open');
  $('#toggle-inspector').setAttribute('aria-expanded', String(open));
};
document.addEventListener('keydown', event => {
  if(event.key === 'Escape') {document.body.classList.remove('inspector-open');$('#toggle-inspector').setAttribute('aria-expanded','false');}
});
$$('.close-dialog').forEach(button => button.onclick = () => button.closest('dialog').close());
$$('dialog').forEach(dialog => dialog.addEventListener('click', event => {if(event.target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();}}));
$('#stop-button').onclick = async () => {
  if(!state?.active_id||busy)return;
  busy=true;$('#stop-button').disabled=true;
  try{await api('/api/cancel',{run_id:state.active_id});toast('已请求停止；已完成的文件修改不会自动撤销。');await refresh();}
  catch(error){toast(error.message);}
  finally{busy=false;}
};
$('#export-button').onclick = () => {
  if(!state?.run)return;
  const blob=new Blob([JSON.stringify(state.run,null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`miniagent-${state.run.id.slice(0,8)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('运行记录已导出，请勿公开包含私人代码的记录。');
};

refresh();
setInterval(refresh, 1000);
setInterval(renderMetrics, 1000);
document.addEventListener('visibilitychange', () => {if(!document.hidden)refresh();});
