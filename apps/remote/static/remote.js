(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const DEBUG = params.get("debug") === "true";

  // ---- Identity / API ----
  /** @type {string} */
  const SELF = (document.body && document.body.dataset && document.body.dataset.self) ? document.body.dataset.self : "";
  const API = `/api/${encodeURIComponent(SELF)}`;

  /** @type {HTMLPreElement | null} */
  const logEl = /** @type {HTMLPreElement | null} */ (document.getElementById("log"));
  if (logEl) logEl.hidden = !DEBUG;

  function log(msg) {
    if (!DEBUG || !logEl) return;
    const ts = new Date().toLocaleTimeString();
    logEl.textContent = `[${ts}] ${msg}\n` + logEl.textContent;
  }

  async function reqJson(method, path, bodyObj) {
    const init = /** @type {RequestInit} */ ({
      method,
      headers: bodyObj ? { "Content-Type": "application/json" } : undefined,
      body: bodyObj ? JSON.stringify(bodyObj) : undefined,
    });

    const r = await fetch(`${API}/${path}`, init);
    const txt = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${txt}`);
    return txt ? JSON.parse(txt) : {};
  }

  // -------------------------
  // Picture upload UI
  // -------------------------
  /** @type {HTMLInputElement | null} */
  const fileInput = /** @type {HTMLInputElement | null} */ (document.getElementById("pictureFile"));
  /** @type {HTMLButtonElement | null} */
  const uploadBtn = /** @type {HTMLButtonElement | null} */ (document.getElementById("pictureUploadBtn"));
  /** @type {HTMLButtonElement | null} */
  const clearBtn = /** @type {HTMLButtonElement | null} */ (document.getElementById("pictureClearBtn"));
  /** @type {HTMLImageElement | null} */
  const previewEl = /** @type {HTMLImageElement | null} */ (document.getElementById("picturePreview"));

  /** @type {string | null} */
  let previewUrl = null;

  function setBtnEnabled(btn, enabled) {
    if (!btn) return;
    btn.disabled = !enabled;
    // also toggle Bootstrap's "disabled" class if you used it in HTML
    btn.classList.toggle("disabled", !enabled);
    if (enabled) btn.removeAttribute("aria-disabled");
    else btn.setAttribute("aria-disabled", "true");
  }

  function setSelected(file) {
    const hasFile = !!file;

    setBtnEnabled(uploadBtn, hasFile);
    setBtnEnabled(clearBtn, hasFile);

    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;

    if (!previewEl) return;

    if (hasFile) {
      previewUrl = URL.createObjectURL(file);
      previewEl.src = previewUrl;
      previewEl.classList.remove("d-none");
    } else {
      previewEl.removeAttribute("src");
      previewEl.classList.add("d-none");
    }
  }

  function syncSelectedFromInput() {
    if (!fileInput) return;
    const f = (fileInput.files && fileInput.files.length > 0) ? fileInput.files[0] : null;
    setSelected(f);
  }

  if (fileInput && uploadBtn && clearBtn && previewEl) {
    // initialize
    syncSelectedFromInput();

    // some browsers fire 'input', some only 'change' — listen to both
    fileInput.addEventListener("change", syncSelectedFromInput);
    fileInput.addEventListener("input", syncSelectedFromInput);

    clearBtn.addEventListener("click", (e) => {
      e.preventDefault();
      fileInput.value = "";
      setSelected(null);
      log("Cleared selected picture");
    });

    uploadBtn.addEventListener("click", async (e) => {
      e.preventDefault();

      const f = (fileInput.files && fileInput.files.length > 0) ? fileInput.files[0] : null;
      if (!f) {
        log("ERR POST picture -> no file selected");
        return;
      }

      const wasDisabled = uploadBtn.disabled;
      setBtnEnabled(uploadBtn, false);

      const fd = new FormData();
      fd.append("file", f, f.name);

      try {
        const r = await fetch(`${API}/picture`, { method: "POST", body: fd });
        const txt = await r.text();
        if (!r.ok) throw new Error(`${r.status} ${txt}`);
        const j = txt ? JSON.parse(txt) : {};
        log(`POST picture -> ${JSON.stringify(j)}`);
        refreshFrameImage();
      } catch (err) {
        log(`ERR POST picture -> ${err}`);
      } finally {
        // restore based on whether a file is still selected
        if (!wasDisabled) syncSelectedFromInput();
      }
    });
  } else {
    log("Picture UI elements missing");
  }

  /** @type {HTMLImageElement | null} */
  const frameImg = /** @type {HTMLImageElement | null} */ (document.getElementById("frameCurrentImg"));

  function refreshFrameImage() {
    if (!frameImg) return;

    // Always fetch latest bytes (bust browser cache)
    const url = `${API}/picture?t=${Date.now()}`;
    frameImg.src = url;
  }

  if (frameImg) {
    refreshFrameImage();
    // poll every 2–5s (pick what you like)
    setInterval(refreshFrameImage, 3000);
  }

  // -------------------------
  // Reactions
  // -------------------------
  /** @type {HTMLInputElement | null} */
  const reactionInput = /** @type {HTMLInputElement | null} */ (document.getElementById("reactionInput"));
  /** @type {HTMLButtonElement | null} */
  const reactionSendBtn = /** @type {HTMLButtonElement | null} */ (document.getElementById("reactionSendBtn"));

  async function sendReaction(message) {
    const m = (message || "").trim();
    if (!m) {
      log(`ERR POST reaction -> empty message`);
      return;
    }
    try {
      const j = await reqJson("POST", "reaction", { message: m });
      log(`POST reaction -> ${JSON.stringify(j)}`);
    } catch (e) {
      log(`ERR POST reaction -> ${e}`);
    }
  }

  // emoji buttons: <button data-reaction="❤️" ...>
  for (const el of document.querySelectorAll("[data-reaction]")) {
    /** @type {HTMLElement} */ const btnEl = /** @type {HTMLElement} */ (el);
    btnEl.addEventListener("click", async (e) => {
      e.preventDefault();
      const emoji = btnEl.getAttribute("data-reaction") || btnEl.textContent || "";
      await sendReaction(emoji);
    });
  }

  if (reactionInput && reactionSendBtn) {
    const syncReactionBtn = () => {
      setBtnEnabled(reactionSendBtn, reactionInput.value.trim().length > 0);
    };
    syncReactionBtn();

    reactionInput.addEventListener("input", syncReactionBtn);

    reactionSendBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      const msg = reactionInput.value;
      await sendReaction(msg);
      reactionInput.value = "";
      syncReactionBtn();
    });
  }

  // -------------------------
  // Generic action buttons
  // -------------------------
  for (const el of document.querySelectorAll("button[data-action]")) {
    /** @type {HTMLButtonElement} */
    const btn = /** @type {HTMLButtonElement} */ (el);

    btn.addEventListener("click", async (e) => {
      e.preventDefault();

      const action = btn.getAttribute("data-action") || "";
      const method = (btn.getAttribute("data-method") || "POST").toUpperCase();

      // don't let generic handler break special endpoints
      if (action === "reaction" || action === "picture") return;

      const wasDisabled = btn.disabled;
      btn.disabled = true;

      try {
        const j = await reqJson(method, action, method === "POST" ? {} : undefined);
        log(`${method} ${action} -> ${JSON.stringify(j)}`);
      } catch (err) {
        log(`ERR ${method} ${action} -> ${err}`);
      } finally {
        btn.disabled = wasDisabled;
      }
    });
  }
})();
