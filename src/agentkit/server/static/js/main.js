/* main.js —— 应用编排入口。
   依赖方向(无环):util ← api/store/doc ← canvas/palette/properties/
   runner/artifacts/diagnostics ← main。跨模块通信一律经 store 事件。
*/

import { $, debounce, download, toast, confirmDanger, uid } from './util.js';
import { on, emit } from './store.js';
import * as api from './api.js';
import * as doc from './doc.js';
import { createCanvas } from './canvas.js';
import { createPalette } from './palette.js';
import { createProperties } from './properties.js';
import { createRunner } from './runner.js';
import { createArtifacts } from './artifacts.js';
import { createDiagnostics } from './diagnostics.js';

/* ── 应用状态 ─────────────────────────────────────────────────── */
const state = {
  name: null,            // 当前工作流名(= 文件名)
  docModel: null,        // 文档模型(JSON)
  dirty: false,
  schemas: {},           // step type -> {fields, container_fields}
  tools: [],
  selection: { path: null, step: null },
};

/* ── 模块实例 ─────────────────────────────────────────────────── */
const canvasApi = createCanvas($('#canvas'));
const palette = createPalette({ listEl: $('#wf-list'), paletteEl: $('#palette') });
const props = createProperties({ paneEl: $('#pane-props') });
const diag = createDiagnostics({ paneEl: $('#pane-diag'), badgeEl: $('#diag-badge') });
const artifacts = createArtifacts({ paneEl: $('#pane-artifacts') });
const runner = createRunner({
  paneEl: $('#pane-run'),
  canvasApi,
  getDocModel: () => state.docModel,
  getWorkflowName: () => state.name,
  onNeedSave: ensureSaved,
});

/* ── 401 鉴权重试包装 ─────────────────────────────────────────── */
async function withAuth(fn) {
  try {
    return await fn();
  } catch (e) {
    if (e.status !== 401) throw e;
    const t = window.prompt('Server 需要访问令牌(AGENTKIT_SERVER_TOKEN):');
    if (t == null) throw e;
    api.setToken(t.trim());
    return await fn(); // 重试一次
  }
}

/* ── 文档加载 / 渲染 ──────────────────────────────────────────── */
function renderCanvas() {
  canvasApi.render(state.docModel, { schemas: state.schemas });
}
const renderCanvasSoon = debounce(renderCanvas, 150);

function renderProps() {
  props.render({
    docModel: state.docModel,
    selection: state.selection,
    schemas: state.schemas,
    tools: state.tools,
  });
}

function setDirty(v) {
  state.dirty = v;
  $('#wf-dirty').hidden = !v;
}

/** 打开指定工作流(服务端读取 config JSON)。 */
async function openWorkflow(name) {
  try {
    const data = await withAuth(() => api.getWorkflow(name));
    state.name = name;
    state.docModel = doc.normalize(data.config);
    state.selection = { path: null, step: null };
    $('#wf-name').textContent = name;
    setDirty(false);
    renderCanvas();
    renderProps();
    runner.reset();
    artifacts.reset();
    diag.render({ is_valid: true, diagnostics: [] });
    await refreshWorkflowList();
  } catch (e) {
    toast(`打开失败: ${e.message}`, 'err');
  }
}

async function refreshWorkflowList() {
  try {
    const data = await withAuth(() => api.listWorkflows());
    palette.renderWorkflows(data.workflows || [], state.name);
  } catch (e) {
    setConn(false);
  }
}

/* ── 保存 / 校验 / 导入导出 ───────────────────────────────────── */
async function saveCurrent() {
  if (!state.docModel) return false;
  if (!state.name) return await saveAsNew();
  try {
    await withAuth(() => api.saveWorkflow(state.name, state.docModel));
    setDirty(false);
    toast('已保存', 'ok');
    refreshWorkflowList();
    return true;
  } catch (e) {
    if (e.status === 400 && e.body?.diagnostics) {
      diag.render(e.body);
      switchTab('diag');
      toast('保存被拒绝:校验未通过', 'err');
    } else {
      toast(`保存失败: ${e.message}`, 'err');
    }
    return false;
  }
}

async function saveAsNew() {
  const name = window.prompt('新工作流名称(字母/数字/_/-):', state.docModel?.name || 'untitled');
  if (!name) return false;
  if (!/^[A-Za-z0-9_-]+$/.test(name)) { toast('名称仅允许字母、数字、_、-', 'err'); return false; }
  state.docModel.name = name;
  state.name = name;
  $('#wf-name').textContent = name;
  try {
    await withAuth(() => api.saveWorkflow(name, state.docModel));
    setDirty(false);
    toast('已创建', 'ok');
    refreshWorkflowList();
    return true;
  } catch (e) {
    toast(`创建失败: ${e.message}`, 'err');
    return false;
  }
}

/** runner 启动前置:有脏改动时引导先保存(run 绑定文件快照)。 */
async function ensureSaved() {
  if (!state.dirty && state.name) return true;
  if (!window.confirm('运行以服务端保存的文件为准。先保存当前修改?')) return !!state.name && !state.dirty;
  return await saveCurrent();
}

async function validateCurrent() {
  if (!state.docModel) return;
  try {
    const report = await withAuth(() => api.validateWorkflow(state.docModel));
    const errs = diag.render(report);
    switchTab('diag');
    toast(errs ? `发现 ${errs} 个错误` : '校验通过', errs ? 'err' : 'ok');
  } catch (e) {
    toast(`校验失败: ${e.message}`, 'err');
  }
}

async function exportYaml() {
  if (!state.name) { toast('请先保存工作流', 'err'); return; }
  try {
    const data = await withAuth(() => api.getWorkflow(state.name));
    download(`${state.name}.yaml`, data.yaml, 'text/yaml;charset=utf-8');
  } catch (e) {
    toast(`导出失败: ${e.message}`, 'err');
  }
}

async function importFile(file) {
  const text = await file.text();
  let name = file.name.replace(/\.(yaml|yml|json)$/i, '').replace(/[^A-Za-z0-9_-]/g, '_') || `imported_${uid('wf')}`;
  try {
    if (/\.json$/i.test(file.name)) {
      const parsed = JSON.parse(text);
      state.docModel = doc.normalize(parsed);
      state.name = null; // 走另存流程,避免覆盖同名文件
      $('#wf-name').textContent = `${name} (未保存)`;
      setDirty(true);
      renderCanvas(); renderProps(); runner.reset(); artifacts.reset();
      toast('已导入 JSON,保存后生效', 'ok');
    } else {
      await withAuth(() => api.putWorkflowYaml(name, text));
      toast(`已导入为 ${name}`, 'ok');
      await openWorkflow(name);
    }
  } catch (e) {
    if (e.status === 400 && e.body?.diagnostics) {
      diag.render(e.body);
      switchTab('diag');
    }
    toast(`导入失败: ${e.message}`, 'err');
  }
}

/* ── 工作流操作意图(store 事件) ─────────────────────────────── */
on('wf:open', async (name) => {
  if (state.dirty && !confirmDanger('当前有未保存修改,切换将丢失。继续?')) return;
  await openWorkflow(name);
});

on('wf:delete', async (name) => {
  if (!confirmDanger(`确认删除工作流 "${name}"?该操作不可恢复。`)) return;
  try {
    await withAuth(() => api.deleteWorkflow(name));
    toast(`已删除 ${name}`, 'ok');
    if (state.name === name) { state.name = null; state.docModel = null; }
    const data = await withAuth(() => api.listWorkflows());
    palette.renderWorkflows(data.workflows || [], null);
    if (data.workflows?.length) await openWorkflow(data.workflows[0].name);
    else { newLocalDoc(); }
  } catch (e) {
    toast(`删除失败: ${e.message}`, 'err');
  }
});

on('pal:add', (typeName) => {
  if (!state.docModel) return;
  const step = doc.createStep(typeName, state.schemas[typeName]?.fields || []);
  // 插入策略:选中列表内节点 → 其后;否则追加到顶层末尾
  const sel = state.selection;
  if (sel.path && sel.step) {
    const ref = doc.getParentRef(state.docModel.steps, sel.path);
    if (ref?.list) {
      doc.insertStep(state.docModel, sel.path.slice(0, -1), sel.path.at(-1).field, ref.index + 1, step);
    } else {
      doc.insertStep(state.docModel, [], 'steps', state.docModel.steps.length, step);
    }
  } else {
    doc.insertStep(state.docModel, [], 'steps', state.docModel.steps.length, step);
  }
  emit('doc', { kind: 'edit', source: 'palette' });
});

/* 文档事件:编辑 → 标脏 + 画布重渲染(防抖) */
on('doc', ({ kind } = {}) => {
  if (kind === 'edit') {
    setDirty(true);
    renderCanvasSoon();
  }
});

on('selection', ({ path, step }) => {
  state.selection = { path, step };
  renderProps();
});

on('diag:goto', (pathStr) => {
  const path = doc.pathFromString(pathStr);
  if (!path) return;
  const step = doc.getStepAt(state.docModel.steps, path);
  canvasApi.setSelection(path);
  state.selection = { path, step };
  renderProps();
  if (step?.id) canvasApi.scrollToStep(step.id);
});

on('diag:paths', (paths) => canvasApi.markDiagnostics(paths));

/* ── 页签切换 ─────────────────────────────────────────────────── */
function switchTab(name) {
  document.querySelectorAll('.lf-tab').forEach((t) =>
    t.classList.toggle('is-active', t.dataset.tab === name));
  document.querySelectorAll('.lf-pane').forEach((p) =>
    p.classList.toggle('is-active', p.id === `pane-${name}`));
  if (name === 'run') runner.activate(); else runner.deactivate();
}
document.querySelectorAll('.lf-tab').forEach((t) =>
  t.addEventListener('click', () => switchTab(t.dataset.tab)));

/* ── 顶栏 ─────────────────────────────────────────────────────── */
$('#btn-save').addEventListener('click', saveCurrent);
$('#btn-validate').addEventListener('click', validateCurrent);
$('#btn-export').addEventListener('click', exportYaml);
$('#btn-run').addEventListener('click', () => { switchTab('run'); runner.start(); });
$('#btn-import').addEventListener('click', () => $('#import-file').click());
$('#import-file').addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  e.target.value = '';
  if (file) await importFile(file);
});
$('#btn-new-wf').addEventListener('click', () => {
  if (state.dirty && !confirmDanger('当前有未保存修改,新建将丢失。继续?')) return;
  newLocalDoc();
});

function newLocalDoc() {
  state.name = null;
  state.docModel = doc.createEmpty('untitled');
  state.selection = { path: null, step: null };
  $('#wf-name').textContent = 'untitled (未保存)';
  setDirty(true);
  renderCanvas(); renderProps(); runner.reset(); artifacts.reset();
  diag.render({ is_valid: true, diagnostics: [] });
}

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveCurrent(); }
});
window.addEventListener('beforeunload', (e) => {
  if (state.dirty) { e.preventDefault(); e.returnValue = ''; }
});

/* ── 连接状态 ─────────────────────────────────────────────────── */
function setConn(ok) {
  $('#conn-status').className = `lf-conn ${ok ? 'is-ok' : 'is-bad'}`;
}

/* ── 启动 ─────────────────────────────────────────────────────── */
async function boot() {
  try {
    await api.listWorkflows(); // 探测连通性(兼预热)
    setConn(true);
  } catch (e) {
    setConn(false);
    if (e.status === 401) {
      // 触发一次带鉴权的重试
      try { await withAuth(() => api.listWorkflows()); setConn(true); }
      catch { toast('鉴权失败,请刷新重试', 'err'); return; }
    } else {
      toast('无法连接 Server', 'err');
      return;
    }
  }

  // 内省(节点面板 + 属性表单的数据源)
  try {
    const [st, tools] = await Promise.all([
      withAuth(() => api.metaStepTypes()),
      withAuth(() => api.metaTools()),
    ]);
    state.tools = tools.tools || [];
    for (const t of st.types || []) state.schemas[t.name] = t;
    palette.renderPalette(st.types || []);
  } catch (e) {
    toast(`内省加载失败: ${e.message}`, 'err');
  }

  // 工作流列表 → 打开第一个
  try {
    const data = await withAuth(() => api.listWorkflows());
    palette.renderWorkflows(data.workflows || [], null);
    if (data.workflows?.length) await openWorkflow(data.workflows[0].name);
    else newLocalDoc();
  } catch (e) {
    toast(`列表加载失败: ${e.message}`, 'err');
    newLocalDoc();
  }
}

boot();
