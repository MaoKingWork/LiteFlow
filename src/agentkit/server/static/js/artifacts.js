/* artifacts.js —— 产物面板:清单 / 预览 / 下载。
   数据源:GET /api/runs/{run_id}/artifacts(历史) + 'artifact:new' 事件(实时)。
   预览经 fetch Blob 本地渲染(下载端点为 attachment,不能直接 iframe),
   HTML 用 sandbox iframe 隔离。
*/

import { el, replaceChildren, fmtBytes, fmtTime, truncate, openModal, download, toast } from './util.js';
import { on } from './store.js';
import * as api from './api.js';

/* content_type → 预览方式 */
function previewKind(ct = '') {
  if (ct.includes('html')) return 'html';
  if (ct.startsWith('image/')) return 'image';
  if (ct.includes('json')) return 'json';
  if (ct.startsWith('text/') || ct.includes('markdown')) return 'text';
  if (ct.includes('pdf')) return 'pdf';
  return 'binary';
}

const KIND_ICON = { html: '🌐', image: '🖼', json: '⧉', text: '📄', pdf: '📕', binary: '📦' };

export function createArtifacts({ paneEl }) {
  let runId = null;
  let items = []; // ArtifactRef dicts

  on('run:view', ({ runId: rid }) => loadRun(rid));
  on('artifact:new', ({ runId: rid, artifact }) => {
    if (rid !== runId) return;
    items.push(artifact);
    render();
  });

  async function loadRun(rid) {
    runId = rid;
    items = [];
    render(); // 先清空,显示加载中
    try {
      const data = await api.listArtifacts(rid);
      items = data.artifacts || [];
    } catch { /* run 可能无事件日志 */ }
    render();
  }

  function render() {
    replaceChildren(paneEl,
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, `产物${runId ? ` · ${runId}` : ''}`,
          runId ? el('button.lf-icon-btn', { title: '刷新', onclick: () => loadRun(runId) }, '⟳') : null),
        !runId ? el('div.lf-empty-hint', {}, '在「运行」页选择一个 run 查看产物')
          : items.length === 0 ? el('div.lf-empty-hint', {}, '该 run 暂无产物')
          : el('div.lf-art-list', {}, items.map(renderItem)),
      ),
    );
  }

  function renderItem(a) {
    const kind = previewKind(a.content_type);
    return el('div.lf-art-item', {},
      el('span.lf-art-icon', {}, KIND_ICON[kind]),
      el('div.lf-art-body', {},
        el('div.lf-art-name', { title: a.id }, a.id),
        el('div.lf-art-meta', {},
          `${a.step_id || '?'} · ${a.content_type || '?'} · ${fmtBytes(a.size)} · md5 ${truncate(a.md5, 8)}${a.ts ? ' · ' + fmtTime(a.ts) : ''}`),
        a.summary ? el('div.lf-art-meta', { title: a.summary }, truncate(a.summary, 60)) : null,
      ),
      el('button.lf-btn', { onclick: () => preview(a) }, '预览'),
      el('button.lf-btn', { onclick: () => downloadArtifact(a), title: '下载' }, '⬇'),
    );
  }

  /* ── 预览 ─────────────────────────────────────────────────── */
  async function preview(a) {
    const kind = previewKind(a.content_type);
    try {
      const blob = await api.fetchArtifactBlob(runId, a.id);
      const typed = blob.type ? blob : new Blob([blob], { type: a.content_type || 'application/octet-stream' });
      const actions = [el('button.lf-btn', {
        onclick: () => download(a.id, typed, typed.type),
      }, '⬇ 下载')];

      if (kind === 'html' || kind === 'pdf' || kind === 'image') {
        const url = URL.createObjectURL(typed);
        const body = kind === 'image'
          ? el('img', { src: url, alt: a.id })
          : el('iframe', { src: url, sandbox: 'allow-scripts', title: a.id });
        openModal({
          title: a.id, body, actions,
          onClose: () => URL.revokeObjectURL(url),
        });
      } else if (kind === 'json' || kind === 'text') {
        const text = await typed.text();
        let pretty = text;
        if (kind === 'json') { try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { /* 原样 */ } }
        openModal({ title: a.id, body: el('pre.mono', {}, pretty), actions });
      } else {
        openModal({
          title: a.id,
          body: el('div.lf-empty-hint', {}, `暂不支持预览 ${a.content_type || '该类型'},请下载查看`),
          actions,
        });
      }
    } catch (e) {
      toast(`预览失败: ${e.message}`, 'err');
    }
  }

  async function downloadArtifact(a) {
    try {
      const blob = await api.fetchArtifactBlob(runId, a.id);
      download(a.id, blob, a.content_type || 'application/octet-stream');
    } catch (e) {
      toast(`下载失败: ${e.message}`, 'err');
    }
  }

  function reset() { runId = null; items = []; render(); }

  render();
  return { loadRun, reset };
}
