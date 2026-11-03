// Live preview for the announcement editor. The server renders the Markdown
// (raw HTML disabled), so the preview always matches what is published.
(() => {
  const form = document.querySelector("form[data-preview-url]");
  const panel = document.querySelector("[data-preview-panel]");
  const imageInput = document.querySelector("[data-image-input]");
  const imagePreview = document.querySelector("[data-image-preview]");
  const previewImage = document.querySelector("[data-image-preview-image]");
  const imageCaption = document.querySelector("[data-image-caption]");
  const imageAlt = document.getElementById("image-alt");
  const removeImage = document.querySelector("[data-remove-image]");
  let selectedImageUrl = null;

  function refreshImagePreview() {
    if (!imageInput || !imagePreview || !previewImage) return;
    if (selectedImageUrl) URL.revokeObjectURL(selectedImageUrl);
    selectedImageUrl = null;
    const file = imageInput.files?.[0];
    if (file) {
      selectedImageUrl = URL.createObjectURL(file);
      previewImage.src = selectedImageUrl;
      imagePreview.hidden = false;
    } else if (removeImage?.checked) {
      imagePreview.hidden = true;
    } else if (imagePreview.dataset.originalSrc) {
      previewImage.src = imagePreview.dataset.originalSrc;
      imagePreview.hidden = false;
    } else {
      imagePreview.hidden = true;
    }
    previewImage.alt = imageAlt?.value || "";
    if (imageCaption) imageCaption.textContent = imageAlt?.value || "";
  }

  imageInput?.addEventListener("change", refreshImagePreview);
  imageAlt?.addEventListener("input", refreshImagePreview);
  removeImage?.addEventListener("change", refreshImagePreview);

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
      const formData = new FormData(form);
      formData.delete("image");
      formData.delete("image_alt");
      formData.delete("remove_image");
      const response = await fetch(form.dataset.previewUrl, {
        method: "POST",
        body: formData,
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
