/**
 * 知识库管理模块
 * 从 admin-settings.js 提取
 */
import { addKnowledgeDocs, getKnowledgeStats, seedKnowledge, syncKnowledge } from './api/rest.js';
import { showToast } from './utils/toast.js';

/** 加载知识库统计信息 */
export async function loadKnowledgeStats() {
  const data = await getKnowledgeStats();
  if (!data) return;
  const el = document.getElementById('knowledgeStats');
  if (!el) return;
  if (!data.available) {
    el.replaceChildren();
    const statEl = document.createElement('div');
    statEl.className = 'admin-stat';
    const labelSpan = document.createElement('span');
    labelSpan.className = 'label';
    labelSpan.textContent = '状态';
    const valSpan = document.createElement('span');
    valSpan.className = 'value';
    valSpan.style.color = '#ef4444';
    valSpan.textContent = '不可用';
    statEl.appendChild(labelSpan);
    statEl.appendChild(valSpan);
    el.appendChild(statEl);
    return;
  }
  const cols = data.collections || {};

  el.replaceChildren();

  const collections = [
    { label: '产品知识', val: `${cols.product_knowledge || 0} 条`, color: '' },
    { label: 'FAQ', val: `${cols.faq || 0} 条`, color: '' },
    { label: '技术支持', val: `${cols.tech_support || 0} 条`, color: '' },
    { label: '投诉知识', val: `${cols.complaint_knowledge || 0} 条`, color: '' },
    { label: '总计', val: `${data.total || 0} 条`, color: 'var(--primary)' },
  ];

  collections.forEach((col) => {
    const statEl = document.createElement('div');
    statEl.className = 'admin-stat';

    const labelSpan = document.createElement('span');
    labelSpan.className = 'label';
    labelSpan.textContent = col.label;
    statEl.appendChild(labelSpan);

    const valSpan = document.createElement('span');
    valSpan.className = 'value';
    valSpan.textContent = col.val;
    if (col.color) valSpan.style.color = col.color;
    statEl.appendChild(valSpan);

    el.appendChild(statEl);
  });
}

/** 重新种子知识库 */
export async function reseedKnowledge() {
  if (!confirm('确定重新种子？这会覆盖现有数据。')) return;
  try {
    const data = await seedKnowledge();
    showToast(data?.message || '操作完成', 'success');
    loadKnowledgeStats();
  } catch (e) {
    showToast(`种子失败: ${e.message}`, 'error');
  }
}

/** 从 ERP 同步知识库 */
export async function syncFromErp() {
  showToast('正在从 ERP 同步...', 'success');
  try {
    const data = await syncKnowledge();
    showToast(data?.message || '同步完成', 'success');
    loadKnowledgeStats();
  } catch (e) {
    showToast(`同步失败: ${e.message}`, 'error');
  }
}

/** 处理添加文档 */
export async function handleAddDocs() {
  const collection = document.getElementById('addDocCollection').value;
  const text = document.getElementById('addDocText').value.trim();
  if (!text) {
    showToast('请输入文档内容', 'error');
    return;
  }
  const documents = text
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!documents.length) {
    showToast('文档内容不能为空', 'error');
    return;
  }
  try {
    const result = await addKnowledgeDocs(collection, documents);
    showToast(result?.message || '添加完成', 'success');
    document.getElementById('addDocText').value = '';
    loadKnowledgeStats();
  } catch (e) {
    showToast(`添加失败: ${e.message}`, 'error');
  }
}
