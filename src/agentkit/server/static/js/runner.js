/* runner.js —— 运行面板:启动 / 中断 / 恢复 + 统一事件渲染器。
   对齐 visualization-design §5.2「历史 = 实时」:渲染器只消费事件流,
   看历史 run 即回放 events.jsonl(经 SSE 端点),看实时即 live SSE,
   二者同一渲染路径。

   事件驱动更新:画布节点着色(step_status) / LLM 流式视图(llm_*) /
   时间线(全事件) / 产物到达(转发 artifacts 模块)。
*/

import { el, replaceChildren, fmtTime, fmtBytes, truncate, toast } from './util.js';
import { emit } from './store.js';
import * as api from './api.js';

const TERMINAL_STATUS = new Set(['completed', 'failed', 'cancelled', 'interrupted']);

export function createRunner({ paneEl, canvasApi, getDocModel, getWorkflowName, onNeedSave }) {
  /* ── 模块状态 ─────────────────────────────────────────────── */
  let visible = false;
  let pollTimer = null;
  let closeStream = null;
  let activeRunId = null;
  let activeStatus = null;      // running / completed / ...
  let stepStatus = new Map();   // stepId -> {status, duration_ms, token_usage, retry_count, error}
  let streams = new Map();      // stepId -> {text, reasoning, attempt, live, agent, model}
  let focusedStep = null;
  let inputsCache = new Map();  // workflowName -> {inputName: value}

  /* DOM 骨架(一次性构建) */
  const controlsEl = el('div.lf-form-sec');
  const inputsEl = el('div.lf-form-sec');
  const runListEl = el('div.lf-form-sec');
  const streamEl = el('div.lf-form-sec');
  const timelineEl = el('div.lf-form-sec');
  replaceChildren(paneEl, controlsEl, inputsEl, streamEl, runListEl, timelineEl);

  /* ── 可见性与轮询 ─────────────────────────────────────────── */
  function setVisible(v) {
    visible = v;
    clearInterval(pollTimer);
    if (v) {
      refreshRuns();
      pollTimer = setInterval(() => { if (!activeIsLive()) refreshRuns(); }, 5000);
    }
  }

  const activeIsLive = () => activeRunId && !TERMINAL_STATUS.has(activeStatus);

  /* ── 控制区 ───────────────────────────────────────────────── */
  function renderControls() {
    const running = activeIsLive();
    replaceChildren(controlsEl,
      el('div.lf-form-sec-head', {}, '运行控制',
        activeStatus ? el('span', { class: `lf-status-pill s-${activeStatus}` }, activeStatus) : null),
      el('div.lf-run-controls', {},
        btn('▶ 运行', 'lf-btn lf-btn-run', start, { title: '以当前保存的工作流启动 run' }),
        btn('中断', 'lf-btn', () => cancel('graceful'), { disabled: !running, title: '协作式取消:当前 step 完成后停止' }),
        btn('硬中断', 'lf-btn lf-btn-danger', () => cancel('immediate'), { disabled: !running, title: '立即取消任务(asyncio.Task.cancel)' }),
        btn('恢复', 'lf-btn', resume, {
          disabled: !(activeRunId && ['interrupted', 'failed', 'cancelled'].includes(activeStatus)),
          title: '从 checkpoint 断点续跑(跳过已完成 step)',
        }),
      ),
      activeRunId ? el('div.lf-hint', {}, `run: ${activeRunId}`) : null,
    );
  }

  function btn(text, cls, onclick, opts = {}) {
    const b = el(`button.${cls.replace(/ /g, '.')}`, { onclick, title: opts.title || '' }, text);
    if (opts.disabled) b.setAttribute('disabled', 'disabled');
    return b;
  }

  function renderInputsForm() {
    const docModel = getDocModel();
    const names = docModel?.inputs || [];
    const wfName = getWorkflowName();
    const cache = inputsCache.get(wfName) || {};
    inputsCache.set(wfName, cache);
    replaceChildren(inputsEl,
      el('div.lf-form-sec-head', {}, '输入变量'),
      names.length ? names.map((n) =>
        el('div.lf-field', {},
          el('div.lf-label', {}, el('span.mono', {}, n)),
          el('input.lf-input.mono', {
            type: 'text', value: cache[n] ?? '',
            placeholder: `输入 ${n} 的值`,
            oninput: (e) => { cache[n] = e.target.value; },
          }),
        ),
      ) : el('div.lf-hint', {}, '该工作流未声明 inputs'),
    );
  }

  /* ── run 列表 ─────────────────────────────────────────────── */
  async function refreshRuns() {
    try {
      const data = await api.listRuns(getWorkflowName());
      renderRunList(data.runs || []);
    } catch (e) {
      if (e.status !== 401) renderRunList([]);
    }
  }

  function renderRunList(runs) {
    replaceChildren(runListEl,
      el('div.lf-form-sec-head', {}, '运行历史',
        el('button.lf-icon-btn', { title: '刷新', onclick: refreshRuns }, '⟳')),
      runs.length ? el('div.lf-run-list', {},
        runs.slice(0, 30).map((r) =>
          el(`div.lf-run-item${r.run_id === activeRunId ? '.is-active' : ''}`, {
            onclick: () => view(r.run_id),
          },
            el('div.lf-run-item-top', {},
              el('span.lf-run-item-id', {}, r.run_id),
              el('span', { class: `lf-status-pill s-${r.status}` }, r.status),
            ),
            el('div.lf-run-item-time', {},
              `${fmtTime(r.started_at)}${r.is_active ? ' · live' : ''}${r.error ? ' · ' + truncate(r.error, 40) : ''}`),
          ),
        ),
      ) : el('div.lf-empty-hint', {}, '暂无运行记录'),
    );
  }

  /* ── 运行操作 ─────────────────────────────────────────────── */
  async function start() {
    if (onNeedSave && !(await onNeedSave())) return;
    const wfName = getWorkflowName();
    const inputs = { ...(inputsCache.get(wfName) || {}) };
    try {
      const { run_id } = await api.startRun(wfName, inputs);
      toast(`已启动 ${run_id}`, 'ok');
      view(run_id);
    } catch (e) {
      toast(`启动失败: ${e.message}`, 'err');
    }
  }

  async function cancel(mode) {
    if (!activeRunId) return;
    try {
      await api.cancelRun(activeRunId, mode);
      toast(mode === 'graceful' ? '已请求协作式中断' : '已硬中断', 'ok');
    } catch (e) {
      toast(`中断失败: ${e.message}`, 'err');
    }
  }

  async function resume() {
    if (!activeRunId) return;
    try {
      const { run_id } = await api.resumeRun(activeRunId);
      toast(`已恢复为 ${run_id}`, 'ok');
      view(run_id);
    } catch (e) {
      toast(`恢复失败: ${e.message}`, 'err');
    }
  }

  /** 查看指定 run:实时与历史同一入口(回放→live 由服务端衔接)。 */
  function view(runId) {
    closeStream?.();
    activeRunId = runId;
    activeStatus = 'running';
    stepStatus = new Map();
    streams = new Map();
    focusedStep = null;
    canvasApi.applyRunStatus(stepStatus);
    replaceChildren(timelineEl, el('div.lf-form-sec-head', {}, '事件流'));
    renderControls();
    renderStreamView();
    emit('run:view', { runId });

    closeStream = api.openEventStream(runId, {
      onEvent: handleEvent,
      onClose: () => { refreshRuns(); },
    });
  }

  /* ── 统一事件渲染器(实时 / 历史同一路径) ─────────────────── */
  function handleEvent(ev) {
    const { type, step_id: sid, payload = {}, attempt } = ev;
    switch (type) {
      case 'run_started':
        activeStatus = 'running';
        renderControls();
        addTimeline(ev, `工作流 ${payload.workflow_name || ''} 开始`);
        break;
      case 'run_completed':
      case 'run_failed':
      case 'run_cancelled':
      case 'run_interrupted':
        activeStatus = (payload.status || type.replace('run_', ''));
        renderControls();
        addTimeline(ev, payload.error ? `错误: ${truncate(payload.error, 80)}` : `运行${activeStatus}`, type === 'run_failed');
        refreshRuns();
        break;
      case 'run_cancelling':
        activeStatus = 'cancelling';
        renderControls();
        addTimeline(ev, '取消中…');
        break;
      case 'step_started':
        stepStatus.set(sid, { status: 'running' });
        canvasApi.applyRunStatus(stepStatus);
        addTimeline(ev, sid);
        break;
      case 'step_finished': {
        const st = payload.status === 'success' ? 'success' : (payload.status || 'success');
        stepStatus.set(sid, {
          status: st,
          duration_ms: payload.duration_ms,
          token_usage: payload.token_usage,
          retry_count: payload.retry_count,
          error: payload.error,
        });
        canvasApi.applyRunStatus(stepStatus);
        addTimeline(ev, `${sid}${payload.duration_ms != null ? ` · ${Math.round(payload.duration_ms)}ms` : ''}${payload.error ? ' · ' + truncate(payload.error, 60) : ''}`, st === 'failed');
        break;
      }
      case 'llm_stream_start':
        streams.set(sid, { text: '', reasoning: '', attempt: attempt ?? 0, live: true, agent: payload.agent_name, model: payload.model });
        focusedStep = sid; // 自动跟随最新流式 step
        renderStreamView();
        break;
      case 'llm_delta': {
        let s = streams.get(sid);
        if (!s) { s = { text: '', reasoning: '', attempt: attempt ?? 0, live: true }; streams.set(sid, s); }
        if (attempt != null && attempt !== s.attempt) { s.text = ''; s.reasoning = ''; s.attempt = attempt; } // attempt 重置缓冲
        if (payload.delta_reasoning) s.reasoning += payload.delta_reasoning;
        if (payload.delta) s.text += payload.delta;
        scheduleStreamPaint();
        break;
      }
      case 'llm_stream_end': {
        const s = streams.get(sid);
        if (s) { s.live = false; scheduleStreamPaint(); }
        break;
      }
      case 'tool_call':
        addTimeline(ev, `${payload.name}${payload.mcp_server ? '@' + payload.mcp_server : ''} → ${payload.status}`, payload.status === 'error');
        break;
      case 'artifact_produced':
        addTimeline(ev, `产物: ${payload.id} (${fmtBytes(payload.size)})`);
        emit('artifact:new', { runId: activeRunId, artifact: payload });
        break;
    }
  }

  /* ── 时间线 ───────────────────────────────────────────────── */
  let timelineBox = null;
  function addTimeline(ev, msg, isErr = false) {
    if (!timelineBox || !timelineBox.isConnected) {
      timelineBox = el('div.lf-timeline');
      replaceChildren(timelineEl, el('div.lf-form-sec-head', {}, '事件流'), timelineBox);
    }
    const item = el(`div.lf-ev.t-${ev.type}${isErr ? '.is-err' : ''}`, {},
      el('span.lf-ev-seq', {}, String(ev.seq ?? '')),
      el('span.lf-ev-type', {}, ev.type),
      el('span.lf-ev-msg', { title: msg }, msg),
    );
    timelineBox.appendChild(item);
    while (timelineBox.childElementCount > 300) timelineBox.firstElementChild.remove();
    item.scrollIntoView({ block: 'nearest' });
  }

  /* ── LLM 流式视图 ─────────────────────────────────────────── */
  let paintScheduled = false;
  function scheduleStreamPaint() {
    if (paintScheduled) return;
    paintScheduled = true;
    requestAnimationFrame(() => { paintScheduled = false; paintStream(); });
  }

  function renderStreamView() {
    replaceChildren(streamEl,
      el('div.lf-form-sec-head', {}, 'LLM 流式输出',
        streams.size > 1 ? el('select.lf-select', {
          style: 'width:140px;height:22px;font-size:11px',
          onchange: (e) => { focusedStep = e.target.value; paintStream(); },
        },
          [...streams.keys()].map((sid) =>
            el('option', { value: sid, ...(sid === focusedStep ? { selected: 'selected' } : {}) }, sid)),
        ) : null),
      el('div.lf-stream-box', {},
        el('div.lf-stream-head', { dataset: { role: 'head' } }),
        el('div.lf-stream-body', { dataset: { role: 'body' } },
          el('span.lf-hint', {}, '运行 LLM 节点后此处显示流式输出')),
      ),
    );
  }

  function paintStream() {
    const head = streamEl.querySelector('[data-role="head"]');
    const body = streamEl.querySelector('[data-role="body"]');
    if (!body) return;
    const s = focusedStep ? streams.get(focusedStep) : null;
    if (!s) return;
    replaceChildren(head,
      el('span.mono', {}, focusedStep),
      s.agent ? el('span', {}, `agent: ${s.agent}`) : null,
      s.model ? el('span', {}, s.model) : null,
      s.live ? el('span', { style: 'color:var(--accent)' }, '● 流式中') : el('span', { style: 'color:var(--ok)' }, '✓ 完成'),
      el('span.mono', {}, `${s.text.length} chars`),
    );
    replaceChildren(body,
      s.reasoning ? el('div.lf-stream-reasoning', {}, s.reasoning) : null,
      document.createTextNode(s.text),
      s.live ? el('span.cursor') : null,
    );
    body.scrollTop = body.scrollHeight;
  }

  /* ── 对外 ─────────────────────────────────────────────────── */
  function activate() { setVisible(true); }
  function deactivate() { setVisible(false); }

  renderControls();
  renderInputsForm();
  renderStreamView();
  renderRunList([]);

  return {
    activate, deactivate, view, refreshRuns, start,
    /** 工作流切换时重置输入表单与列表。 */
    reset() {
      closeStream?.();
      activeRunId = null; activeStatus = null;
      stepStatus = new Map(); streams = new Map(); focusedStep = null;
      canvasApi.applyRunStatus(stepStatus);
      renderControls(); renderInputsForm(); renderStreamView(); renderRunList([]);
    },
  };
}
