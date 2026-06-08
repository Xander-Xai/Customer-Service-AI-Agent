/**
 * 快捷键模块
 */
import { startNewChat } from './sessions.js';

export function toggleShortcuts() {
  const overlay = document.getElementById('shortcutsOverlay');
  if (overlay) overlay.classList.toggle('open');
}

export function closeShortcuts() {
  const overlay = document.getElementById('shortcutsOverlay');
  if (overlay) overlay.classList.remove('open');
}

function handleGlobalKeydown(e) {
  const isInput = ['INPUT', 'TEXTAREA'].includes(e.target.tagName);

  if (e.key === 'Escape') { closeShortcuts(); return; }
  if (e.key === '?' && !isInput) { e.preventDefault(); toggleShortcuts(); return; }
  if (e.key === '/' && !isInput) {
    e.preventDefault();
    const input = document.getElementById('chatInput');
    if (input) input.focus();
    return;
  }
  if (e.key === 'n' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    startNewChat();
    return;
  }
}

export function initShortcuts() {
  const btnShortcuts = document.getElementById('btnShortcuts');
  const shortcutsOverlay = document.getElementById('shortcutsOverlay');

  if (btnShortcuts) btnShortcuts.addEventListener('click', toggleShortcuts);
  if (shortcutsOverlay) shortcutsOverlay.addEventListener('click', (e) => {
    if (e.target === shortcutsOverlay) closeShortcuts();
  });
  document.addEventListener('keydown', handleGlobalKeydown);
}
