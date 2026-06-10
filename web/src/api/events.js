/**
 * 事件发布/订阅系统
 */
const _listeners = {};

/** 订阅事件，返回取消订阅函数 */
export function on(event, callback) {
  if (!_listeners[event]) _listeners[event] = [];
  _listeners[event].push(callback);
  return () => off(event, callback);
}

/** 取消订阅 */
export function off(event, callback) {
  if (_listeners[event]) {
    _listeners[event] = _listeners[event].filter((cb) => cb !== callback);
  }
}

/** 触发事件 */
export function emit(event, data) {
  (_listeners[event] || []).forEach((cb) => {
    try {
      cb(data);
    } catch (_e) {}
  });
}
