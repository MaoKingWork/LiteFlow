/* diagnostics.js —— 校验诊断面板。
   渲染 POST /api/workflows/validate 的 {is_valid, diagnostics[]};
   每条诊断含 severity/code/path("steps[2].then[1]"),点击定位画布节点。
*/

import { el, replaceChildren } from './util.js';
import { emit } from './store.js';
import { t, onLocaleChange } from './i18n.js';

export function createDiagnostics({ paneEl, badgeEl }) {
  let lastReport = null;

  /** 渲染校验报告;返回错误数。 */
  function render(report) {
    lastReport = report;
    const diags = report?.diagnostics || [];
    const errCount = diags.filter((d) => d.severity !== 'warning').length;

    // 徽标
    badgeEl.hidden = diags.length === 0;
    badgeEl.textContent = String(diags.length);
    badgeEl.style.background = errCount ? 'var(--alert)' : 'var(--warn)';

    replaceChildren(paneEl,
      el('div.lf-form-sec', {},
        el('div.lf-form-sec-head', {}, t('diag.title')),
        diags.length === 0
          ? el('div.lf-diag-ok', {}, t('diag.ok'))
          : diags.map((d) =>
            el(`div.lf-diag-item.sev-${d.severity || 'error'}`, {
              onclick: () => { if (d.path) emit('diag:goto', d.path); },
              title: d.path ? t('diag.clickToLocate') : '',
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

  // 语言切换时按上次报告重渲染
  onLocaleChange(() => { if (lastReport) render(lastReport); });

  return { render };
}
