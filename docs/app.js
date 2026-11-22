/* 10KTF Kagami Circular Photo Cropper — 100% client-side, zero dependencies */
(() => {
  const OUTPUT_SIZE = 1080;
  const JPEG_QUALITY = 0.92;

  const canvas = document.getElementById("previewCanvas");
  const ctx = canvas.getContext("2d");
  const dropZone = document.getElementById("dropZone");
  const emptyPrompt = document.getElementById("emptyPrompt");
  const safeGuide = document.getElementById("safeGuide");
  const fileInput = document.getElementById("fileInput");
  const zoomSlider = document.getElementById("zoomSlider");
  const zoomVal = document.getElementById("zoomVal");
  const toggleGuide = document.getElementById("toggleGuide");
  const btnAdd = document.getElementById("btnAdd");
  const btnReset = document.getElementById("btnReset");
  const btnDownloadOne = document.getElementById("btnDownloadOne");
  const btnDownloadAll = document.getElementById("btnDownloadAll");
  const btnClear = document.getElementById("btnClear");
  const thumbList = document.getElementById("thumbList");
  const photoCount = document.getElementById("photoCount");
  const btnCopyFfmpeg = document.getElementById("btnCopyFfmpeg");
  const ffmpegCmd = document.getElementById("ffmpegCmd");

  /** @type {Array<{id:number, name:string, img:HTMLImageElement, url:string, zoom:number, offX:number, offY:number}>} */
  let photos = [];
  let activeIdx = -1;
  let nextId = 1;

  let dragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragOrigX = 0;
  let dragOrigY = 0;

  function clampOffsets(item, size) {
    const baseScale = Math.max(size / item.img.naturalWidth, size / item.img.naturalHeight);
    const drawW = item.img.naturalWidth * baseScale * item.zoom;
    const drawH = item.img.naturalHeight * baseScale * item.zoom;
    const maxX = Math.max(0, (drawW - size) / 2);
    const maxY = Math.max(0, (drawH - size) / 2);
    item.offX = Math.max(-maxX, Math.min(maxX, item.offX));
    item.offY = Math.max(-maxY, Math.min(maxY, item.offY));
  }

  function renderToContext(targetCtx, item, size) {
    targetCtx.fillStyle = "#000";
    targetCtx.fillRect(0, 0, size, size);
    if (!item) return;

    const scaleRatio = size / canvas.width;
    const baseScale = Math.max(size / item.img.naturalWidth, size / item.img.naturalHeight);
    const drawW = item.img.naturalWidth * baseScale * item.zoom;
    const drawH = item.img.naturalHeight * baseScale * item.zoom;
    const x = (size - drawW) / 2 + item.offX * scaleRatio;
    const y = (size - drawH) / 2 + item.offY * scaleRatio;

    targetCtx.imageSmoothingEnabled = true;
    targetCtx.imageSmoothingQuality = "high";
    targetCtx.drawImage(item.img, x, y, drawW, drawH);
  }

  function renderPreview() {
    const item = photos[activeIdx] || null;
    if (item) clampOffsets(item, canvas.width);
    renderToContext(ctx, item, canvas.width);

    emptyPrompt.classList.toggle("hidden", Boolean(item));
    const hasPhotos = photos.length > 0;
    zoomSlider.disabled = !item;
    btnReset.disabled = !item;
    btnDownloadOne.disabled = !item;
    btnDownloadAll.disabled = !hasPhotos;
    btnClear.disabled = !hasPhotos;

    if (item) {
      zoomSlider.value = item.zoom.toFixed(2);
      zoomVal.textContent = `${item.zoom.toFixed(2)}×`;
    } else {
      zoomSlider.value = "1";
      zoomVal.textContent = "1.00×";
    }
  }

  function renderThumbs() {
    photoCount.textContent = String(photos.length);
    if (photos.length === 0) {
      thumbList.innerHTML = '<p class="muted">No photos loaded yet.</p>';
      return;
    }
    thumbList.innerHTML = "";
    photos.forEach((item, idx) => {
      const div = document.createElement("div");
      div.className = "thumb-item" + (idx === activeIdx ? " active" : "");
      div.title = item.name;
      const img = document.createElement("img");
      img.src = item.url;
      div.appendChild(img);

      const rm = document.createElement("button");
      rm.className = "thumb-remove";
      rm.textContent = "×";
      rm.title = "Remove";
      rm.addEventListener("click", (e) => {
        e.stopPropagation();
        URL.revokeObjectURL(item.url);
        photos.splice(idx, 1);
        if (activeIdx >= photos.length) activeIdx = photos.length - 1;
        renderThumbs();
        renderPreview();
      });
      div.appendChild(rm);

      div.addEventListener("click", () => {
        activeIdx = idx;
        renderThumbs();
        renderPreview();
      });
      thumbList.appendChild(div);
    });
  }

  function addFiles(fileList) {
    const incoming = Array.from(fileList).filter((f) => f.type.startsWith("image/"));
    if (!incoming.length) return;

    incoming.forEach((file) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        const stem = file.name.replace(/\.[^.]+$/, "");
        photos.push({
          id: nextId++,
          name: `${stem}_1080.jpg`,
          img,
          url,
          zoom: 1,
          offX: 0,
          offY: 0,
        });
        if (activeIdx === -1) activeIdx = 0;
        renderThumbs();
        renderPreview();
      };
      img.src = url;
    });
  }

  function exportItemBlob(item) {
    return new Promise((resolve) => {
      const outCanvas = document.createElement("canvas");
      outCanvas.width = OUTPUT_SIZE;
      outCanvas.height = OUTPUT_SIZE;
      const outCtx = outCanvas.getContext("2d");
      renderToContext(outCtx, item, OUTPUT_SIZE);
      outCanvas.toBlob((blob) => resolve(blob), "image/jpeg", JPEG_QUALITY);
    });
  }

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // Minimal zero-dependency STORE ZIP builder (JPEGs are already compressed)
  const crcTable = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[i] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) c = crcTable[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  function buildZip(entries) {
    const enc = new TextEncoder();
    const parts = [];
    const central = [];
    let offset = 0;

    for (const { name, data } of entries) {
      const nameBytes = enc.encode(name);
      const crc = crc32(data);

      const lh = new ArrayBuffer(30);
      const lv = new DataView(lh);
      lv.setUint32(0, 0x04034b50, true);
      lv.setUint16(4, 20, true);
      lv.setUint16(8, 0, true);
      lv.setUint32(14, crc, true);
      lv.setUint32(18, data.length, true);
      lv.setUint32(22, data.length, true);
      lv.setUint16(26, nameBytes.length, true);

      parts.push(new Uint8Array(lh), nameBytes, data);

      const ch = new ArrayBuffer(46);
      const cv = new DataView(ch);
      cv.setUint32(0, 0x02014b50, true);
      cv.setUint16(4, 20, true);
      cv.setUint16(6, 20, true);
      cv.setUint32(16, crc, true);
      cv.setUint32(20, data.length, true);
      cv.setUint32(24, data.length, true);
      cv.setUint16(28, nameBytes.length, true);
      cv.setUint32(42, offset, true);

      central.push(new Uint8Array(ch), nameBytes);
      offset += 30 + nameBytes.length + data.length;
    }

    const centralSize = central.reduce((s, a) => s + a.length, 0);
    const eocd = new ArrayBuffer(22);
    const ev = new DataView(eocd);
    ev.setUint32(0, 0x06054b50, true);
    ev.setUint16(8, entries.length, true);
    ev.setUint16(10, entries.length, true);
    ev.setUint32(12, centralSize, true);
    ev.setUint32(16, offset, true);

    return new Blob([...parts, ...central, new Uint8Array(eocd)], { type: "application/zip" });
  }

  // Events
  btnAdd.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) =>
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropZone.classList.add("drag-over");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
      if (ev === "drop" && e.dataTransfer?.files) addFiles(e.dataTransfer.files);
    })
  );

  zoomSlider.addEventListener("input", () => {
    const item = photos[activeIdx];
    if (!item) return;
    item.zoom = parseFloat(zoomSlider.value);
    renderPreview();
  });

  canvas.addEventListener("wheel", (e) => {
    const item = photos[activeIdx];
    if (!item) return;
    e.preventDefault();
    const delta = e.deltaY < 0 ? 0.08 : -0.08;
    item.zoom = Math.max(1, Math.min(3.5, item.zoom + delta));
    renderPreview();
  }, { passive: false });

  canvas.addEventListener("pointerdown", (e) => {
    const item = photos[activeIdx];
    if (!item) return;
    dragging = true;
    canvas.setPointerCapture(e.pointerId);
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragOrigX = item.offX;
    dragOrigY = item.offY;
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const item = photos[activeIdx];
    if (!item) return;
    const rect = canvas.getBoundingClientRect();
    const ratio = canvas.width / rect.width;
    item.offX = dragOrigX + (e.clientX - dragStartX) * ratio;
    item.offY = dragOrigY + (e.clientY - dragStartY) * ratio;
    renderPreview();
  });

  ["pointerup", "pointercancel"].forEach((ev) =>
    canvas.addEventListener(ev, () => { dragging = false; })
  );

  toggleGuide.addEventListener("change", () => {
    safeGuide.classList.toggle("hidden", !toggleGuide.checked);
  });

  btnReset.addEventListener("click", () => {
    const item = photos[activeIdx];
    if (!item) return;
    item.zoom = 1;
    item.offX = 0;
    item.offY = 0;
    renderPreview();
  });

  btnClear.addEventListener("click", () => {
    photos.forEach((p) => URL.revokeObjectURL(p.url));
    photos = [];
    activeIdx = -1;
    renderThumbs();
    renderPreview();
  });

  btnDownloadOne.addEventListener("click", async () => {
    const item = photos[activeIdx];
    if (!item) return;
    const blob = await exportItemBlob(item);
    triggerDownload(blob, item.name);
  });

  btnDownloadAll.addEventListener("click", async () => {
    if (!photos.length) return;
    btnDownloadAll.disabled = true;
    btnDownloadAll.textContent = "Packaging ZIP...";
    try {
      const entries = [];
      const seen = new Map();
      for (const item of photos) {
        const blob = await exportItemBlob(item);
        const data = new Uint8Array(await blob.arrayBuffer());
        const count = seen.get(item.name) || 0;
        seen.set(item.name, count + 1);
        const safeName = count === 0 ? item.name : item.name.replace(/\.jpg$/, `_${count + 1}.jpg`);
        entries.push({ name: safeName, data });
      }
      const zipBlob = buildZip(entries);
      triggerDownload(zipBlob, "kagami_photos_1080.zip");
    } finally {
      btnDownloadAll.textContent = "Download All (.zip)";
      btnDownloadAll.disabled = false;
    }
  });

  btnCopyFfmpeg.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(ffmpegCmd.textContent);
      btnCopyFfmpeg.textContent = "Copied!";
      setTimeout(() => { btnCopyFfmpeg.textContent = "Copy ffmpeg command"; }, 1500);
    } catch {
      btnCopyFfmpeg.textContent = "Select & copy above";
    }
  });

  renderPreview();
})();
