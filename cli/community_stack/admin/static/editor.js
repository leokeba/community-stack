// Live preview for the announcement editor. The server renders the Markdown
// (raw HTML disabled), so the preview always matches what is published.
(() => {
  const form = document.querySelector("form[data-preview-url]");
  const panel = document.querySelector("[data-preview-panel]");
  if (!form || !panel || !window.fetch) return;
  const article = document.getElementById("live-preview");
  const channels = document.getElementById("channel-previews");
  let timer = null;

  function channelCard(channel) {
    const card = document.createElement("div");
    card.className = "panel";
    const heading = document.createElement("strong");
    heading.textContent = channel.label;
    const count = document.createElement("p");
    count.className = channel.over_limit && !channel.shortened ? "error" : "muted";
    count.textContent = channel.limit
      ? `${channel.length} of ${channel.limit} characters` +
        (channel.shortened ? ", shortened with a link to the full announcement" : "")
      : "No length limit";
    const text = document.createElement("pre");
    text.style.whiteSpace = "pre-wrap";
    text.textContent = channel.preview;
    card.append(heading, count, text);
    return card;
  }

  async function refresh() {
    try {
      const response = await fetch(form.dataset.previewUrl, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const data = await response.json();
      article.innerHTML = data.html;
      channels.replaceChildren(...data.channels.map(channelCard));
      panel.hidden = false;
    } catch (error) {
      // The saved draft's page shows the same previews; the editor keeps working.
    }
  }

  form.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(refresh, 400);
  });
  refresh();
})();
