/* Стенограф — клиентская логика */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const api = async (path, opts = {}) => {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let msg = `Ошибка ${res.status}`;
      try { const j = await res.json(); if (j.error) msg = j.error; } catch {}
      throw new Error(msg);
    }
    return res.json();
  };
  const postJSON = (path, data) =>
    api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    boot: null,
    cfg: {},
    page: "dictation",
    recStart: 0,
    jobs: [],
    hist: [],
    histQuery: "",
    capturing: false,
    lastError: null,
    micTest: false,
    modelLoaded: false,
  };

  /* ─────────────────────────── тосты ─────────────────────────── */
  function toast(text, kind = "ok", ms = 3200) {
    const el = document.createElement("div");
    el.className = "toast" + (kind === "error" ? " error" : kind === "warn" ? " warn" : "");
    el.textContent = text;
    $("#toasts").appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 260);
    }, ms);
  }

  /* ─────────────────────────── навигация ─────────────────────────── */
  const PAGES = {
    dictation: ["Диктовка", "Говорите — текст появится в активном окне"],
    files: ["Аудио и видео", "Расшифровка файлов целиком на этом компьютере"],
    history: ["История", "Всё, что было расшифровано"],
    settings: ["Настройки", "Микрофон, клавиши и вставка текста"],
    about: ["О программе", "Версия, технологии и авторские права"],
  };

  function go(page) {
    if (!PAGES[page]) page = "dictation";
    state.page = page;
    $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.page === page));
    $$(".page").forEach((p) => p.classList.toggle("is-active", p.id === `page-${page}`));
    $("#pageTitle").textContent = PAGES[page][0];
    $("#pageSub").textContent = PAGES[page][1];
    if (page === "history") loadHistory();
    if (page === "settings") fillSettings();
    if (page === "files") pollJobs();
    $(".main").scrollTop = 0;
  }

  $$(".nav-item").forEach((btn) => btn.addEventListener("click", () => go(btn.dataset.page)));

  /* ─────────────────────────── загрузка ─────────────────────────── */
  async function bootstrap() {
    const data = await api("/api/bootstrap");
    state.boot = data;
    state.cfg = data.config;
    applyTheme(state.cfg.theme || "dark");

    $("#hotkeyText").textContent = data.hotkey_text;
    $("#hotkeyText2").textContent = data.hotkey_text;
    $("#hotkeyCapture").textContent = data.hotkey_text;

    const modelOk = data.model.ready;
    state.modelLoaded = data.model_loaded;
    setStatus(
      modelOk ? (data.model_loaded ? "Готово" : "Загрузка модели…") : "Нет модели",
      modelOk ? (data.model_loaded ? "ok" : "warn") : "warn"
    );
    $("#modelChipText").textContent = modelOk ? "GigaAM v3 · офлайн" : "Модель не найдена";
    $("#modelChip .dot").className = "dot " + (modelOk ? "ok" : "warn");

    fillDevices(data.devices);
    fillSettings();
    renderStats(data.stats);
    setIdleWave($("#levelWrap"));
    setIdleWave($("#micLevel"));
    renderJobs();
    if (!data.ffmpeg) toast("FFmpeg не найден — часть форматов может не открыться", "warn", 5200);
    if (data.hotkey_error) {
      const box = $("#hotkeyStatus");
      box.hidden = false;
      box.className = "hint-box bad";
      box.textContent = `Горячая клавиша не назначена: ${data.hotkey_error}. Выберите другое сочетание.`;
    }

    $("#boot").classList.add("is-hidden");
    startPolling();
    if (data.model.ready && !data.model_loaded) waitForModel();
  }

  async function waitForModel() {
    for (let i = 0; i < 120; i++) {
      await sleep(500);
      try {
        const d = await api("/api/bootstrap");
        if (d.model_loaded) {
          state.modelLoaded = true;
          setStatus("Готово", "ok");
          $("#modelChipText").textContent = "GigaAM v3 · офлайн";
          toast("Модель загружена — можно диктовать");
          return;
        }
        if (d.model_error) break;
      } catch { break; }
    }
    setStatus("Модель не загружена", "warn");
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function setStatus(text, kind = "") {
    $("#statusText").textContent = text;
    $("#statusDot").className = "dot " + kind;
  }

  /* ─────────────────────────── тема ─────────────────────────── */
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
  }
  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    applyTheme(next);
    state.cfg.theme = next;
    saveSettings({ theme: next });
  });

  /* ─────────────────────────── настройки ─────────────────────────── */
  let saveTimer = null;
  function saveSettings(patch) {
    Object.assign(state.cfg, patch);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const res = await postJSON("/api/settings", patch);
        state.cfg = res.config;
        if ("hotkey" in patch) {
          $("#hotkeyText").textContent = res.hotkey_text;
          $("#hotkeyText2").textContent = res.hotkey_text;
          $("#hotkeyCapture").textContent = res.hotkey_text;
          const box = $("#hotkeyStatus");
          if (!res.hotkey_ok) {
            box.hidden = false;
            box.className = "hint-box bad";
            box.textContent = `Не удалось назначить: ${res.hotkey_error || "сочетание занято"}`;
          } else {
            box.hidden = true;
          }
        }
      } catch (e) { toast(e.message, "error"); }
    }, 220);
  }

  function fillDevices(devices) {
    const sel = $("#selMic");
    const current = state.cfg.mic_device;
    sel.innerHTML = "";
    const def = document.createElement("option");
    def.value = "";
    def.textContent = "Системный по умолчанию";
    sel.appendChild(def);
    (devices || []).forEach((d) => {
      const o = document.createElement("option");
      o.value = String(d.index);
      o.textContent = d.name + (d.default ? " · по умолчанию" : d.api ? ` · ${d.api}` : "");
      sel.appendChild(o);
    });
    const val = current === null || current === undefined ? "" : String(current);
    if ($$("option", sel).some((o) => o.value === val)) sel.value = val;
    $("#ffmpegState").textContent = state.boot?.ffmpeg ? "подключён" : "не найден";
    $("#ffmpegState").className = state.boot?.ffmpeg ? "ok" : "bad";
    $("#dataPath").textContent = state.boot?.data_dir || "—";
    $("#versionState").textContent = state.boot?.version || "—";
    $("#aboutVersion").textContent = "версия " + (state.boot?.version || "1.0.0");
    renderModel();
  }

  function renderModel() {
    const m = state.boot?.model || { ready: false, missing: [], dir: "" };
    const box = $("#modelInfo");
    box.innerHTML = `
      <div><span>Статус</span><code class="${m.ready ? "ok" : "bad"}">${m.ready ? "установлена" : "не найдена"}</code></div>
      <div><span>Размер</span><code>${m.size_mb ? m.size_mb + " МБ" : "—"}</code></div>
      <div><span>Путь</span><code title="${esc(m.dir)}">${esc(m.dir)}</code></div>`;
    const actions = $("#modelActions");
    actions.innerHTML = "";
    if (!m.ready) {
      const b = document.createElement("button");
      b.className = "btn";
      b.textContent = "Скачать модель (220 МБ)";
      b.onclick = downloadModel;
      actions.appendChild(b);
    } else {
      const tag = document.createElement("span");
      tag.className = "muted";
      tag.textContent = "Работает без интернета. Поддерживает пунктуацию и числа.";
      actions.appendChild(tag);
    }
  }

  async function downloadModel() {
    const btn = $("#modelActions .btn");
    if (btn) { btn.disabled = true; btn.textContent = "Загрузка…"; }
    try {
      await postJSON("/api/model/download");
      const t = setInterval(async () => {
        const st = await api("/api/download-model");
        const s = st.state;
        if (btn) btn.textContent = `${s.stage} ${Math.round((s.progress || 0) * 100)}%`;
        if (s.status === "done") {
          clearInterval(t);
          toast("Модель установлена");
          const d = await api("/api/bootstrap");
          state.boot = d;
          fillDevices(d.devices);
          waitForModel();
        } else if (s.status === "error") {
          clearInterval(t);
          toast("Не удалось скачать модель: " + s.error, "error", 6000);
          if (btn) { btn.disabled = false; btn.textContent = "Повторить загрузку"; }
        }
      }, 900);
    } catch (e) {
      toast(e.message, "error");
      if (btn) btn.disabled = false;
    }
  }

  function fillSettings() {
    const c = state.cfg;
    $("#selInsert").value = c.insert_mode;
    $("#selInsert2").value = c.insert_mode;
    $("#chkRestore").checked = !!c.restore_clipboard;
    $("#chkRestore2").checked = !!c.restore_clipboard;
    $("#chkSounds").checked = !!c.sound_cues;
    $("#chkSounds2").checked = !!c.sound_cues;
    $("#chkTray").checked = !!c.minimize_to_tray;
    $("#chkWidget").checked = c.widget !== false;
    $("#chkWidget2").checked = c.widget !== false;
    $$("#modeSeg button, #modeSeg2 button").forEach((b) =>
      b.classList.toggle("is-active", b.dataset.mode === c.mode)
    );
    const sel = $("#selMic");
    const val = c.mic_device === null || c.mic_device === undefined ? "" : String(c.mic_device);
    if ($$("option", sel).some((o) => o.value === val)) sel.value = val;
  }

  $("#selMic").addEventListener("change", (e) => {
    const v = e.target.value;
    saveSettings({ mic_device: v === "" ? null : Number(v) });
  });
  $("#selInsert").addEventListener("change", (e) => {
    saveSettings({ insert_mode: e.target.value });
    $("#selInsert2").value = e.target.value;
  });
  $("#selInsert2").addEventListener("change", (e) => {
    saveSettings({ insert_mode: e.target.value });
    $("#selInsert").value = e.target.value;
  });
  $("#chkRestore").addEventListener("change", (e) => {
    saveSettings({ restore_clipboard: e.target.checked });
    $("#chkRestore2").checked = e.target.checked;
  });
  $("#chkRestore2").addEventListener("change", (e) => {
    saveSettings({ restore_clipboard: e.target.checked });
    $("#chkRestore").checked = e.target.checked;
  });
  $("#chkSounds").addEventListener("change", (e) => {
    saveSettings({ sound_cues: e.target.checked });
    $("#chkSounds2").checked = e.target.checked;
  });
  $("#chkSounds2").addEventListener("change", (e) => {
    saveSettings({ sound_cues: e.target.checked });
    $("#chkSounds").checked = e.target.checked;
  });
  $("#chkTray").addEventListener("change", (e) => saveSettings({ minimize_to_tray: e.target.checked }));

  function syncWidget(on) {
    saveSettings({ widget: on });
    $("#chkWidget").checked = on;
    $("#chkWidget2").checked = on;
  }
  $("#chkWidget").addEventListener("change", (e) => syncWidget(e.target.checked));
  $("#chkWidget2").addEventListener("change", (e) => syncWidget(e.target.checked));

  $$("#modeSeg button, #modeSeg2 button").forEach((b) =>
    b.addEventListener("click", () => {
      saveSettings({ mode: b.dataset.mode });
      $$("#modeSeg button, #modeSeg2 button").forEach((x) =>
        x.classList.toggle("is-active", x.dataset.mode === b.dataset.mode)
      );
      updateModeNote(b.dataset.mode);
    })
  );

  function updateModeNote(mode) {
    $("#hotkeyNote").textContent =
      mode === "toggle"
        ? "Одно нажатие запускает запись, второе — останавливает."
        : "Зажмите сочетание, говорите, отпустите — текст вставится сам.";
    const hint = $("#recHint");
    if (!state.recStart) {
      hint.innerHTML =
        mode === "toggle"
          ? `Нажмите <kbd>${esc($("#hotkeyText").textContent)}</kbd> для старта и ещё раз — для остановки`
          : `Удерживайте <kbd>${esc($("#hotkeyText").textContent)}</kbd> и говорите`;
    }
  }

  /* ─────────────────────── горячая клавиша (захват) ─────────────────────── */
  const KEYCODES = {
    Space: "space", Enter: "enter", Tab: "tab",
    F1: "f1", F2: "f2", F3: "f3", F4: "f4", F5: "f5", F6: "f6",
    F7: "f7", F8: "f8", F9: "f9", F10: "f10", F11: "f11", F12: "f12",
    Insert: "insert", Home: "home", End: "end", PageUp: "pageup", PageDown: "pagedown",
    Backquote: "`", Minus: "minus", Equal: "equal",
  };

  $("#btnHotkey").addEventListener("click", () => {
    state.capturing = true;
    const btn = $("#btnHotkey");
    btn.classList.add("is-capturing");
    $("#hotkeyCapture").textContent = "Нажмите сочетание…";
    postJSON("/api/hotkey-capture", { active: true }).catch(() => {});
  });

  function stopCapture() {
    state.capturing = false;
    $("#btnHotkey").classList.remove("is-capturing");
    postJSON("/api/hotkey-capture", { active: false }).catch(() => {});
  }

  window.addEventListener("keydown", (e) => {
    if (!state.capturing) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Escape") {
      stopCapture();
      $("#hotkeyCapture").textContent = $("#hotkeyText").textContent;
      return;
    }
    const mods = [];
    if (e.ctrlKey) mods.push("ctrl");
    if (e.altKey) mods.push("alt");
    if (e.shiftKey) mods.push("shift");
    if (e.metaKey) mods.push("win");

    let key = KEYCODES[e.code];
    if (!key && /^Key[A-Z]$/.test(e.code)) key = e.code.slice(3).toLowerCase();
    if (!key && /^Digit\d$/.test(e.code)) key = e.code.slice(5);
    if (!key) return;
    if (mods.length === 0) {
      toast("Нужен хотя бы один модификатор: Ctrl, Alt, Shift или Win", "warn");
      return;
    }
    stopCapture();
    const label = mods.map((m) => ({ ctrl: "Ctrl", alt: "Alt", shift: "Shift", win: "Win" }[m])).join(" + ")
      + " + " + (key.length === 1 ? key.toUpperCase() : key.charAt(0).toUpperCase() + key.slice(1));
    $("#hotkeyCapture").textContent = label;
    saveSettings({ hotkey: { mods, key } });
    toast("Горячая клавиша обновлена");
  }, true);

  $("#hotkeyPill").addEventListener("click", () => go("settings"));

  /* ─────────────────────────── статистика ─────────────────────────── */
  function renderStats(stats) {
    if (!stats) return;
    $("#stToday").textContent = stats.words_today ?? 0;
    $("#stTotal").textContent = stats.words_total ?? 0;
    $("#stSpeed").textContent = stats.wpm ?? 0;
    const min = Number(stats.minutes ?? 0);
    $("#stMinutes").textContent = min < 10 ? (Math.round(min * 10) / 10).toFixed(1).replace(".0", "") : Math.round(min);
  }

  /* ─────────────────────────── диктовка ─────────────────────────── */
  const micBtn = $("#micBtn");
  let recording = false;

  const IDLE_SHAPE = [4, 6, 9, 13, 17, 21, 16, 11, 7, 5, 5, 7, 11, 16, 21, 17, 13, 9, 6, 4];
  function setIdleWave(container) {
    const bars = $$("span", container);
    bars.forEach((b, i) => {
      b.style.height = `${IDLE_SHAPE[i % IDLE_SHAPE.length]}px`;
    });
    container.classList.remove("is-live");
  }

  micBtn.addEventListener("click", async () => {
    try {
      if (recording) await postJSON("/api/dictation/stop");
      else await postJSON("/api/dictation/start");
    } catch (e) { toast(e.message, "error"); }
  });

  function setRecording(on) {
    recording = on;
    micBtn.classList.toggle("is-recording", on);
    if (on) {
      state.recStart = Date.now();
      $("#recState").textContent = "Идёт запись…";
      $("#recHint").textContent = "Говорите обычной скоростью, затем остановите запись";
      $("#levelWrap").classList.add("is-live");
      setStatus("Запись", "rec");
    } else {
      state.recStart = 0;
      $("#recTimer").textContent = "00:00";
      setIdleWave($("#levelWrap"));
    }
  }

  function renderLevel(level) {
    const bars = $$("#levelWrap span");
    const n = bars.length;
    const t = Date.now() / 110;
    bars.forEach((b, i) => {
      const shape = Math.abs(Math.sin(t + i * 0.7)) * 0.55 + Math.abs(Math.sin(t * 0.5 + i * 1.9)) * 0.45;
      const h = 4 + Math.min(1, level * (0.5 + shape)) * 24;
      b.style.height = `${h.toFixed(1)}px`;
    });
  }

  function renderMicLevel(level) {
    const bars = $$("#micLevel span");
    const t = Date.now() / 130;
    bars.forEach((b, i) => {
      const shape = Math.abs(Math.sin(t + i * 0.55)) * 0.6 + 0.4;
      const h = 4 + Math.min(1, level * shape) * 34;
      b.style.height = `${h.toFixed(1)}px`;
    });
  }

  function renderLast(res) {
    const text = (res && res.text) || "";
    const el = $("#lastText");
    if (!text) {
      el.textContent = "Здесь появится распознанный текст.";
      el.classList.add("is-empty");
      $("#btnCopyLast").disabled = true;
      $("#lastMeta").textContent = "";
      return;
    }
    el.textContent = text;
    el.classList.remove("is-empty");
    $("#btnCopyLast").disabled = false;
    const parts = [];
    if (res.duration) parts.push(`${res.duration.toFixed(1)} с речи`);
    parts.push(`${text.split(/\s+/).filter(Boolean).length} слов`);
    if (res.inserted === "clipboard") parts.push("вставлено");
    else if (res.inserted === "type") parts.push("напечатано");
    else if (res.inserted === "clipboard-ui") parts.push("скопировано");
    $("#lastMeta").textContent = parts.join(" · ");
  }

  $("#btnCopyLast").addEventListener("click", async () => {
    const text = $("#lastText").textContent;
    if (!text || $("#lastText").classList.contains("is-empty")) return;
    await postJSON("/api/copy", { text });
    toast("Текст скопирован");
  });

  /* ─────────────────────────── опрос состояния ─────────────────────────── */
  let lastState = "idle";

  async function poll() {
    try {
      const d = await api("/api/dictation");

      if (d.recording !== recording) setRecording(d.recording);

      if (d.recording) {
        renderLevel(d.level || 0);
        const secs = Math.floor((Date.now() - state.recStart) / 1000);
        $("#recTimer").textContent =
          String(Math.floor(secs / 60)).padStart(2, "0") + ":" + String(secs % 60).padStart(2, "0");
      }

      if (d.state === "working" && lastState !== "working") {
        $("#recState").textContent = "Распознаём…";
        $("#recHint").textContent = "Обычно это занимает меньше секунды";
        setStatus("Распознавание", "warn");
      }

      if (d.state === "idle" && lastState !== "idle") {
        setStatus(state.modelLoaded ? "Готово" : "Загрузка модели…", state.modelLoaded ? "ok" : "warn");
        $("#recState").textContent = "Готово к записи";
        updateModeNote(state.cfg.mode);
        if (d.last_result) {
          renderLast(d.last_result);
          if (d.last_result.text) toast("Распознано: " + d.last_result.text.slice(0, 70));
          refreshStats();
          if (state.page === "history") loadHistory();
        }
      }

      if (d.error && d.error !== state.lastError) {
        state.lastError = d.error;
        toast(d.error, "error", 5000);
        $("#recState").textContent = "Готово к записи";
      }
      if (!d.error) state.lastError = null;

      if (d.last_result && $("#lastText").classList.contains("is-empty")) renderLast(d.last_result);

      if (state.micTest) {
        renderMicLevel(d.level || 0);
      }

      lastState = d.state;
    } catch (e) {
      /* сервер перезапускается — молча */
    }
    setTimeout(poll, recording ? 180 : 700);
  }

  function startPolling() { poll(); }

  async function refreshStats() {
    try {
      const d = await api("/api/history");
      renderStats(d.stats);
    } catch {}
  }

  /* ─────────────────────────── фокус окна ─────────────────────────── */
  let focusTimer = null;
  function reportFocus(focused) {
    clearTimeout(focusTimer);
    focusTimer = setTimeout(() => {
      postJSON("/api/ui-focus", { focused }).catch(() => {});
    }, 120);
  }
  window.addEventListener("focus", () => reportFocus(true));
  window.addEventListener("blur", () => reportFocus(false));
  document.addEventListener("visibilitychange", () => reportFocus(document.visibilityState === "visible"));
  reportFocus(document.hasFocus());

  /* ─────────────────────────── микрофон: тест ─────────────────────────── */
  $("#btnMicTest").addEventListener("click", async () => {
    const btn = $("#btnMicTest");
    if (!state.micTest) {
      try {
        const r = await postJSON("/api/mic-test/start");
        if (!r.ok) throw new Error(r.error || "микрофон недоступен");
        state.micTest = true;
        btn.textContent = "Остановить проверку";
        $("#micTestHint").textContent = "Говорите — столбики должны реагировать";
      } catch (e) { toast(e.message, "error"); }
    } else {
      state.micTest = false;
      btn.textContent = "Проверить микрофон";
      $("#micTestHint").textContent = "Покажет уровень сигнала без записи в историю";
      await postJSON("/api/mic-test/stop");
      setIdleWave($("#micLevel"));
    }
  });

  /* ─────────────────────── проверка вставки ─────────────────────── */
  $("#btnInsertTest").addEventListener("click", async () => {
    const btn = $("#btnInsertTest");
    const box = $("#insertTestResult");
    const hint = $("#insertTestHint");
    btn.disabled = true;
    box.hidden = true;
    let left = 3;
    hint.textContent = `Переключитесь в Блокнот… ${left}`;
    const timer = setInterval(() => {
      left -= 1;
      if (left > 0) hint.textContent = `Переключитесь в Блокнот… ${left}`;
    }, 1000);
    try {
      const r = await postJSON("/api/insert-test", { delay: 3, text: "Стенограф — проверка вставки." });
      clearInterval(timer);
      if (r.paste_events > 0) {
        box.className = "hint-box";
        box.textContent = `Windows приняла нажатие Ctrl+V (${r.paste_events} события). Если в Блокноте текст не появился — ввод перехватывает другая программа или защита антивируса.`;
      } else if (r.typed_events > 0) {
        box.className = "hint-box";
        box.textContent = `Вставка из буфера заблокирована, но печать работает (${r.typed_events} событий). Переключите способ вставки на «Симуляция нажатий клавиш» — диктовка заработает.`;
      } else {
        box.className = "hint-box bad";
        box.textContent = "Windows полностью блокирует синтетический ввод (0 событий). Обычно это «защита ввода» антивируса или права администратора. Запустите Стенограф от имени администратора и добавьте его в исключения антивируса.";
      }
      box.hidden = false;
      hint.textContent = "Проверка выполнена";
    } catch (e) {
      clearInterval(timer);
      box.className = "hint-box bad";
      box.textContent = "Ошибка проверки: " + e.message;
      box.hidden = false;
    } finally {
      btn.disabled = false;
    }
  });

  /* ─────────────────────────── файлы ─────────────────────────── */
  const drop = $("#drop");
  const fileInput = $("#fileInput");

  $("#btnPick").addEventListener("click", (e) => { e.stopPropagation(); fileInput.click(); });
  drop.addEventListener("click", (e) => { if (e.target === drop || e.target.closest(".drop-icon")) fileInput.click(); });
  fileInput.addEventListener("change", () => { uploadFiles(fileInput.files); fileInput.value = ""; });

  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("is-over"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("is-over"); })
  );
  drop.addEventListener("drop", (e) => {
    if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files);
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  async function uploadFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    go("files");
    for (const f of list) {
      try {
        const res = await fetch(`/api/transcribe?name=${encodeURIComponent(f.name)}`, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: f,
        });
        if (!res.ok) {
          const j = await res.json().catch(() => ({}));
          throw new Error(j.error || `ошибка ${res.status}`);
        }
        await res.json();
      } catch (e) {
        toast(`${f.name}: ${e.message}`, "error", 5000);
      }
    }
    pollJobs();
  }

  let jobsTimer = null;
  async function pollJobs() {
    clearTimeout(jobsTimer);
    try {
      const d = await api("/api/jobs");
      state.jobs = d.jobs;
      renderJobs();
      const active = state.jobs.some((j) => j.status === "running" || j.status === "queued");
      if (active) jobsTimer = setTimeout(pollJobs, 600);
      else if (state.jobs.length) refreshStats();
    } catch {}
  }

  function renderJobs() {
    const box = $("#jobsList");
    const jobs = state.jobs;
    $("#btnClearJobs").hidden = !jobs.some((j) => j.status === "done" || j.status === "error");
    $("#jobsSummary").textContent = jobs.length
      ? `${jobs.length} файл(ов) · готово ${jobs.filter((j) => j.status === "done").length}`
      : "Файлы не выбраны";

    if (!jobs.length) {
      box.innerHTML = `<div class="empty">
        <svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m10 9.5 5 2.5-5 2.5Z"/></svg>
        <strong>Пока ничего не расшифровано</strong>
        <span>Перетащите файлы в область выше — текст появится здесь</span>
      </div>`;
      return;
    }

    box.innerHTML = jobs.map((j) => {
      const running = j.status === "running" || j.status === "queued";
      const pct = Math.round((j.progress || 0) * 100);
      const meta = [];
      if (j.duration) meta.push(fmtDur(j.duration));
      if (j.status === "done") {
        meta.push(`${j.elapsed?.toFixed(1) || "?"} с`);
        if (j.rtf) meta.push(`×${(1 / j.rtf).toFixed(0)} быстрее`);
      }
      if (running) meta.push(j.stage);
      const actions = j.status === "done" ? `
        <div class="job-actions">
          <button class="mini-btn" data-act="copy" data-id="${j.id}">Копировать</button>
          ${j.history_id ? `<a class="mini-btn" href="/api/history/${j.history_id}.txt" download>.txt</a>` : ""}
          ${j.history_id && j.segments?.length ? `<a class="mini-btn" href="/api/history/${j.history_id}.srt" download>.srt</a>` : ""}
          <button class="mini-btn danger" data-act="remove" data-id="${j.id}">Убрать</button>
        </div>` : "";
      return `<div class="job ${running ? "is-running" : ""} ${j.status === "error" ? "is-error" : ""}" data-id="${j.id}">
        <div class="job-top">
          ${running ? `<span class="spinner"></span>` : ""}
          <span class="job-name" title="${esc(j.name)}">${esc(j.name)}</span>
          <span class="job-meta">${esc(meta.join(" · "))}</span>
        </div>
        ${running ? `<div class="job-bar"><i style="width:${pct}%"></i></div>` : ""}
        ${j.text ? `<div class="job-text">${esc(j.text)}</div>` : ""}
        ${j.error ? `<div class="job-err">${esc(j.error)}</div>` : ""}
        ${actions}
      </div>`;
    }).join("");

    box.querySelectorAll("[data-act]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        const job = state.jobs.find((j) => j.id === id);
        if (btn.dataset.act === "copy") {
          await postJSON("/api/copy", { text: job.text });
          toast("Текст скопирован");
        } else if (btn.dataset.act === "remove") {
          await postJSON(`/api/jobs/${id}/cancel`);
          state.jobs = state.jobs.filter((j) => j.id !== id);
          renderJobs();
        }
      });
    });
  }

  $("#btnClearJobs").addEventListener("click", async () => {
    await postJSON("/api/jobs/clear");
    pollJobs();
  });

  function fmtDur(sec) {
    sec = Math.round(sec || 0);
    const m = Math.floor(sec / 60), s = sec % 60;
    return m ? `${m}:${String(s).padStart(2, "0")}` : `${s} с`;
  }

  /* ─────────────────────────── история ─────────────────────────── */
  let histTimer = null;
  $("#histSearch").addEventListener("input", (e) => {
    clearTimeout(histTimer);
    histTimer = setTimeout(() => { state.histQuery = e.target.value.trim(); loadHistory(); }, 220);
  });

  async function loadHistory() {
    try {
      const d = await api("/api/history" + (state.histQuery ? `?q=${encodeURIComponent(state.histQuery)}` : ""));
      state.hist = d.items;
      renderStats(d.stats);
      renderHistory();
    } catch (e) { /* ignore */ }
  }

  function renderHistory() {
    const box = $("#histList");
    if (!state.hist.length) {
      box.innerHTML = `<div class="empty">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.8V12l3 1.8"/></svg>
        <strong>${state.histQuery ? "Ничего не найдено" : "История пока пуста"}</strong>
        <span>${state.histQuery ? "Попробуйте другой запрос" : "Расшифруйте файл или продиктуйте текст"}</span>
      </div>`;
      return;
    }
    box.innerHTML = state.hist.map((it) => `
      <div class="hist-item" data-id="${it.id}">
        <div class="hist-head">
          <span class="hist-kind ${it.kind === "file" ? "file" : ""}">${it.kind === "file" ? "файл" : "диктовка"}</span>
          <span class="hist-src" title="${esc(it.source)}">${esc(it.source)}</span>
          <span class="hist-time">${fmtTime(it.created)}</span>
        </div>
        <div class="hist-text">${esc(it.text)}</div>
        <div class="hist-actions">
          <span class="hist-words">${it.words} слов${it.duration ? " · " + fmtDur(it.duration) : ""}</span>
          <button class="mini-btn" data-act="toggle">Развернуть</button>
          <button class="mini-btn" data-act="copy">Копировать</button>
          <a class="mini-btn" href="/api/history/${it.id}.txt" download>Скачать</a>
          ${it.segments?.length ? `<a class="mini-btn" href="/api/history/${it.id}.srt" download>SRT</a>` : ""}
          <button class="mini-btn danger" data-act="del">Удалить</button>
        </div>
      </div>`).join("");

    box.querySelectorAll(".hist-item").forEach((card) => {
      const id = card.dataset.id;
      const item = state.hist.find((x) => x.id === id);
      card.querySelectorAll("[data-act]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const act = btn.dataset.act;
          if (act === "toggle") {
            card.classList.toggle("is-open");
            btn.textContent = card.classList.contains("is-open") ? "Свернуть" : "Развернуть";
          } else if (act === "copy") {
            await postJSON("/api/copy", { text: item.text });
            toast("Текст скопирован");
          } else if (act === "del") {
            await postJSON(`/api/history/${id}/delete`);
            state.hist = state.hist.filter((x) => x.id !== id);
            renderHistory();
            refreshStats();
          }
        });
      });
    });
  }

  function fmtTime(ts) {
    const d = new Date((ts || 0) * 1000);
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    const hm = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    return sameDay ? `сегодня, ${hm}` : `${d.toLocaleDateString("ru-RU", { day: "2-digit", month: "short" })}, ${hm}`;
  }

  $("#btnClearHist").addEventListener("click", async () => {
    if (!state.hist.length) return;
    if (!confirm("Удалить всю историю распознаваний?")) return;
    await postJSON("/api/history/clear");
    state.hist = [];
    renderHistory();
    refreshStats();
    toast("История очищена");
  });

  /* ─────────────────────────── прочее ─────────────────────────── */
  $("#btnOpenData").addEventListener("click", async () => {
    const r = await postJSON("/api/open-folder", { which: "data" });
    toast("Открыто: " + r.path);
  });
  $("#btnQuit").addEventListener("click", async () => {
    if (!confirm("Завершить работу Стенограф?")) return;
    await postJSON("/api/shutdown");
    document.body.innerHTML = `<div class="boot"><div class="boot-card"><strong>Приложение закрыто</strong><span>Окно можно закрыть</span></div></div>`;
  });

  // горячие клавиши в интерфейсе
  window.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea")) return;
    if (e.key === "1" && e.ctrlKey) go("dictation");
    if (e.key === "2" && e.ctrlKey) go("files");
    if (e.key === "3" && e.ctrlKey) go("history");
    if (e.key === "4" && e.ctrlKey) go("settings");
  });

  /* ─────────────────────────── старт ─────────────────────────── */  bootstrap().catch((e) => {
    $("#bootHint").textContent = "Не удалось подключиться к локальному серверу: " + e.message;
  });
})();
