/**
 * WCAG AA 对比度自动化测试
 * 验证所有按钮/标签元素的文本/背景对比度 ≥ 4.5:1
 *
 * 原理：用 jsdom 模拟 + getComputedStyle 无法直接用于 CSS 变量，
 * 所以改为纯 token 值静态校验 —— 避免依赖浏览器环境。
 */

import { describe, it, expect } from 'vitest';

// ── WCAG 2.1 对比度计算 ──────────────────────────────────────────────
function srgbToLinear(c) {
  c /= 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function luminance(r, g, b) {
  return (
    0.2126 * srgbToLinear(r) +
    0.7152 * srgbToLinear(g) +
    0.0722 * srgbToLinear(b)
  );
}

function hexToRGB(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function contrastRatio(bg, fg) {
  const [br, bg2, bb] = hexToRGB(bg);
  const [fr, fg2, fb] = hexToRGB(fg);
  const L1 = luminance(br, bg2, bb);
  const L2 = luminance(fr, fg2, fb);
  const [hi, lo] = L1 > L2 ? [L1, L2] : [L2, L1];
  return (hi + 0.05) / (lo + 0.05);
}

// ── 测试用例：每对 token 值必须 ≥ 4.5:1 ──────────────────────────────

describe('WCAG AA token contrast (ratio ≥ 4.5)', () => {
  // 浅色模式 — 默认暖骨白
  it('浅色: --text-soft-gray on --bone-hover', () => {
    expect(contrastRatio('#f0efec', '#595959')).toBeGreaterThanOrEqual(4.5);
  });

  it('浅色: --text-mid-gray on --bone-warm', () => {
    expect(contrastRatio('#f7f6f3', '#5a5a5a')).toBeGreaterThanOrEqual(4.5);
  });

  it('浅色: disabled-fg on disabled-bg', () => {
    expect(contrastRatio('#d4d3d0', '#5a5a5a')).toBeGreaterThanOrEqual(4.5);
  });

  it('浅色: --success-foreground on --success-soft', () => {
    expect(contrastRatio('#edf3ec', '#2a5430')).toBeGreaterThanOrEqual(4.5);
  });

  it('浅色: --warning-foreground on --warning-soft', () => {
    expect(contrastRatio('#fbf3db', '#7a4f00')).toBeGreaterThanOrEqual(4.5);
  });

  it('浅色: --error-foreground on --error-soft', () => {
    expect(contrastRatio('#fdebec', '#8a2424')).toBeGreaterThanOrEqual(4.5);
  });

  it('浅色: --info-foreground on --info-soft', () => {
    expect(contrastRatio('#e1f3fe', '#14507a')).toBeGreaterThanOrEqual(4.5);
  });

  // 浅色模式 — 奶油米主题
  it('cream: --text-secondary on --color-cream', () => {
    expect(contrastRatio('#faf7f2', '#5d5240')).toBeGreaterThanOrEqual(4.5);
  });

  it('cream: --text-muted on --color-cream', () => {
    expect(contrastRatio('#faf7f2', '#645840')).toBeGreaterThanOrEqual(4.5);
  });

  // 浅色模式 — 柔和灰主题
  it('soft: --text-secondary on --color-soft-gray', () => {
    expect(contrastRatio('#f8f9fa', '#5a6573')).toBeGreaterThanOrEqual(4.5);
  });

  it('soft: --text-muted on --color-soft-gray', () => {
    expect(contrastRatio('#f8f9fa', '#636b76')).toBeGreaterThanOrEqual(4.5);
  });

  // 深色模式 — 经典深色
  it('dark: --color-text-muted on --color-surface-base', () => {
    expect(contrastRatio('#0f1117', '#9ca3af')).toBeGreaterThanOrEqual(4.5);
  });

  it('dark: disabled-fg on disabled-bg', () => {
    expect(contrastRatio('#2a2d3a', '#949cac')).toBeGreaterThanOrEqual(4.5);
  });

  it('dark: --color-success on --color-success-bg', () => {
    expect(contrastRatio('#1a3a1f', '#9ed7a4')).toBeGreaterThanOrEqual(4.5);
  });

  it('dark: --color-warning on --color-warning-bg', () => {
    expect(contrastRatio('#3a2e10', '#e0b873')).toBeGreaterThanOrEqual(4.5);
  });

  it('dark: --color-error on --color-error-bg', () => {
    expect(contrastRatio('#3a1818', '#f0a8a6')).toBeGreaterThanOrEqual(4.5);
  });

  // 深色模式 — 暖调深色
  it('warm-dark: --color-text-muted on base', () => {
    expect(contrastRatio('#1a1814', '#907f6a')).toBeGreaterThanOrEqual(4.5);
  });

  it('warm-dark: disabled-fg on disabled-bg', () => {
    expect(contrastRatio('#3a322a', '#b0a080')).toBeGreaterThanOrEqual(4.5);
  });
});
