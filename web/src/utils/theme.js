/**
 * 主题管理器 + 设置面板 UI
 * 管理用户偏好：浅色主题、颜色模式、深色主题、字号、行高、动画
 */

export const THEME_STORAGE_KEY = 'user-prefs';

export const DEFAULT_PREFS = {
  themeLight: 'warm',
  colorMode: 'system',
  themeDark: 'classic',
  fontSize: 'medium',
  lineHeight: 'standard',
  motion: 'full',
};

const _DATASET_KEYS = {
  themeLight: 'themeLight',
  colorMode: 'colorMode',
  themeDark: 'themeDark',
  fontSize: 'fontSize',
  lineHeight: 'lineHeight',
  motion: 'motion',
};

// ── localStorage 降级层 ──

let _memoryStore = {};
let _useStorage = true;

try {
  localStorage.getItem('__theme_probe__');
} catch {
  _useStorage = false;
}

function _readStorage() {
  if (_useStorage) {
    try {
      const raw = localStorage.getItem(THEME_STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  }
  return _memoryStore;
}

function _writeStorage(prefs) {
  if (_useStorage) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(prefs));
    } catch {
      /* ignore */
    }
  }
  _memoryStore = { ...prefs };
}

// ── 偏好读写 ──

export function getPrefs() {
  const stored = _readStorage();
  return { ...DEFAULT_PREFS, ...stored };
}

export function setPref(key, value) {
  const prefs = getPrefs();
  prefs[key] = value;
  _writeStorage(prefs);
  _applyPref(key, value);
  window.dispatchEvent(new CustomEvent('themechange', { detail: { key, value } }));
}

// ── 应用偏好到 DOM ──

function _applyPref(key, value) {
  const html = document.documentElement;
  switch (key) {
    case 'themeLight':
      html.dataset.themeLight = value;
      break;
    case 'colorMode':
      if (value === 'system') {
        delete html.dataset.colorMode;
        _applySystemColorMode();
      } else {
        html.dataset.colorMode = value;
      }
      _updateDeepDarkGroupVisibility();
      break;
    case 'themeDark':
      html.dataset.themeDark = value;
      break;
    case 'fontSize':
      html.dataset.fontSize = value;
      break;
    case 'lineHeight':
      html.dataset.lineHeight = value;
      break;
    case 'motion':
      html.dataset.motion = value;
      break;
  }
}

function _applySystemColorMode() {
  const _prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  // 不设置 data-color-mode，让 CSS @media 生效
  // 但需要通知 UI 更新
  _updateDeepDarkGroupVisibility();
}

function _updateDeepDarkGroupVisibility() {
  // 在设置面板中显示/隐藏深色主题选项
  const darkGroup = document.querySelector('.settings-group[data-show-when="dark"]');
  if (!darkGroup) return;

  const html = document.documentElement;
  const mode = html.dataset.colorMode;
  const isDark =
    mode === 'dark' || (!mode && window.matchMedia('(prefers-color-scheme: dark)').matches);
  darkGroup.style.display = isDark ? 'block' : 'none';
}

// ── 恢复默认 ──

export function reset() {
  const html = document.documentElement;
  delete html.dataset.themeLight;
  delete html.dataset.colorMode;
  delete html.dataset.themeDark;
  delete html.dataset.fontSize;
  delete html.dataset.lineHeight;
  delete html.dataset.motion;

  _writeStorage({});

  // 重新应用默认值（colorMode 默认 'system'，不设 data 属性，依赖 CSS @media）
  Object.entries(DEFAULT_PREFS).forEach(([key, value]) => {
    if (key !== 'colorMode') {
      _applyPref(key, value);
    }
  });

  window.dispatchEvent(new CustomEvent('themechange', { detail: { key: 'all', value: 'reset' } }));
}

// ── 系统偏好监听 ──

let _mediaQuery = null;
let _mediaHandler = null;

export function watchSystem() {
  unwatchSystem();

  _mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
  _mediaHandler = () => {
    const prefs = getPrefs();
    if (prefs.colorMode === 'system') {
      _updateDeepDarkGroupVisibility();
    }
  };
  _mediaQuery.addEventListener('change', _mediaHandler);

  return unwatchSystem;
}

function unwatchSystem() {
  if (_mediaQuery && _mediaHandler) {
    _mediaQuery.removeEventListener('change', _mediaHandler);
    _mediaQuery = null;
    _mediaHandler = null;
  }
}

// ── 初始化主题（读取 localStorage 应用到 DOM） ──

export function initTheme() {
  const prefs = getPrefs();
  const html = document.documentElement;

  // 按顺序应用所有偏好
  if (prefs.themeLight && prefs.themeLight !== DEFAULT_PREFS.themeLight) {
    html.dataset.themeLight = prefs.themeLight;
  }
  if (prefs.colorMode && prefs.colorMode !== 'system') {
    html.dataset.colorMode = prefs.colorMode;
  }
  if (prefs.themeDark && prefs.themeDark !== DEFAULT_PREFS.themeDark) {
    html.dataset.themeDark = prefs.themeDark;
  }
  if (prefs.fontSize && prefs.fontSize !== DEFAULT_PREFS.fontSize) {
    html.dataset.fontSize = prefs.fontSize;
  }
  if (prefs.lineHeight && prefs.lineHeight !== DEFAULT_PREFS.lineHeight) {
    html.dataset.lineHeight = prefs.lineHeight;
  }
  if (prefs.motion && prefs.motion !== DEFAULT_PREFS.motion) {
    html.dataset.motion = prefs.motion;
  }

  // 监听系统颜色方案变化
  watchSystem();
}

// ── 设置面板 UI ──

export function initSettingsPanel() {
  // Widget 隔离：跳过 widget 页面
  if (
    document.body.classList.contains('widget-page') ||
    document.querySelector('.widget-container')
  ) {
    return;
  }

  const btn = document.getElementById('btnSettings');
  const panel = document.getElementById('settingsPanel');
  const backdrop = document.getElementById('settingsBackdrop');
  const closeBtn = document.getElementById('btnSettingsClose');

  if (!btn || !panel) return;

  // 打开/关闭面板
  function openPanel() {
    panel.setAttribute('aria-hidden', 'false');
    _syncPanelState();
    // 焦点移到关闭按钮
    setTimeout(() => closeBtn?.focus(), 100);
  }

  function closePanel() {
    panel.setAttribute('aria-hidden', 'true');
    btn.focus();
  }

  btn.addEventListener('click', openPanel);
  backdrop?.addEventListener('click', closePanel);
  closeBtn?.addEventListener('click', closePanel);

  // Esc 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && panel.getAttribute('aria-hidden') === 'false') {
      e.preventDefault();
      closePanel();
    }
  });

  // 焦点陷阱
  panel.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const focusable = panel.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  // 绑定控件事件
  _bindControls();
  _syncPanelState();
}

function _bindControls() {
  const _prefs = getPrefs();

  // 浅色主题 radio
  document.querySelectorAll('input[name="themeLight"]').forEach((radio) => {
    radio.addEventListener('change', () => setPref('themeLight', radio.value));
  });

  // 颜色模式分段控制
  const colorModeSeg = document.querySelector('.seg-control[data-key="colorMode"]');
  if (colorModeSeg) {
    colorModeSeg.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        setPref('colorMode', btn.dataset.value);
        _syncPanelState();
      });
    });
  }

  // 深色主题 radio
  document.querySelectorAll('input[name="themeDark"]').forEach((radio) => {
    radio.addEventListener('change', () => setPref('themeDark', radio.value));
  });

  // 字号分段控制
  const fontSizeSeg = document.querySelector('.seg-control[data-key="fontSize"]');
  if (fontSizeSeg) {
    fontSizeSeg.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        setPref('fontSize', btn.dataset.value);
        _syncPanelState();
      });
    });
  }

  // 行高分段控制
  const lineHeightSeg = document.querySelector('.seg-control[data-key="lineHeight"]');
  if (lineHeightSeg) {
    lineHeightSeg.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        setPref('lineHeight', btn.dataset.value);
        _syncPanelState();
      });
    });
  }

  // 减少动画开关
  const motionSwitch = document.getElementById('switchMotion');
  if (motionSwitch) {
    motionSwitch.addEventListener('change', () => {
      const reduced = motionSwitch.checked;
      motionSwitch.setAttribute('aria-checked', String(reduced));
      setPref('motion', reduced ? 'reduced' : 'full');
    });
  }

  // 恢复默认按钮
  const resetBtn = document.getElementById('btnResetPrefs');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      reset();
      _syncPanelState();
    });
  }
}

function _syncPanelState() {
  const prefs = getPrefs();

  // 浅色主题 radio
  const lightRadio = document.querySelector(
    `input[name="themeLight"][value="${prefs.themeLight}"]`,
  );
  if (lightRadio) lightRadio.checked = true;

  // 颜色模式分段
  const colorModeSeg = document.querySelector('.seg-control[data-key="colorMode"]');
  if (colorModeSeg) {
    colorModeSeg.querySelectorAll('button').forEach((btn) => {
      btn.setAttribute('aria-pressed', btn.dataset.value === prefs.colorMode ? 'true' : 'false');
    });
  }

  // 深色主题 radio
  const darkRadio = document.querySelector(`input[name="themeDark"][value="${prefs.themeDark}"]`);
  if (darkRadio) darkRadio.checked = true;

  // 字号分段
  const fontSizeSeg = document.querySelector('.seg-control[data-key="fontSize"]');
  if (fontSizeSeg) {
    fontSizeSeg.querySelectorAll('button').forEach((btn) => {
      btn.setAttribute('aria-pressed', btn.dataset.value === prefs.fontSize ? 'true' : 'false');
    });
  }

  // 行高分段
  const lineHeightSeg = document.querySelector('.seg-control[data-key="lineHeight"]');
  if (lineHeightSeg) {
    lineHeightSeg.querySelectorAll('button').forEach((btn) => {
      btn.setAttribute('aria-pressed', btn.dataset.value === prefs.lineHeight ? 'true' : 'false');
    });
  }

  // 减少动画开关
  const motionSwitch = document.getElementById('switchMotion');
  if (motionSwitch) {
    motionSwitch.checked = prefs.motion === 'reduced';
    motionSwitch.setAttribute('aria-checked', String(motionSwitch.checked));
  }

  // 深色主题组可见性
  _updateDeepDarkGroupVisibility();
}
