(function () {
  const SELF = window.__SELF__;
  const API = `/api/${SELF}`;

  const logEl = document.getElementById("log");
  function log(msg) {
    const ts = new Date().toLocaleTimeString();
    logEl.textContent = `[${ts}] ${msg}\n` + logEl.textContent;
  }

  async function req(method, path) {
    const r = await fetch(`${API}/${path}`, {
      method,
      headers: method === "POST" ? { "Content-Type": "application/json" } : undefined,
      body: method === "POST" ? "{}" : undefined,
    });
    const txt = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${txt}`);
    return txt ? JSON.parse(txt) : {};
  }

  for (const btn of document.querySelectorAll("button[data-action]")) {
    btn.addEventListener("click", async () => {
      const action = btn.getAttribute("data-action");
      const method = (btn.getAttribute("data-method") || "POST").toUpperCase();
      try {
        btn.disabled = true;
        const j = await req(method, action);
        log(`${method} ${action} -> ${JSON.stringify(j)}`);
      } catch (e) {
        log(`ERR ${method} ${action} -> ${e}`);
      } finally {
        btn.disabled = false;
      }
    });
  }
})();
