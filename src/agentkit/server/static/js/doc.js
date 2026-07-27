/* doc.js —— 工作流文档模型(JSON 同构 YAML)。
   前端唯一事实来源 = JSON 文档;YAML 序列化/解析全部在服务端完成。

   路径模型(与 validator 的 path 对齐,如 "steps[2].then[1]"):
     段(segment) = {field: 'steps'|'then'|'else'|'branches', index: n}   列表位
                  | {field: 'step'}                                      单步槽(loop)
*/

import { deepClone, uid } from './util.js';

/* 容器型 step 的嵌套字段(对齐 server.routes.workflows._CONTAINER_FIELDS) */
export const CONTAINER_FIELDS = {
  condition: [{ name: 'then', kind: 'steps' }, { name: 'else', kind: 'steps' }],
  loop: [{ name: 'step', kind: 'step' }],
  parallel: [{ name: 'branches', kind: 'steps' }],
};

export const isContainer = (type) => type in CONTAINER_FIELDS;

/* ── 文档构造 ─────────────────────────────────────────────────── */
export function createEmpty(name = 'untitled') {
  return { name, inputs: [], steps: [], ui: { viewport: { x: 0, y: 0, zoom: 1 } } };
}

/** 规整化:保证关键段存在,不破坏未知字段(round-trip 无损)。 */
export function normalize(config) {
  const doc = deepClone(config) || {};
  if (typeof doc !== 'object' || Array.isArray(doc)) return createEmpty();
  if (!Array.isArray(doc.steps)) doc.steps = [];
  if (doc.ui == null || typeof doc.ui !== 'object') doc.ui = {};
  return doc;
}

/** 依据内省字段 schema 生成新 step(必填字段给占位值)。 */
export function createStep(typeName, fieldsSchema = []) {
  const step = { id: uid(typeName), type: typeName };
  for (const f of fieldsSchema) {
    if (['id', 'type', 'ui'].includes(f.name)) continue;
    if (!f.required) continue;
    if (f.name === 'prompt') step.prompt = '';
    else if (f.name === 'tool') step.tool = '';
    else if (f.name === 'when') step.when = '';
    else if (f.name === 'iter') step.iter = '[]';
    else if (f.name === 'agent') step.agent = '';
  }
  // 容器字段预置空结构
  for (const cf of CONTAINER_FIELDS[typeName] || []) {
    step[cf.name] = cf.kind === 'steps' ? [] : null;
  }
  return step;
}

/* ── 路径解析 ─────────────────────────────────────────────────── */
/**
 * 取路径指向的 step 对象;root 为文档 steps 数组。
 * @returns step 对象;路径无效返回 null。
 */
export function getStepAt(rootSteps, path) {
  let current = null;
  for (const seg of path) {
    if (seg.field === 'step') {
      current = current?.step ?? null;
    } else {
      // 首段的 owner 为虚拟根(其 steps = 顶层列表);嵌套段从 current 取字段
      const owner = current ?? { steps: rootSteps };
      const fieldList = owner[seg.field];
      current = Array.isArray(fieldList) ? (fieldList[seg.index] ?? null) : null;
    }
    if (current == null) return null;
  }
  return current;
}

/**
 * 取路径的父容器信息,用于插入/删除。
 * @returns {list, index}        路径位于列表中(then/else/branches/steps)
 *          {owner, field:'step'} 路径位于 loop 单步槽
 *          null                  无效路径
 */
export function getParentRef(rootSteps, path) {
  if (!path.length) return null;
  const last = path[path.length - 1];
  const parentPath = path.slice(0, -1);
  const parent = parentPath.length ? getStepAt(rootSteps, parentPath) : null;
  if (last.field === 'step') {
    return parent ? { owner: parent, field: 'step' } : null;
  }
  const list = parentPath.length ? (parent ? parent[last.field] : null) : rootSteps;
  return Array.isArray(list) ? { list, index: last.index } : null;
}

/** 在 parentPath 指向的容器字段 field 的 index 处插入 step。 */
export function insertStep(doc, parentPath, field, index, step) {
  if (field === 'step') {
    const owner = getStepAt(doc.steps, parentPath);
    if (owner && (owner.step == null)) owner.step = step;
    return;
  }
  let list;
  if (!parentPath.length) list = doc.steps;
  else {
    const parent = getStepAt(doc.steps, parentPath);
    if (!parent) return;
    if (!Array.isArray(parent[field])) parent[field] = [];
    list = parent[field];
  }
  list.splice(Math.max(0, Math.min(index, list.length)), 0, step);
}

/** 删除路径指向的 step;返回被删对象(供撤销/移动)。 */
export function removeStep(doc, path) {
  const ref = getParentRef(doc.steps, path);
  if (!ref) return null;
  if (ref.field === 'step') {
    const removed = ref.owner.step;
    ref.owner.step = null;
    return removed;
  }
  return ref.list.splice(ref.index, 1)[0] ?? null;
}

/** 移动 step:先删后插(同列表内移动自动修正索引)。 */
export function moveStep(doc, fromPath, toParentPath, field, toIndex) {
  const fromRef = getParentRef(doc.steps, fromPath);
  if (!fromRef || fromRef.field === 'step') {
    // loop 单步槽的移动:按普通删除+插入处理
    const moved = removeStep(doc, fromPath);
    if (moved) insertStep(doc, toParentPath, field, toIndex, moved);
    return;
  }
  // 同列表且删除位在插入位之前 → 插入位前移
  const sameList =
    field !== 'step' &&
    fromRef.list === resolveList(doc, toParentPath, field);
  const moved = removeStep(doc, fromPath);
  if (!moved) return;
  const idx = sameList && fromRef.index < toIndex ? toIndex - 1 : toIndex;
  insertStep(doc, toParentPath, field, idx, moved);
}

function resolveList(doc, parentPath, field) {
  if (!parentPath.length) return doc.steps;
  const parent = getStepAt(doc.steps, parentPath);
  return parent ? parent[field] : null;
}

/* ── 路径 ⇄ 字符串(对齐 validator "steps[2].then[1]") ────────── */
export function pathToString(path) {
  return path.map((s) =>
    s.field === 'step' ? 'step' : `${s.field}[${s.index}]`
  ).join('.');
}

/** 解析 validator path(如 "steps[2].then[1]" / "steps[0].step")为段数组;失败返回 null。 */
export function pathFromString(str) {
  if (!str || typeof str !== 'string') return null;
  const segs = [];
  for (const part of str.split('.')) {
    const m = /^([A-Za-z_]+)(?:\[(\d+)\])?$/.exec(part);
    if (!m) return null;
    const field = m[1];
    if (!['steps', 'then', 'else', 'branches', 'step'].includes(field)) return null;
    segs.push(m[2] != null ? { field, index: Number(m[2]) } : { field });
  }
  return segs.length ? segs : null;
}

/* ── 遍历 ─────────────────────────────────────────────────────── */
/** 深度优先遍历全部 step,回调 (step, path)。 */
export function walkSteps(rootSteps, fn) {
  const visitList = (list, prefix, field) => {
    (list || []).forEach((step, i) => {
      if (!step || typeof step !== 'object') return;
      const path = [...prefix, { field, index: i }];
      fn(step, path);
      visitContainers(step, path);
    });
  };
  const visitContainers = (step, path) => {
    for (const cf of CONTAINER_FIELDS[step.type] || []) {
      if (cf.kind === 'steps') {
        visitList(step[cf.name], path, cf.name);
      } else if (step.step && typeof step.step === 'object') {
        const p = [...path, { field: 'step' }];
        fn(step.step, p);
        visitContainers(step.step, p);
      }
    }
  };
  visitList(rootSteps, [], 'steps');
}

/** 收集全部 step(扁平数组,供运行状态匹配/统计)。 */
export function allSteps(rootSteps) {
  const out = [];
  walkSteps(rootSteps, (step, path) => out.push({ step, path }));
  return out;
}
