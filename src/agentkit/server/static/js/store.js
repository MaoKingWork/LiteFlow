/* store.js —— 轻量发布/订阅状态中心。
   所有跨模块通信经此总线,模块间不直接相互引用,保证可维护性。

   事件约定:
     meta            内省数据就绪 {stepTypes, tools}
     workflows       工作流列表变化 [{name, path, updated_at}]
     doc             当前文档变化(含加载/编辑) {config, name, dirty}
     dirty           脏标记变化 bool
     selection       画布选中变化 {path: [{field, index}|{field}], step: obj|null}
     run:status      节点运行状态图变化 Map<stepId, {status, duration_ms, ...}>
     run:list        run 列表变化
     run:active      当前查看的 run 变化 {runId, status} (null 表示退出运行视图)
     stream:focus    请求聚焦某 step 的流式视图 stepId
     diagnostics     校验结果 {is_valid, diagnostics[]}
     artifacts       产物列表变化 [{id, step_id, uri, content_type, size, md5, summary}]
*/

const listeners = new Map(); // event -> Set<fn>

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => off(event, fn);
}

export function off(event, fn) {
  listeners.get(event)?.delete(fn);
}

export function emit(event, payload) {
  for (const fn of [...(listeners.get(event) || [])]) {
    try {
      fn(payload);
    } catch (e) {
      console.error(`[store] listener error on "${event}":`, e);
    }
  }
}
