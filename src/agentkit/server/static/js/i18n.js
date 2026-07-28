/* i18n.js —— 轻量国际化核心(零依赖,与 util 同层)。
   设计:
     - 单一字典源 dict.{zh|en}.<key>,平铺键(模块前缀.名),便于全局检索;
     - t(key, params):{name} 占位符替换;缺失键回退到 key 本身(永不抛错);
     - data-i18n / data-i18n-title / data-i18n-placeholder:静态 DOM 文本声明式翻译;
     - setLocale():持久化(localStorage) + 触发 onLocaleChange 监听器,
       各模块自行重渲染(模块 owns 自己的状态,无需 main 调度);
     - 语言探测优先级:localStorage > navigator.language > 默认中文。
   依赖方向:无,被所有需要翻译的模块引用。
*/

const SUPPORTED = ['zh', 'en'];
const DEFAULT = 'zh';
const STORAGE_KEY = 'lf.locale';

const dict = {
  zh: {
    /* ── 顶栏 ── */
    'topbar.dirty': '有未保存修改',
    'topbar.validate': '校验',
    'topbar.validateTitle': '静态校验 (Ctrl+Shift+V)',
    'topbar.save': '保存',
    'topbar.saveTitle': '保存 (Ctrl+S)',
    'topbar.import': '导入',
    'topbar.importTitle': '导入 YAML / JSON',
    'topbar.export': '导出',
    'topbar.exportTitle': '导出 YAML',
    'topbar.run': '运行',
    'topbar.runTitle': '运行当前工作流',
    'topbar.close': '关闭',
    'topbar.langTitle': '语言 / Language',

    /* ── 左栏 ── */
    'sidebar.workflows': '工作流',
    'sidebar.newWorkflow': '新建工作流',
    'sidebar.nodes': '节点',

    /* ── 右栏页签 ── */
    'tab.props': '属性',
    'tab.run': '运行',
    'tab.artifacts': '产物',
    'tab.diag': '诊断',

    /* ── 画布 ── */
    'canvas.empty': '画布为空',
    'canvas.emptyHint': '从左侧「节点」面板点击或拖入一个节点开始编排',
    'canvas.unnamed': '(未命名)',
    'canvas.expand': '展开',
    'canvas.collapse': '折叠',
    'canvas.deleteNode': '删除节点',
    'canvas.dropLoopBody': '拖入循环体(单步)',
    'canvas.emptyBranch': '空分支',
    'canvas.emptyShort': '空',
    'canvas.concurrency': '并发 {n}',

    /* ── 节点面板 / 工作流列表 ── */
    'palette.kind.condition': '分支',
    'palette.kind.loop': '循环',
    'palette.kind.parallel': '并发',
    'palette.delete': '删除',
    'palette.noWorkflows': '暂无工作流',
    'palette.noWorkflowsHint': '点 ＋ 新建或导入',
    'palette.itemTitle': '点击添加到画布,或拖入指定位置\n字段: {fields}',
    'palette.updatedAt': '更新于 {time}',

    /* ── 属性面板 ── */
    'props.nodeSection': '节点 · {type}',
    'props.note': '备注',
    'props.notePlaceholder': '节点备注(保存在 ui.note,引擎忽略)',
    'props.extraFields': '扩展字段',
    'props.extraFieldsHint': 'schema 之外的字段,JSON 编辑,保存时原样写入 YAML',
    'props.selectTool': '(选择工具)',
    'props.params': '参数: ',
    'props.workflow': '工作流',
    'props.nameHint': '名称 = 文件名,重命名请用「另存/新建」',
    'props.inputPlaceholder': '输入变量名,回车添加',
    'props.unnamed': '(未命名)',
    'props.delete': '删除',
    'props.clickToDelete': '点击删除',
    'props.add': '添加',

    /* ── 运行面板 ── */
    'runner.control': '运行控制',
    'runner.run': '▶ 运行',
    'runner.runTitle': '以当前保存的工作流启动 run',
    'runner.cancel': '中断',
    'runner.cancelTitle': '协作式取消:当前 step 完成后停止',
    'runner.hardCancel': '硬中断',
    'runner.hardCancelTitle': '立即取消任务(asyncio.Task.cancel)',
    'runner.resume': '恢复',
    'runner.resumeTitle': '从 checkpoint 断点续跑(跳过已完成 step)',
    'runner.runId': 'run: {id}',
    'runner.inputs': '输入变量',
    'runner.inputPlaceholder': '输入 {name} 的值',
    'runner.noInputs': '该工作流未声明 inputs',
    'runner.history': '运行历史',
    'runner.refresh': '刷新',
    'runner.liveSuffix': ' · 实时',
    'runner.noRuns': '暂无运行记录',
    'runner.events': '事件流',
    'runner.llmStream': 'LLM 流式输出',
    'runner.streamPlaceholder': '运行 LLM 节点后此处显示流式输出',
    'runner.streaming': '● 流式中',
    'runner.done': '✓ 完成',
    'runner.chars': '{n} 字符',
    'runner.cancelling': '取消中…',
    'runner.wfStarted': '工作流 {name} 开始',
    'runner.runStatus': '运行{status}',
    'runner.artifactProduced': '产物: {id} ({size})',
    'runner.errorPrefix': '错误: {msg}',

    /* ── 产物面板 ── */
    'artifacts.title': '产物',
    'artifacts.titleWithRun': '产物 · {runId}',
    'artifacts.refresh': '刷新',
    'artifacts.selectRun': '在「运行」页选择一个 run 查看产物',
    'artifacts.noArtifacts': '该 run 暂无产物',
    'artifacts.preview': '预览',
    'artifacts.download': '⬇ 下载',
    'artifacts.downloadTitle': '下载',
    'artifacts.unsupportedPreview': '暂不支持预览 {type},请下载查看',
    'artifacts.unsupportedType': '该类型',

    /* ── 诊断面板 ── */
    'diag.title': '校验结果',
    'diag.ok': '✓ 校验通过,未发现问题',
    'diag.clickToLocate': '点击定位到节点',

    /* ── 轻提示(toast)── */
    'toast.saved': '已保存',
    'toast.saveRejected': '保存被拒绝:校验未通过',
    'toast.created': '已创建',
    'toast.valid': '校验通过',
    'toast.errorsFound': '发现 {n} 个错误',
    'toast.importJson': '已导入 JSON,保存后生效',
    'toast.importedAs': '已导入为 {name}',
    'toast.deleted': '已删除 {name}',
    'toast.started': '已启动 {id}',
    'toast.cancelGraceful': '已请求协作式中断',
    'toast.cancelHard': '已硬中断',
    'toast.resumed': '已恢复为 {id}',
    'toast.authFailed': '鉴权失败,请刷新重试',
    'toast.noServer': '无法连接 Server',
    'toast.openFailed': '打开失败: {msg}',
    'toast.saveFailed': '保存失败: {msg}',
    'toast.createFailed': '创建失败: {msg}',
    'toast.validateFailed': '校验失败: {msg}',
    'toast.exportFailed': '导出失败: {msg}',
    'toast.importFailed': '导入失败: {msg}',
    'toast.deleteFailed': '删除失败: {msg}',
    'toast.startFailed': '启动失败: {msg}',
    'toast.cancelFailed': '中断失败: {msg}',
    'toast.resumeFailed': '恢复失败: {msg}',
    'toast.previewFailed': '预览失败: {msg}',
    'toast.downloadFailed': '下载失败: {msg}',
    'toast.metaFailed': '内省加载失败: {msg}',
    'toast.listFailed': '列表加载失败: {msg}',
    'toast.saveFirst': '请先保存工作流',

    /* ── 原生对话框(prompt / confirm)── */
    'prompt.token': 'Server 需要访问令牌(AGENTKIT_SERVER_TOKEN):',
    'prompt.newName': '新工作流名称(字母/数字/_/-):',
    'prompt.nameRule': '名称仅允许字母、数字、_、-',
    'prompt.saveBeforeRun': '运行以服务端保存的文件为准。先保存当前修改?',
    'prompt.dirtySwitch': '当前有未保存修改,切换将丢失。继续?',
    'prompt.dirtyNew': '当前有未保存修改,新建将丢失。继续?',
    'prompt.confirmDelete': '确认删除工作流 "{name}"?该操作不可恢复。',
    'prompt.untitledUnsaved': 'untitled (未保存)',
    'prompt.nameUnsaved': '{name} (未保存)',

    /* ── API 层错误 ── */
    'api.needToken': '需要访问令牌(token)',
    'api.artifactDownloadFailed': '产物下载失败 HTTP {status}',
  },

  en: {
    /* ── topbar ── */
    'topbar.dirty': 'Unsaved changes',
    'topbar.validate': 'Validate',
    'topbar.validateTitle': 'Static validation (Ctrl+Shift+V)',
    'topbar.save': 'Save',
    'topbar.saveTitle': 'Save (Ctrl+S)',
    'topbar.import': 'Import',
    'topbar.importTitle': 'Import YAML / JSON',
    'topbar.export': 'Export',
    'topbar.exportTitle': 'Export YAML',
    'topbar.run': 'Run',
    'topbar.runTitle': 'Run current workflow',
    'topbar.close': 'Close',
    'topbar.langTitle': 'Language / 语言',

    /* ── sidebar ── */
    'sidebar.workflows': 'Workflows',
    'sidebar.newWorkflow': 'New workflow',
    'sidebar.nodes': 'Nodes',

    /* ── tabs ── */
    'tab.props': 'Properties',
    'tab.run': 'Run',
    'tab.artifacts': 'Artifacts',
    'tab.diag': 'Diagnostics',

    /* ── canvas ── */
    'canvas.empty': 'Canvas is empty',
    'canvas.emptyHint': 'Click or drag a node from the left "Nodes" panel to start',
    'canvas.unnamed': '(unnamed)',
    'canvas.expand': 'Expand',
    'canvas.collapse': 'Collapse',
    'canvas.deleteNode': 'Delete node',
    'canvas.dropLoopBody': 'Drag in loop body (single step)',
    'canvas.emptyBranch': 'Empty branch',
    'canvas.emptyShort': 'empty',
    'canvas.concurrency': 'concurrency {n}',

    /* ── palette ── */
    'palette.kind.condition': 'Branch',
    'palette.kind.loop': 'Loop',
    'palette.kind.parallel': 'Parallel',
    'palette.delete': 'Delete',
    'palette.noWorkflows': 'No workflows',
    'palette.noWorkflowsHint': 'Click + to create or import',
    'palette.itemTitle': 'Click to add to canvas, or drag to a position\nFields: {fields}',
    'palette.updatedAt': 'Updated {time}',

    /* ── properties ── */
    'props.nodeSection': 'Node · {type}',
    'props.note': 'Note',
    'props.notePlaceholder': 'Node note (saved in ui.note, ignored by engine)',
    'props.extraFields': 'Extra fields',
    'props.extraFieldsHint': 'Fields outside schema, JSON edit, written to YAML as-is',
    'props.selectTool': '(select tool)',
    'props.params': 'Params: ',
    'props.workflow': 'Workflow',
    'props.nameHint': 'Name = filename, use "Save As / New" to rename',
    'props.inputPlaceholder': 'Input variable name, press Enter to add',
    'props.unnamed': '(unnamed)',
    'props.delete': 'Delete',
    'props.clickToDelete': 'Click to delete',
    'props.add': 'Add',

    /* ── runner ── */
    'runner.control': 'Run Control',
    'runner.run': '▶ Run',
    'runner.runTitle': 'Start a run with the current saved workflow',
    'runner.cancel': 'Cancel',
    'runner.cancelTitle': 'Cooperative cancel: stop after current step completes',
    'runner.hardCancel': 'Hard Cancel',
    'runner.hardCancelTitle': 'Cancel immediately (asyncio.Task.cancel)',
    'runner.resume': 'Resume',
    'runner.resumeTitle': 'Resume from checkpoint (skip completed steps)',
    'runner.runId': 'run: {id}',
    'runner.inputs': 'Input Variables',
    'runner.inputPlaceholder': 'Enter value for {name}',
    'runner.noInputs': 'This workflow declares no inputs',
    'runner.history': 'Run History',
    'runner.refresh': 'Refresh',
    'runner.liveSuffix': ' · live',
    'runner.noRuns': 'No runs yet',
    'runner.events': 'Event Stream',
    'runner.llmStream': 'LLM Stream Output',
    'runner.streamPlaceholder': 'Stream output appears here after running an LLM node',
    'runner.streaming': '● streaming',
    'runner.done': '✓ done',
    'runner.chars': '{n} chars',
    'runner.cancelling': 'Cancelling…',
    'runner.wfStarted': 'Workflow {name} started',
    'runner.runStatus': 'Run {status}',
    'runner.artifactProduced': 'Artifact: {id} ({size})',
    'runner.errorPrefix': 'Error: {msg}',

    /* ── artifacts ── */
    'artifacts.title': 'Artifacts',
    'artifacts.titleWithRun': 'Artifacts · {runId}',
    'artifacts.refresh': 'Refresh',
    'artifacts.selectRun': 'Select a run in the "Run" tab to view artifacts',
    'artifacts.noArtifacts': 'No artifacts for this run',
    'artifacts.preview': 'Preview',
    'artifacts.download': '⬇ Download',
    'artifacts.downloadTitle': 'Download',
    'artifacts.unsupportedPreview': 'Preview not supported for {type}, please download',
    'artifacts.unsupportedType': 'this type',

    /* ── diagnostics ── */
    'diag.title': 'Validation Results',
    'diag.ok': '✓ Validation passed, no issues found',
    'diag.clickToLocate': 'Click to locate node',

    /* ── toast ── */
    'toast.saved': 'Saved',
    'toast.saveRejected': 'Save rejected: validation failed',
    'toast.created': 'Created',
    'toast.valid': 'Validation passed',
    'toast.errorsFound': 'Found {n} error(s)',
    'toast.importJson': 'JSON imported, takes effect after saving',
    'toast.importedAs': 'Imported as {name}',
    'toast.deleted': 'Deleted {name}',
    'toast.started': 'Started {id}',
    'toast.cancelGraceful': 'Cooperative cancel requested',
    'toast.cancelHard': 'Hard cancelled',
    'toast.resumed': 'Resumed as {id}',
    'toast.authFailed': 'Authentication failed, please refresh and retry',
    'toast.noServer': 'Cannot connect to server',
    'toast.openFailed': 'Open failed: {msg}',
    'toast.saveFailed': 'Save failed: {msg}',
    'toast.createFailed': 'Create failed: {msg}',
    'toast.validateFailed': 'Validation failed: {msg}',
    'toast.exportFailed': 'Export failed: {msg}',
    'toast.importFailed': 'Import failed: {msg}',
    'toast.deleteFailed': 'Delete failed: {msg}',
    'toast.startFailed': 'Start failed: {msg}',
    'toast.cancelFailed': 'Cancel failed: {msg}',
    'toast.resumeFailed': 'Resume failed: {msg}',
    'toast.previewFailed': 'Preview failed: {msg}',
    'toast.downloadFailed': 'Download failed: {msg}',
    'toast.metaFailed': 'Introspection failed: {msg}',
    'toast.listFailed': 'List load failed: {msg}',
    'toast.saveFirst': 'Please save the workflow first',

    /* ── prompt / confirm ── */
    'prompt.token': 'Server requires an access token (AGENTKIT_SERVER_TOKEN):',
    'prompt.newName': 'New workflow name (letters/digits/_/-):',
    'prompt.nameRule': 'Name allows only letters, digits, _, -',
    'prompt.saveBeforeRun': 'Runs use the server-saved file. Save current changes first?',
    'prompt.dirtySwitch': 'You have unsaved changes, switching will lose them. Continue?',
    'prompt.dirtyNew': 'You have unsaved changes, creating new will lose them. Continue?',
    'prompt.confirmDelete': 'Delete workflow "{name}"? This cannot be undone.',
    'prompt.untitledUnsaved': 'untitled (unsaved)',
    'prompt.nameUnsaved': '{name} (unsaved)',

    /* ── api ── */
    'api.needToken': 'Access token required',
    'api.artifactDownloadFailed': 'Artifact download failed HTTP {status}',
  },
};

/* ── 语言探测 ─────────────────────────────────────────────────── */
function detectLocale() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && SUPPORTED.includes(saved)) return saved;
  } catch { /* localStorage 不可用时回退默认 */ }
  const nav = (navigator.language || '').toLowerCase();
  if (nav.startsWith('en')) return 'en';
  return DEFAULT;
}

let currentLocale = detectLocale();
const listeners = new Set();

/* ── 公共 API ─────────────────────────────────────────────────── */

/**
 * 翻译:key 形如 'module.key';params 替换 {name} 占位符。
 * 缺失键回退到 key 本身(永不抛错,便于发现遗漏)。
 */
export function t(key, params) {
  const entry = dict[currentLocale]?.[key];
  if (entry == null) return key;
  if (!params) return entry;
  return entry.replace(/\{(\w+)\}/g, (_, k) =>
    (params[k] != null ? String(params[k]) : `{${k}}`));
}

export function getLocale() { return currentLocale; }
export function getSupportedLocales() { return SUPPORTED; }

/** 切换语言:持久化 + 更新 <html lang> + 应用静态 DOM + 通知监听器。 */
export function setLocale(locale) {
  if (!SUPPORTED.includes(locale) || locale === currentLocale) return;
  currentLocale = locale;
  try { localStorage.setItem(STORAGE_KEY, locale); } catch { /* 忽略 */ }
  document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en';
  applyToDOM(document);
  for (const fn of [...listeners]) {
    try { fn(locale); } catch (e) { console.error('[i18n] listener error:', e); }
  }
}

/** 监听语言变化;返回取消订阅函数。各模块据此自行重渲染。 */
export function onLocaleChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * 声明式翻译静态 DOM:
 *   data-i18n             → textContent
 *   data-i18n-title       → title 属性
 *   data-i18n-placeholder → placeholder 属性
 */
export function applyToDOM(root = document) {
  root.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-title]').forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
  root.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
}

/* 初始化 <html lang> */
document.documentElement.lang = currentLocale === 'zh' ? 'zh-CN' : 'en';
