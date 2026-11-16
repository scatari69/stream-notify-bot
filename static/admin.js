(() => {
  const list = document.querySelector('#stream-list');
  if (!list) return;
  const form = document.querySelector('#stream-editor');
  const dialog = document.querySelector('#stream-dialog');
  const confirmDialog = document.querySelector('#delete-dialog');
  const empty = document.querySelector('#stream-empty');
  const dirty = document.querySelector('#save-hint');
  const editable = ['name', 'youtube', 'twitch', 'user'];
  const fields = [...editable, 'paused'];
  const editors = Object.fromEntries(editable.map((key) => [key, form.elements.namedItem(key)]));
  const saveButton = document.querySelector('#settings-form button[type=submit]');
  let pendingPauses = 0;
  let editing = null;
  let deleting = null;

  function values(row) {
    return Object.fromEntries(fields.map((key) => [key, row.querySelector(`input[name="${key}"]`).value]));
  }
  function render(row, data) {
    fields.forEach((key) => { row.querySelector(`input[name="${key}"]`).value = data[key]; });
    row.querySelector('.avatar').textContent = data.name.trim().slice(0, 1).toUpperCase() || '•';
    row.querySelector('.stream-name').textContent = data.name;
    const paused = data.paused === '1';
    const pauseButton = row.querySelector('[data-action=pause]');
    const saved = row.dataset.persisted === '1' && row.dataset.savedYoutube === data.youtube && row.dataset.savedTwitch === data.twitch;
    pauseButton.disabled = !saved;
    pauseButton.title = saved ? '' : 'Сначала сохраните изменения стримера';
    pauseButton.textContent = paused ? 'Возобновить' : 'Пауза';
    pauseButton.setAttribute('aria-pressed', String(paused));
    pauseButton.setAttribute('aria-label', `${paused ? 'Возобновить уведомления' : 'Приостановить уведомления'}: ${data.name}`);
    row.querySelector('.pause-badge').hidden = !paused;
    row.querySelector('.pause-result').textContent = saved ? '' : 'Сохраните стримера, чтобы управлять паузой.';
    row.querySelector('[data-action=edit]').setAttribute('aria-label', `Изменить ${data.name}`);
    row.querySelector('[data-action=delete]').setAttribute('aria-label', `Удалить ${data.name}`);
    const platforms = [data.youtube && `YouTube · ${data.youtube}`, data.twitch && `Twitch · ${data.twitch}`].filter(Boolean);
    const member = Array.from(editors.user.options).find((option) => option.value && option.value === data.user);
    if (member) platforms.push(`Discord · ${member.textContent}`);
    row.querySelector('.stream-meta').textContent = platforms.join('   /   ');
  }
  function markChanged() {
    empty.hidden = Boolean(list.querySelector('.stream-row'));
    dirty.textContent = 'Изменения ещё не сохранены';
    dirty.classList.add('dirty');
  }
  function openEditor(row) {
    editing = row;
    if (!row && list.querySelectorAll('.stream-row').length >= 101) {
      dirty.textContent = 'Можно добавить не больше 101 стримера.';
      dirty.classList.add('dirty');
      return;
    }
    form.reset();
    editors.youtube.setCustomValidity('');
    editors.name.setCustomValidity('');
    if (row) {
      const data = values(row);
      editable.forEach((key) => { editors[key].value = data[key]; });
    }
    document.querySelector('#stream-dialog-title').textContent = row ? 'Изменить стримера' : 'Добавить стримера';
    dialog.showModal();
    editors.name.focus();
  }
  document.querySelector('#add-stream').addEventListener('click', () => openEditor(null));
  list.addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const row = button.closest('.stream-row');
    if (button.dataset.action === 'edit') openEditor(row);
    if (button.dataset.action === 'pause') {
      const data = values(row);
      const result = row.querySelector('.pause-result');
      button.disabled = true;
      row.querySelectorAll('[data-action=edit], [data-action=delete]').forEach((control) => { control.disabled = true; });
      pendingPauses += 1;
      saveButton.disabled = true;
      result.textContent = 'Сохранение…';
      try {
        const body = new URLSearchParams({csrf: document.querySelector('#settings-form [name=csrf]').value,
          youtube: data.youtube, twitch: data.twitch, paused: data.paused === '1' ? '0' : '1'});
        const response = await fetch(list.dataset.pauseUrl, {method: 'POST', body, credentials: 'same-origin', redirect: 'error'});
        if (!response.ok) throw new Error('request failed');
        const saved = await response.json();
        if (typeof saved.paused !== 'boolean') throw new Error('invalid response');
        data.paused = saved.paused ? '1' : '0';
        render(row, data);
        result.textContent = saved.paused ? 'Пауза сохранена. Бот применит её при обновлении настроек.' : 'Возобновление сохранено. Бот применит его при обновлении настроек.';
      } catch {
        result.textContent = 'Не удалось изменить паузу. Обновите страницу и попробуйте снова.';
        button.disabled = false;
      } finally {
        row.querySelectorAll('[data-action=edit], [data-action=delete]').forEach((control) => { control.disabled = false; });
        pendingPauses -= 1;
        saveButton.disabled = pendingPauses > 0;
      }
    }
    if (button.dataset.action === 'delete') {
      deleting = row;
      document.querySelector('#delete-name').textContent = values(row).name;
      confirmDialog.showModal();
    }
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const data = Object.fromEntries(editable.map((key) => [key, editors[key].value.trim()]));
    data.paused = editing ? values(editing).paused : '0';
    editors.name.setCustomValidity(data.name ? '' : 'Укажите имя стримера.');
    const duplicate = Array.from(list.querySelectorAll('.stream-row')).some((row) => {
      if (row === editing) return false;
      const other = values(row);
      return (data.youtube && data.youtube === other.youtube.trim()) ||
        (data.twitch && data.twitch.toLowerCase() === other.twitch.trim().toLowerCase());
    });
    editors.youtube.setCustomValidity(!data.youtube && !data.twitch ? 'Укажите YouTube или Twitch.' : duplicate ? 'Этот канал уже добавлен.' : '');
    if (!form.reportValidity()) return;
    const row = editing || document.querySelector('#stream-template').content.firstElementChild.cloneNode(true);
    render(row, data);
    if (!editing) list.append(row);
    dialog.close();
    markChanged();
  });
  editors.name.addEventListener('input', () => editors.name.setCustomValidity(''));
  ['youtube', 'twitch'].forEach((key) => editors[key].addEventListener('input', () => editors.youtube.setCustomValidity('')));
  document.querySelector('#confirm-delete').addEventListener('click', () => {
    if (deleting) deleting.remove();
    deleting = null;
    confirmDialog.close();
    markChanged();
  });
  document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
})();
