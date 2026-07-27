/* properties.js —— 属性面板(内省 schema 驱动表单)。
   - 选中 step:按 /api/meta/step-types 的 fields schema 生成表单;
     容器字段(then/else/step/branches)不在表单出现(画布上编辑)。
   - 未选中:工作流级配置(inputs / providers / agents / ui 备注)。
   所有编辑原地修改文档对象并 emit('doc', {kind:'edit'}),未知字段天然保留。
*/

import { el, replaceChildren, debounce } from './util.js';
import { emit } from './store.js';
import { CONTAINER_FIELDS } from './doc.js';

/* 长文本字段(用 textarea + 等宽) */
const TEXTAREA_FIELDS = new Set(['prompt', 'system', 'when', 'iter', 'template', 'content', 'skill']);
/* 复杂结构字段(JSON 编辑) */
const JSON_FIELDS = new Set(['params', 'options', 'formats', 'input', 'args', 'mapping', 'outputs', 'items']);
/* 数值字段 */
const NUMBER_FIELDS = new Set(['temperature', 'timeout', 'max_tokens', 'max_concurrency', 'top_p', 'seed', 'retries', 'retry_interval', 'count', 'width', 'height', 'n']);
/* 布尔字段 */
const BOOL_FIELDS = new Set(['stream', 'thinking', 'merge', 'enabled', 'strict']);

export function createProperties({ paneEl }) {
  let ctx = null; // {docModel, selection, schemas, tools}

  /** 由 main 在 selection/doc 加载时调用。 */
  function render(context) {
    ctx = context;
    const { step } = context.selection || {};
    if (step) renderStepForm(step);
    else renderWorkflowForm();
  }

  /* ── step 表单 ────────────────────────────────────────────── */
  function renderStepForm(step) {
    const schema = ctx.schemas?.[step.type];
    const containerFields = new Set((CONTAINER_FIELDS[step.type] || []).map((c) => c.name));
    const fields = schema?.fields?.filter((f) => f.name !== 'ui' && !containerFields.has(f.name))
      || Object.keys(step).filter((k) => k !== 'ui' && !containerFields.has(k));

    const secs = [
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, `节点 · ${step.type}`),
        fields.map((f) => fieldControl(step, normalizeField(f))),
      ),
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, '备注'),
        el('div.lf-field', {},
          el('textarea.lf-textarea', {
            rows: 2,
            placeholder: '节点备注(保存在 ui.note,引擎忽略)',
            oninput: debounce((e) => {
              step.ui = { ...(step.ui || {}), note: e.target.value };
              touch();
            }, 250),
          }, step.ui?.note || ''),
        ),
      ),
    ];
    // 已知 schema 之外的额外字段(JSON 兜底,保证不丢)
    if (schema) {
      const known = new Set(schema.fields.map((f) => f.name));
      const extraKeys = Object.keys(step).filter((k) => !known.has(k) && k !== 'ui');
      if (extraKeys.length) {
        secs.push(el('div.lf-form-sec', {},
          el('div.lf-form-sec-head', {}, '扩展字段'),
          jsonArea(extraKeys.reduce((o, k) => (o[k] = step[k], o), {}), (val) => {
            for (const k of extraKeys) delete step[k];
            Object.assign(step, val);
          }),
          el('div.lf-hint', {}, 'schema 之外的字段,JSON 编辑,保存时原样写入 YAML'),
        ));
      }
    }
    replaceChildren(paneEl, ...secs);
  }

  function normalizeField(f) {
    return typeof f === 'string' ? { name: f, type: 'any', required: false } : f;
  }

  /** 单字段控件:按名称/类型分派。 */
  function fieldControl(step, f) {
    const name = f.name;
    const label = el('div.lf-label', {},
      el('span', {}, name, f.required ? el('span.req', {}, ' *') : null),
      el('span.ftype', {}, f.type || ''),
    );
    const commit = () => touch();

    if (name === 'tool') {
      return el('div.lf-field', {}, label, toolSelect(step), toolSchemaHint(step));
    }
    if (name === 'agent') {
      return el('div.lf-field', {}, label, agentInput(step));
    }
    if (TEXTAREA_FIELDS.has(name)) {
      return el('div.lf-field', {}, label, el('textarea.lf-textarea.mono', {
        rows: 4,
        oninput: debounce((e) => { step[name] = e.target.value; commit(); }, 250),
      }, step[name] ?? ''));
    }
    if (NUMBER_FIELDS.has(name)) {
      return el('div.lf-field', {}, label, el('input.lf-input.mono', {
        type: 'number', step: 'any', value: step[name] ?? '',
        oninput: debounce((e) => {
          step[name] = e.target.value === '' ? undefined : Number(e.target.value);
          if (step[name] === undefined) delete step[name];
          commit();
        }, 250),
      }));
    }
    if (BOOL_FIELDS.has(name) || typeof step[name] === 'boolean') {
      return el('div.lf-field', {}, label, el('label.lf-check-row', {},
        el('input', {
          type: 'checkbox',
          ...(step[name] ? { checked: 'checked' } : {}),
          onchange: (e) => { step[name] = e.target.checked; commit(); },
        }),
        el('span', {}, step[name] ? 'true' : 'false'),
      ));
    }
    if (JSON_FIELDS.has(name) || (step[name] != null && typeof step[name] === 'object')) {
      return el('div.lf-field', {}, label, jsonArea(step[name], (val) => { step[name] = val; }));
    }
    // 默认:单行文本
    return el('div.lf-field', {}, label, el('input.lf-input.mono', {
      type: 'text', value: step[name] ?? '',
      oninput: debounce((e) => { step[name] = e.target.value; commit(); }, 250),
    }));
  }

  /** tool 字段:下拉选择(内省 tools),可手输。 */
  function toolSelect(step) {
    const tools = ctx.tools || [];
    const sel = el('select.lf-select', {
      onchange: (e) => { step.tool = e.target.value; touch(); render(ctx); },
    },
      el('option', { value: '' }, '(选择工具)'),
      tools.map((t) => el('option', { value: t.name, ...(t.name === step.tool ? { selected: 'selected' } : {}) },
        `${t.name}${t.description ? ` — ${t.description.slice(0, 30)}` : ''}`)),
    );
    return sel;
  }

  /** 选中工具的 param schema 摘要提示。 */
  function toolSchemaHint(step) {
    const tool = (ctx.tools || []).find((t) => t.name === step.tool);
    const props = tool?.param_model_schema?.properties;
    if (!props) return null;
    const required = new Set(tool.param_model_schema.required || []);
    return el('div.lf-hint', {},
      '参数: ' + Object.entries(props)
        .map(([k, v]) => `${k}${required.has(k) ? '*' : ''}:${v.type ?? 'any'}`).join(', '));
  }

  /** agent 字段:文本输入 + 文档内 agents datalist 联想。 */
  function agentInput(step) {
    const listId = 'lf-agents-dl';
    return el('div', {},
      el('input.lf-input.mono', {
        type: 'text', value: step.agent ?? '', list: listId,
        oninput: debounce((e) => { step.agent = e.target.value; touch(); }, 250),
      }),
      el('datalist', { id: listId },
        (ctx.docModel.agents || []).map((a) => el('option', { value: a.name }))),
    );
  }

  /* ── 工作流配置表单 ───────────────────────────────────────── */
  function renderWorkflowForm() {
    const docModel = ctx.docModel;
    replaceChildren(paneEl,
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, '工作流'),
        el('div.lf-field', {},
          el('div.lf-label', {}, el('span', {}, 'name')),
          el('input.lf-input.mono', { type: 'text', value: docModel.name ?? '', disabled: 'disabled' }),
          el('div.lf-hint', {}, '名称 = 文件名,重命名请用「另存/新建」'),
        ),
        el('div.lf-field', {},
          el('div.lf-label', {}, el('span', {}, 'inputs'), el('span.ftype', {}, 'string[]')),
          listEditor(docModel.inputs || [], {
            placeholder: '输入变量名,回车添加',
            onChange: (arr) => { docModel.inputs = arr; },
          }),
        ),
      ),
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, 'Providers',
          addBtn(() => { (docModel.providers ||= []).push({ name: '', model: '', api_key: '' }); touch(); render(ctx); })),
        (docModel.providers || []).map((p, i) =>
          kvCard(p, ['name', 'model', 'api_key', 'base_url'], () => {
            docModel.providers.splice(i, 1); touch(); render(ctx);
          }),
        ),
      ),
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, 'Agents',
          addBtn(() => { (docModel.agents ||= []).push({ name: '', provider: '', model: '', system: '', temperature: 0.7 }); touch(); render(ctx); })),
        (docModel.agents || []).map((a, i) =>
          kvCard(a, ['name', 'provider', 'model', 'temperature', 'system'], () => {
            docModel.agents.splice(i, 1); touch(); render(ctx);
          }),
        ),
      ),
    );
  }

  /** providers/agents 卡片:常见键内联编辑,未知键保留。 */
  function kvCard(obj, keys, onRemove) {
    return el('div.lf-kv-card', {},
      el('div.lf-kv-card-head', {},
        el('b', {}, obj.name || '(未命名)'),
        iconBtn('✕', '删除', onRemove),
      ),
      keys.map((k) => el('div.lf-field', {},
        el('div.lf-label', {}, el('span', {}, k)),
        k === 'system'
          ? el('textarea.lf-textarea.mono', {
              rows: 3,
              oninput: debounce((e) => { obj[k] = e.target.value; touch(); }, 250),
            }, obj[k] ?? '')
          : el('input.lf-input.mono', {
              type: 'text', value: obj[k] ?? '',
              oninput: debounce((e) => { obj[k] = e.target.value; touch(); }, 250),
            }),
      )),
    );
  }

  /** string[] 编辑器:chips + 输入框。 */
  function listEditor(arr, { placeholder, onChange }) {
    const wrap = el('div');
    const redraw = () => {
      replaceChildren(wrap,
        el('div.lf-node-io', {},
          arr.map((item, i) =>
            el('span.lf-chip', { title: '点击删除' }, String(item),
              el('button.lf-icon-btn', {
                onclick: () => { arr.splice(i, 1); onChange([...arr]); touch(); redraw(); },
              }, '✕')),
          ),
        ),
        el('input.lf-input.mono', {
          type: 'text', placeholder,
          onkeydown: (e) => {
            if (e.key === 'Enter' && e.target.value.trim()) {
              arr.push(e.target.value.trim());
              onChange([...arr]); touch(); redraw();
            }
          },
        }),
      );
    };
    redraw();
    return wrap;
  }

  /* ── 通用小件 ─────────────────────────────────────────────── */
  /** JSON 编辑区:失焦解析,非法 JSON 红框且不写回。 */
  function jsonArea(value, apply) {
    const ta = el('textarea.lf-textarea.mono', {
      rows: 4, spellcheck: 'false',
    }, JSON.stringify(value ?? null, null, 2));
    ta.addEventListener('blur', () => {
      try {
        const parsed = JSON.parse(ta.value || 'null');
        ta.style.borderColor = '';
        apply(parsed);
        touch();
      } catch {
        ta.style.borderColor = 'var(--alert)';
      }
    });
    return ta;
  }

  const addBtn = (onclick) => el('button.lf-icon-btn', { title: '添加', onclick }, '＋');
  const iconBtn = (text, title, onclick) => el('button.lf-icon-btn', { title, onclick }, text);

  /** 标脏并广播编辑。 */
  function touch() { emit('doc', { kind: 'edit', source: 'props' }); }

  return { render };
}
