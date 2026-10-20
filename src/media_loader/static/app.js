const TOKEN_KEY = "media_loader_token";
const POLL_FALLBACK_MS = 2000;

const fallbackConfig = {
  auth_required: true,
  default_video_format: "mp4",
  default_audio_format: "mp3",
  quality_levels: ["240p", "360p", "480p", "720p", "1080p", "1440p", "2160p"],
  video_formats: ["mp4", "mkv"],
  audio_formats: ["m4a", "mp3", "opus", "original"],
};

const el = {
  form: document.querySelector("#download-form"),
  url: document.querySelector("#url"),
  options: document.querySelector("#options"),
  modeVideo: document.querySelector("#mode-video"),
  modeAudio: document.querySelector("#mode-audio"),
  videoFormat: document.querySelector("#video-format"),
  audioFormat: document.querySelector("#audio-format"),
  videoFormatField: document.querySelector("#video-format-field"),
  audioFormatField: document.querySelector("#audio-format-field"),
  quality: document.querySelector("#quality"),
  qualityField: document.querySelector("#quality-field"),
  startButton: document.querySelector("#start-button"),
  formError: document.querySelector("#form-error"),
  statusCard: document.querySelector("#status-card"),
  statusLabel: document.querySelector("#status-label"),
  statusBadge: document.querySelector("#status-badge"),
  mediaTitle: document.querySelector("#media-title"),
  mediaMeta: document.querySelector("#media-meta"),
  progress: document.querySelector("#progress"),
  progressText: document.querySelector("#progress-text"),
  startedAt: document.querySelector("#started-at"),
  statusDetail: document.querySelector("#status-detail"),
  downloadLink: document.querySelector("#download-link"),
  resetButton: document.querySelector("#reset-button"),
  tokenToggle: document.querySelector("#token-toggle"),
  tokenPanel: document.querySelector("#token-panel"),
  tokenInput: document.querySelector("#token"),
  tokenSave: document.querySelector("#token-save"),
  tokenClear: document.querySelector("#token-clear"),
};

const state = {
  config: fallbackConfig,
  token: readToken(),
  controller: null,
  timer: null,
  running: false,
  request: null,
  errors: 0,
};

function readToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function writeToken(value) {
  state.token = value;
  try {
    if (value) {
      localStorage.setItem(TOKEN_KEY, value);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  } catch {
    return;
  }
  updateTokenUi();
}

function updateTokenUi() {
  el.tokenToggle.textContent = state.token ? "Токен •" : "Токен";
  el.tokenInput.placeholder = state.token ? "Токен сохранён" : "mlk_...";
}

function setTokenPanel(open) {
  el.tokenPanel.hidden = !open;
  el.tokenToggle.setAttribute("aria-expanded", String(open));
  if (open) {
    el.tokenInput.focus();
  }
}

function showFormError(message) {
  el.formError.textContent = message;
  el.formError.hidden = !message;
}

function setBusy(busy) {
  el.startButton.disabled = busy;
  el.startButton.textContent = busy ? "Загрузка…" : "Загрузить";
}

function authHeaders() {
  return state.token ? { Authorization: `Bearer ${state.token}` } : {};
}

async function loadConfig() {
  try {
    const response = await fetch("/ui/config");
    if (!response.ok) {
      throw new Error("config unavailable");
    }
    return await response.json();
  } catch {
    return fallbackConfig;
  }
}

function fillSelect(select, values, selected) {
  select.replaceChildren();
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value.toUpperCase();
    select.append(option);
  }
  if (selected && values.includes(selected)) {
    select.value = selected;
  }
}

function fillConfig(config) {
  state.config = config;
  fillSelect(el.videoFormat, config.video_formats, config.default_video_format);
  fillSelect(el.audioFormat, config.audio_formats, config.default_audio_format);

  el.quality.replaceChildren();
  const maximum = document.createElement("option");
  maximum.value = "";
  maximum.textContent = "Максимум";
  el.quality.append(maximum);
  for (const quality of config.quality_levels) {
    const option = document.createElement("option");
    option.value = quality;
    option.textContent = quality.toUpperCase();
    el.quality.append(option);
  }
}

function isAudioFormat(value) {
  return state.config.audio_formats.includes(value);
}

function syncMode() {
  const audio = el.modeAudio.checked;
  el.videoFormatField.hidden = audio;
  el.audioFormatField.hidden = !audio;
  el.qualityField.hidden = audio;
  el.quality.disabled = audio;
}

function paramsFromForm() {
  const audio = el.modeAudio.checked;
  const params = new URLSearchParams();
  params.set("url", el.url.value.trim());
  params.set("audio_only", audio ? "1" : "0");
  params.set("format", audio ? el.audioFormat.value : el.videoFormat.value);
  if (!audio && el.quality.value) {
    params.set("quality", el.quality.value);
  }
  return params;
}

function writeLocation(params) {
  const target = `/ui?${params.toString()}`;
  history.pushState({}, "", target);
}

function applyLocation() {
  const params = new URLSearchParams(location.search);
  const url = params.get("url") || "";
  const rawFormat = (params.get("format") || "").toLowerCase();
  const audio = ["1", "true", "yes"].includes(
    (params.get("audio_only") || "").toLowerCase(),
  ) || isAudioFormat(rawFormat);

  el.url.value = url;
  el.modeAudio.checked = audio;
  el.modeVideo.checked = !audio;

  if (audio) {
    el.audioFormat.value = state.config.audio_formats.includes(rawFormat)
      ? rawFormat
      : state.config.default_audio_format;
    el.quality.value = "";
  } else {
    el.videoFormat.value = state.config.video_formats.includes(rawFormat)
      ? rawFormat
      : state.config.default_video_format;
    const quality = params.get("quality") || "";
    el.quality.value = state.config.quality_levels.includes(quality)
      ? quality
      : "";
  }
  syncMode();
  return url;
}

function formatDuration(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return "";
  }
  const seconds = Math.round(value);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${
      String(rest).padStart(2, "0")
    }`;
  }
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatSpeed(value) {
  if (typeof value !== "number" || value <= 0) {
    return "";
  }
  if (value >= 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB/s`;
  }
  if (value >= 1024) {
    return `${(value / 1024).toFixed(0)} KB/s`;
  }
  return `${value.toFixed(0)} B/s`;
}

function statusText(status) {
  return {
    queued: "В очереди",
    waiting_for_source: "Ждёт источник",
    downloading_source: "Скачивание",
    processing: "Обработка",
    ready: "Готово",
    failed: "Ошибка",
  }[status] || status;
}

function stageText(stage) {
  return {
    queued: "В очереди",
    waiting: "Ожидание источника",
    downloading: "Скачивание",
    merging: "Сборка контейнера",
    remuxing: "Ремукс",
    transcoding: "Перекодирование",
    extracting: "Извлечение аудио",
    finalizing: "Финализация",
    ready: "Готово",
    failed: "Ошибка",
  }[stage] || stage;
}

function detailText(data) {
  if (data.status === "failed") {
    return data.error || "Не удалось выполнить запрос";
  }
  if (data.status === "ready") {
    return "Файл готов к скачиванию.";
  }
  if (data.status === "waiting_for_source") {
    return "Этот источник уже скачивается другим запросом.";
  }
  if (data.status === "queued") {
    return "Задача в очереди.";
  }
  const progress = data.progress || {};
  const speed = formatSpeed(progress.speed_bps);
  if (speed) {
    return `${stageText(data.stage)} · ${speed}`;
  }
  return stageText(data.stage);
}

function renderMeta(data) {
  const parts = [];
  const duration = formatDuration(data.duration);
  if (duration) {
    parts.push(duration);
  }
  if (data.format) {
    parts.push(String(data.format).toUpperCase());
  }
  const quality = data.actual_quality || data.source_quality;
  if (quality) {
    parts.push(quality);
  }
  if (data.source_cached) {
    parts.push("из кэша");
  }
  el.mediaMeta.textContent = parts.join(" · ");
  el.mediaMeta.hidden = parts.length === 0;
}

function renderStatus(data) {
  el.statusCard.hidden = false;
  el.statusCard.dataset.status = data.status || "queued";
  el.statusLabel.textContent = stageText(data.stage || data.status);
  el.statusBadge.textContent = statusText(data.status);
  el.mediaTitle.textContent = data.title || "Загрузка";
  renderMeta(data);

  const progress = data.progress || {};
  const percent = typeof progress.percent === "number"
    ? Math.max(0, Math.min(100, progress.percent))
    : null;
  if (percent === null) {
    el.progress.removeAttribute("value");
    el.progressText.textContent = "…";
  } else {
    el.progress.value = percent;
    el.progressText.textContent = `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
  }

  el.startedAt.textContent = data.started_at_local || "";
  el.statusDetail.textContent = detailText(data);
  el.downloadLink.hidden = true;
}

function renderReady(data) {
  renderStatus({ ...data, status: "ready", stage: "ready" });
  el.progress.value = 100;
  el.progressText.textContent = "100%";
  el.statusLabel.textContent = "Готово";
  el.statusBadge.textContent = "Готово";
  el.statusCard.dataset.status = "ready";
  el.downloadLink.href = data.download_url;
  if (data.filename) {
    el.downloadLink.download = data.filename;
  } else {
    el.downloadLink.removeAttribute("download");
  }
  el.downloadLink.hidden = false;
  el.downloadLink.focus();
  setBusy(false);
  state.running = false;
}

function renderError(message) {
  el.statusCard.hidden = false;
  el.statusCard.dataset.status = "failed";
  el.statusLabel.textContent = "Ошибка";
  el.statusBadge.textContent = "Ошибка";
  el.mediaTitle.textContent = "Не удалось выполнить запрос";
  el.mediaMeta.hidden = true;
  el.progress.removeAttribute("value");
  el.progressText.textContent = "";
  el.startedAt.textContent = "";
  el.statusDetail.textContent = message;
  el.downloadLink.hidden = true;
  setBusy(false);
  state.running = false;
}

function stopPolling() {
  state.running = false;
  if (state.timer) {
    clearTimeout(state.timer);
    state.timer = null;
  }
  if (state.controller) {
    state.controller.abort();
    state.controller = null;
  }
}

function schedulePoll(seconds) {
  if (!state.running) {
    return;
  }
  const delay = Math.max(
    1000,
    Math.min(30000, (Number(seconds) || POLL_FALLBACK_MS / 1000) * 1000),
  );
  state.timer = setTimeout(poll, delay);
}

async function parseJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function handleAuthError() {
  stopPolling();
  setBusy(false);
  showFormError("Нужен API-токен: откройте «Токен» и сохраните его.");
  setTokenPanel(true);
}

async function poll() {
  if (!state.running || !state.request) {
    return;
  }
  const controller = new AbortController();
  state.controller = controller;
  try {
    const response = await fetch("/videos", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
      },
      body: JSON.stringify({
        url: state.request.url,
        audio_only: state.request.audioOnly,
        format: state.request.format,
        quality: state.request.audioOnly ? null : state.request.quality || null,
        respond_json: true,
      }),
      signal: controller.signal,
    });

    if (response.status === 401) {
      handleAuthError();
      return;
    }

    const data = await parseJson(response);
    state.errors = 0;
    if (response.status === 202) {
      renderStatus(data);
      setBusy(true);
      schedulePoll(data.retry_after);
      return;
    }

    if (
      response.status === 200 && data.status === "ready" && data.download_url
    ) {
      renderReady(data);
      return;
    }

    const message = data.error || data.detail || `HTTP ${response.status}`;
    renderError(String(message));
  } catch (error) {
    if (error?.name === "AbortError") {
      return;
    }
    state.errors += 1;
    if (state.errors <= 5) {
      el.statusDetail.textContent = "Сеть недоступна, повторяем…";
      schedulePoll(3);
      return;
    }
    renderError("Не удалось связаться с сервером. Проверьте сеть и повторите.");
  } finally {
    if (state.controller === controller) {
      state.controller = null;
    }
  }
}

function startRequest(params) {
  stopPolling();
  showFormError("");
  const rawFormat = (params.get("format") || "").toLowerCase();
  const audioOnly = params.get("audio_only") === "1" ||
    isAudioFormat(rawFormat);
  const format = audioOnly
    ? (isAudioFormat(rawFormat) ? rawFormat : state.config.default_audio_format)
    : (state.config.video_formats.includes(rawFormat)
      ? rawFormat
      : state.config.default_video_format);
  state.request = {
    url: params.get("url") || "",
    audioOnly,
    format,
    quality: audioOnly ? "" : (params.get("quality") || ""),
  };
  state.running = true;
  state.errors = 0;
  setBusy(true);
  el.statusCard.hidden = false;
  el.statusCard.dataset.status = "queued";
  el.statusLabel.textContent = "Загрузка";
  el.statusBadge.textContent = "queued";
  el.mediaTitle.textContent = "Загрузка";
  el.mediaMeta.hidden = true;
  el.progress.removeAttribute("value");
  el.progressText.textContent = "Ожидание";
  el.startedAt.textContent = "";
  el.statusDetail.textContent = "Отправляем запрос…";
  el.downloadLink.hidden = true;
  poll();
}

function validUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function handleSubmit(event) {
  event.preventDefault();
  showFormError("");
  const url = el.url.value.trim();
  if (!validUrl(url)) {
    showFormError("Укажите корректную HTTP/HTTPS-ссылку.");
    return;
  }
  if (state.config.auth_required && !state.token) {
    showFormError("Сначала сохраните API-токен.");
    setTokenPanel(true);
    return;
  }
  const params = paramsFromForm();
  writeLocation(params);
  startRequest(params);
}

function resetView() {
  stopPolling();
  setBusy(false);
  el.statusCard.hidden = true;
  el.downloadLink.hidden = true;
  el.statusDetail.textContent = "";
  el.mediaTitle.textContent = "Загрузка";
  el.mediaMeta.hidden = true;
  showFormError("");
  el.url.value = "";
  history.pushState({}, "", "/ui");
  el.url.focus();
}

async function init() {
  const config = await loadConfig();
  fillConfig(config);
  updateTokenUi();
  if (state.token) {
    el.tokenInput.value = "";
  }
  const initialUrl = applyLocation();
  el.tokenToggle.addEventListener("click", () => {
    setTokenPanel(el.tokenPanel.hidden);
  });
  el.tokenSave.addEventListener("click", () => {
    const value = el.tokenInput.value.trim();
    if (!value) {
      showFormError("Введите токен.");
      return;
    }
    writeToken(value);
    el.tokenInput.value = "";
    setTokenPanel(false);
    showFormError("");
    const currentUrl = new URLSearchParams(location.search).get("url") || "";
    if (currentUrl && !state.running) {
      startRequest(new URLSearchParams(location.search));
    }
  });
  el.tokenClear.addEventListener("click", () => {
    writeToken("");
    setTokenPanel(true);
    showFormError("");
  });
  el.form.addEventListener("submit", handleSubmit);
  el.resetButton.addEventListener("click", resetView);
  el.modeVideo.addEventListener("change", syncMode);
  el.modeAudio.addEventListener("change", syncMode);
  addEventListener("popstate", () => {
    stopPolling();
    setBusy(false);
    const nextUrl = applyLocation();
    if (nextUrl) {
      startRequest(new URLSearchParams(location.search));
    } else {
      el.statusCard.hidden = true;
      el.downloadLink.hidden = true;
    }
  });
  if (initialUrl) {
    el.options.open = false;
    if (!state.config.auth_required || state.token) {
      startRequest(new URLSearchParams(location.search));
    } else {
      setTokenPanel(true);
      showFormError("Сохраните API-токен, чтобы начать загрузку.");
    }
  }
}

init();
