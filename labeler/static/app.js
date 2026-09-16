(() => {
  'use strict';

  let csrf = '';
  const statusLabels = {
    pending: '待标注',
    annotated: '已标注',
    positive: '正样本',
    negative: '负样本',
    uncertain: '不确定',
    deleted: '已删除',
  };
  const sourceLabels = {accepted: '初审正样本', uncertain: '初审不确定', rejected: '初审负样本'};
  const taskLabels = {pending: '待标注', completed: '已完成', not_required: '无需框标注'};
  const fallbackBoxClasses = [{id: 0, name: 'object', label_zh: '目标', color: '#1677ff'}];
  const parameters = new URLSearchParams(location.search);
  const requestedStatus = parameters.get('status');
  const initialPage = Math.max(1, Number.parseInt(parameters.get('page') || '1', 10) || 1);
  const state = {
    user: null,
    status: statusLabels[requestedStatus] ? requestedStatus : 'pending',
    items: [], detail: null, image: null, activeBox: -1, drawMode: false,
    drag: null, summary: null, saving: false, toastTimer: null, searchTimer: null,
    page: initialPage, pageSize: 100,
    pagination: {page: initialPage, page_size: 100, pages: 1, total: 0},
    returnFocus: null, boxClasses: fallbackBoxClasses, defaultClassId: 0, savedSnapshot: '',
  };

  const grid = document.getElementById('grid');
  const dialog = document.getElementById('detail-dialog');
  const canvas = document.getElementById('editor-canvas');
  const context = canvas.getContext('2d');
  const originalImage = document.getElementById('original-image');

  async function api(url, options = {}) {
    const headers = {...(options.headers || {})};
    if (options.body) {
      headers['Content-Type'] = 'application/json';
      if (csrf && url !== '/api/login') headers['X-CSRF-Token'] = csrf;
    }
    const response = await fetch(url, {...options, headers});
    let payload = {};
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) {
      if (response.status === 401 && url !== '/api/login') showLogin();
      const error = new Error(payload.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function showLogin(message = '') {
    csrf = '';
    state.user = null;
    document.getElementById('login-screen').classList.remove('hidden');
    document.getElementById('session-controls').classList.add('hidden');
    document.getElementById('login-error').textContent = message;
    if (dialog.open) dialog.close();
  }

  function showApp(session) {
    state.user = {username: session.username, role: session.role};
    csrf = session.csrf_token;
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('session-controls').classList.remove('hidden');
    document.getElementById('current-user').textContent =
      `${session.username} · ${session.role === 'admin' ? '管理员' : '标注用户'}`;
    document.getElementById('assignee-filter').classList.toggle('hidden', session.role !== 'admin');
    document.getElementById('export').classList.toggle('hidden', session.role !== 'admin');
    if (Array.isArray(session.box_classes) && session.box_classes.length > 0) {
      state.boxClasses = session.box_classes;
    }
    state.defaultClassId = state.boxClasses[0].id;
    const project = session.project || {};
    document.title = project.title || 'Labeler';
    document.querySelector('h1').textContent = project.title || 'Labeler';
    document.querySelector('.dataset-nav').textContent = `${state.boxClasses.length} 个对象类别 · 多人协作 · 六类同步视图`;
    document.getElementById('project-instructions').textContent = project.instructions || '请按项目规则标注可见对象';
    const picker = document.querySelector('.class-segmented');
    picker.replaceChildren();
    const legend = document.querySelector('.class-legend');
    legend.replaceChildren();
    state.boxClasses.forEach(definition => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'class-option';
      button.dataset.classId = definition.id;
      button.setAttribute('role', 'radio');
      button.style.setProperty('--class-color', definition.color);
      button.textContent = definition.label_zh;
      picker.append(button);
      const chip = document.createElement('span');
      chip.className = 'class-chip';
      chip.style.color = definition.color;
      chip.textContent = definition.label_zh;
      legend.append(chip);
    });
    const owners = document.getElementById('assignee-filter');
    const previousOwner = owners.value;
    owners.replaceChildren(new Option('全部账号', ''), new Option('未分配', 'unassigned'));
    (project.annotators || []).forEach(user => owners.append(new Option(user, user)));
    if ([...owners.options].some(option => option.value === previousOwner)) owners.value = previousOwner;
    try {
      state.returnFocus = JSON.parse(
        localStorage.getItem(`labelerAnnotationCompletedFocus:${session.username}`) || 'null'
      );
    } catch {
      state.returnFocus = null;
    }
    const rememberedClass = Number(localStorage.getItem('labelerAnnotationDefaultClass'));
    if (state.boxClasses.some(item => item.id === rememberedClass)) {
      state.defaultClassId = rememberedClass;
    }
  }

  function showNotice(message, error = false) {
    const notice = document.getElementById('notice');
    notice.textContent = message;
    notice.classList.toggle('error', error);
    notice.style.display = message ? 'block' : 'none';
  }

  function toastHosts() {
    return [
      document.getElementById('toast-host-global'),
      document.getElementById('modal-toast-host'),
    ];
  }

  function relocateToast() {
    const toast = document.getElementById('toast');
    const visible = toast.classList.contains('visible');
    const target = dialog.open
      ? document.getElementById('modal-toast-host')
      : document.getElementById('toast-host-global');
    toastHosts().forEach(host => host.classList.remove('visible'));
    target.append(toast);
    target.classList.toggle('visible', visible);
  }

  function hideToast() {
    clearTimeout(state.toastTimer);
    state.toastTimer = null;
    const toast = document.getElementById('toast');
    toast.classList.remove('visible', 'error');
    toastHosts().forEach(host => host.classList.remove('visible'));
  }

  function showToast(message, error = false) {
    clearTimeout(state.toastTimer);
    const toast = document.getElementById('toast');
    document.getElementById('toast-icon').textContent = error ? '!' : '✓';
    document.getElementById('toast-title').textContent = error
      ? (message.startsWith('保存失败') ? '保存失败' : '操作未完成')
      : (message.startsWith('已保存') ? '保存成功' : '操作成功');
    const description = document.getElementById('toast-description');
    description.textContent = message;
    description.title = message;
    toast.classList.toggle('error', error);
    toast.setAttribute('role', error ? 'alert' : 'status');
    toast.setAttribute('aria-live', error ? 'assertive' : 'polite');
    toast.classList.add('visible');
    relocateToast();
    state.toastTimer = error ? null : setTimeout(hideToast, 4500);
  }

  function setReturnFocus(value) {
    state.returnFocus = value;
    if (state.user) {
      localStorage.setItem(
        `labelerAnnotationCompletedFocus:${state.user.username}`, JSON.stringify(value)
      );
    }
  }

  function detailSnapshot(detail) {
    return detail ? JSON.stringify({status: detail.status, boxes: detail.boxes}) : '';
  }

  function hasUnsavedChanges() {
    return Boolean(state.detail && state.savedSnapshot &&
      detailSnapshot(state.detail) !== state.savedSnapshot);
  }

  function boxClass(classId) {
    return state.boxClasses.find(item => item.id === Number(classId)) || fallbackBoxClasses[0];
  }

  function boxHasValidClass(box) {
    const definition = state.boxClasses.find(item => item.id === box.class_id);
    return Boolean(definition && definition.name === box.class_name);
  }

  function assignBoxClass(box, classId) {
    const definition = boxClass(classId);
    box.class_id = definition.id;
    box.class_name = definition.name;
  }

  function renderClassPicker() {
    if (!state.detail) return;
    const selected = state.activeBox >= 0
      ? state.detail.boxes[state.activeBox]?.class_id
      : state.defaultClassId;
    document.getElementById('box-class-title').textContent =
      state.activeBox >= 0 ? `当前框 ${state.activeBox + 1} 的类别` : '新框默认类别';
    document.querySelectorAll('.class-option').forEach(button => {
      const active = Number(button.dataset.classId) === selected;
      button.classList.toggle('active', active);
      button.setAttribute('aria-checked', String(active));
      button.disabled = state.detail.status === 'deleted';
    });
  }

  function setThumbSize(value) {
    const size = Math.max(190, Math.min(380, Number(value) || 240));
    document.documentElement.style.setProperty('--card-width', `${size}px`);
    document.getElementById('thumb-size').value = String(size);
    localStorage.setItem('labelerAnnotationThumbSize', String(size));
  }

  function overlayGeometry(item) {
    const imageRatio = item.width / item.height;
    const frameRatio = 4 / 3;
    if (imageRatio > frameRatio) {
      const height = frameRatio / imageRatio * 100;
      return {left: 0, top: (100 - height) / 2, width: 100, height};
    }
    const width = imageRatio / frameRatio * 100;
    return {left: (100 - width) / 2, top: 0, width, height: 100};
  }

  function makeCard(item) {
    const article = document.createElement('article');
    article.className = 'item';
    article.dataset.candidateId = item.candidate_id;
    const thumb = document.createElement('button');
    thumb.type = 'button';
    thumb.className = 'thumb';
    thumb.title = '打开详情';
    const image = document.createElement('img');
    image.src = item.media_url;
    image.alt = item.candidate_id;
    image.loading = 'lazy';
    const overlay = document.createElement('span');
    overlay.className = 'overlay';
    const geometry = overlayGeometry(item);
    Object.assign(overlay.style, {
      left: `${geometry.left}%`, top: `${geometry.top}%`,
      width: `${geometry.width}%`, height: `${geometry.height}%`,
    });
    item.boxes.forEach(box => {
      const marker = document.createElement('span');
      marker.className = 'overlay-box';
      const definition = boxClass(box.class_id);
      marker.style.setProperty('--class-color', definition.color);
      marker.title = definition.label_zh;
      Object.assign(marker.style, {
        left: `${box.x1 / item.width * 100}%`, top: `${box.y1 / item.height * 100}%`,
        width: `${(box.x2 - box.x1) / item.width * 100}%`,
        height: `${(box.y2 - box.y1) / item.height * 100}%`,
      });
      overlay.append(marker);
    });
    thumb.append(image, overlay);
    const owner = document.createElement('span');
    owner.className = 'assignee';
    owner.textContent = item.assignee ? `分配给：${item.assignee}` : '未分配（初审负样本）';
    const badge = document.createElement('span');
    badge.className = `status ${item.status}`;
    badge.textContent = statusLabels[item.status];
    const meta = document.createElement('span');
    meta.className = 'meta';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = item.candidate_id;
    const details = document.createElement('span');
    details.className = 'details';
    const geometryLine = document.createElement('span');
    geometryLine.className = 'detail-line';
    geometryLine.textContent = `${item.width} × ${item.height} · 框 ${item.box_count}` +
      `${item.no_target ? ' · 无目标' : ''}`;
    const sourceLine = document.createElement('span');
    sourceLine.className = 'detail-line';
    sourceLine.textContent = sourceLabels[item.source_status];
    details.append(geometryLine, sourceLine);
    const classSummary = document.createElement('span');
    classSummary.className = 'class-summary';
    state.boxClasses.forEach(definition => {
      const count = item.box_class_counts?.[definition.name] || 0;
      if (!count) return;
      const chip = document.createElement('span');
      chip.className = 'class-chip';
      chip.style.color = definition.color;
      chip.textContent = `${definition.label_zh} ${count}`;
      classSummary.append(chip);
    });
    const task = document.createElement('span');
    task.className = `task-chip ${item.task_state}`;
    task.textContent = taskLabels[item.task_state];
    meta.append(name, details, classSummary, task);
    article.append(thumb, owner, badge, meta);
    if (item.revision) {
      const revision = document.createElement('span');
      revision.className = 'revision';
      revision.textContent = `修订 ${item.revision}`;
      article.append(revision);
    }
    article.addEventListener('click', () => openDetail(item.candidate_id));
    return article;
  }

  function updateVisibleCount() {
    const total = state.pagination.total;
    const start = total ? (state.page - 1) * state.pageSize + 1 : 0;
    const end = total ? start + state.items.length - 1 : 0;
    document.getElementById('visible-count').textContent = `显示 ${start}–${end} / ${total} 张`;
  }

  function renderPager() {
    document.querySelectorAll('.page-info').forEach(node => {
      node.textContent = `第 ${state.page} / ${state.pagination.pages} 页`;
    });
    document.querySelectorAll('.page-prev').forEach(button => { button.disabled = state.page <= 1; });
    document.querySelectorAll('.page-next').forEach(button => {
      button.disabled = state.page >= state.pagination.pages;
    });
  }

  function renderGrid() {
    grid.innerHTML = '';
    state.items.forEach(item => grid.append(makeCard(item)));
    if (!state.items.length) grid.innerHTML = '<p class="empty">没有符合当前筛选条件的图片。</p>';
    updateVisibleCount();
    renderPager();
  }

  function renderSummary(summary) {
    state.summary = summary;
    document.getElementById('total-summary').textContent =
      `已标注 ${summary.annotated} · 已完成任务 ${summary.completed} · ` +
      `待标注 ${summary.pending} · ${summary.boxes} 个框`;
    document.getElementById('revision-summary').textContent = `修订 ${summary.revisions}`;
    if (summary.invalid_positive_boxes) {
      showNotice(
        `检测到 ${summary.invalid_positive_boxes} 张正样本仍含无效类别框，已从“已标注”和导出中排除。`,
        true
      );
    }
    const tabs = document.getElementById('tabs');
    tabs.innerHTML = '';
    Object.entries(statusLabels).forEach(([status, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `tab${status === state.status ? ' active' : ''}`;
      button.innerHTML = `${label} <span>${summary[status] ?? 0}</span>`;
      button.addEventListener('click', () => loadGroup(status, true, 1));
      tabs.append(button);
    });
    const progress = document.getElementById('admin-progress');
    if (state.user?.role === 'admin' && summary.per_user) {
      progress.classList.remove('hidden');
      progress.textContent = summary.per_user.map(item =>
        `${item.username}：已完成 ${item.completed} / ${item.total}，待标注 ${item.pending}`
      ).join('　　');
    } else {
      progress.classList.add('hidden');
    }
  }

  function currentAssigneeFilter() {
    return state.user?.role === 'admin' ? document.getElementById('assignee-filter').value : '';
  }

  function updateGroupUrl(mode = 'replace') {
    const url = new URL(location.href);
    const query = document.getElementById('search').value.trim();
    const assignee = currentAssigneeFilter();
    url.searchParams.set('status', state.status);
    if (state.page > 1) url.searchParams.set('page', state.page); else url.searchParams.delete('page');
    if (query) url.searchParams.set('q', query); else url.searchParams.delete('q');
    if (assignee) url.searchParams.set('assignee', assignee); else url.searchParams.delete('assignee');
    url.searchParams.delete('detail');
    history[mode === 'push' ? 'pushState' : 'replaceState'](
      {status: state.status, page: state.page}, '', url
    );
  }

  async function loadGroup(status, push = true, page = 1, replace = false) {
    state.status = status;
    grid.innerHTML = '<p class="empty">正在载入...</p>';
    showNotice('');
    const query = document.getElementById('search').value.trim();
    const params = new URLSearchParams({status, page: String(page), page_size: String(state.pageSize)});
    if (query) params.set('q', query);
    const assignee = currentAssigneeFilter();
    if (assignee) params.set('assignee', assignee);
    const payload = await api(`/api/group?${params}`);
    state.items = payload.items;
    state.pagination = payload.pagination;
    state.page = payload.pagination.page;
    renderSummary(payload.summary);
    renderGrid();
    if (push || replace) updateGroupUrl(push ? 'push' : 'replace');
  }

  function removalFocus(index) {
    if (index < state.items.length - 1) {
      return {status: state.status, page: state.page, candidateId: state.items[index + 1].candidate_id};
    }
    if (index > 0) return {status: state.status, page: state.page, edge: 'last'};
    if (state.page > 1) return {status: state.status, page: state.page - 1, edge: 'last'};
    return {status: state.status, page: state.page, edge: 'first'};
  }

  async function highlightReturnFocus() {
    const target = state.returnFocus;
    if (!target) return;
    if (target.status !== state.status || target.page !== state.page) {
      await loadGroup(target.status, false, target.page);
      updateGroupUrl('replace');
    }
    document.querySelectorAll('.item.last-viewed').forEach(card => {
      card.classList.remove('last-viewed');
      card.removeAttribute('aria-current');
    });
    let card = target.candidateId
      ? [...grid.querySelectorAll('.item')].find(item => item.dataset.candidateId === target.candidateId)
      : null;
    if (!card && target.edge === 'last') card = grid.querySelector('.item:last-of-type');
    if (!card && target.edge === 'first') card = grid.querySelector('.item');
    if (card) {
      card.classList.add('last-viewed');
      card.setAttribute('aria-current', 'true');
      requestAnimationFrame(() => card.scrollIntoView({block: 'nearest', behavior: 'smooth'}));
    }
  }

  function drawEditor() {
    if (!state.image || !state.detail) return;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(state.image, 0, 0, canvas.width, canvas.height);
    const scale = Math.max(canvas.width, canvas.height) / 900;
    state.detail.boxes.forEach((box, index) => {
      context.save();
      const definition = boxClass(box.class_id);
      context.strokeStyle = '#fff';
      context.lineWidth = Math.max(4, 5 * scale);
      context.strokeRect(box.x1, box.y1, box.x2 - box.x1, box.y2 - box.y1);
      context.strokeStyle = definition.color;
      context.lineWidth = Math.max(2, 3 * scale);
      context.strokeRect(box.x1, box.y1, box.x2 - box.x1, box.y2 - box.y1);
      const label = `${index + 1} · ${definition.label_zh}`;
      const fontSize = Math.max(13, 15 * scale);
      context.font = `600 ${fontSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
      const labelWidth = Math.min(canvas.width, context.measureText(label).width + 12 * scale);
      const labelHeight = fontSize + 8 * scale;
      const labelTop = Math.max(0, box.y1 - labelHeight);
      const labelLeft = Math.max(0, Math.min(box.x1, canvas.width - labelWidth));
      context.fillStyle = definition.color;
      context.fillRect(labelLeft, labelTop, labelWidth, labelHeight);
      context.fillStyle = '#fff';
      context.fillText(label, labelLeft + 6 * scale, labelTop + fontSize + 1 * scale);
      if (index === state.activeBox) {
        const size = Math.max(7, 10 * scale);
        context.fillStyle = definition.color;
        [[box.x1, box.y1], [box.x2, box.y1], [box.x1, box.y2], [box.x2, box.y2]].forEach(
          ([x, y]) => context.fillRect(x - size / 2, y - size / 2, size, size)
        );
      }
      context.restore();
    });
  }

  function fitMediaElement(element, sourceWidth, sourceHeight) {
    const stage = element.parentElement;
    if (!stage || !sourceWidth || !sourceHeight) return;
    const style = getComputedStyle(stage);
    const availableWidth = stage.clientWidth -
      Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight);
    const availableHeight = stage.clientHeight -
      Number.parseFloat(style.paddingTop) - Number.parseFloat(style.paddingBottom);
    if (availableWidth <= 0 || availableHeight <= 0) return;
    const scale = Math.min(1, availableWidth / sourceWidth, availableHeight / sourceHeight);
    element.style.width = `${Math.max(1, Math.floor(sourceWidth * scale))}px`;
    element.style.height = `${Math.max(1, Math.floor(sourceHeight * scale))}px`;
  }

  function fitDetailMedia() {
    if (!state.detail || !dialog.open) return;
    fitMediaElement(originalImage, state.detail.width, state.detail.height);
    fitMediaElement(canvas, state.detail.width, state.detail.height);
  }

  function renderStatus() {
    if (!state.detail) return;
    document.querySelectorAll('.status-option').forEach(button => {
      button.classList.toggle('active', button.dataset.status === state.detail.status);
      button.disabled = state.detail.status === 'deleted';
    });
    const owner = state.detail.assignee || '未分配（初审负样本）';
    document.getElementById('modal-context').textContent =
      `${sourceLabels[state.detail.source_status]} · 分配给 ${owner} · ` +
      `${taskLabels[state.detail.task_state]} · 修订 ${state.detail.revision}`;
    const deleteButton = document.getElementById('delete');
    deleteButton.textContent = state.detail.status === 'deleted' ? '恢复图片' : '移至删除分组';
    deleteButton.disabled = state.saving;
    document.getElementById('save').disabled = state.saving || state.detail.status === 'deleted';
    document.getElementById('save-next').disabled = state.saving || state.detail.status === 'deleted';
    document.getElementById('draw-box').disabled = state.detail.status === 'deleted';
    document.getElementById('clear-boxes').disabled = state.detail.status === 'deleted';
    renderClassPicker();
  }

  function renderBoxList() {
    const list = document.getElementById('box-list');
    list.innerHTML = '';
    state.detail.boxes.forEach((box, index) => {
      const row = document.createElement('div');
      row.className = `box-row${index === state.activeBox ? ' active' : ''}`;
      const marker = document.createElement('span');
      marker.className = 'box-index';
      const definition = boxClass(box.class_id);
      marker.style.setProperty('--class-color', definition.color);
      marker.textContent = index + 1;
      const label = document.createElement('span');
      label.className = 'box-class-name';
      label.textContent = definition.label_zh;
      const coordinates = document.createElement('span');
      coordinates.className = 'box-coordinates';
      coordinates.textContent = `${Math.round(box.x1)},${Math.round(box.y1)} – ` +
        `${Math.round(box.x2)},${Math.round(box.y2)}`;
      row.append(marker, label, coordinates);
      row.addEventListener('click', () => {
        state.activeBox = index;
        state.drawMode = false;
        renderBoxList();
        drawEditor();
      });
      list.append(row);
    });
    if (!state.detail.boxes.length) list.innerHTML = '<p class="empty">暂无标注框</p>';
    document.getElementById('remove-box').disabled =
      state.activeBox < 0 || state.detail.status === 'deleted';
    document.getElementById('draw-box').classList.toggle('primary', state.drawMode);
    document.getElementById('draw-box').setAttribute('aria-pressed', String(state.drawMode));
    canvas.classList.toggle('drawing', state.drawMode);
    renderClassPicker();
    renderStatus();
  }

  async function openDetail(candidateId, startDrawing = false) {
    const payload = await api(`/api/item?id=${encodeURIComponent(candidateId)}`);
    state.detail = payload;
    state.savedSnapshot = detailSnapshot(payload);
    state.activeBox = -1;
    state.drawMode = startDrawing && payload.status !== 'deleted';
    document.getElementById('modal-title').textContent =
      `${payload.candidate_id} · ${statusLabels[payload.status]}`;
    const image = new Image();
    image.onload = () => {
      state.image = image;
      canvas.width = payload.width;
      canvas.height = payload.height;
      originalImage.src = payload.media_url;
      renderBoxList();
      drawEditor();
      requestAnimationFrame(fitDetailMedia);
    };
    image.onerror = () => showToast('原图载入失败。', true);
    image.src = payload.media_url;
    const url = new URL(location.href);
    url.searchParams.set('detail', candidateId);
    history.replaceState(null, '', url);
    if (!dialog.open) dialog.showModal();
    relocateToast();
    renderBoxList();
    requestAnimationFrame(fitDetailMedia);
  }

  function closeDetail(force = false) {
    if (!force && hasUnsavedChanges() &&
        !window.confirm('当前框或分类尚未保存，确定关闭并放弃修改吗？')) return;
    dialog.close();
    relocateToast();
    state.savedSnapshot = '';
    const url = new URL(location.href);
    url.searchParams.delete('detail');
    history.replaceState(null, '', url);
    highlightReturnFocus().catch(error => showToast(`定位缩略图失败：${error.message}`, true));
  }

  function canvasPoint(event) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) / rect.width * canvas.width)),
      y: Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) / rect.height * canvas.height)),
    };
  }

  function hitTest(point) {
    const tolerance = Math.max(canvas.width, canvas.height) * 0.018;
    for (let index = state.detail.boxes.length - 1; index >= 0; index -= 1) {
      const box = state.detail.boxes[index];
      const corners = [
        ['nw', box.x1, box.y1], ['ne', box.x2, box.y1],
        ['sw', box.x1, box.y2], ['se', box.x2, box.y2],
      ];
      for (const [handle, x, y] of corners) {
        if (Math.hypot(point.x - x, point.y - y) <= tolerance) return {index, mode: handle};
      }
      if (point.x >= box.x1 && point.x <= box.x2 && point.y >= box.y1 && point.y <= box.y2) {
        return {index, mode: 'move'};
      }
    }
    return null;
  }

  canvas.addEventListener('pointerdown', event => {
    if (!state.detail || state.detail.status === 'deleted') return;
    canvas.setPointerCapture(event.pointerId);
    const point = canvasPoint(event);
    if (state.drawMode) {
      const box = {x1: point.x, y1: point.y, x2: point.x, y2: point.y};
      assignBoxClass(box, state.defaultClassId);
      state.detail.boxes.push(box);
      state.detail.status = 'positive';
      state.activeBox = state.detail.boxes.length - 1;
      state.drag = {mode: 'se', start: point, original: {...box}, drawing: true};
    } else {
      const hit = hitTest(point);
      if (!hit) {
        state.activeBox = -1;
        renderBoxList();
        drawEditor();
        return;
      }
      state.activeBox = hit.index;
      state.drag = {mode: hit.mode, start: point, original: {...state.detail.boxes[hit.index]}};
    }
    renderBoxList();
    drawEditor();
  });

  canvas.addEventListener('pointermove', event => {
    if (!state.drag || state.activeBox < 0) return;
    const point = canvasPoint(event);
    const box = state.detail.boxes[state.activeBox];
    const original = state.drag.original;
    const dx = point.x - state.drag.start.x;
    const dy = point.y - state.drag.start.y;
    if (state.drag.mode === 'move') {
      const width = original.x2 - original.x1;
      const height = original.y2 - original.y1;
      box.x1 = Math.max(0, Math.min(canvas.width - width, original.x1 + dx));
      box.x2 = box.x1 + width;
      box.y1 = Math.max(0, Math.min(canvas.height - height, original.y1 + dy));
      box.y2 = box.y1 + height;
    } else {
      if (state.drag.mode.includes('w')) box.x1 = point.x;
      if (state.drag.mode.includes('e')) box.x2 = point.x;
      if (state.drag.mode.includes('n')) box.y1 = point.y;
      if (state.drag.mode.includes('s')) box.y2 = point.y;
    }
    drawEditor();
  });

  canvas.addEventListener('pointerup', () => {
    if (!state.drag || state.activeBox < 0) return;
    const box = state.detail.boxes[state.activeBox];
    const wasDrawing = Boolean(state.drag.drawing);
    [box.x1, box.x2] = [Math.min(box.x1, box.x2), Math.max(box.x1, box.x2)];
    [box.y1, box.y2] = [Math.min(box.y1, box.y2), Math.max(box.y1, box.y2)];
    if (box.x2 - box.x1 < 2 || box.y2 - box.y1 < 2) {
      state.detail.boxes.splice(state.activeBox, 1);
      state.activeBox = -1;
    }
    state.drag = null;
    if (wasDrawing) {
      state.activeBox = -1;
      state.drawMode = false;
    }
    renderBoxList();
    drawEditor();
  });

  function belongsToCurrentGroup(item) {
    if (state.status === 'pending') {
      return item.task_required && item.task_state === 'pending' &&
        ['positive', 'uncertain'].includes(item.status);
    }
    if (state.status === 'annotated') {
      return item.status === 'positive' && item.boxes.length > 0 &&
        item.boxes.every(boxHasValidClass);
    }
    return item.status === state.status;
  }

  async function saveCurrent(openNext, statusOverride = null) {
    if (!state.detail || state.saving) return;
    const requested = statusOverride || state.detail.status;
    if (requested === 'positive' && !state.detail.boxes.length) {
      showToast('正样本至少需要一个目标框。', true);
      return;
    }
    const invalidIndex = state.detail.boxes.findIndex(box => !boxHasValidClass(box));
    if (requested === 'positive' && invalidIndex >= 0) {
      state.activeBox = invalidIndex;
      renderBoxList();
      drawEditor();
      showToast(`框 ${invalidIndex + 1} 尚未选择有效的对象类别。`, true);
      return;
    }
    if (['negative', 'uncertain'].includes(requested) && state.detail.boxes.length) {
      showToast(`${statusLabels[requested]}不能保留目标框，请先清空。`, true);
      return;
    }
    const currentId = state.detail.candidate_id;
    const currentIndex = state.items.findIndex(item => item.candidate_id === currentId);
    const fallback = currentIndex >= 0 ? removalFocus(currentIndex) : state.returnFocus;
    const scrollPosition = {x: window.scrollX, y: window.scrollY};
    state.saving = true;
    renderStatus();
    try {
      const result = await api('/api/save', {
        method: 'POST',
        body: JSON.stringify({
          candidate_id: currentId,
          status: requested,
          boxes: state.detail.boxes,
          revision: state.detail.revision,
        }),
      });
      const remains = belongsToCurrentGroup(result);
      if (remains && currentIndex >= 0) {
        state.items[currentIndex] = result;
        const oldCard = [...grid.querySelectorAll('.item')].find(
          card => card.dataset.candidateId === currentId
        );
        if (oldCard) oldCard.replaceWith(makeCard(result));
        setReturnFocus({status: state.status, page: state.page, candidateId: currentId});
      } else {
        setReturnFocus(fallback);
        await loadGroup(state.status, false, state.page);
        if (currentIndex >= 0 && state.items.length) {
          const replacement = state.items[Math.min(currentIndex, state.items.length - 1)];
          setReturnFocus({
            status: state.status, page: state.page, candidateId: replacement.candidate_id,
          });
        }
      }
      state.detail = result;
      state.savedSnapshot = detailSnapshot(result);
      state.activeBox = Math.min(state.activeBox, state.detail.boxes.length - 1);
      document.getElementById('modal-title').textContent =
        `${currentId} · ${statusLabels[result.status]}`;
      renderSummary(result.summary);
      renderBoxList();
      drawEditor();
      window.scrollTo(scrollPosition.x, scrollPosition.y);
      const message = result.status === 'negative' && result.no_target
        ? `已保存为负样本（无目标） ${currentId}`
        : result.status === 'deleted'
          ? `已移至删除分组 ${currentId}`
          : `已保存 ${currentId}`;
      showToast(`${message} · 修订 ${result.revision}`);
      if (openNext) {
        let target = null;
        if (state.status === 'pending') {
          target = currentIndex >= 0
            ? state.items[Math.min(currentIndex, state.items.length - 1)]
            : state.items[0];
        } else if (currentIndex >= 0) {
          target = state.items[currentIndex + (remains ? 1 : 0)] || state.items[currentIndex];
        }
        if (target && target.candidate_id !== currentId) {
          await openDetail(target.candidate_id, true);
        } else if (!target) {
          closeDetail(true);
          showToast('当前筛选条件下已经没有下一张图片。');
        }
      }
    } catch (error) {
      if (error.status === 409) {
        showToast(`${error.message}；正在重新载入。`, true);
        try { await openDetail(currentId); } catch { /* item may have moved */ }
      } else {
        showToast(`保存失败：${error.message}`, true);
      }
    } finally {
      state.saving = false;
      renderStatus();
    }
  }

  async function deleteOrRestore() {
    if (!state.detail || state.saving) return;
    if (state.detail.status !== 'deleted') {
      await saveCurrent(true, 'deleted');
      return;
    }
    state.saving = true;
    renderStatus();
    try {
      const result = await api('/api/restore', {
        method: 'POST',
        body: JSON.stringify({
          candidate_id: state.detail.candidate_id,
          revision: state.detail.revision,
        }),
      });
      const currentId = result.candidate_id;
      await loadGroup(state.status, false, state.page);
      state.detail = result;
      state.savedSnapshot = detailSnapshot(result);
      setReturnFocus({status: state.status, page: state.page, edge: 'first'});
      document.getElementById('modal-title').textContent =
        `${currentId} · ${statusLabels[result.status]}`;
      renderBoxList();
      drawEditor();
      showToast(`已恢复 ${currentId} · 修订 ${result.revision}`);
    } catch (error) {
      showToast(`恢复失败：${error.message}`, true);
    } finally {
      state.saving = false;
      renderStatus();
    }
  }

  document.getElementById('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = document.getElementById('login-button');
    const username = document.getElementById('login-username').value.trim();
    const passwordInput = document.getElementById('login-password');
    button.disabled = true;
    document.getElementById('login-error').textContent = '';
    try {
      const session = await api('/api/login', {
        method: 'POST', body: JSON.stringify({username, password: passwordInput.value}),
      });
      passwordInput.value = '';
      showApp(session);
      await loadGroup(state.status, false, state.page, true);
    } catch (error) {
      passwordInput.value = '';
      document.getElementById('login-error').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById('logout').addEventListener('click', async () => {
    try { await api('/api/logout', {method: 'POST', body: '{}'}); } catch { /* clear locally */ }
    showLogin('已退出登录。');
  });

  document.getElementById('search').addEventListener('input', () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      loadGroup(state.status, false, 1, true).catch(
        error => showNotice(`筛选失败：${error.message}`, true)
      );
    }, 250);
  });
  document.getElementById('assignee-filter').addEventListener('change', () => {
    loadGroup(state.status, false, 1, true).catch(
      error => showNotice(`筛选失败：${error.message}`, true)
    );
  });
  document.getElementById('modal-close').addEventListener('click', closeDetail);
  document.getElementById('toast-close').addEventListener('click', hideToast);
  function toggleDrawMode() {
    if (!state.detail || state.detail.status === 'deleted') return;
    state.drawMode = !state.drawMode;
    if (state.drawMode) state.activeBox = -1;
    renderBoxList();
  }

  document.getElementById('draw-box').addEventListener('click', toggleDrawMode);
  window.addEventListener('keydown', event => {
    const target = event.target;
    const isTyping = target instanceof HTMLElement && (
      target.matches('input, textarea, select, [contenteditable="true"]') ||
      target.isContentEditable
    );
    if (!dialog.open || !state.detail || state.detail.status === 'deleted' || isTyping ||
        event.repeat || event.ctrlKey || event.altKey || event.metaKey ||
        event.key.toLowerCase() !== 'q') return;
    event.preventDefault();
    toggleDrawMode();
  });
  if ('ResizeObserver' in window) {
    const mediaResizeObserver = new ResizeObserver(() => fitDetailMedia());
    document.querySelectorAll('.image-stage').forEach(stage => mediaResizeObserver.observe(stage));
  }
  window.addEventListener('resize', fitDetailMedia);
  document.querySelector('.class-segmented').addEventListener('click', event => {
      const button = event.target.closest('.class-option');
      if (!button || button.disabled) return;
      const classId = Number(button.dataset.classId);
      state.defaultClassId = classId;
      localStorage.setItem('labelerAnnotationDefaultClass', String(classId));
      if (state.activeBox >= 0 && state.detail?.boxes[state.activeBox]) {
        assignBoxClass(state.detail.boxes[state.activeBox], classId);
        renderBoxList();
        drawEditor();
      } else {
        if (state.detail?.status !== 'deleted') state.drawMode = true;
        renderClassPicker();
        renderBoxList();
      }
  });
  document.getElementById('remove-box').addEventListener('click', () => {
    if (state.activeBox < 0) return;
    state.detail.boxes.splice(state.activeBox, 1);
    state.activeBox = Math.min(state.activeBox, state.detail.boxes.length - 1);
    if (!state.detail.boxes.length) state.detail.status = 'negative';
    renderBoxList();
    drawEditor();
  });
  document.getElementById('clear-boxes').addEventListener('click', () => {
    state.detail.boxes = [];
    state.activeBox = -1;
    state.detail.status = 'negative';
    renderBoxList();
    drawEditor();
  });
  document.getElementById('fit-status').addEventListener('click', () => {
    state.detail.status = state.detail.boxes.length ? 'positive' : 'negative';
    renderStatus();
  });
  document.querySelectorAll('.status-option').forEach(button => {
    button.addEventListener('click', () => {
      const nextStatus = button.dataset.status;
      if (['negative', 'uncertain'].includes(nextStatus) && state.detail.boxes.length) {
        const accepted = window.confirm(`改为“${statusLabels[nextStatus]}”会清空当前所有框，是否继续？`);
        if (!accepted) return;
        state.detail.boxes = [];
        state.activeBox = -1;
      }
      state.detail.status = nextStatus;
      renderBoxList();
      drawEditor();
    });
  });
  document.getElementById('save').addEventListener('click', () => saveCurrent(false));
  document.getElementById('save-next').addEventListener('click', () => saveCurrent(true));
  document.getElementById('delete').addEventListener('click', deleteOrRestore);
  document.querySelectorAll('.page-prev').forEach(button => button.addEventListener('click', () => {
    if (state.page > 1) loadGroup(state.status, true, state.page - 1).catch(
      error => showNotice(`翻页失败：${error.message}`, true)
    );
  }));
  document.querySelectorAll('.page-next').forEach(button => button.addEventListener('click', () => {
    if (state.page < state.pagination.pages) loadGroup(state.status, true, state.page + 1).catch(
      error => showNotice(`翻页失败：${error.message}`, true)
    );
  }));
  document.getElementById('thumb-size').addEventListener('input', event => setThumbSize(event.target.value));
  document.getElementById('export').addEventListener('click', async () => {
    try {
      const result = await api('/api/export', {method: 'POST', body: '{}'});
      showNotice(
        `已导出 ${result.task_records} 条人工完成记录：${result.path}\nSHA-256: ${result.sha256}`
      );
    } catch (error) {
      showNotice(`导出失败：${error.message}`, true);
    }
  });
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    closeDetail();
  });
  window.addEventListener('popstate', () => {
    if (!state.user) return;
    const current = new URLSearchParams(location.search);
    document.getElementById('search').value = current.get('q') || '';
    if (state.user.role === 'admin') {
      document.getElementById('assignee-filter').value = current.get('assignee') || '';
    }
    loadGroup(
      current.get('status') || 'pending', false,
      Math.max(1, Number.parseInt(current.get('page') || '1', 10) || 1)
    ).catch(error => showNotice(`载入失败：${error.message}`, true));
  });

  async function boot() {
    setThumbSize(localStorage.getItem('labelerAnnotationThumbSize') || 240);
    document.getElementById('search').value = parameters.get('q') || '';
    document.getElementById('assignee-filter').value = parameters.get('assignee') || '';
    try {
      const session = await api('/api/session');
      if (!session.authenticated) {
        showLogin();
        return;
      }
      showApp(session);
      await loadGroup(state.status, false, state.page);
      await highlightReturnFocus();
      const detailId = parameters.get('detail');
      if (detailId) await openDetail(detailId);
    } catch (error) {
      showLogin(`载入登录状态失败：${error.message}`);
    }
  }

  boot();
})();
