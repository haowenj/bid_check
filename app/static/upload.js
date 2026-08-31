const form = document.querySelector('#bid-check-form');
const startButton = document.querySelector('#start-check');
const formError = document.querySelector('#form-error');
const uploadCards = [...document.querySelectorAll('[data-upload-card]')];

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function selectedMode() {
  return form?.querySelector('input[name="check_mode"]:checked');
}

function refreshSubmitState() {
  const filesReady = uploadCards.every((card) => {
    const input = card.querySelector('input[type="file"]');
    return input?.files?.length === 1;
  });
  if (startButton) startButton.disabled = !(filesReady && selectedMode());
}

function showFile(card, file) {
  const zone = card.querySelector('.upload-zone');
  const summary = card.querySelector('.file-summary');
  card.querySelector('[data-file-name]').textContent = file.name;
  card.querySelector('[data-file-size]').textContent = formatFileSize(file.size);
  zone.hidden = true;
  summary.hidden = false;
  card.classList.add('has-file');
}

function clearFile(card) {
  const input = card.querySelector('input[type="file"]');
  const zone = card.querySelector('.upload-zone');
  const summary = card.querySelector('.file-summary');
  input.value = '';
  zone.hidden = false;
  summary.hidden = true;
  card.classList.remove('has-file');
  refreshSubmitState();
}

function showError(message) {
  if (!formError) return;
  formError.textContent = message;
  formError.hidden = !message;
}

uploadCards.forEach((card) => {
  const input = card.querySelector('input[type="file"]');
  input.addEventListener('change', () => {
    const file = input.files?.[0];
    if (!file) {
      clearFile(card);
      return;
    }
    if (!file.name.toLowerCase().endsWith('.docx')) {
      clearFile(card);
      showError('当前仅支持 .docx 文件。');
      return;
    }
    showError('');
    showFile(card, file);
    refreshSubmitState();
  });
  card.querySelector('[data-clear-file]').addEventListener('click', () => {
    clearFile(card);
  });
});

form?.querySelectorAll('input[name="check_mode"]').forEach((input) => {
  input.addEventListener('change', refreshSubmitState);
});

form?.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (startButton.disabled) return;
  showError('');
  startButton.disabled = true;
  startButton.classList.add('is-loading');
  startButton.firstChild.textContent = '正在创建任务 ';
  try {
    const response = await fetch('/api/bid-check/tasks', {
      method: 'POST',
      body: new FormData(form),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || '创建任务失败，请稍后重试。');
    window.location.assign(
      `/bid-check/tasks/${encodeURIComponent(payload.task_id)}`,
    );
  } catch (error) {
    showError(error.message || '创建任务失败，请稍后重试。');
    startButton.classList.remove('is-loading');
    startButton.firstChild.textContent = '开始校验 ';
    refreshSubmitState();
  }
});

