/* diagnostics.js —— 校验诊断面板。
   渲染 POST /api/workflows/validate 的 {is_valid, diagnostics[]};
   每条诊断含 severity/code/path("steps[2].then[1]"),点击定位画布节点。
*/

import { el, replaceChildren } from './util.js';
import { emit } from './store.js';

export function createDiagnostics({ paneEl, badgeEl }) {
  /** 渲染校验报告;返回错误数。 */
  function render(report) {
    const diags = report?.diagnostics || [];
    const errCount = diags.filter((d) => d.severity !== 'warning').length;

    // 徽标
    badgeEl.hidden = diags.length === 0;
    badgeEl.textContent = String(diags.length);
    badgeEl.style.background = errCount ? 'var(--alert)' : 'var(--warn)';

    replaceChildren(paneEl,
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, '校验结果'),
        diags.length === 0
          ? el('div.lf-diag-ok', {}, '✓ 校验通过,未发现问题')
          : diags.map((d) =>
            el(`div.lf-diag-item.sev-${d.severity || 'error'}`, {
              onclick: () => { if (d.path) emit('diag:goto', d.path); },
              title: d.path ? '点击定位到节点' : '',
            },
              el('div.lf-diag-code', {}, d.code || 'error'),
              d.path ? el('div.lf-diag-path', {}, d.path) : null,
              el('div.lf-diag-msg', {}, d.message || d.msg || ''),
            ),
          ),
      ),
    );
    // 画布标注
    emit('diag:paths', diags.map((d) => d.path).filter(Boolean));
    return errCount;
  }

  return { render };
}
