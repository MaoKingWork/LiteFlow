/* util.js —— 通用工具:DOM 构建 / 格式化 / toast / 下载。
   无依赖,被所有模块复用。 */

/** querySelector 简写 */
export const $ = (sel, root = document) => root.querySelector(sel);

/**
 * DOM 构建器:el('div.cls-a.cls-b', {attr: v, onclick: fn}, ...children)
 * children 支持 Node / string(自动转文本节点) / 数组 / null(跳过)。
 */
export function el(spec, attrs = {}, ...children) {
  const [tag, ...classes] = spec.split('.');
  const node = document.createElement(tag || 'div');
  if (classes.length) node.className = classes.join(' ');
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'html') node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(node, c);
    else if (c instanceof Node) node.appendChild(c);
    else node.appendChild(document.createTextNode(String(c)));
  }
}

/** 清空元素并追加新子节点 */
export function replaceChildren(node, ...children) {
  node.textContent = '';
  append(node, children);
}

export function debounce(fn, ms = 300) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function uid(prefix = 's') {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}

/** 时间戳(秒) → HH:MM:SS */
export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 时间戳(秒) → MM-DD HH:MM */
export function fmtDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtBytes(n) {
  if (n == null || isNaN(n)) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtDuration(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** 触发浏览器下载 */
export function download(filename, content, mime = 'application/octet-stream') {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/** 轻提示:type = ok | err | info */
export function toast(message, type = 'info', ms = 2600) {
  const root = $('#toast-root');
  if (!root) return;
  const item = el(`div.lf-toast.${type}`, {}, message);
  root.appendChild(item);
  setTimeout(() => { item.style.opacity = '0'; item.style.transition = 'opacity .3s'; }, ms - 300);
  setTimeout(() => item.remove(), ms);
}

/** 截断字符串(摘要展示用) */
export function truncate(s, max = 80) {
  if (s == null) return '';
  s = String(s);
  return s.length > max ? s.slice(0, max) + '…' : s;
}

/** 深拷贝(JSON 安全) */
export function deepClone(v) {
  return v == null ? v : JSON.parse(JSON.stringify(v));
}

/** 通用确认对话框(简单封装 confirm,便于后续替换为自定义模态) */
export function confirmDanger(message) {
  return window.confirm(message);
}

/* ── 模态框(依赖 index.html 的 #modal-root 结构) ─────────────── */
// 一次性事件委托:点击弹窗内的关闭按钮或遮罩时关闭
// 绑定在 document 上,不依赖 DOM 就绪时机,openModal 不再重复绑定
function modalClickDelegate(e) {
  const root = $('#modal-root');
  if (!root || root.hidden) return;
  if (e.target.closest('[data-close]')) closeModal();
}
document.addEventListener('click', modalClickDelegate);

/**
 * 打开模态框。
 * @param opts { title, body: Node, actions: Node[] , onClose }
 */
export function openModal({ title = '', body = null, actions = [], onClose = null } = {}) {
  const root = $('#modal-root');
  if (!root) return;
  $('#modal-title').textContent = title;
  const bodyEl = $('#modal-body');
  replaceChildren(bodyEl, body);
  replaceChildren($('#modal-actions'), ...actions);
  root.hidden = false;
  root._onClose = onClose;
  document.addEventListener('keydown', escClose);
}

export function closeModal() {
  const root = $('#modal-root');
  if (!root || root.hidden) return;
  root.hidden = true;
  $('#modal-body')?.replaceChildren();
  document.removeEventListener('keydown', escClose);
  root._onClose?.();
  root._onClose = null;
}

function escClose(e) {
  if (e.key === 'Escape') closeModal();
}
