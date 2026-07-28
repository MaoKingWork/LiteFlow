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
import { t, getLocale, setLocale, applyToDOM, onLocaleChange } from './i18n.js';

/* ── 应用状态 ─────────────────────────────────────────────────── */
const state = {
  name: null,            // 当前工作流名(= 文件名)
  unsavedName: null,     // 导入未保存时的展示名(本地草稿)
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
    const token = window.prompt(t('prompt.token'));
    if (token == null) throw e;
    api.setToken(token.trim());
    return await fn(); // 重试一次
  }
}

/* ── 工作流名展示(随语言变化的部分本地化) ───────────────────── */
function renderWfName() {
  const el = $('#wf-name');
  if (state.name) el.textContent = state.name;
  else if (state.unsavedName) el.textContent = t('prompt.nameUnsaved', { name: state.unsavedName });
  else el.textContent = t('prompt.untitledUnsaved');
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
    state.unsavedName = null;
    state.docModel = doc.normalize(data.config);
    state.selection = { path: null, step: null };
    renderWfName();
    setDirty(false);
    renderCanvas();
    renderProps();
    runner.reset();
    artifacts.reset();
    diag.render({ is_valid: true, diagnostics: [] });
    await refreshWorkflowList();
  } catch (e) {
    toast(t('toast.openFailed', { msg: e.message }), 'err');
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
    toast(t('toast.saved'), 'ok');
    refreshWorkflowList();
    return true;
  } catch (e) {
    if (e.status === 400 && e.body?.diagnostics) {
      diag.render(e.body);
      switchTab('diag');
      toast(t('toast.saveRejected'), 'err');
    } else {
      toast(t('toast.saveFailed', { msg: e.message }), 'err');
    }
    return false;
  }
}

async function saveAsNew() {
  const name = window.prompt(t('prompt.newName'), state.docModel?.name || 'untitled');
  if (!name) return false;
  if (!/^[A-Za-z0-9_-]+$/.test(name)) { toast(t('prompt.nameRule'), 'err'); return false; }
  state.docModel.name = name;
  state.name = name;
  state.unsavedName = null;
  renderWfName();
  try {
    await withAuth(() => api.saveWorkflow(name, state.docModel));
    setDirty(false);
    toast(t('toast.created'), 'ok');
    refreshWorkflowList();
    return true;
  } catch (e) {
    toast(t('toast.createFailed', { msg: e.message }), 'err');
    return false;
  }
}

/** runner 启动前置:有脏改动时引导先保存(run 绑定文件快照)。 */
async function ensureSaved() {
  if (!state.dirty && state.name) return true;
  if (!window.confirm(t('prompt.saveBeforeRun'))) return !!state.name && !state.dirty;
  return await saveCurrent();
}

async function validateCurrent() {
  if (!state.docModel) return;
  try {
    const report = await withAuth(() => api.validateWorkflow(state.docModel));
    const errs = diag.render(report);
    switchTab('diag');
    toast(errs ? t('toast.errorsFound', { n: errs }) : t('toast.valid'), errs ? 'err' : 'ok');
  } catch (e) {
    toast(t('toast.validateFailed', { msg: e.message }), 'err');
  }
}

async function exportYaml() {
  if (!state.name) { toast(t('toast.saveFirst'), 'err'); return; }
  try {
    const data = await withAuth(() => api.getWorkflow(state.name));
    download(`${state.name}.yaml`, data.yaml, 'text/yaml;charset=utf-8');
  } catch (e) {
    toast(t('toast.exportFailed', { msg: e.message }), 'err');
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
      state.unsavedName = name;
      renderWfName();
      setDirty(true);
      renderCanvas(); renderProps(); runner.reset(); artifacts.reset();
      toast(t('toast.importJson'), 'ok');
    } else {
      await withAuth(() => api.putWorkflowYaml(name, text));
      toast(t('toast.importedAs', { name }), 'ok');
      await openWorkflow(name);
    }
  } catch (e) {
    if (e.status === 400 && e.body?.diagnostics) {
      diag.render(e.body);
      switchTab('diag');
    }
    toast(t('toast.importFailed', { msg: e.message }), 'err');
  }
}

/* ── 工作流操作意图(store 事件) ─────────────────────────────── */
on('wf:open', async (name) => {
  if (state.dirty && !confirmDanger(t('prompt.dirtySwitch'))) return;
  await openWorkflow(name);
});

on('wf:delete', async (name) => {
  if (!confirmDanger(t('prompt.confirmDelete', { name }))) return;
  try {
    await withAuth(() => api.deleteWorkflow(name));
    toast(t('toast.deleted', { name }), 'ok');
    if (state.name === name) { state.name = null; state.docModel = null; }
    const data = await withAuth(() => api.listWorkflows());
    palette.renderWorkflows(data.workflows || [], null);
    if (data.workflows?.length) await openWorkflow(data.workflows[0].name);
    else { newLocalDoc(); }
  } catch (e) {
    toast(t('toast.deleteFailed', { msg: e.message }), 'err');
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
  if (state.dirty && !confirmDanger(t('prompt.dirtyNew'))) return;
  newLocalDoc();
});

function newLocalDoc() {
  state.name = null;
  state.unsavedName = null;
  state.docModel = doc.createEmpty('untitled');
  state.selection = { path: null, step: null };
  renderWfName();
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

/* ── 国际化:语言切换器接线 ─────────────────────────────────────── */
const langSwitch = $('#lang-switch');
if (langSwitch) {
  langSwitch.value = getLocale();
  langSwitch.addEventListener('change', (e) => setLocale(e.target.value));
}
// 首次进入:翻译静态 DOM(顶栏 / 侧栏 / 页签等 data-i18n 节点)
applyToDOM(document);
// 工作流名展示随语言变化(各模块自管重渲染,这里只补 wf-name)
onLocaleChange(() => renderWfName());

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
      catch { toast(t('toast.authFailed'), 'err'); return; }
    } else {
      toast(t('toast.noServer'), 'err');
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
    for (const typeInfo of st.types || []) state.schemas[typeInfo.name] = typeInfo;
    palette.renderPalette(st.types || []);
  } catch (e) {
    toast(t('toast.metaFailed', { msg: e.message }), 'err');
  }

  // 工作流列表 → 打开第一个
  try {
    const data = await withAuth(() => api.listWorkflows());
    palette.renderWorkflows(data.workflows || [], null);
    if (data.workflows?.length) await openWorkflow(data.workflows[0].name);
    else newLocalDoc();
  } catch (e) {
    toast(t('toast.listFailed', { msg: e.message }), 'err');
    newLocalDoc();
  }
}

boot();
