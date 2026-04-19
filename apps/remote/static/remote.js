(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const DEBUG = params.get("debug") === "true";

  const SELF = document.body?.dataset?.self || "";
  const OTHER = document.body?.dataset?.other || (SELF === "adam" ? "steve" : "adam");
  const OTHER_LABEL = OTHER.charAt(0).toUpperCase() + OTHER.slice(1);
  const API = `/api/${encodeURIComponent(SELF)}`;

  const logEl = document.getElementById("log");
  if (logEl) logEl.hidden = !DEBUG;

  function log(msg) {
    if (!DEBUG || !logEl) return;
    const ts = new Date().toLocaleTimeString();
    logEl.textContent = `[${ts}] ${msg}\n${logEl.textContent}`;
  }

  function setBtnEnabled(btn, enabled) {
    if (!btn) return;
    btn.disabled = !enabled;
    btn.classList.toggle("disabled", !enabled);
    if (enabled) btn.removeAttribute("aria-disabled");
    else btn.setAttribute("aria-disabled", "true");
  }

  function setStatus(el, text, tone = "muted") {
    if (!el) return;
    el.textContent = text || "";
    if (text) el.setAttribute("data-tone", tone);
    else el.removeAttribute("data-tone");
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatInterval(seconds) {
    if (!Number.isFinite(seconds)) return "-";
    if (seconds < 60) return `${seconds} sekúndur`;
    if (seconds % 60 === 0) return `${seconds / 60} mínútur`;
    return `${seconds} sekúndur`;
  }

  function pluralizePictures(count) {
    if (count === 1) return "1 mynd";
    return `${count} myndir`;
  }

  async function reqJson(method, path, bodyObj) {
    const init = {
      method,
      headers: bodyObj ? { "Content-Type": "application/json" } : undefined,
      body: bodyObj ? JSON.stringify(bodyObj) : undefined,
    };

    const response = await fetch(`${API}/${path}`, init);
    const text = await response.text();
    if (!response.ok) throw new Error(`${response.status} ${text}`);
    return text ? JSON.parse(text) : {};
  }

  const fileInput = document.getElementById("pictureFile");
  const uploadBtn = document.getElementById("pictureUploadBtn");
  const clearBtn = document.getElementById("pictureClearBtn");
  const previewEl = document.getElementById("picturePreview");
  const previewEmptyEl = document.getElementById("picturePreviewEmpty");
  const uploadStatusEl = document.getElementById("pictureUploadStatus");
  const playlistEl = document.getElementById("partnerPlaylist");
  const playlistSummaryEl = document.getElementById("playlistSummary");
  const frameImg = document.getElementById("frameCurrentImg");
  const frameEmptyEl = document.getElementById("frameCurrentEmpty");

  const intervalInput = document.getElementById("slideshowIntervalInput");
  const intervalSaveBtn = document.getElementById("slideshowSaveBtn");
  const intervalStatusEl = document.getElementById("slideshowStatus");
  const intervalCurrentValueEl = document.getElementById("slideshowCurrentValue");

  const reactionInput = document.getElementById("reactionInput");
  const reactionSendBtn = document.getElementById("reactionSendBtn");

  let previewUrl = null;
  let playlistRefreshToken = 0;
  let playlistPollHandle = null;

  function showSelectionPreview(file) {
    const hasFile = !!file;
    setBtnEnabled(uploadBtn, hasFile);
    setBtnEnabled(clearBtn, hasFile);

    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }

    if (!previewEl || !previewEmptyEl) return;

    if (hasFile) {
      previewUrl = URL.createObjectURL(file);
      previewEl.src = previewUrl;
      previewEl.classList.remove("d-none");
      previewEmptyEl.classList.add("d-none");
    } else {
      previewEl.removeAttribute("src");
      previewEl.classList.add("d-none");
      previewEmptyEl.classList.remove("d-none");
    }
  }

  function syncSelectedFromInput() {
    if (!fileInput) return;
    const file = fileInput.files && fileInput.files.length > 0 ? fileInput.files[0] : null;
    showSelectionPreview(file);
  }

  function renderCurrentPictureMeta(meta) {
    if (!frameImg || !frameEmptyEl) return;
    if (!meta || meta.empty || !meta.url) {
      frameEmptyEl.textContent = "Engin mynd";
      frameImg.removeAttribute("src");
      frameImg.classList.add("d-none");
      frameEmptyEl.classList.remove("d-none");
      return;
    }

    frameImg.src = `${API}/picture?t=${meta.updated_at || Date.now()}`;
    frameImg.classList.remove("d-none");
    frameEmptyEl.classList.add("d-none");
  }

  async function refreshCurrentPicture() {
    try {
      const meta = await reqJson("GET", "picture/meta");
      renderCurrentPictureMeta(meta);
      return true;
    } catch (err) {
      log(`ERR GET picture/meta -> ${err}`);
      if (frameImg) frameImg.classList.add("d-none");
      if (frameEmptyEl) {
        frameEmptyEl.textContent = "Ekki tókst að sækja mynd";
        frameEmptyEl.classList.remove("d-none");
      }
      return false;
    }
  }

  async function deletePicture(pictureId, button) {
    const previousText = button?.textContent;
    if (button) {
      button.disabled = true;
      button.textContent = "Eyði...";
    }

    try {
      const response = await reqJson("DELETE", `pictures/${encodeURIComponent(pictureId)}`);
      log(`DELETE pictures/${pictureId} -> ${JSON.stringify(response)}`);
      setStatus(uploadStatusEl, "Mynd fjarlægð", "success");
      await refreshPartnerPictures();
    } catch (err) {
      log(`ERR DELETE pictures/${pictureId} -> ${err}`);
      setStatus(uploadStatusEl, `Ekki tókst að eyða mynd`, "error");
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = previousText || "Eyða";
      }
    }
  }

  function bindPlaylistActions() {
    if (!playlistEl) return;
    for (const el of playlistEl.querySelectorAll("[data-delete-picture-id]")) {
      el.addEventListener("click", async (event) => {
        event.preventDefault();
        const pictureId = el.getAttribute("data-delete-picture-id") || "";
        if (!pictureId) return;
        await deletePicture(pictureId, el);
      });
    }
  }

  function renderPlaylist(playlist) {
    if (playlistSummaryEl) {
      playlistSummaryEl.textContent = playlist.empty
        ? `Engar myndir`
        : pluralizePictures(playlist.images.length);
    }

    if (!playlistEl) return;

    if (playlist.empty || !Array.isArray(playlist.images) || playlist.images.length === 0) {
      playlistEl.innerHTML = `<div class="frame-empty">Engar myndir</div>`;
      return;
    }

    playlistEl.innerHTML = playlist.images
      .map((image) => {
        const isCurrent = image.picture_id === playlist.current_picture_id;
        const url = `${API}/pictures/${encodeURIComponent(image.picture_id)}/file?t=${image.uploaded_at || Date.now()}`;
        return `
          <div class="playlist-item">
            <img class="playlist-thumb" src="${url}" alt="${escapeHtml(image.filename)}">
            <div class="playlist-meta">
              <div class="playlist-name">${escapeHtml(image.filename)}</div>
              <div class="playlist-actions">
                <button type="button" class="btn btn-sm btn-outline-danger" data-delete-picture-id="${escapeHtml(image.picture_id)}">Eyða</button>
              </div>
            </div>
          </div>
        `;
      })
      .join("");

    bindPlaylistActions();
  }

  async function refreshPartnerPictures() {
    const refreshToken = ++playlistRefreshToken;
    try {
      const playlist = await reqJson("GET", "pictures");
      if (refreshToken !== playlistRefreshToken) return;
      renderPlaylist(playlist);
      await refreshCurrentPicture();
    } catch (err) {
      log(`ERR GET pictures -> ${err}`);
      const hasCurrentPicture = await refreshCurrentPicture();
      const playlistUnsupported = String(err).includes("404");

      if (playlistSummaryEl) {
        playlistSummaryEl.textContent = playlistUnsupported ? "Eldri útgáfa" : "Villa";
      }
      if (playlistEl) {
        playlistEl.innerHTML = playlistUnsupported
          ? `<div class="frame-empty">Error 67</div>`
          : `<div class="frame-empty">Ekki tókst að sækja myndir</div>`;
      }
      if (!hasCurrentPicture && frameEmptyEl) {
        frameEmptyEl.textContent = "Ekki tókst að sækja mynd";
      }
    }
  }

  async function refreshSlideshow() {
    try {
      const response = await reqJson("GET", "slideshow");
      const intervalSeconds = Number(response.interval_seconds);
      if (intervalInput) intervalInput.value = String(intervalSeconds);
      if (intervalCurrentValueEl) intervalCurrentValueEl.textContent = formatInterval(intervalSeconds);
      setStatus(intervalStatusEl, "", "muted");
      log(`GET slideshow -> ${JSON.stringify(response)}`);
    } catch (err) {
      log(`ERR GET slideshow -> ${err}`);
      setStatus(intervalStatusEl, "Ekki tókst að sækja skiptitíma", "error");
    }
  }

  async function sendReaction(message) {
    const trimmed = (message || "").trim();
    if (!trimmed) {
      log("ERR POST reaction -> empty message");
      return;
    }
    try {
      const response = await reqJson("POST", "reaction", { message: trimmed });
      log(`POST reaction -> ${JSON.stringify(response)}`);
    } catch (err) {
      log(`ERR POST reaction -> ${err}`);
    }
  }

  if (fileInput && uploadBtn && clearBtn) {
    syncSelectedFromInput();
    fileInput.addEventListener("change", syncSelectedFromInput);
    fileInput.addEventListener("input", syncSelectedFromInput);

    clearBtn.addEventListener("click", (event) => {
      event.preventDefault();
      fileInput.value = "";
      showSelectionPreview(null);
      setStatus(uploadStatusEl, "", "muted");
      log("Cleared selected picture");
    });

    uploadBtn.addEventListener("click", async (event) => {
      event.preventDefault();

      const file = fileInput.files && fileInput.files.length > 0 ? fileInput.files[0] : null;
      if (!file) {
        setStatus(uploadStatusEl, "Veldu mynd", "error");
        log("ERR POST picture -> no file selected");
        return;
      }

      setBtnEnabled(uploadBtn, false);
      const formData = new FormData();
      formData.append("file", file, file.name);

      try {
        const response = await fetch(`${API}/picture`, { method: "POST", body: formData });
        const text = await response.text();
        if (!response.ok) throw new Error(`${response.status} ${text}`);
        const json = text ? JSON.parse(text) : {};
        log(`POST picture -> ${JSON.stringify(json)}`);
        setStatus(uploadStatusEl, "Mynd send", "success");
        fileInput.value = "";
        showSelectionPreview(null);
        await refreshPartnerPictures();
      } catch (err) {
        log(`ERR POST picture -> ${err}`);
        setStatus(uploadStatusEl, "Ekki tókst að senda mynd", "error");
      } finally {
        setBtnEnabled(uploadBtn, !!(fileInput.files && fileInput.files.length > 0));
      }
    });
  } else {
    log("Picture UI elements missing");
  }

  if (intervalInput && intervalSaveBtn) {
    intervalSaveBtn.addEventListener("click", async (event) => {
      event.preventDefault();

      const intervalSeconds = Number(intervalInput.value);
      if (!Number.isInteger(intervalSeconds) || intervalSeconds < 5 || intervalSeconds > 3600) {
        setStatus(intervalStatusEl, "Veldu heila tölu á bilinu 5 til 3600 sekúndur.", "error");
        return;
      }

      const wasDisabled = intervalSaveBtn.disabled;
      intervalSaveBtn.disabled = true;
      try {
        const response = await reqJson("PUT", "slideshow", { interval_seconds: intervalSeconds });
        log(`PUT slideshow -> ${JSON.stringify(response)}`);
        if (intervalCurrentValueEl) intervalCurrentValueEl.textContent = formatInterval(intervalSeconds);
        setStatus(intervalStatusEl, "Skiptitími vistaður", "success");
      } catch (err) {
        log(`ERR PUT slideshow -> ${err}`);
        setStatus(intervalStatusEl, "Ekki tókst að vista skiptitíma", "error");
      } finally {
        intervalSaveBtn.disabled = wasDisabled;
      }
    });
  }

  for (const el of document.querySelectorAll("[data-reaction]")) {
    el.addEventListener("click", async (event) => {
      event.preventDefault();
      const emoji = el.getAttribute("data-reaction") || el.textContent || "";
      await sendReaction(emoji);
    });
  }

  if (reactionInput && reactionSendBtn) {
    const syncReactionBtn = () => {
      setBtnEnabled(reactionSendBtn, reactionInput.value.trim().length > 0);
    };
    syncReactionBtn();

    reactionInput.addEventListener("input", syncReactionBtn);

    reactionSendBtn.addEventListener("click", async (event) => {
      event.preventDefault();
      const msg = reactionInput.value;
      await sendReaction(msg);
      reactionInput.value = "";
      syncReactionBtn();
    });
  }

  for (const el of document.querySelectorAll("button[data-action]")) {
    el.addEventListener("click", async (event) => {
      event.preventDefault();

      const action = el.getAttribute("data-action") || "";
      const method = (el.getAttribute("data-method") || "POST").toUpperCase();
      const wasDisabled = el.disabled;
      el.disabled = true;

      try {
        const response = await reqJson(method, action, method === "POST" ? {} : undefined);
        log(`${method} ${action} -> ${JSON.stringify(response)}`);
      } catch (err) {
        log(`ERR ${method} ${action} -> ${err}`);
      } finally {
        el.disabled = wasDisabled;
      }
    });
  }

  refreshPartnerPictures();
  refreshSlideshow();

  if (playlistPollHandle) window.clearInterval(playlistPollHandle);
  playlistPollHandle = window.setInterval(() => {
    refreshPartnerPictures();
  }, 3000);
})();
