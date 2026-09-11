const taskId = document.body.dataset.taskId;
const initialStatus = document.body.dataset.taskStatus;
const statusLabels = {
  pending: '等待中',
  running: '运行中',
  complete: '已完成',
  failed: '失败',
};

function updateStage(stageName, status) {
  const stage = document.querySelector(`[data-stage="${stageName}"]`);
  if (!stage) return;
  stage.classList.remove(
    'status-surface-pending',
    'status-surface-running',
    'status-surface-complete',
    'status-surface-failed',
  );
  stage.classList.add(`status-surface-${status}`);
  const badge = stage.querySelector('.stage-status');
  if (!badge) return;
  badge.className = `stage-status status-${status}`;
  badge.replaceChildren();
  if (status === 'running') {
    const dot = document.createElement('span');
    dot.className = 'status-dot';
    dot.setAttribute('aria-hidden', 'true');
    badge.append(dot);
  }
  badge.append(document.createTextNode(statusLabels[status] || status));
}

function showPollingWarning(message) {
  const warning = document.querySelector('#polling-warning');
  if (!warning) return;
  warning.textContent = message;
  warning.hidden = !message;
}

async function pollTask() {
  try {
    const response = await fetch(
      `/api/bid-check/tasks/${encodeURIComponent(taskId)}`,
      { cache: 'no-store' },
    );
    if (!response.ok) throw new Error('任务状态查询失败');
    const payload = await response.json();
    if (payload.status === 'complete' || payload.status === 'failed') {
      window.location.reload();
      return;
    }
    updateStage('requirements', payload.requirements_status);
    updateStage('bid_parse', payload.bid_parse_status);
    updateStage('review', payload.review_status);
    updateStage('evaluation', payload.evaluation_progress_status);
    showPollingWarning('');
    window.setTimeout(pollTask, 700);
  } catch (error) {
    showPollingWarning('暂时无法获取最新任务状态，正在重试。');
    window.setTimeout(pollTask, 1800);
  }
}

if (taskId && ['pending', 'running'].includes(initialStatus)) {
  window.setTimeout(pollTask, 400);
}
