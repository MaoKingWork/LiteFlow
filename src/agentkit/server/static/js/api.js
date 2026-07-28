/* api.js —— Server REST + SSE 客户端。
   对齐 agentkit.server 路由契约(RunEvent v1):
     workflows CRUD / validate / meta 内省 / runs 控制 / artifacts 下载。
   SSE 用 fetch + ReadableStream 实现(可携带 Authorization 头,
   支持 Last-Event-ID 续传),不依赖 EventSource。
*/

import { t } from './i18n.js';

class ApiError extends Error {
  constructor(status, detail, body) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

function token() {
  return sessionStorage.getItem('lf.token') || '';
}

export function setToken(t) {
  if (t) sessionStorage.setItem('lf.token', t);
  else sessionStorage.removeItem('lf.token');
}

function authHeaders(extra = {}) {
  const h = { ...extra };
  const t = token();
  if (t) h['Authorization'] = `Bearer ${t}`;
  return h;
}

async function request(method, url, { body, headers } = {}) {
  const resp = await fetch(url, {
    method,
    headers: authHeaders(headers),
    body,
  });
  if (resp.status === 401) {
    // 未鉴权:抛出带标记的错误,由上层弹 token 输入
    throw new ApiError(401, t('api.needToken'), null);
  }
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) {
    const detail = (data && data.detail) ? data.detail : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, detail, data);
  }
  return data;
}

const get = (url) => request('GET', url);
const post = (url, body, headers) => request('POST', url, { body, headers });
const put = (url, body, headers) => request('PUT', url, { body, headers });
const del = (url) => request('DELETE', url);

/* ── 工作流 CRUD ─────────────────────────────────────────────── */
export const listWorkflows = () => get('/api/workflows');
export const getWorkflow = (name) => get(`/api/workflows/${encodeURIComponent(name)}`);
export const deleteWorkflow = (name) => del(`/api/workflows/${encodeURIComponent(name)}`);

/** 保存工作流。doc 为 JSON 文档模型(dict),服务端渲染为 YAML。 */
export function saveWorkflow(name, doc) {
  return put(`/api/workflows/${encodeURIComponent(name)}`, JSON.stringify(doc), {
    'Content-Type': 'application/json',
  });
}

/** 以原始 YAML 文本导入/保存(保留注释与 ${ENV} 占位符)。 */
export function putWorkflowYaml(name, yamlText) {
  return put(`/api/workflows/${encodeURIComponent(name)}`, yamlText, {
    'Content-Type': 'text/yaml',
  });
}

export function validateWorkflow(doc) {
  return post('/api/workflows/validate', JSON.stringify(doc), {
    'Content-Type': 'application/json',
  });
}

/* ── 内省 ────────────────────────────────────────────────────── */
export const metaStepTypes = () => get('/api/meta/step-types');
export const metaTools = () => get('/api/meta/tools');
export const metaAgents = () => get('/api/meta/agents');

/* ── 运行控制 ────────────────────────────────────────────────── */
export function startRun(name, inputs = {}, runId = undefined) {
  return post(`/api/workflows/${encodeURIComponent(name)}/runs`,
    JSON.stringify({ inputs, run_id: runId }),
    { 'Content-Type': 'application/json' });
}
export const listRuns = (workflow) =>
  get('/api/runs' + (workflow ? `?workflow=${encodeURIComponent(workflow)}` : ''));
export const getRun = (runId) => get(`/api/runs/${encodeURIComponent(runId)}`);
export const cancelRun = (runId, mode = 'graceful') =>
  post(`/api/runs/${encodeURIComponent(runId)}/cancel?mode=${mode}`);
export const resumeRun = (runId) => post(`/api/runs/${encodeURIComponent(runId)}/resume`);

/* ── 产物 ────────────────────────────────────────────────────── */
export const listArtifacts = (runId) => get(`/api/runs/${encodeURIComponent(runId)}/artifacts`);

/** 拉取产物内容为 Blob(预览用;下载端点为 attachment,不能直接 iframe)。 */
export async function fetchArtifactBlob(runId, artifactId) {
  const resp = await fetch(`/api/artifacts/${encodeURIComponent(runId)}/${encodeURIComponent(artifactId)}`,
    { headers: authHeaders() });
  if (!resp.ok) throw new ApiError(resp.status, t('api.artifactDownloadFailed', { status: resp.status }), null);
  return resp.blob();
}

/* ── SSE 事件流(fetch + ReadableStream,支持鉴权头与续传) ─────── */
/**
 * 订阅 run 事件流。历史回放与实时推送同一入口:
 * 服务端先补齐 events.jsonl 历史,再转 live;浏览器断线自动以
 * Last-Event-ID 续传(最多 retries 次)。
 *
 * @param runId    run id
 * @param handlers { onEvent(eventObj), onOpen(), onClose(terminal:boolean) }
 * @param options  { lastEventId, retries }
 * @returns        close() 手动关闭
 */
export function openEventStream(runId, handlers, { lastEventId = 0, retries = 3 } = {}) {
  let closed = false;
  let controller = null;
  let lastId = lastEventId;
  let attempt = 0;

  const TERMINAL = new Set(['run_completed', 'run_failed', 'run_cancelled', 'run_interrupted']);
  let sawTerminal = false;

  async function connect() {
    if (closed) return;
    controller = new AbortController();
    try {
      const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`, {
        headers: authHeaders(lastId ? { 'Last-Event-ID': String(lastId) } : {}),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`SSE HTTP ${resp.status}`);
      attempt = 0;
      handlers.onOpen?.();
      await parseSSE(resp.body, (msg) => {
        if (msg.id) lastId = Number(msg.id) || lastId;
        // data = 完整 RunEvent(v/seq/run_id/type/ts/step_id/attempt/payload)
        let eventObj = {};
        try { eventObj = msg.data ? JSON.parse(msg.data) : {}; } catch { /* 忽略坏包 */ }
        if (!eventObj.type) eventObj = { type: msg.event, seq: Number(msg.id) || 0, payload: eventObj };
        if (TERMINAL.has(msg.event)) sawTerminal = true;
        handlers.onEvent?.(eventObj);
      });
      // 流自然结束
      if (!closed) {
        if (sawTerminal) handlers.onClose?.(true);
        else scheduleRetry();
      }
    } catch (e) {
      if (closed || e.name === 'AbortError') return;
      scheduleRetry();
    }
  }

  function scheduleRetry() {
    if (closed) return;
    if (attempt >= retries) { handlers.onClose?.(sawTerminal); return; }
    attempt += 1;
    setTimeout(connect, 800 * attempt);
  }

  connect();
  return () => {
    closed = true;
    controller?.abort();
  };
}

/** 逐行解析 SSE 文本流(event:/data:/id: 三字段,空行分发)。 */
async function parseSSE(body, dispatch) {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  let cur = { event: 'message', data: '', id: '' };

  const flush = () => {
    if (cur.data === '' && cur.event === 'message' && cur.id === '') return;
    dispatch({ event: cur.event, data: cur.data.replace(/\n$/, ''), id: cur.id });
    cur = { event: 'message', data: '', id: '' };
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).replace(/\r$/, '');
      buf = buf.slice(idx + 1);
      if (line === '') { flush(); continue; }
      if (line.startsWith(':')) continue; // 心跳注释
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      let val = colon < 0 ? '' : line.slice(colon + 1);
      if (val.startsWith(' ')) val = val.slice(1);
      if (field === 'event') cur.event = val;
      else if (field === 'data') cur.data += val + '\n';
      else if (field === 'id') cur.id = val;
    }
  }
  flush();
}

export { ApiError };
