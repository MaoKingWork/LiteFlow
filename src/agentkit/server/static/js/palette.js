/* palette.js —— 左栏:工作流列表 + 节点面板。
   仅负责展示与发起意图(经 store 事件),不直接改文档:
     wf:open / wf:new / wf:delete   工作流操作意图
     pal:add                        点击面板项 → 在选中位置后追加节点
   拖拽经 MIME 'application/x-lf-new' 直接对接画布 dropzone。
*/

import { el, replaceChildren, fmtDateTime } from './util.js';
import { emit } from './store.js';
import { TYPE_COLORS } from './canvas.js';

const MIME_NEW = 'application/x-lf-new';

/* 容器类型标注(展示用) */
const KIND_LABEL = { condition: '分支', loop: '循环', parallel: '并发' };

export function createPalette({ listEl, paletteEl }) {
  /** 渲染工作流列表。 */
  function renderWorkflows(workflows, activeName) {
    replaceChildren(listEl,
      (workflows || []).map((wf) =>
        el(`div.lf-wf-item${wf.name === activeName ? '.is-active' : ''}`, {
          onclick: () => emit('wf:open', wf.name),
        },
          el('span.lf-wf-item-name', { title: `${wf.name}\n更新于 ${fmtDateTime(wf.updated_at)}` }, wf.name),
          el('button.lf-icon-btn', {
            title: '删除',
            onclick: (e) => { e.stopPropagation(); emit('wf:delete', wf.name); },
          }, '🗑'),
        ),
      ),
      workflows?.length ? null : el('div.lf-empty-hint', {}, '暂无工作流', el('br'), '点 ＋ 新建或导入'),
    );
  }

  /** 渲染节点面板(内省 step-types 驱动)。 */
  function renderPalette(stepTypes) {
    replaceChildren(paletteEl,
      (stepTypes || []).map((t) => {
        const color = TYPE_COLORS[t.name] || '#98989D';
        return el('div.lf-pal-item', {
          draggable: 'true',
          style: `--node-c:${color}`,
          title: `点击添加到画布,或拖入指定位置\n字段: ${(t.fields || []).map((f) => f.name).join(', ')}`,
          onclick: () => emit('pal:add', t.name),
          ondragstart: (e) => {
            e.dataTransfer.setData(MIME_NEW, t.name);
            e.dataTransfer.effectAllowed = 'copy';
          },
        },
          el('span.lf-pal-dot'),
          el('span.lf-pal-name', {}, t.name),
          KIND_LABEL[t.name] ? el('span.lf-pal-kind', {}, KIND_LABEL[t.name]) : null,
        );
      }),
    );
  }

  return { renderWorkflows, renderPalette };
}
