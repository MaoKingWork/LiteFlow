/* palette.js —— 左栏:工作流列表 + 节点面板。
   仅负责展示与发起意图(经 store 事件),不直接改文档:
     wf:open / wf:new / wf:delete   工作流操作意图
     pal:add                        点击面板项 → 在选中位置后追加节点
   拖拽经 MIME 'application/x-lf-new' 直接对接画布 dropzone。
*/

import { el, replaceChildren, fmtDateTime } from './util.js';
import { emit } from './store.js';
import { TYPE_COLORS } from './canvas.js';
import { t, onLocaleChange } from './i18n.js';

const MIME_NEW = 'application/x-lf-new';

/* 容器类型标注键(展示用) */
const KIND_KEY = { condition: 'palette.kind.condition', loop: 'palette.kind.loop', parallel: 'palette.kind.parallel' };

export function createPalette({ listEl, paletteEl }) {
  // 缓存最近一次渲染数据,供语言切换时重渲染
  let lastWorkflows = null;
  let lastActiveName = null;
  let lastStepTypes = null;

  /** 渲染工作流列表。 */
  function renderWorkflows(workflows, activeName) {
    lastWorkflows = workflows;
    lastActiveName = activeName;
    replaceChildren(listEl,
      (workflows || []).map((wf) =>
        el(`div.lf-wf-item${wf.name === activeName ? '.is-active' : ''}`, {
          onclick: () => emit('wf:open', wf.name),
        },
          el('span.lf-wf-item-name', { title: `${wf.name}\n${t('palette.updatedAt', { time: fmtDateTime(wf.updated_at) })}` }, wf.name),
          el('button.lf-icon-btn', {
            title: t('palette.delete'),
            onclick: (e) => { e.stopPropagation(); emit('wf:delete', wf.name); },
          }, '🗑'),
        ),
      ),
      workflows?.length ? null : el('div.lf-empty-hint', {}, t('palette.noWorkflows'), el('br'), t('palette.noWorkflowsHint')),
    );
  }

  /** 渲染节点面板(内省 step-types 驱动)。 */
  function renderPalette(stepTypes) {
    lastStepTypes = stepTypes;
    replaceChildren(paletteEl,
      (stepTypes || []).map((st) => {
        const color = TYPE_COLORS[st.name] || '#98989D';
        return el('div.lf-pal-item', {
          draggable: 'true',
          style: `--node-c:${color}`,
          title: t('palette.itemTitle', { fields: (st.fields || []).map((f) => f.name).join(', ') }),
          onclick: () => emit('pal:add', st.name),
          ondragstart: (e) => {
            e.dataTransfer.setData(MIME_NEW, st.name);
            e.dataTransfer.effectAllowed = 'copy';
          },
        },
          el('span.lf-pal-dot'),
          el('span.lf-pal-name', {}, st.name),
          KIND_KEY[st.name] ? el('span.lf-pal-kind', {}, t(KIND_KEY[st.name])) : null,
        );
      }),
    );
  }

  // 语言切换时按缓存数据重渲染
  onLocaleChange(() => {
    if (lastStepTypes) renderPalette(lastStepTypes);
    if (lastWorkflows) renderWorkflows(lastWorkflows, lastActiveName);
  });

  return { renderWorkflows, renderPalette };
}
