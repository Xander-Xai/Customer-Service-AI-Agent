import { getUsers, updateUserRole } from './api/rest.js';
import { showToast } from './utils/toast.js';

/**
 * 加载并渲染用户列表
 */
export async function loadUsers() {
  const data = await getUsers();
  if (!data) return;

  const ROLES = ['customer', 'agent', 'supervisor', 'admin'];
  const tbody = document.getElementById('usersTableBody');
  if (!tbody) return;

  // 清空表格内容
  tbody.innerHTML = '';

  data.users.forEach((u) => {
    const tr = document.createElement('tr');

    // ID
    const tdId = document.createElement('td');
    tdId.textContent = String(u.user_id);
    tr.appendChild(tdId);

    // 用户名
    const tdUsername = document.createElement('td');
    tdUsername.textContent = u.username;
    tr.appendChild(tdUsername);

    // 显示名
    const tdDisplay = document.createElement('td');
    tdDisplay.textContent = u.display_name || '';
    tr.appendChild(tdDisplay);

    // 角色标签
    const tdRole = document.createElement('td');
    const roleSpan = document.createElement('span');
    roleSpan.className = `tag tag-${u.role}`;
    roleSpan.textContent = u.role;
    tdRole.appendChild(roleSpan);
    tr.appendChild(tdRole);

    // 激活状态
    const tdActive = document.createElement('td');
    const activeSpan = document.createElement('span');
    activeSpan.className = 'tag tag-active';
    activeSpan.textContent = u.is_active ? '启用' : '禁用';
    tdActive.appendChild(activeSpan);
    tr.appendChild(tdActive);

    // 注册时间
    const tdTime = document.createElement('td');
    tdTime.textContent = u.created_at ? new Date(u.created_at * 1000).toLocaleString('zh-CN') : '-';
    tr.appendChild(tdTime);

    // 操作（选择角色 + 保存按钮）
    const tdActions = document.createElement('td');
    const select = document.createElement('select');
    select.className = 'role-select';
    select.dataset.userId = u.user_id;
    select.dataset.current = u.role;
    select.style.padding = '3px 6px';
    select.style.border = '1px solid var(--border)';
    select.style.borderRadius = '4px';
    select.style.background = 'var(--bg-base)';
    select.style.fontSize = '12px';

    ROLES.forEach((r) => {
      const option = document.createElement('option');
      option.value = r;
      option.textContent = r;
      if (r === u.role) option.selected = true;
      select.appendChild(option);
    });
    tdActions.appendChild(select);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-sm btn-role-save';
    button.dataset.userId = u.user_id;
    button.style.padding = '2px 8px';
    button.style.fontSize = '11px';
    button.style.marginLeft = '4px';
    button.textContent = '保存';

    button.addEventListener('click', async () => {
      const newRole = select.value;
      const currentRole = select.dataset.current;
      if (newRole === currentRole) {
        showToast('角色未变更', 'info');
        return;
      }
      if (!confirm(`确定将用户 ID ${u.user_id} 的角色从 ${currentRole} 修改为 ${newRole}？`))
        return;
      try {
        const result = await updateUserRole(u.user_id, newRole);
        showToast(result?.message || '角色已更新', 'success');
        loadUsers();
      } catch (e) {
        showToast(`修改失败: ${e.message}`, 'error');
      }
    });

    tdActions.appendChild(button);
    tr.appendChild(tdActions);

    tr.className = 'admin-row-item';
    tbody.appendChild(tr);
  });
}
