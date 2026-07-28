/* canvas.js —— 受限流程画布(对齐 visualization-design §5.7)。
   形态 = 顶层线性流 + condition/loop/parallel 容器块嵌套,与执行模型一一对应;
   不做自由 DAG。连线即执行顺序,容器内缩即嵌套。

   职责:渲染 / 选择 / 拖拽编排(新增·移动·删除) / 折叠 / 运行状态着色 / 诊断标注。
   文档变更经 store.emit('doc') 广播,由 main.js 统一调度重渲染。
*/

import { el, replaceChildren, truncate } from './util.js';
import { emit } from './store.js';
import * as doc from './doc.js';
import { t, onLocaleChange } from './i18n.js';

/* step 类型 → 节点色(深色底高饱和,符合 DESIGN.md 氛围) */
export const TYPE_COLORS = {
  llm: '#00E5FF',
  tool: '#32D74B',
  condition: '#FF9F0A',
  loop: '#BF5AF2',
  parallel: '#0A84FF',
  skill: '#64D2FF',
  image: '#FF9F0A',
};
const colorOf = (type) => TYPE_COLORS[type] || '#98989D';

const MIME_NEW = 'application/x-lf-new';   // 面板拖入(新建)
const MIME_MOVE = 'application/x-lf-move'; // 画布内移动

export function createCanvas(container) {
  let currentDoc = null;       // 文档模型引用(原地修改)
  let selection = null;        // 当前选中 path(段数组)
  let stepSchemas = {};        // type -> fields[](内省)
  let runStatus = new Map();   // stepId -> {status, duration_ms, ...}
  let diagPaths = new Set();   // 诊断 path 字符串集合

  /* ── 公共 API ─────────────────────────────────────────────── */
  function render(docModel, { schemas = stepSchemas } = {}) {
    currentDoc = docModel;
    stepSchemas = schemas;
    draw();
  }

  function setSelection(path) {
    selection = path;
    draw();
  }

  function applyRunStatus(map) {
    runStatus = map || new Map();
    draw();
  }

  function markDiagnostics(pathStrings) {
    diagPaths = new Set(pathStrings || []);
    draw();
  }

  function scrollToStep(stepId) {
    const node = container.querySelector(`[data-step-id="${CSS.escape(stepId)}"]`);
    node?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  /* ── 渲染 ─────────────────────────────────────────────────── */
  function draw() {
    if (!currentDoc) { replaceChildren(container); return; }
    const root = renderSeq(currentDoc.steps, [], 'steps');
    replaceChildren(container, root);
    if (!currentDoc.steps.length) {
      container.appendChild(el('div.lf-canvas-empty', {},
        t('canvas.empty'), el('br'), t('canvas.emptyHint')));
    }
  }

  /** 渲染一个 step 列表为纵向序列(dropzone 相间)。 */
  function renderSeq(steps, parentPath, field) {
    const seq = el('div.lf-seq');
    seq.appendChild(dropzone(parentPath, field, 0, steps.length === 0));
    (steps || []).forEach((step, i) => {
      const path = [...parentPath, { field, index: i }];
      seq.appendChild(renderNode(step, path));
      seq.appendChild(dropzone(parentPath, field, i + 1, false, i === steps.length - 1));
    });
    return seq;
  }

  /** 渲染单个 step 节点(容器递归嵌套)。 */
  function renderNode(step, path) {
    const type = step.type || '?';
    const color = colorOf(type);
    const st = runStatus.get(step.id);
    const pathStr = doc.pathToString(path);
    const containerTypes = doc.CONTAINER_FIELDS[type] || [];
    const isCont = containerTypes.length > 0;
    const collapsed = !!step.ui?.collapsed;

    const classes = ['lf-node'];
    if (isCont) classes.push('is-container');
    if (collapsed) classes.push('is-collapsed');
    if (selection && doc.pathToString(selection) === pathStr) classes.push('is-selected');
    if (st) classes.push(`st-${st.status}`);
    if (diagPaths.has(pathStr)) classes.push('st-failed');

    const node = el(`div.${classes.join('.')}`, {
      draggable: 'true',
      dataset: { stepId: step.id || '', path: pathStr },
      style: `--node-c:${color}`,
      onclick: (e) => { e.stopPropagation(); select(path); },
      ondragstart: (e) => {
        e.stopPropagation();
        e.dataTransfer.setData(MIME_MOVE, JSON.stringify(path));
        e.dataTransfer.effectAllowed = 'move';
        requestAnimationFrame(() => node.classList.add('is-dragging'));
      },
      ondragend: () => node.classList.remove('is-dragging'),
    });

    /* 头部:状态点 · 类型徽标 · id · 备注旗 · 操作 */
    const head = el('div.lf-node-head', {},
      el('span.lf-status-dot'),
      el('span.lf-node-badge', {}, type),
      el('span.lf-node-id', {}, step.id || t('canvas.unnamed')),
      step.ui?.note ? el('span.lf-node-note-flag', { title: step.ui.note }, '✎') : null,
      el('span.lf-node-actions', {},
        isCont ? iconBtn(collapsed ? '▸' : '▾', collapsed ? t('canvas.expand') : t('canvas.collapse'), (e) => {
          e.stopPropagation();
          step.ui = { ...(step.ui || {}), collapsed: !collapsed };
          emit('doc', { kind: 'edit' });
        }) : null,
        iconBtn('✕', t('canvas.deleteNode'), (e) => {
          e.stopPropagation();
          doc.removeStep(currentDoc, path);
          if (selection && doc.pathToString(selection) === pathStr) select(null);
          emit('doc', { kind: 'edit' });
        }),
      ),
    );
    node.appendChild(head);

    /* 摘要行 */
    const summary = summaryOf(step);
    if (summary) node.appendChild(el('div.lf-node-summary', {}, summary));

    /* 输入/输出 chips */
    const chips = [];
    if (step.agent) chips.push(el('span.lf-chip', {}, `agent: ${step.agent}`));
    if (step.tool) chips.push(el('span.lf-chip', {}, step.tool));
    if (step.output) chips.push(el('span.lf-chip.lf-chip-out', {}, `→ ${step.output}`));
    if (chips.length) node.appendChild(el('div.lf-node-io', {}, chips));

    /* 运行元信息(耗时 / token / 重试) */
    if (st && (st.duration_ms != null || st.token_usage != null || st.retry_count)) {
      const parts = [];
      if (st.duration_ms != null) parts.push(`${Math.round(st.duration_ms)}ms`);
      const tok = st.token_usage;
      if (tok && (tok.total_tokens || tok.total)) parts.push(`tok ${tok.total_tokens ?? tok.total}`);
      if (st.retry_count) parts.push(`retry ×${st.retry_count}`);
      if (st.error) parts.push(truncate(st.error, 60));
      node.appendChild(el('div.lf-node-meta', {}, parts.join(' · ')));
    }

    /* 容器嵌套区 */
    if (isCont && !collapsed) node.appendChild(renderNest(step, path, containerTypes));
    return node;
  }

  /** 容器节点的嵌套区域。 */
  function renderNest(step, path, containerTypes) {
    const nest = el('div.lf-nest');
    for (const cf of containerTypes) {
      if (step.type === 'condition') continue; // condition 双栏单独处理
      nest.appendChild(el('div.lf-nest-label', {}, cf.name));
      if (cf.kind === 'step') {
        // loop 单步槽
        const zone = el('div.lf-nest-zone');
        if (step.step && typeof step.step === 'object') {
          zone.appendChild(renderNode(step.step, [...path, { field: 'step' }]));
        } else {
          zone.appendChild(el('div.lf-nest-empty', {}, t('canvas.dropLoopBody')));
          makeDropTarget(zone, path, 'step', 0);
        }
        nest.appendChild(zone);
      } else {
        const zone = el('div.lf-nest-zone');
        const list = Array.isArray(step[cf.name]) ? step[cf.name] : [];
        if (!list.length) zone.appendChild(el('div.lf-nest-empty', {}, t('canvas.emptyBranch')));
        zone.appendChild(renderSeq(list, path, cf.name));
        nest.appendChild(zone);
      }
    }
    if (step.type === 'condition') {
      const cols = el('div.lf-nest-cols');
      for (const cf of containerTypes) {
        const col = el('div');
        col.appendChild(el('div.lf-nest-label', {}, cf.name === 'then' ? '✓ then' : '✗ else'));
        const zone = el('div.lf-nest-zone');
        const list = Array.isArray(step[cf.name]) ? step[cf.name] : [];
        if (!list.length) zone.appendChild(el('div.lf-nest-empty', {}, t('canvas.emptyShort')));
        zone.appendChild(renderSeq(list, path, cf.name));
        col.appendChild(zone);
        cols.appendChild(col);
      }
      nest.appendChild(cols);
    }
    return nest;
  }

  /* ── 拖放 ─────────────────────────────────────────────────── */
  function dropzone(parentPath, field, index, isFirst = false, isLast = false) {
    const z = el('div.lf-dropzone', {
      dataset: { drop: '1' },
    });
    if (isFirst) z.classList.add('is-first');
    if (isLast) z.classList.add('is-last');
    makeDropTarget(z, parentPath, field, index);
    return z;
  }

  function makeDropTarget(zone, parentPath, field, index) {
    zone.addEventListener('dragover', (e) => {
      if (![...e.dataTransfer.types].some((t) => t === MIME_NEW || t === MIME_MOVE)) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = e.dataTransfer.types.includes(MIME_MOVE) ? 'move' : 'copy';
      zone.classList.add('is-over');
    });
    zone.addEventListener('dragleave', () => zone.classList.remove('is-over'));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      zone.classList.remove('is-over');
      handleDrop(e, parentPath, field, index);
    });
  }

  function handleDrop(e, parentPath, field, index) {
    const newType = e.dataTransfer.getData(MIME_NEW);
    if (newType) {
      const step = doc.createStep(newType, stepSchemas[newType]?.fields || []);
      doc.insertStep(currentDoc, parentPath, field, index, step);
      emit('doc', { kind: 'edit' });
      const insertedPath = field === 'step'
        ? [...parentPath, { field: 'step' }]
        : [...parentPath, { field, index }];
      select(insertedPath);
      return;
    }
    const moveRaw = e.dataTransfer.getData(MIME_MOVE);
    if (moveRaw) {
      let fromPath;
      try { fromPath = JSON.parse(moveRaw); } catch { return; }
      // 禁止把容器拖入自己的后代
      const fromStr = doc.pathToString(fromPath);
      const toStr = doc.pathToString([...parentPath, { field, index: 0 }]);
      if (toStr === fromStr || toStr.startsWith(fromStr + '.')) return;
      doc.moveStep(currentDoc, fromPath, parentPath, field, index);
      emit('doc', { kind: 'edit' });
    }
  }

  /* ── 选择 ─────────────────────────────────────────────────── */
  function select(path) {
    selection = path;
    const step = path ? doc.getStepAt(currentDoc.steps, path) : null;
    emit('selection', { path, step });
    draw();
  }

  // 点击画布空白 → 选中工作流本身(编辑工作流级配置)
  container.addEventListener('click', () => select(null));

  // 语言切换时按当前文档重绘(文案 / 摘要随语言变化)
  onLocaleChange(() => { if (currentDoc) draw(); });

  return { render, setSelection, applyRunStatus, markDiagnostics, scrollToStep, clearSelection: () => select(null) };
}

function iconBtn(text, title, onclick) {
  return el('button.lf-icon-btn', { title, onclick }, text);
}

/** 类型摘要(节点卡片第二行)。 */
function summaryOf(step) {
  switch (step.type) {
    case 'llm': return truncate((step.prompt || '').replace(/\s+/g, ' '), 90);
    case 'tool': return step.tool ? truncate(JSON.stringify(step.params ?? {}), 90) : '';
    case 'condition': return truncate(step.when || '', 90);
    case 'loop': return `iter: ${truncate(step.iter ?? '', 40)}${step.as ? ` as ${step.as}` : ''}${step.output_mode ? ` · ${step.output_mode}` : ''}`;
    case 'parallel': return `branches ×${(step.branches || []).length}${step.max_concurrency ? ` · ${t('canvas.concurrency', { n: step.max_concurrency })}` : ''}`;
    case 'skill': return truncate(step.skill || step.name || '', 90);
    case 'image': return truncate(step.prompt || '', 90);
    default: return '';
  }
}
