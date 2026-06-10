import { beforeEach, describe, expect, it, vi } from 'vitest';

// Mock matchMedia before importing theme.js
const matchMediaMock = vi.fn().mockReturnValue({
  matches: false,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
});
Object.defineProperty(window, 'matchMedia', { value: matchMediaMock, writable: true });

// Mock localStorage
const store = {};
const localStorageMock = {
  getItem: vi.fn((k) => store[k] ?? null),
  setItem: vi.fn((k, v) => {
    store[k] = String(v);
  }),
  removeItem: vi.fn((k) => {
    delete store[k];
  }),
  clear: vi.fn(() => {
    Object.keys(store).forEach((k) => {
      delete store[k];
    });
  }),
};
Object.defineProperty(window, 'localStorage', { value: localStorageMock });

import {
  DEFAULT_PREFS,
  getPrefs,
  initSettingsPanel,
  reset,
  setPref,
  THEME_STORAGE_KEY,
} from '../utils/theme.js';

describe('theme manager', () => {
  beforeEach(() => {
    // Clear all data-* attributes on html
    const html = document.documentElement;
    Object.keys(html.dataset).forEach((k) => {
      delete html.dataset[k];
    });
    // Clear localStorage
    localStorageMock.clear();
    localStorageMock.getItem.mockClear();
    localStorageMock.setItem.mockClear();
  });

  describe('getPrefs()', () => {
    it('returns DEFAULT_PREFS when localStorage is empty', () => {
      const prefs = getPrefs();
      expect(prefs).toEqual(DEFAULT_PREFS);
    });

    it('merges stored values with defaults', () => {
      store[THEME_STORAGE_KEY] = JSON.stringify({ colorMode: 'dark', fontSize: 'large' });
      const prefs = getPrefs();
      expect(prefs.colorMode).toBe('dark');
      expect(prefs.fontSize).toBe('large');
      expect(prefs.themeLight).toBe(DEFAULT_PREFS.themeLight);
    });

    it('handles corrupt localStorage gracefully', () => {
      store[THEME_STORAGE_KEY] = '{invalid json';
      const prefs = getPrefs();
      expect(prefs).toEqual(DEFAULT_PREFS);
    });
  });

  describe('setPref()', () => {
    it('sets data-theme-light on html', () => {
      setPref('themeLight', 'pure');
      expect(document.documentElement.dataset.themeLight).toBe('pure');
    });

    it('sets data-color-mode for non-system values', () => {
      setPref('colorMode', 'dark');
      expect(document.documentElement.dataset.colorMode).toBe('dark');
    });

    it('removes data-color-mode for system', () => {
      document.documentElement.dataset.colorMode = 'dark';
      setPref('colorMode', 'system');
      expect(document.documentElement.dataset.colorMode).toBeUndefined();
    });

    it('sets data-font-size', () => {
      setPref('fontSize', 'large');
      expect(document.documentElement.dataset.fontSize).toBe('large');
    });

    it('sets data-line-height', () => {
      setPref('lineHeight', 'relaxed');
      expect(document.documentElement.dataset.lineHeight).toBe('relaxed');
    });

    it('sets data-motion', () => {
      setPref('motion', 'reduced');
      expect(document.documentElement.dataset.motion).toBe('reduced');
    });

    it('persists to localStorage', () => {
      setPref('themeLight', 'cream');
      expect(localStorageMock.setItem).toHaveBeenCalledWith(
        THEME_STORAGE_KEY,
        expect.stringContaining('"cream"'),
      );
    });

    it('dispatches themechange event', () => {
      const handler = vi.fn();
      window.addEventListener('themechange', handler);
      setPref('fontSize', 'xlarge');
      expect(handler).toHaveBeenCalledOnce();
      expect(handler.mock.calls[0][0].detail).toEqual({ key: 'fontSize', value: 'xlarge' });
      window.removeEventListener('themechange', handler);
    });

    it('sets data-theme-dark', () => {
      setPref('themeDark', 'warm');
      expect(document.documentElement.dataset.themeDark).toBe('warm');
    });
  });

  describe('reset()', () => {
    it('removes all data-* attributes and reapplies defaults', () => {
      document.documentElement.dataset.themeLight = 'pure';
      document.documentElement.dataset.colorMode = 'dark';
      document.documentElement.dataset.fontSize = 'large';
      reset();
      // reset clears user overrides and re-applies DEFAULT_PREFS
      // (except colorMode='system' which is implicit via CSS @media)
      expect(document.documentElement.dataset.themeLight).toBe('warm'); // DEFAULT_PREFS.themeLight
      expect(document.documentElement.dataset.colorMode).toBeUndefined(); // 'system' → no data attr
      expect(document.documentElement.dataset.fontSize).toBe('medium'); // DEFAULT_PREFS.fontSize
    });

    it('clears localStorage', () => {
      reset();
      expect(localStorageMock.setItem).toHaveBeenCalledWith(THEME_STORAGE_KEY, '{}');
    });
  });

  describe('initSettingsPanel()', () => {
    it('skips initialization when .widget-container exists', () => {
      const widget = document.createElement('div');
      widget.className = 'widget-container';
      document.body.appendChild(widget);

      // Should not throw and should return early
      expect(() => initSettingsPanel()).not.toThrow();

      document.body.removeChild(widget);
    });

    it('binds gear button click to open panel', () => {
      // Set up DOM
      document.body.innerHTML = `
        <button id="btnSettings"></button>
        <div id="settingsPanel" aria-hidden="true">
          <div id="settingsBackdrop"></div>
          <aside class="settings-drawer">
            <button id="btnSettingsClose"></button>
            <input name="themeLight" type="radio" value="warm" checked>
            <input name="themeLight" type="radio" value="pure">
            <div class="seg-control" data-key="colorMode">
              <button data-value="light">浅色</button>
              <button data-value="dark">深色</button>
              <button data-value="system">跟随系统</button>
            </div>
            <div class="seg-control" data-key="fontSize">
              <button data-value="small">小</button>
              <button data-value="medium">中</button>
              <button data-value="large">大</button>
              <button data-value="xlarge">超大</button>
            </div>
            <div class="seg-control" data-key="lineHeight">
              <button data-value="compact">紧凑</button>
              <button data-value="standard">标准</button>
              <button data-value="relaxed">宽松</button>
            </div>
            <input type="checkbox" id="switchMotion">
            <button id="btnResetPrefs">重置</button>
          </aside>
        </div>
      `;

      initSettingsPanel();

      const btn = document.getElementById('btnSettings');
      const panel = document.getElementById('settingsPanel');

      btn.click();
      expect(panel.getAttribute('aria-hidden')).toBe('false');
    });

    it('seg-control button click updates aria-pressed and dataset', () => {
      document.body.innerHTML = `
        <button id="btnSettings"></button>
        <div id="settingsPanel" aria-hidden="true">
          <div id="settingsBackdrop"></div>
          <aside class="settings-drawer">
            <button id="btnSettingsClose"></button>
            <div class="seg-control" data-key="colorMode">
              <button data-value="light" aria-pressed="false">浅色</button>
              <button data-value="dark" aria-pressed="false">深色</button>
              <button data-value="system" aria-pressed="true">跟随系统</button>
            </div>
            <div class="seg-control" data-key="fontSize">
              <button data-value="small" aria-pressed="false">小</button>
              <button data-value="medium" aria-pressed="true">中</button>
              <button data-value="large" aria-pressed="false">大</button>
              <button data-value="xlarge" aria-pressed="false">超大</button>
            </div>
            <div class="seg-control" data-key="lineHeight">
              <button data-value="compact" aria-pressed="false">紧凑</button>
              <button data-value="standard" aria-pressed="true">标准</button>
              <button data-value="relaxed" aria-pressed="false">宽松</button>
            </div>
          </aside>
        </div>
      `;

      initSettingsPanel();

      // Click "深色" in color mode
      const darkBtn = document.querySelector('.seg-control[data-key="colorMode"] button[data-value="dark"]');
      darkBtn.click();
      expect(document.documentElement.dataset.colorMode).toBe('dark');
      expect(darkBtn.getAttribute('aria-pressed')).toBe('true');
      // 其他按钮应为 false
      document
        .querySelectorAll('.seg-control[data-key="colorMode"] button')
        .forEach((b) => {
          if (b !== darkBtn) expect(b.getAttribute('aria-pressed')).toBe('false');
        });

      // Click "大" in font size
      const largeBtn = document.querySelector('.seg-control[data-key="fontSize"] button[data-value="large"]');
      largeBtn.click();
      expect(document.documentElement.dataset.fontSize).toBe('large');
      expect(largeBtn.getAttribute('aria-pressed')).toBe('true');
      document
        .querySelectorAll('.seg-control[data-key="fontSize"] button')
        .forEach((b) => {
          if (b !== largeBtn) expect(b.getAttribute('aria-pressed')).toBe('false');
        });

      // Click "宽松" in line height
      const relaxedBtn = document.querySelector('.seg-control[data-key="lineHeight"] button[data-value="relaxed"]');
      relaxedBtn.click();
      expect(document.documentElement.dataset.lineHeight).toBe('relaxed');
      expect(relaxedBtn.getAttribute('aria-pressed')).toBe('true');
      document
        .querySelectorAll('.seg-control[data-key="lineHeight"] button')
        .forEach((b) => {
          if (b !== relaxedBtn) expect(b.getAttribute('aria-pressed')).toBe('false');
        });
    });
  });
});
