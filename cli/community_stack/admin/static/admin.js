// Progressive enhancement for the admin console. Links and forms load in the
// background and replace the page, so an action keeps the page where it was
// instead of reloading at the top. Without JavaScript every link and form
// works as plain HTML; with it, nothing is cached in the browser.
(() => {
  document.documentElement.classList.add("js");

  const supported = Boolean(window.fetch && window.DOMParser && window.history.pushState);
  const parser = supported ? new DOMParser() : null;
  const HISTORY_STATE = { console: true };
  let controller = null;
  let submitting = false;

  // Only pages behind the login; the login pages always use full page loads.
  const enhanced = () => supported && document.body.hasAttribute("data-console");

  function consoleUrl(url) {
    return (
      url.origin === location.origin &&
      url.pathname.startsWith("/admin") &&
      !url.pathname.startsWith("/admin/static/")
    );
  }

  // Messages ----------------------------------------------------------------

  function makeToast(text, tone) {
    const toast = document.createElement("div");
    toast.className = `toast toast-${tone}`;
    toast.setAttribute("role", tone === "error" ? "alert" : "status");
    toast.dataset.toast = "";
    const message = document.createElement("p");
    message.className = "toast-text";
    message.textContent = text;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.dataset.toastClose = "";
    close.setAttribute("aria-label", "Dismiss message");
    close.textContent = "×";
    toast.append(message, close);
    return toast;
  }

  function showToast(text, tone) {
    let region = document.querySelector(".toast-region");
    if (!region) {
      region = document.createElement("div");
      region.className = "toast-region";
      document.querySelector("main")?.prepend(region);
    }
    region.replaceChildren(makeToast(text, tone));
  }

  // Successful results fade after a while; errors and passwords stay until dismissed.
  function startToastTimers() {
    for (const toast of document.querySelectorAll('[data-toast="auto"]')) {
      const timer = setTimeout(() => toast.remove(), 8000);
      const keep = () => clearTimeout(timer);
      toast.addEventListener("pointerenter", keep, { once: true });
      toast.addEventListener("focusin", keep, { once: true });
    }
  }

  // Confirmation --------------------------------------------------------------

  let dialog = null;

  function confirmDialog() {
    if (dialog?.isConnected) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    dialog.setAttribute("aria-labelledby", "confirm-dialog-title");
    dialog.innerHTML = `
      <form method="dialog">
        <h2 id="confirm-dialog-title">Are you sure?</h2>
        <p data-confirm-message></p>
        <div class="actions">
          <button value="cancel" class="secondary">Cancel</button>
          <button value="confirm" class="danger" data-confirm-ok></button>
        </div>
      </form>`;
    document.body.append(dialog);
    return dialog;
  }

  function askToConfirm(form, submitter) {
    const message = form.dataset.confirm;
    const label = form.dataset.confirmLabel || submitter?.textContent.trim() || "Confirm";
    const proceed = () => {
      form.dataset.confirmed = "true";
      form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
    };
    if (!window.HTMLDialogElement) {
      if (window.confirm(message)) proceed();
      return;
    }
    const box = confirmDialog();
    box.querySelector("[data-confirm-message]").textContent = message;
    box.querySelector("[data-confirm-ok]").textContent = label;
    box.returnValue = "";
    box.addEventListener(
      "close",
      () => {
        if (box.returnValue === "confirm") proceed();
        else submitter?.focus();
      },
      { once: true },
    );
    box.showModal();
    box.querySelector("[value=cancel]").focus();
  }

  // Page loading --------------------------------------------------------------

  // Identifies a form across page loads, so focus can return to the same row's button.
  function formKey(form) {
    const hidden = [...form.querySelectorAll("input[type=hidden]")]
      .filter((input) => input.name !== "csrf_token")
      .map((input) => `${input.name}=${input.value}`);
    return `${form.getAttribute("action")}|${hidden.join("&")}`;
  }

  function rememberFocus() {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    if (active.id) return { id: active.id };
    if (active.form) return { form: formKey(active.form) };
    return null;
  }

  function restoreFocus(memory) {
    if (!memory) return;
    let target = memory.id ? document.getElementById(memory.id) : null;
    if (!target && memory.form) {
      const form = [...document.forms].find((candidate) => formKey(candidate) === memory.form);
      target = form?.querySelector("button[type=submit], button:not([type])");
    }
    target?.focus({ preventScroll: true });
  }

  function swapBody(body) {
    const fresh = document.adoptNode(body);
    // Scripts parsed from a response never run; recreate them so page scripts start.
    for (const old of fresh.querySelectorAll("script")) {
      const script = document.createElement("script");
      for (const { name, value } of old.attributes) script.setAttribute(name, value);
      script.textContent = old.textContent;
      old.replaceWith(script);
    }
    document.body.replaceWith(fresh);
  }

  function setBusy(submitter, busy) {
    document.documentElement.classList.toggle("is-loading", busy);
    if (!busy) submitting = false;
    if (submitter) {
      if (busy) submitter.setAttribute("aria-busy", "true");
      else submitter.removeAttribute("aria-busy");
    }
  }

  async function load(url, init, { form = null, submitter = null, hash = "" } = {}) {
    controller?.abort();
    controller = new AbortController();
    const method = init.method || "GET";
    const scrollY = window.scrollY;
    const focus = rememberFocus();
    const currentPage = document.body.dataset.page;
    setBusy(submitter, true);
    let response;
    let text;
    try {
      response = await fetch(url, { ...init, credentials: "same-origin", signal: controller.signal });
      text = await response.text();
    } catch (error) {
      setBusy(submitter, false);
      if (error.name === "AbortError") return;
      // Network trouble: let the browser try the plain way, which shows its own error.
      if (form) {
        form.dataset.native = "true";
        form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
      } else {
        location.assign(url);
      }
      return;
    }
    const type = response.headers.get("content-type") || "";
    if (!type.includes("text/html")) {
      setBusy(submitter, false);
      location.assign(response.url);
      return;
    }
    const doc = parser.parseFromString(text, "text/html");
    // A refused action keeps the page as it is, typed values included, and says why.
    if (method !== "GET" && doc.body.dataset.page === "error.html") {
      setBusy(submitter, false);
      const reason = doc.querySelector("[data-error-message]")?.textContent.trim();
      showToast(reason || "That did not work.", "error");
      return;
    }

    const finalUrl = new URL(response.url);
    // Redirects land on a new address; a page rendered straight from a form keeps the
    // current one, so reloading it never sends the form again.
    const navigated = method === "GET" || response.redirected;
    const samePath = navigated ? finalUrl.pathname === location.pathname : true;
    const samePage = samePath && doc.body.dataset.page === currentPage;
    if (navigated) {
      const target = finalUrl.pathname + finalUrl.search + hash;
      if (target === location.pathname + location.search + location.hash) {
        history.replaceState(HISTORY_STATE, "", target);
      } else {
        history.pushState(HISTORY_STATE, "", target);
      }
    }
    document.title = doc.title;
    swapBody(doc.body);
    setBusy(null, false);

    if (samePage) {
      window.scrollTo(0, scrollY);
      restoreFocus(focus);
    } else {
      const anchor = hash && document.getElementById(decodeURIComponent(hash.slice(1)));
      if (anchor) anchor.scrollIntoView();
      else window.scrollTo(0, 0);
      const heading = document.querySelector("main h1");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: true });
      }
    }
    pageReady();
  }

  function formData(form, submitter) {
    try {
      return new FormData(form, submitter);
    } catch {
      const data = new FormData(form);
      if (submitter?.name) data.append(submitter.name, submitter.value);
      return data;
    }
  }

  // Wiring --------------------------------------------------------------------

  function pageReady() {
    startToastTimers();
    for (const button of document.querySelectorAll("[data-history-back]")) button.hidden = false;
    // Content the last action produced, such as recovery codes, is brought into view.
    const reveal = document.querySelector("[data-reveal]");
    if (reveal) {
      const box = reveal.getBoundingClientRect();
      if (box.top < 0 || box.bottom > window.innerHeight) reveal.scrollIntoView({ block: "center" });
    }
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-toast-close]")) {
      event.target.closest("[data-toast]")?.remove();
      return;
    }
    if (event.target.closest("[data-history-back]")) {
      history.back();
      return;
    }
    const toggle = event.target.closest("[data-nav-toggle]");
    if (toggle) {
      const open = toggle.closest(".sidebar").classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", String(open));
      return;
    }
    if (!enhanced() || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest("a[href]");
    if (!link || link.target || link.hasAttribute("download") || link.hasAttribute("data-reload")) {
      return;
    }
    const url = new URL(link.href);
    if (!consoleUrl(url)) return;
    if (url.hash && url.pathname === location.pathname && url.search === location.search) return;
    event.preventDefault();
    const hash = url.hash;
    url.hash = "";
    load(url, { method: "GET" }, { hash });
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    const submitter = event.submitter;
    // Read attributes, not properties: a field named "action" or "method" shadows them.
    const method = (
      submitter?.getAttribute("formmethod") ||
      form.getAttribute("method") ||
      "get"
    ).toUpperCase();
    if (method === "DIALOG") return;
    if (form.hasAttribute("data-confirm")) {
      if (form.dataset.confirmed === "true") {
        delete form.dataset.confirmed;
      } else {
        event.preventDefault();
        askToConfirm(form, submitter);
        return;
      }
    }
    if (form.dataset.native) {
      delete form.dataset.native;
      return;
    }
    if (!enhanced() || form.getAttribute("target") || form.hasAttribute("data-reload")) return;
    const action = new URL(
      submitter?.getAttribute("formaction") || form.getAttribute("action") || location.href,
      location.href,
    );
    if (!consoleUrl(action)) return;
    event.preventDefault();
    const data = formData(form, submitter);
    if (method === "GET") {
      action.search = new URLSearchParams(data).toString();
      load(action, { method: "GET" }, { form, submitter });
      return;
    }
    if (submitting) return; // One action at a time: a second click must not repeat it.
    submitting = true;
    load(action, { method: "POST", body: data }, { form, submitter });
  });

  if (supported) {
    history.replaceState(HISTORY_STATE, "", location.href);
    // Pages are never cached in the browser, so going back or forward loads them fresh.
    window.addEventListener("popstate", (event) => {
      if (event.state?.console) location.reload();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", pageReady);
  else pageReady();
})();
