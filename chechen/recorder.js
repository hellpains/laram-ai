const record = document.querySelector("#record");
const recordLabel = document.querySelector("#record-label");
const upload = document.querySelector("#upload");
const picker = document.querySelector("#audio");
const status = document.querySelector("#status");
const detail = document.querySelector("#detail");
const output = document.querySelector("#transcript");
const copy = document.querySelector("#copy");
const retry = document.querySelector("#retry");
const originalPanel = document.querySelector("#original-panel");
const originalText = document.querySelector("#original-text");
fetch("/health").then(response => response.json()).then(info => {
  document.querySelector("#gemini-notice").hidden = !info.gemini_enabled;
}).catch(() => {});
let recorder, interval, pending, transcript = "", busy = false;

function show(title, message, state = "idle") {
  status.textContent = title;
  detail.textContent = message;
  document.body.dataset.state = state;
}

function lock(value) {
  busy = value;
  record.disabled = upload.disabled = retry.disabled = value;
}

async function transcribe(audio) {
  if (!audio.size || audio.size >= 32 * 1024 * 1024) {
    show("Проверьте файл", "Выберите непустое аудио размером до 32 МБ.", "error");
    return;
  }
  pending = audio;
  retry.hidden = true;
  lock(true);
  show("Распознаём речь", "Первый запуск может занять больше времени, пока загружается модель.", "working");
  const form = new FormData();
  form.append("audio", audio, audio.name || "microphone");
  try {
    const response = await fetch("/api/transcribe", { method: "POST", body: form });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Ошибка распознавания.");
    transcript = result.text;
    originalPanel.hidden = result.correction !== "applied";
    originalPanel.open = false;
    originalText.textContent = result.original_text || "";
    output.textContent = transcript || "Речь не распознана.";
    output.classList.toggle("empty", !transcript);
    copy.disabled = !transcript;
    pending = null;
    if (result.correction === "failed") {
      show("Расшифровка готова", "Gemini недоступен. Показан исходный текст.");
    } else {
      show(transcript ? "Расшифровка готова" : "Речь не распознана",
        result.correction === "applied" ? "Текст исправлен Gemini." :
          transcript ? "Текст можно скопировать." : "Попробуйте записать речь ближе к микрофону.");
    }
  } catch (error) {
    show("Не удалось обработать аудио", error.message, "error");
    retry.hidden = false;
  } finally {
    lock(false);
  }
}

record.addEventListener("click", async () => {
  if (recorder?.state === "recording") {
    record.disabled = true;
    recorder.stop();
    return;
  }
  if (busy) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    show("Микрофон недоступен", "Откройте страницу через localhost или HTTPS либо загрузите файл.", "error");
    return;
  }
  lock(true);
  retry.hidden = true;
  show("Подключаем микрофон", "Разрешите доступ в браузере.", "working");
  let stream;
  let failed = false;
  function release() {
    clearInterval(interval);
    stream?.getTracks().forEach(track => track.stop());
    recorder = null;
    recordLabel.textContent = "Начать запись";
    record.setAttribute("aria-label", recordLabel.textContent);
    lock(false);
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorder = new MediaRecorder(stream);
    const chunks = [];
    recorder.addEventListener("dataavailable", event => {
      if (event.data.size) chunks.push(event.data);
    });
    recorder.addEventListener("error", () => {
      failed = true;
      release();
      show("Запись прервалась", "Проверьте микрофон и попробуйте снова.", "error");
    });
    recorder.addEventListener("stop", () => {
      if (failed) return;
      release();
      void transcribe(new Blob(chunks, { type: chunks[0]?.type }));
    }, { once: true });
    recorder.start(1000);
    record.disabled = false;
    recordLabel.textContent = "Остановить и распознать";
    record.setAttribute("aria-label", recordLabel.textContent);
    show("Идёт запись · 00:00", "Говорите по-чеченски. Максимум 5 минут.", "recording");
    const started = performance.now();
    interval = setInterval(() => {
      const seconds = Math.floor((performance.now() - started) / 1000);
      const time = [Math.floor(seconds / 60), seconds % 60].map(value => String(value).padStart(2, "0")).join(":");
      status.textContent = `Идёт запись · ${time}`;
      if (seconds >= 299 && recorder?.state === "recording") {
        record.disabled = true;
        recorder.stop();
      }
    }, 250);
  } catch (error) {
    release();
    show("Не удалось начать запись", error.name === "NotAllowedError"
      ? "Разрешите доступ к микрофону в браузере или загрузите файл."
      : "Подключите микрофон и попробуйте снова.", "error");
  }
});

upload.addEventListener("click", () => picker.click());
picker.addEventListener("change", () => {
  const file = picker.files[0];
  picker.value = "";
  if (file && !busy) void transcribe(file);
});
retry.addEventListener("click", () => {
  if (pending && !busy) void transcribe(pending);
});
copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(transcript);
    copy.textContent = "Скопировано";
    setTimeout(() => { copy.textContent = "Копировать"; }, 1500);
  } catch {
    show("Не удалось скопировать", "Выделите текст и скопируйте его вручную.", "error");
  }
});
