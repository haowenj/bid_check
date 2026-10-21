const form = document.getElementById("mineru-form");
const probeButton = document.getElementById("probe-mineru");
const testButton = document.getElementById("test-mineru");
const feedback = document.getElementById("mineru-feedback");
const urlInput = document.getElementById("mineru-url");
let revision = 0;

form.addEventListener("input", () => {
  revision += 1;
  feedback.hidden = true;
});

function showFeedback(message, state) {
  feedback.textContent = message;
  feedback.className = `settings-feedback ${state}`;
  feedback.hidden = false;
}

async function diagnose(kind) {
  const startedAt = revision;
  const button = kind === "probe" ? probeButton : testButton;
  const originalLabel = button.textContent;
  probeButton.disabled = true;
  testButton.disabled = true;
  button.textContent = kind === "probe" ? "探测中…" : "测试中…";
  showFeedback(kind === "probe" ? "正在探测 GPUStack 实例…" : "正在检查 MinerU 连接…", "pending");
  try {
    const response = await fetch(`/settings/mineru/${kind}`, {
      method: "POST",
      body: new FormData(form),
    });
    if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}），请稍后重试。`);
    const result = await response.json();
    if (revision !== startedAt) {
      showFeedback("表单已修改，请按当前配置重新探测或测试。", "pending");
      return;
    }
    if (result.ok && kind === "probe") {
      urlInput.value = result.url;
      revision += 1;
    }
    showFeedback(result.message || "请求未完成，请重试。", result.ok ? "success" : "failure");
  } catch (error) {
    if (revision === startedAt) showFeedback(error.message || "请求失败，请检查网络后重试。", "failure");
  } finally {
    probeButton.disabled = false;
    testButton.disabled = false;
    button.textContent = originalLabel;
  }
}

probeButton.addEventListener("click", () => diagnose("probe"));
testButton.addEventListener("click", () => diagnose("test"));
