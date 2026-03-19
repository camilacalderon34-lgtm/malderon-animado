/* ──────────────────────────────────────────────────────────────────────────
   YouTube Video Creator – Frontend JS
   ────────────────────────────────────────────────────────────────────────── */

// ── State ─────────────────────────────────────────────────────────────────
let currentProjectId = null;
let pollInterval = null;
let logEventSource = null;
let lastLogId = 0;

// Track which chunk cards are expanded (survives re-renders)
const _openChunks = new Set();
const _openPrompts = new Set();

// Store chunk data for modal access (keyed by chunk_number)
const _chunkData = {};

// Cached settings (loaded on demand)
let _settings = {};

// Asset type filter — which types the planner can assign
const ALL_ASSET_TYPES = [
  { key: 'title_card',   icon: '📝', label: 'Titulo' },
  { key: 'web_image',    icon: '🌐', label: 'Imagen Animada' },
  { key: 'web_image_full', icon: '🖼️', label: 'Imagen Completa' },
  { key: 'ai_image',     icon: '🤖', label: 'AI Image' },
  { key: 'stock_video',  icon: '📹', label: 'Stock Vid' },
  { key: 'archive_footage', icon: '🏛️', label: 'Archivo' },
];
let _activeAssetTypes = new Set(['title_card', 'web_image', 'ai_image']);

/* ── Dynamic pills from chain config ──────────────────────────────────── */
const TITLE_PILL = { key: 'title_card', icon: '📝', label: 'Titulo' };
const CAT_PILL_ICONS = { film: '📹', youtube: '▶️', globe: '🌐', robot: '🤖', archive: '🏛️' };
let _visiblePills = [TITLE_PILL, ...ALL_ASSET_TYPES.slice(1)]; // default: all

async function loadChainPills(colName) {
  try {
    const data = await apiFetch(`/api/projects/collections/${encodeURIComponent(colName)}/chain`);
    const cats = (data.categories || []).filter(c => c.enabled && c.pct > 0);
    _visiblePills = [TITLE_PILL];
    for (const cat of cats) {
      _visiblePills.push({
        key: cat.asset_type,
        catId: cat.id,
        icon: CAT_PILL_ICONS[cat.icon] || '📦',
        label: cat.label,
      });
    }
  } catch (_) {
    _visiblePills = [TITLE_PILL, ...ALL_ASSET_TYPES.slice(1)];
  }
  _activeAssetTypes = new Set(_visiblePills.map(p => p.key));
  renderAssetTypeFilters();
}

/** Return capitalised image provider name from settings. */
function _imgProviderName() {
  const p = (_settings['image_provider'] || 'wavespeed');
  return p.charAt(0).toUpperCase() + p.slice(1);
}

// Reference YouTube videos collected in the form [{url, title, transcript}]
const referenceVideos = [];

const STATUS_ICONS = {
  queued: '🕐',
  processing: '⚙️',
  awaiting_approval: '✏️',
  awaiting_voice_config: '🎙️',
  awaiting_audio_approval: '🎵',
  audio_approved: '✅',
  scenes_ready: '🎬',
  generating_images: '🖼️',
  images_ready: '✅',
  rendering: '🎬',
  done: '✅',
  error: '❌',
};

const MODE_ICONS = {
  animated: '🎨',
  stock: '📹',
};

// ── View routing ──────────────────────────────────────────────────────────
function showView(name, projectId = null) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(`view-${name}`).classList.add('active');

  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  const navBtn = document.querySelector(`[data-view="${name}"]`);
  if (navBtn) navBtn.classList.add('active');

  stopPolling();
  stopLogs();
  // Stop editing-specific resources
  if (_editingLogSource) { _editingLogSource.close(); _editingLogSource = null; }
  if (editingPollInterval) { clearInterval(editingPollInterval); editingPollInterval = null; }

  if (name === 'dashboard') loadDashboard();
  if (name === 'detail' && projectId) openDetail(projectId);
  if (name === 'editing' && projectId) openEditing(projectId);
  if (name === 'settings') loadSettingsPage();
}

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

// ── Dashboard ─────────────────────────────────────────────────────────────
async function loadDashboard() {
  try {
    const projects = await apiFetch('/api/projects/');
    const list = document.getElementById('projectList');

    list.innerHTML = '';

    if (!projects.length) {
      list.innerHTML = `
        <div class="empty-state" id="emptyState">
          <div class="empty-icon">🎥</div>
          <p>No hay proyectos todavía.</p>
          <button class="btn btn-primary" onclick="showView('new')">Crear primer video</button>
        </div>`;
      return;
    }

    projects.forEach(p => {
      const pct = p.chunk_count > 0 ? Math.round((p.chunks_done / p.chunk_count) * 100) : 0;
      const card = document.createElement('div');
      card.className = 'project-card';
      card.innerHTML = `
        <div class="project-card-icon">${MODE_ICONS[p.mode] || '🎬'}</div>
        <div class="project-card-body">
          <div class="project-card-title">${escHtml(p.title)}</div>
          <div class="project-card-meta">
            <span class="badge badge-${p.status}">${p.status.toUpperCase()}</span>
            ${p.mode === 'stock' && p.collection ? `<span style="font-size:11px;color:var(--muted);">📁 ${escHtml(p.collection)}</span>` : ''}
            <span>${new Date(p.created_at).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric' })}</span>
            ${p.chunk_count > 0 ? `<span>${p.chunks_done}/${p.chunk_count} escenas</span>` : ''}
          </div>
          ${p.chunk_count > 0 ? `
          <div class="project-card-progress">
            <div class="mini-progress-bar">
              <div class="mini-progress-fill" style="width:${pct}%"></div>
            </div>
          </div>` : ''}
        </div>
        <div class="project-card-actions">
          <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); openDetail(${p.id})">Ver</button>
          ${p.status === 'error' ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); retryProject(${p.id})">Reintentar</button>` : ''}
          <button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="event.stopPropagation(); deleteProject(${p.id})">Borrar</button>
        </div>
      `;
      card.addEventListener('click', () => openDetail(p.id));
      list.appendChild(card);
    });

    updateWorkerStatus();
  } catch (e) {
    showToast('Error cargando proyectos: ' + e.message, 'error');
  }
}



// ── Detail view ───────────────────────────────────────────────────────────
async function openDetail(projectId) {
  currentProjectId = projectId;
  _outlineOpen = false;
  _selectedVoiceId = '';
  _selectedVoiceName = '';
  _allVoices = [];

  // Pre-load settings so the API key warning is accurate when voice config shows
  await _fetchSettings();

  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-detail').classList.add('active');

  // Reset all sections
  document.getElementById('progressCard').style.display = 'none';
  document.getElementById('scriptSection').style.display = 'none';
  document.getElementById('voiceConfigSection').style.display = 'none';
  document.getElementById('voiceoverApprovalSection').style.display = 'none';
  document.getElementById('chunksSection').style.display = 'none';
  document.getElementById('videoPreviewContainer').style.display = 'none';
  document.getElementById('chunkGrid').innerHTML = '';
  document.getElementById('chunksList').innerHTML = '';
  document.getElementById('logsContainer').innerHTML = '<div class="log-placeholder">Cargando logs\u2026</div>';

  const p = await refreshDetail(projectId);

  // Only stream logs if project is actively processing (not done)
  const activeStates = ['queued', 'rendering', 'generating', 'processing', 'generating_images', 'generating_videos'];
  if (p && activeStates.includes(p.status)) {
    startLogStream(projectId);
  }
  // Slower polling for large projects to reduce UI lag
  const pollMs = (p && p.chunk_count > 200) ? 8000 : 4000;
  pollInterval = setInterval(() => refreshDetail(projectId), pollMs);
}

async function refreshDetail(projectId) {
  try {
    const p = await apiFetch(`/api/projects/${projectId}`);
    const chunks = p.chunks || [];

    // ── Header ───────────────────────────────────────────────────────────
    document.getElementById('detailTitle').textContent = p.title;
    const badge = document.getElementById('detailBadge');
    badge.textContent = p.status.toUpperCase();
    badge.className = `badge badge-${p.status}`;
    const collBadge = document.getElementById('detailCollectionBadge');
    if (collBadge) {
      if (p.mode === 'stock' && p.collection) {
        collBadge.textContent = `📁 ${p.collection}`;
        collBadge.style.display = '';
      } else {
        collBadge.style.display = 'none';
      }
    }

    // ── Progress card (queued / processing / error) ───────────────────────
    const progressCard = document.getElementById('progressCard');
    const isRunning = ['queued', 'processing', 'rendering', 'error'].includes(p.status);
    progressCard.style.display = isRunning ? '' : 'none';
    if (isRunning) {
      const total = chunks.length;
      const done = chunks.filter(c => c.status === 'done').length;
      const pct = total > 0 ? Math.round((done / total) * 100) : 0;
      document.getElementById('progressBar').style.width = `${pct}%`;
      document.getElementById('progressPct').textContent = `${pct}%`;
      document.getElementById('progressLabel').textContent =
        p.status === 'error' ? `Error: ${p.error_message || ''}` :
          p.status === 'processing' ? 'Procesando…' : 'En cola…';

      const grid = document.getElementById('chunkGrid');
      grid.innerHTML = '';
      chunks.forEach(c => {
        const dot = document.createElement('div');
        dot.className = `chunk-dot ${c.status}`;
        dot.textContent = c.chunk_number;
        dot.title = `Chunk ${c.chunk_number}: ${c.status}`;
        grid.appendChild(dot);
      });
    }



    // ── 2. Script section (editable ↔ read-only) ──────────────────────────
    const scriptSection = document.getElementById('scriptSection');
    const approvalTextarea = document.getElementById('approvalTextarea');
    const scriptContent = document.getElementById('scriptContent');
    const scriptApprovalControls = document.getElementById('scriptApprovalControls');
    const scriptDoneBadge = document.getElementById('scriptDoneBadge');
    const chunkConfigBar = document.getElementById('chunkConfigBar');
    const scriptHint = document.getElementById('scriptHint');

    if (p.script || p.script_final) {
      scriptSection.style.display = '';

      if (p.status === 'awaiting_approval') {
        // — Editable mode —
        scriptSection.classList.add('script-awaiting');
        approvalTextarea.style.display = '';
        scriptContent.style.display = 'none';
        scriptApprovalControls.style.display = '';
        scriptDoneBadge.style.display = 'none';
        chunkConfigBar.style.display = '';
        scriptHint.textContent = 'Edita el texto si lo deseas, luego aprueba para continuar.';

        if (!approvalTextarea.dataset.edited) {
          approvalTextarea.value = p.script;
          const sizeInput = document.getElementById('chunkSizeInput');
          if (sizeInput && p.target_chunk_size) sizeInput.value = p.target_chunk_size;
          updateChunkPreview();
        }
        updateWordCount();
      } else {
        // — Read-only mode —
        scriptSection.classList.remove('script-awaiting');
        approvalTextarea.dataset.edited = '';
        approvalTextarea.style.display = 'none';
        const wcContainer = document.getElementById('scriptWordCount');
        if (wcContainer) wcContainer.style.display = 'none';
        scriptContent.style.display = '';
        scriptContent.textContent = p.script_final || p.script;
        scriptApprovalControls.style.display = 'none';
        scriptDoneBadge.style.display = '';
        chunkConfigBar.style.display = 'none';
        scriptHint.textContent = '';
      }
    } else {
      scriptSection.style.display = 'none';
    }

    // ── 3. Voice config (awaiting_voice_config only) ──────────────────────
    const voiceConfigSection = document.getElementById('voiceConfigSection');
    const voiceChunksSummary = document.getElementById('voiceChunksSummary');
    if (p.status === 'awaiting_voice_config') {
      voiceConfigSection.style.display = '';

      // Show chunk summary + resplit
      if (chunks.length > 0 && voiceChunksSummary) {
        voiceChunksSummary.style.display = '';
        const info = document.getElementById('voiceChunksInfo');
        if (info) info.textContent = `Script dividido en ${chunks.length} chunks de ~${p.target_chunk_size || 1500} chars`;
        const resplitInput = document.getElementById('resplitInput');
        if (resplitInput) resplitInput.value = p.target_chunk_size || 1500;
      }

      // Restore saved voice selection
      if (p.tts_voice_id && !_selectedVoice.voice_id) {
        let voiceName = p.tts_voice_id;
        let voiceDesc = '';
        try {
          const cfg = JSON.parse(p.tts_config || '{}');
          if (cfg.voice_name) voiceName = cfg.voice_name;
        } catch (_) { }
        _selectedVoice = { ..._selectedVoice, voice_id: p.tts_voice_id, name: voiceName, description: voiceDesc };
        updateVoiceCard();
      }

      // Load voices if not yet loaded
      if (!_allVoices.length) initVoiceConfig();
    } else {
      voiceConfigSection.style.display = 'none';
    }

    // ── 3b. Voiceover section — visible whenever a voiceover exists ───────
    // Audio player stays visible after approval; buttons hide once approved.
    const approvalSection = document.getElementById('voiceoverApprovalSection');
    const isAwaitingAudio = p.status === 'awaiting_audio_approval';
    const isAudioApproved = p.status === 'audio_approved';
    const isScenesReady = p.status === 'scenes_ready';
    const isGeneratingImages = p.status === 'generating_images';
    const isImagesReady = p.status === 'images_ready';
    const isErrorWithVO = p.status === 'error' && !!p.voiceover_path;
    const isRendering = p.status === 'rendering';
    const isDone = p.status === 'done';
    const isErrorWithMedia = p.status === 'error' && chunks.some(c => c.image_path || c.video_path);
    const showImagePanel = isScenesReady || isGeneratingImages || isImagesReady || isRendering || isDone || isErrorWithMedia;
    const hasVoiceover = !!p.voiceover_path;

    if (approvalSection && hasVoiceover) {
      approvalSection.style.display = '';

      // Load waveform player for approval section
      const approvalWaveCanvas = document.getElementById('approvalWaveCanvas');
      const audioUrl = `/api/projects/${p.id}/voiceover/audio?t=${new Date(p.updated_at).getTime() || Date.now()}`;
      if (approvalWaveCanvas && approvalWaveCanvas.dataset.src !== audioUrl) {
        approvalWaveCanvas.dataset.src = audioUrl;
        fetch(audioUrl).then(r => r.blob()).then(blob => renderWaveformPlayer(blob, 'approval')).catch(() => {});
      }

      // Show approve/regenerate buttons only when pending approval
      const approvalActions = approvalSection.querySelector('.voiceover-approval-actions');
      if (approvalActions) approvalActions.style.display = isAwaitingAudio ? '' : 'none';

      // Show "Continuar con Escenas" button only when audio is approved
      const continueActions = document.getElementById('voiceoverContinueActions');
      if (continueActions) continueActions.style.display = isAudioApproved ? '' : 'none';

      // Show "Reintentar" button when errored but voiceover exists
      const retryActions = document.getElementById('voiceoverRetryActions');
      if (retryActions) retryActions.style.display = isErrorWithVO ? '' : 'none';

      // Update badge
      const badge = approvalSection.querySelector('.badge');
      if (badge) {
        if (isAwaitingAudio) badge.textContent = '✓ Listo para revisar';
        else if (isAudioApproved) badge.textContent = '✓ APROBADO';
        else if (isScenesReady) badge.textContent = '✓ Aprobado';
        else badge.textContent = '✓ Aprobado';
      }
    } else if (approvalSection) {
      approvalSection.style.display = 'none';
    }

    // ── 4. Escenas — visible desde scenes_ready en adelante ──────────────
    const chunksSection = document.getElementById('chunksSection');
    const hiddenStatuses = ['awaiting_voice_config', 'awaiting_audio_approval', 'audio_approved'];
    const _isStockMode = p.mode === 'stock';
    _isStockGlobal = _isStockMode;
    if (chunks.length > 0 && !hiddenStatuses.includes(p.status)) {
      chunksSection.style.display = '';

      // Dynamic header based on mode
      const hdr = document.getElementById('scenesTableHeader');
      if (hdr) {
        if (_isStockMode) {
          hdr.innerHTML = `
            <div class="st-col st-num">#</div>
            <div class="st-col st-text">Guion</div>
            <div class="st-col st-salida">Salida</div>
            <div class="st-col st-time">Tiempo</div>
            <div class="st-col st-status">Estado</div>
            <div class="st-col st-actions"></div>`;
        } else {
          hdr.innerHTML = `
            <div class="st-col st-num">#</div>
            <div class="st-col st-img">Imagen</div>
            <div class="st-col st-text">Guion</div>
            <div class="st-col st-vid">Video</div>
            <div class="st-col st-time">Tiempo</div>
            <div class="st-col st-status">Estado</div>
            <div class="st-col st-actions"></div>`;
        }
      }

      const countEl = document.getElementById('chunksCount');
      if (countEl) {
        const doneImgs = chunks.filter(c => c.image_path).length;
        const doneVids = chunks.filter(c => c.video_path).length;
        let parts = [`${chunks.length} escenas`];
        if (doneImgs > 0) parts.push(`${doneImgs} img`);
        if (doneVids > 0) parts.push(`${doneVids} vid`);
        countEl.textContent = `— ${parts.join(' · ')}`;
      }

      const list = document.getElementById('chunksList');

      const _fmtTime = (ms) => {
        const totalSec = Math.floor(ms / 1000);
        const m = Math.floor(totalSec / 60);
        const s = totalSec % 60;
        return `${m}:${String(s).padStart(2, '0')}`;
      };

      // Build per-chunk fingerprints to detect individual row changes
      if (!window._chunkFingerprints) window._chunkFingerprints = {};
      const needsFullBuild = list.children.length === 0 || list.children.length !== chunks.length;
      const changedChunks = new Set();
      chunks.forEach(c => {
        const fp = `${c.chunk_number}:${c.status}:${c.image_path||''}:${c.video_path||''}:${c.image_prompt||''}:${c.motion_prompt||''}:${c.asset_type||''}:${c.updated_at||''}`;
        if (window._chunkFingerprints[c.chunk_number] !== fp) {
          changedChunks.add(c.chunk_number);
          window._chunkFingerprints[c.chunk_number] = fp;
        }
      });

      // Skip if nothing changed
      if (changedChunks.size === 0 && !needsFullBuild) {
        // No changes — skip rebuild to avoid flicker
      } else {
        if (needsFullBuild) list.innerHTML = '';

        chunks.forEach((c, idx) => {
          const n = c.chunk_number;
          // Skip unchanged rows (only when doing partial update)
          if (!needsFullBuild && !changedChunks.has(n)) return;
          const text = c.scene_text || '';
          const cacheBust = c.updated_at ? `?t=${new Date(c.updated_at).getTime()}` : `?t=${Date.now()}`;
          const imgUrl = c.image_path ? `/api/projects/${p.id}/chunk/${n}/image${cacheBust}` : '';
          const vidUrl = c.video_path ? `/api/projects/${p.id}/chunk/${n}/video${cacheBust}` : '';

          // Time
          let timeHtml = '<span class="st-time-val">—</span>';
          if (c.start_ms != null && c.end_ms != null) {
            const durSec = ((c.end_ms - c.start_ms) / 1000).toFixed(1);
            timeHtml = `<span class="st-time-val">${_fmtTime(c.start_ms)}-${_fmtTime(c.end_ms)}</span><br><span class="st-dur">${durSec}s</span>`;
          }

          // Store chunk data for modal access
          _chunkData[n] = { image_prompt: c.image_prompt || '', motion_prompt: c.motion_prompt || '', scene_text: c.scene_text || '' };

          // Image cell
          const imgCell = imgUrl
            ? `<img class="st-thumb" src="${imgUrl}" alt="Escena ${n}" loading="lazy" onclick="openImagePreview('${imgUrl}', ${n})" />`
            : `<div class="st-thumb-empty">—</div>`;

          // Video cell
          let vidCell = `<div class="st-thumb-empty">—</div>`;
          if (vidUrl) {
            vidCell = `<div class="st-vid-wrap" onclick="openVideoPreview('${vidUrl}', ${n})">
              <video src="${vidUrl}" preload="metadata" muted></video>
              <div class="st-vid-play">&#9654;</div>
            </div>`;
          }

          // Asset type badge (stock mode)
          const _assetLabels = {stock_video:'Stock Vid',title_card:'Titulo',web_image:'Imagen Animada',web_image_full:'Imagen Completa',ai_image:'AI Image',archive_footage:'Archivo',space_media:'Espacio',video:'Video',image:'Imagen'};
          const _assetIcons = {stock_video:'📹',title_card:'📝',web_image:'🌐',web_image_full:'🖼️',ai_image:'🤖',archive_footage:'🏛️',space_media:'🚀',video:'🎬',image:'🖼️'};
          let assetBadge = '';
          if (c.asset_type) {
            const aLabel = _assetLabels[c.asset_type] || c.asset_type;
            const aIcon = _assetIcons[c.asset_type] || '';
            assetBadge = `<span class="asset-badge ${c.asset_type}" data-chunk="${n}" onclick="event.stopPropagation(); toggleAssetDropdown(this, ${p.id}, ${n})" title="${c.search_keywords || ''}">${aIcon} ${aLabel}</span>`;
          }

          // Prompt tags (hover to see full text)
          let promptTags = '';
          if (c.image_prompt) {
            promptTags += `<span class="st-prompt-tag" title="${escHtml(c.image_prompt)}">IMG</span>`;
          }
          if (c.motion_prompt) {
            promptTags += `<span class="st-prompt-tag" title="${escHtml(c.motion_prompt)}">MOV</span>`;
          }

          // Status — descriptive labels for stock mode
          let statusLabel, statusClass;
          if (_isStockMode) {
            if (c.image_path || c.video_path) {
              statusLabel = 'listo'; statusClass = 'done';
            } else if (c.status === 'error' && c.error_message === 'sin asset') {
              statusLabel = 'sin asset'; statusClass = 'no-asset';
            } else if (c.asset_type === 'title_card') {
              statusLabel = 'título'; statusClass = 'title-card';
            } else if (c.status === 'done') {
              statusLabel = 'listo'; statusClass = 'done';
            } else if (c.status === 'processing') {
              statusLabel = 'buscando'; statusClass = 'processing';
            } else if (c.status === 'error') {
              statusLabel = 'error'; statusClass = 'error';
            } else {
              statusLabel = 'pendiente'; statusClass = 'pending';
            }
          } else {
            statusLabel = c.status === 'done' ? 'done' : c.status === 'processing' ? 'proc' : c.status === 'error' ? 'error' : c.status;
            statusClass = c.status;
          }

          // Action buttons
          let actions = '';
          if (_isStockMode) {
            // Stock mode: always show "Rebuscar" button so user can retry any scene individually
            actions += `<button class="st-action-btn" title="Rebuscar" onclick="event.stopPropagation(); retryStockSearch(${n})">&#x1F504;</button>`;
          } else {
            // Animated mode: original buttons
            if (c.image_prompt) {
              actions += `<button class="st-action-btn" title="Rehacer imagen" onclick="event.stopPropagation(); regenerateImageGenaipro(${n})">&#x1F504;</button>`;
            }
            if (c.image_path) {
              actions += `<button class="st-action-btn" title="Reanimar video" onclick="event.stopPropagation(); retryMetaAnimation(${n})">&#x26A1;</button>`;
            }
          }

          // Build "Salida" cell for stock mode (shows whatever output exists)
          let salidaCell = `<div class="st-thumb-empty">—</div>`;
          if (vidUrl) {
            salidaCell = `<div class="st-vid-wrap" onclick="openVideoPreview('${vidUrl}', ${n})">
              <video src="${vidUrl}" preload="metadata" muted></video>
              <div class="st-vid-play">&#9654;</div>
            </div>`;
          } else if (imgUrl) {
            salidaCell = `<img class="st-thumb" src="${imgUrl}" alt="Escena ${n}" loading="lazy" onclick="openImagePreview('${imgUrl}', ${n})" />`;
          }

          const row = document.createElement('div');
          row.className = 'scene-row' + (_isStockMode ? ' stock-mode' : '');
          row.setAttribute('data-chunk', n);

          if (_isStockMode) {
            // Stock: # | Guion (badge + text) | Salida | Tiempo | Estado | Actions
            row.innerHTML = `
              <div class="st-col st-num">${n}</div>
              <div class="st-col st-text">
                ${assetBadge ? `<div class="st-asset-row">${assetBadge}</div>` : ''}
                <div class="st-script">${escHtml(text)}</div>
                ${promptTags ? `<div class="st-prompts">${promptTags}</div>` : ''}
              </div>
              <div class="st-col st-salida">${salidaCell}</div>
              <div class="st-col st-time">${timeHtml}</div>
              <div class="st-col st-status"><span class="st-status-badge ${statusClass}">${statusLabel}</span></div>
              <div class="st-col st-actions">${actions}</div>
            `;
          } else {
            // Animated: # | Imagen | Guion | Video | Tiempo | Estado | Actions
            row.innerHTML = `
              <div class="st-col st-num">${n}</div>
              <div class="st-col st-img">${imgCell}</div>
              <div class="st-col st-text">
                <div class="st-script">${escHtml(text)}</div>
                ${promptTags ? `<div class="st-prompts">${promptTags}</div>` : ''}
              </div>
              <div class="st-col st-vid">${vidCell}</div>
              <div class="st-col st-time">${timeHtml}</div>
              <div class="st-col st-status"><span class="st-status-badge ${statusClass}">${statusLabel}</span></div>
              <div class="st-col st-actions">${actions}</div>
            `;
          }
          if (needsFullBuild) {
            list.appendChild(row);
          } else {
            // Replace only the changed row
            const existing = list.querySelector(`[data-chunk="${n}"]`);
            if (existing) existing.replaceWith(row);
          }
        });
      }
    } else {
      chunksSection.style.display = 'none';
    }

    // ── 4b. Imagen panel — visible en scenes_ready / generating_images / images_ready ──
    const scenesReadySection = document.getElementById('scenesReadySection');
    if (scenesReadySection) {
      const isStock = p.mode === 'stock';
      // Stock mode: bottom panel hidden (buttons are in chunks header)
      // Animated mode: show the panel with image generation controls
      scenesReadySection.style.display = (!isStock && showImagePanel) ? '' : 'none';

      const animatedControls = document.getElementById('animatedModeControls');
      if (animatedControls) animatedControls.style.display = isStock ? 'none' : '';

      // Show/hide stock action buttons in the chunks header
      const stockBtns = document.getElementById('stockActionButtons');
      if (stockBtns) {
        stockBtns.style.display = (isStock && showImagePanel) ? 'flex' : 'none';
        if (isStock && showImagePanel) {
          const col = p.collection || 'general';
          loadChainPills(col);  // loads chain config → renders only enabled pills
        }
      }

      // Character reference UI
      const charPreview = document.getElementById('refCharPreview');
      const charThumb = document.getElementById('refCharThumb');
      const charStatus = document.getElementById('refCharStatus');
      const deleteCharBtn = document.getElementById('deleteRefCharBtn');
      if (p.reference_character_path) {
        if (charPreview) charPreview.style.display = '';
        if (charThumb) charThumb.src = `/api/projects/${p.id}/reference-character?t=${Date.now()}`;
        if (charStatus) { charStatus.textContent = '✅'; charStatus.style.color = '#2ecc71'; }
        if (deleteCharBtn) deleteCharBtn.style.display = '';
      } else {
        if (charPreview) charPreview.style.display = 'none';
        if (charStatus) { charStatus.textContent = ''; charStatus.style.color = ''; }
        if (deleteCharBtn) deleteCharBtn.style.display = 'none';
      }
      // Style reference UI
      const stylePreview = document.getElementById('refStylePreview');
      const styleThumb = document.getElementById('refStyleThumb');
      const styleStatus = document.getElementById('refStyleStatus');
      const deleteStyleBtn = document.getElementById('deleteRefStyleBtn');
      if (p.reference_style_path) {
        if (stylePreview) stylePreview.style.display = '';
        if (styleThumb) styleThumb.src = `/api/projects/${p.id}/reference-style?t=${Date.now()}`;
        if (styleStatus) { styleStatus.textContent = '✅'; styleStatus.style.color = '#2ecc71'; }
        if (deleteStyleBtn) deleteStyleBtn.style.display = '';
      } else {
        if (stylePreview) stylePreview.style.display = 'none';
        if (styleStatus) { styleStatus.textContent = ''; styleStatus.style.color = ''; }
        if (deleteStyleBtn) deleteStyleBtn.style.display = 'none';
      }

      if (showImagePanel) {
        const doneImgs = chunks.filter(c => c.image_path).length;
        const label = document.getElementById('scenesReadyLabel');
        const progressCount = document.getElementById('imagesProgressCount');
        const generateBtn = document.getElementById('generateImagesBtn');
        const continueBtn = document.getElementById('continueWithVideoBtn');
        const hint = document.getElementById('scenesReadyHint');

        if (isGeneratingImages) {
          if (label) label.textContent = `🎨 Generando escena ${doneImgs} de ${chunks.length}…`;
          if (progressCount) { progressCount.style.display = ''; progressCount.textContent = `${doneImgs} de ${chunks.length} escenas completadas`; }
          if (generateBtn) { generateBtn.style.display = ''; generateBtn.disabled = true; generateBtn.textContent = `⏳ Generando con ${_imgProviderName()}…`; }
          if (continueBtn) continueBtn.style.display = 'none';
          if (hint) hint.textContent = 'Google Imagen 4 Fast está procesando cada escena. Puedes seguir viendo su progreso en vivo minimizando esta ventana.';
        } else if (isImagesReady) {
          const hasErrors = chunks.some(c => c.status === 'error');
          const doneVideos = chunks.filter(c => c.video_path).length;
          if (label) label.textContent = hasErrors
            ? `⚠️ ${doneImgs} de ${chunks.length} escenas generadas (con errores)`
            : `✅ ${doneImgs} imágenes · ${doneVideos} videos generados`;
          if (progressCount) { progressCount.style.display = ''; progressCount.textContent = `${doneImgs} imágenes · ${doneVideos} videos de ${chunks.length}`; }

          if (hasErrors) {
            if (generateBtn) {
              generateBtn.style.display = '';
              generateBtn.disabled = false;
              generateBtn.textContent = '🔄 Reintentar Escenas Fallidas';
              generateBtn.className = 'btn btn-warning btn-lg';
            }
          } else {
            if (generateBtn) generateBtn.style.display = 'none';
          }

          if (continueBtn) continueBtn.style.display = 'none';
          if (hint) hint.textContent = hasErrors
            ? 'Algunas escenas fallaron. Puedes reintentar las fallidas individualmente.'
            : `✅ Imágenes listas con ${_imgProviderName()}. Motion prompts generados para ajuste manual.`;

          // "Regenerar TODAS" button
          let regenAllBtn = document.getElementById('regenAllGenaipro');
          if (!regenAllBtn) {
            regenAllBtn = document.createElement('button');
            regenAllBtn.id = 'regenAllGenaipro';
            regenAllBtn.className = 'btn btn-warning btn-sm';
            regenAllBtn.style.marginTop = '8px';
            regenAllBtn.textContent = '⚠️ Regenerar TODAS las imágenes';
            regenAllBtn.onclick = regenerateAllGenaipro;
            hint.parentNode.insertBefore(regenAllBtn, hint.nextSibling);
          }
          regenAllBtn.style.display = '';

          // Show Veo3 Animation Block
          const veo3Section = document.getElementById('veo3AnimationSection');
          if (veo3Section) {
            veo3Section.style.display = 'block';
            const doneVids = chunks.filter(c => c.video_path).length;
            const progressEl = document.getElementById('animationProgress');
            if (progressEl) {
              progressEl.textContent = doneVids > 0 ? `${doneVids} de ${chunks.length} videos animados` : '';
            }
            const veoBtn = document.getElementById('startVeo3AnimationBtn');
            if (veoBtn) {
              if (doneVids === chunks.length && chunks.length > 0) {
                veoBtn.textContent = '✅ Todas las escenas animadas';
                veoBtn.disabled = true;
              } else if (doneVids > 0) {
                veoBtn.textContent = `🎬 Animar ${chunks.length - doneVids} escenas restantes`;
                veoBtn.disabled = false;
              }
            }
          }
        }
        else {
          // scenes_ready — ready to generate
          if (label) label.textContent = `✅ ${chunks.length} escenas listas para generar`;
          if (progressCount) progressCount.style.display = 'none';
          if (generateBtn) { generateBtn.style.display = ''; generateBtn.disabled = false; generateBtn.textContent = `🎨 Generar Imágenes (${_imgProviderName()})`; }
          if (continueBtn) continueBtn.style.display = 'none';
          if (hint) hint.textContent = `Gemini generará un prompt visual por escena, luego ${_imgProviderName()} creará la imagen (16:9).`;
        }
      }
    }

    // ── 5. "Ir a Edición" button — visible when videos/images exist ──────
    const goToEditingSection = document.getElementById('goToEditingSection');
    const doneVideos = chunks.filter(c => c.video_path).length;
    const hasVideos = doneVideos > 0;
    const showEditing = ['images_ready', 'rendering', 'done', 'error'].includes(p.status) && hasVideos;
    if (goToEditingSection) {
      goToEditingSection.style.display = showEditing ? '' : 'none';
      const btn = document.getElementById('goToEditingBtn');
      if (btn) {
        if (p.final_video_path) {
          btn.textContent = '✅ Video Final Listo';
          btn.style.background = 'var(--green)';
          btn.style.borderColor = 'var(--green)';
        } else if (p.status === 'rendering') {
          btn.textContent = '⏳ Renderizando…';
          btn.style.background = '';
          btn.style.borderColor = '';
        } else {
          btn.textContent = '🎬 Ir a Edición';
          btn.style.background = '';
          btn.style.borderColor = '';
        }
      }
    }

    // ── Legacy video preview container ────────────────────────────────────
    const legacyContainer = document.getElementById('videoPreviewContainer');
    if (legacyContainer) legacyContainer.style.display = 'none';

    // ── Stop polling when in stable state ─────────────────────────────────
    if (['done', 'error', 'awaiting_approval', 'awaiting_voice_config', 'awaiting_audio_approval', 'audio_approved', 'scenes_ready', 'images_ready'].includes(p.status)) {
      // Keep polling while Veo3 animation is running (videos still being generated)
      const animating = p.status === 'images_ready' && chunks.some(c => c.image_path && !c.video_path);
      if (!animating) stopPolling();
    }
    return p;
  } catch (e) {
    console.error('refreshDetail error:', e);
    return null;
  }
}

// ── Log streaming (SSE) ───────────────────────────────────────────────────
function startLogStream(projectId) {
  stopLogs();
  const container = document.getElementById('logsContainer');
  container.innerHTML = '';
  lastLogId = 0;

  logEventSource = new EventSource(`/api/logs/${projectId}/stream`);

  logEventSource.onmessage = (event) => {
    const log = JSON.parse(event.data);
    appendLog(log);
  };

  logEventSource.onerror = () => {
    // Fallback to polling if SSE fails
    logEventSource.close();
    logEventSource = null;
    startLogPolling(projectId);
  };
}

function startLogPolling(projectId) {
  const pollLogs = async () => {
    try {
      const logs = await apiFetch(`/api/logs/${projectId}?since_id=${lastLogId}&limit=50`);
      logs.forEach(log => {
        lastLogId = Math.max(lastLogId, log.id);
        appendLog(log);
      });
    } catch (e) { /* ignore */ }
  };
  const logPollTimer = setInterval(pollLogs, 1500);
  // Store timer for cleanup
  window._logPollTimer = logPollTimer;
}

function appendLog(log) {
  const container = document.getElementById('logsContainer');
  const placeholder = container.querySelector('.log-placeholder');
  if (placeholder) placeholder.remove();

  const line = document.createElement('div');
  line.className = `log-line ${log.level}`;
  const ts = new Date(log.timestamp).toLocaleTimeString('es-ES', { hour12: false });
  line.innerHTML = `
    <span class="log-ts">${ts}</span>
    <span class="log-stage">[${escHtml(log.stage || 'general')}]</span>
    <span class="log-msg">${escHtml(log.message)}</span>
  `;
  container.appendChild(line);
  container.scrollTop = container.scrollHeight;
}

function clearLogs() {
  document.getElementById('logsContainer').innerHTML = '';
}

function toggleLogs() {
  const el = document.getElementById('detailLogs');
  if (el) el.classList.toggle('collapsed');
}

function stopLogs() {
  if (logEventSource) { logEventSource.close(); logEventSource = null; }
  if (window._logPollTimer) { clearInterval(window._logPollTimer); window._logPollTimer = null; }
}

function stopPolling() {
  if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
}

// ── Settings ───────────────────────────────────────────────────────────────

const _PROVIDER_KEY_MAP = {
  genaipro: 'genaipro_api_key',
  elevenlabs: 'genaipro_api_key',  // ElevenLabs uses same Genaipro key (proxy)
  openai: 'anthropic_api_key', // fallback; adjust if OpenAI key is separate
};

async function _fetchSettings() {
  try {
    const result = await apiFetch('/api/settings/');
    _settings = result.data || {};
  } catch (e) {
    _settings = {};
  }
}

async function loadSettingsPage() {
  await _fetchSettings();

  const masked = '••••••••';
  const fields = [
    'anthropic_api_key', 'genaipro_api_key', 'pollinations_api_key', 'wavespeed_api_key',
    'google_api_key', 'pexels_api_key', 'pixabay_api_key',
    'image_provider',
    'default_tts_provider', 'default_tts_voice_id', 'default_tts_model_id',
    'default_tts_speed', 'default_tts_stability', 'default_tts_similarity', 'default_tts_style',
    'default_video_mode', 'default_image_interval',
  ];

  fields.forEach(key => {
    const el = document.getElementById(`setting_${key}`);
    if (!el) return;
    const val = _settings[key] || '';
    el.value = val;
    // Update slider display values
    const valDisplay = document.getElementById(`setting_${key}_val`);
    if (valDisplay && val) valDisplay.textContent = parseFloat(val).toFixed(2);
  });
}

async function saveSetting(key) {
  const el = document.getElementById(`setting_${key}`);
  if (!el) return;
  const value = el.value.trim();

  try {
    const result = await apiFetch('/api/settings/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: { [key]: value } }),
    });
    _settings = result.data || {};
    // Show the mask placeholder after saving so the user knows it's saved
    el.value = _settings[key] || '';
    showToast('✓ Guardado', 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function testGenaIproImage() {
  const resultEl = document.getElementById('genaipro-test-result');
  resultEl.style.display = 'block';
  resultEl.textContent = '⏳ Enviando prueba a Genaipro /veo/create-image… (60s timeout)';
  try {
    const result = await apiFetch('/api/settings/test-genaipro-image', { method: 'POST' });
    let txt = `Clave usada: ${result.api_key_suffix}\nPrompt: ${result.test_prompt}\n\n`;
    for (const r of (result.results || [])) {
      txt += `\n── ${r.strategy} ──\n`;
      if (r.error) {
        txt += `  ERROR: ${r.error}\n`;
      } else {
        txt += `  HTTP: ${r.http_status}  Content-Type: ${r.content_type}\n`;
        txt += `  Body:\n${r.body_preview}\n`;
      }
    }
    resultEl.textContent = txt;
  } catch (e) {
    resultEl.textContent = 'Error: ' + e.message;
  }
}

async function saveVoiceDefaults() {
  const data = {
    default_tts_provider: document.getElementById('setting_default_tts_provider')?.value || '',
    default_tts_voice_id: document.getElementById('setting_default_tts_voice_id')?.value?.trim() || '',
    default_tts_model_id: document.getElementById('setting_default_tts_model_id')?.value?.trim() || '',
    default_tts_speed: document.getElementById('setting_default_tts_speed')?.value || '1.0',
    default_tts_stability: document.getElementById('setting_default_tts_stability')?.value || '0.5',
    default_tts_similarity: document.getElementById('setting_default_tts_similarity')?.value || '0.75',
    default_tts_style: document.getElementById('setting_default_tts_style')?.value || '0.0',
  };
  try {
    const result = await apiFetch('/api/settings/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
    });
    _settings = result.data || {};
    showToast('✓ Configuración de voz guardada', 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function saveVideoDefaults() {
  const data = {
    default_video_mode: document.getElementById('setting_default_video_mode')?.value || 'animated',
    default_image_interval: document.getElementById('setting_default_image_interval')?.value || '5',
  };
  try {
    const result = await apiFetch('/api/settings/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
    });
    _settings = result.data || {};
    showToast('✓ Configuración de video guardada', 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

// ── Reference Videos ──────────────────────────────────────────────────────

function renderReferenceList() {
  const list = document.getElementById('referenceList');
  list.innerHTML = '';
  referenceVideos.forEach((v, idx) => {
    const item = document.createElement('div');
    item.className = 'ref-item';
    item.innerHTML = `
      <div class="ref-item-icon">&#x1F3AC;</div>
      <div class="ref-item-body">
        <div class="ref-item-title">${escHtml(v.title || v.url)}</div>
        <div class="ref-item-status ${v.status === 'ok' ? 'ok' : v.status === 'error' ? 'error' : 'loading'}">
          ${v.status === 'ok' ? 'Transcripci\u00f3n obtenida \u2713' : v.status === 'error' ? v.error : 'Obteniendo transcripci\u00f3n\u2026'}
        </div>
      </div>
      <button class="ref-item-remove" onclick="removeReferenceVideo(${idx})" title="Eliminar">&#x2715;</button>
    `;
    list.appendChild(item);
  });

  // Hide add button when 3 videos reached
  const addBtn = document.getElementById('refAddBtn');
  const refInput = document.getElementById('refUrlInput');
  if (referenceVideos.length >= 3) {
    addBtn.disabled = true;
    refInput.disabled = true;
    refInput.placeholder = 'M\u00e1ximo 3 videos de referencia';
  } else {
    addBtn.disabled = false;
    refInput.disabled = false;
    refInput.placeholder = 'https://www.youtube.com/watch?v=...';
  }
}

async function addReferenceVideo() {
  const input = document.getElementById('refUrlInput');
  const url = input.value.trim();
  if (!url) return;
  if (referenceVideos.length >= 3) {
    showToast('M\u00e1ximo 3 videos de referencia', 'error');
    return;
  }

  // Add placeholder entry while loading
  const idx = referenceVideos.length;
  referenceVideos.push({ url, title: url, status: 'loading', transcript: '' });
  input.value = '';
  renderReferenceList();

  try {
    const result = await apiFetch('/api/youtube/transcript', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    referenceVideos[idx] = {
      url,
      title: result.title,
      transcript: result.transcript,
      status: 'ok',
    };
    showToast(`"${result.title}" \u2013 transcripci\u00f3n obtenida`, 'success');
  } catch (e) {
    referenceVideos[idx] = { url, title: url, status: 'error', error: e.message, transcript: '' };
    showToast('No se pudo obtener la transcripci\u00f3n: ' + e.message, 'error');
  }
  renderReferenceList();
}

function removeReferenceVideo(idx) {
  referenceVideos.splice(idx, 1);
  renderReferenceList();
}

// ── New video form ────────────────────────────────────────────────────────
const modeOptions = document.querySelectorAll('.mode-option');
modeOptions.forEach(opt => {
  opt.addEventListener('click', () => {
    modeOptions.forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
    const isAnimated = opt.querySelector('input').value === 'animated';
    document.getElementById('characterGroup').style.display = isAnimated ? '' : 'none';
    const collectionGroup = document.getElementById('collectionGroup');
    if (collectionGroup) collectionGroup.style.display = isAnimated ? 'none' : '';
    if (!isAnimated) loadCollections();
  });
});

let _selectedCollection = 'general';
let _allCollections = []; // [{name, icon, display_name}]
let _clipBankAvailable = false;

/* ── SVG icon system for collections ── */
const COL_ICONS = {
  general:  '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  film:     '<svg viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="16" rx="2"/><line x1="2" y1="8" x2="22" y2="8"/><line x1="6" y1="4" x2="6" y2="8"/><line x1="10" y1="4" x2="10" y2="8"/><line x1="14" y1="4" x2="14" y2="8"/><line x1="18" y1="4" x2="18" y2="8"/></svg>',
  travel:   '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><line x1="3" y1="12" x2="21" y2="12"/></svg>',
  nature:   '<svg viewBox="0 0 24 24"><path d="M12 3C7 8 4 12 4 15a8 8 0 0016 0c0-3-3-7-8-12z"/></svg>',
  tech:     '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="12" rx="2"/><line x1="8" y1="20" x2="16" y2="20"/><line x1="12" y1="16" x2="12" y2="20"/></svg>',
  food:     '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 0118 0H3z"/><line x1="3" y1="15" x2="21" y2="15"/><path d="M5 15v2a7 7 0 0014 0v-2"/></svg>',
  music:    '<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  history:  '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 15"/></svg>',
  sports:   '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3c-2 3-3 6-3 9s1 6 3 9"/><path d="M12 3c2 3 3 6 3 9s-1 6-3 9"/></svg>',
  default:  '<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>',
};

function colIconSvg(name) {
  const n = (name || '').toLowerCase();
  if (n === 'general') return COL_ICONS.general;
  if (/film|cine|movie|pelicula/.test(n)) return COL_ICONS.film;
  if (/viaj|travel|turism/.test(n)) return COL_ICONS.travel;
  if (/natur|animal|wild/.test(n)) return COL_ICONS.nature;
  if (/tech|comput|robot|ia|ai|prog/.test(n)) return COL_ICONS.tech;
  if (/food|cocina|comida|recet/.test(n)) return COL_ICONS.food;
  if (/music|musica|song/.test(n)) return COL_ICONS.music;
  if (/histor|antiguo|medieval/.test(n)) return COL_ICONS.history;
  if (/sport|deport|futbol|soccer/.test(n)) return COL_ICONS.sports;
  return COL_ICONS.default;
}

async function loadCollections() {
  try {
    const data = await apiFetch('/api/projects/collections/list');
    _clipBankAvailable = data.source === 'clip_bank';
    const cols = data.collections || [];
    _allCollections = cols.map(c =>
      typeof c === 'string' ? { name: c, display_name: c } : c
    );
    const gi = _allCollections.findIndex(c => c.name === 'general');
    if (gi > 0) { const g = _allCollections.splice(gi, 1)[0]; _allCollections.unshift(g); }
    else if (gi < 0) { _allCollections.unshift({ name: 'general', display_name: 'general' }); }
  } catch (_) {
    _allCollections = [{ name: 'general', display_name: 'general' }];
  }
  renderCollectionPills();
}

function renderCollectionPills() {
  const strip = document.getElementById('colStrip');
  if (!strip) return;
  strip.innerHTML = _allCollections.map(c => {
    const isActive = c.name === _selectedCollection;
    const isGeneral = c.name === 'general';
    const icon = colIconSvg(c.name);
    return `<div class="col-pill ${isActive ? 'active' : ''}" data-col="${c.name}" onclick="colSelect('${c.name}')">
      <span class="col-pill-icon">${icon}</span>
      <span class="col-pill-name">${c.display_name || c.name}</span>
      <span class="col-pill-actions" onclick="event.stopPropagation()">
        <button type="button" onclick="openChainConfig('${c.name}')" title="Configurar cadena">
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2.5"/><path d="M8 1v2m0 10v2M1 8h2m10 0h2m-2.5-5l-1.4 1.4M5.9 10.1L4.5 11.5m0-7L5.9 5.9m4.2 4.2l1.4 1.4"/></svg>
        </button>
        ${!isGeneral ? `<button type="button" class="col-del-btn" onclick="colStartDelete('${c.name}')" title="Eliminar coleccion">
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/></svg>
        </button>` : ''}
      </span>
    </div>`;
  }).join('');
}

function colSelect(name) {
  _selectedCollection = name;
  document.querySelectorAll('.col-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.col === name));
}

/* ── Delete collection with inline confirmation ── */
function colStartDelete(name) {
  document.querySelectorAll('.col-del-confirm').forEach(el => el.remove());
  const pill = document.querySelector(`.col-pill[data-col="${name}"]`);
  if (!pill) return;
  const overlay = document.createElement('div');
  overlay.className = 'col-del-confirm';
  overlay.innerHTML = `Eliminar?
    <button class="col-del-yes" onclick="event.stopPropagation();colConfirmDelete('${name}')">Si</button>
    <button class="col-del-no" onclick="event.stopPropagation();colCancelDelete()">No</button>`;
  pill.appendChild(overlay);
  setTimeout(() => { if (overlay.parentNode) overlay.remove(); }, 4000);
}

function colCancelDelete() {
  document.querySelectorAll('.col-del-confirm').forEach(el => el.remove());
}

async function colConfirmDelete(name) {
  colCancelDelete();
  try {
    await apiFetch(`/api/projects/collections/${name}`, { method: 'DELETE' });
    _allCollections = _allCollections.filter(c => c.name !== name);
    if (_selectedCollection === name) _selectedCollection = 'general';
    renderCollectionPills();
    showToast(`Coleccion "${name}" eliminada`, 'success');
  } catch (e) {
    showToast('Error al eliminar coleccion', 'error');
  }
}

/* ── Create collection inline ── */
function colToggleNewInput() {
  const inp = document.getElementById('colNewInput');
  const btn = document.getElementById('colAddBtn');
  const isShown = inp.style.display !== 'none';
  inp.style.display = isShown ? 'none' : 'inline-flex';
  btn.style.display = isShown ? 'inline-flex' : 'none';
  if (!isShown) {
    const field = document.getElementById('colNewName');
    field.value = '';
    field.focus();
  }
}

function colCancelNew() {
  document.getElementById('colNewInput').style.display = 'none';
  document.getElementById('colAddBtn').style.display = 'inline-flex';
}

async function colCreateFromInput() {
  const field = document.getElementById('colNewName');
  const name = (field.value || '').trim();
  if (!name || name.length < 2) { showToast('Nombre muy corto (minimo 2 caracteres)', 'error'); return; }
  if (_allCollections.some(c => c.name === name)) { showToast('Esa coleccion ya existe', 'error'); return; }
  if (_clipBankAvailable) {
    try {
      await apiFetch('/api/projects/collections/create', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
    } catch (e) { console.warn('[ClipBank] Could not create collection:', e); }
  }
  _allCollections.push({ name, display_name: name });
  _selectedCollection = name;
  renderCollectionPills();
  colCancelNew();
  showToast(`Coleccion "${name}" creada`, 'success');
}

/* ── Search Chain Configuration Modal (v2 — categories + percentages) ── */

const CHAIN_ICONS = {
  film: '📹', archive: '🏛️', youtube: '▶️', globe: '🌐', robot: '🤖',
};

let _chainModalCol = null;
let _chainCategories = [];  // array of category objects from server

async function openChainConfig(colName) {
  _chainModalCol = colName;
  try {
    const data = await apiFetch(`/api/projects/collections/${colName}/chain`);
    _chainCategories = JSON.parse(JSON.stringify(data.categories || []));
  } catch (_) {
    _chainCategories = [];
  }
  if (!_chainCategories.length) {
    showToast('No se pudo cargar la cadena', 'error');
    return;
  }
  _renderChainList();
  document.getElementById('chainModal').style.display = 'flex';
}

function _renderChainList() {
  const list = document.getElementById('chainList');
  if (!list) return;
  const titleEl = document.getElementById('chainTitle');
  if (titleEl) titleEl.textContent = `Cadena de busqueda — ${_chainModalCol}`;

  list.innerHTML = _chainCategories.map((cat) => {
    const enabled = cat.enabled !== false;
    const icon = CHAIN_ICONS[cat.icon] || '📦';
    const sourcesText = (cat.sources || []).join(', ');
    return `<li class="chain-item ${enabled ? '' : 'disabled'}" data-id="${cat.id}">
      <div class="chain-item-header">
        <button type="button" class="chain-toggle" data-action="toggle" data-sid="${cat.id}">${enabled ? '✅' : '❌'}</button>
        <span class="chain-icon">${icon}</span>
        <span class="chain-label">${cat.label}</span>
        <span class="chain-pct-value" data-pct-display="${cat.id}">${cat.pct}%</span>
      </div>
      ${sourcesText ? `<div class="chain-sources">${sourcesText}</div>` : ''}
      <div class="chain-slider-row">
        <input type="range" class="chain-slider" min="0" max="100" value="${cat.pct}"
               data-slider="${cat.id}" ${enabled ? '' : 'disabled'}>
      </div>
    </li>`;
  }).join('');

  // Total row
  const total = _chainCategories.reduce((s, c) => s + (c.enabled !== false ? c.pct : 0), 0);
  const totalClass = total === 100 ? 'ok' : 'warn';
  list.insertAdjacentHTML('afterend', '');
  let totalRow = document.getElementById('chainTotalRow');
  if (!totalRow) {
    totalRow = document.createElement('div');
    totalRow.id = 'chainTotalRow';
    totalRow.className = 'chain-total-row';
    list.parentNode.insertBefore(totalRow, list.nextSibling);
  }
  totalRow.innerHTML = `Total: <span class="chain-total-value ${totalClass}">${total}%</span>`;
}

function _onSliderChange(catId, newVal) {
  const cat = _chainCategories.find(c => c.id === catId);
  if (!cat || cat.enabled === false) return;
  cat.pct = parseInt(newVal, 10);
  // Update display
  const display = document.querySelector(`[data-pct-display="${catId}"]`);
  if (display) display.textContent = cat.pct + '%';
  // Update total
  const total = _chainCategories.reduce((s, c) => s + (c.enabled !== false ? c.pct : 0), 0);
  const totalRow = document.getElementById('chainTotalRow');
  if (totalRow) {
    const cls = total === 100 ? 'ok' : 'warn';
    totalRow.innerHTML = `Total: <span class="chain-total-value ${cls}">${total}%</span>`;
  }
}

function _equalizePercentages() {
  const enabled = _chainCategories.filter(c => c.enabled !== false);
  if (!enabled.length) return;
  const base = Math.floor(100 / enabled.length);
  let remainder = 100 - base * enabled.length;
  enabled.forEach((cat, i) => {
    cat.pct = base + (i < remainder ? 1 : 0);
  });
  _chainCategories.filter(c => c.enabled === false).forEach(c => { c.pct = 0; });
  _renderChainList();
}

async function saveChainConfig() {
  try {
    await apiFetch(`/api/projects/collections/${_chainModalCol}/chain`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: 2, categories: _chainCategories }),
    });
    showToast('Cadena guardada', 'success');
  } catch (e) {
    showToast('Error al guardar cadena', 'error');
  }
  closeChainModal();
  // Reload pills if current project uses this collection
  if (currentProjectId) loadChainPills(_chainModalCol);
}

function closeChainModal() {
  const m = document.getElementById('chainModal');
  if (m) m.style.display = 'none';
  const totalRow = document.getElementById('chainTotalRow');
  if (totalRow) totalRow.remove();
}

/* ── Chain modal event delegation (set up once) ─────────────────────────── */
(function initChainModal() {
  document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('chainModal');
    if (!modal) return;

    // Click delegation
    modal.addEventListener('click', (e) => {
      // Toggle button
      const toggleBtn = e.target.closest('[data-action="toggle"]');
      if (toggleBtn) {
        e.preventDefault();
        e.stopPropagation();
        const sid = toggleBtn.dataset.sid;
        const cat = _chainCategories.find(c => c.id === sid);
        if (cat) {
          cat.enabled = !cat.enabled;
          if (!cat.enabled) cat.pct = 0;
          _renderChainList();
        }
        return;
      }
      // Equalize button
      if (e.target.closest('[data-action="equalize"]')) {
        e.preventDefault();
        _equalizePercentages();
        return;
      }
      // Save button
      if (e.target.closest('[data-action="save"]')) {
        e.preventDefault();
        saveChainConfig();
        return;
      }
      // Cancel button
      if (e.target.closest('[data-action="cancel"]')) {
        e.preventDefault();
        closeChainModal();
        return;
      }
      // Click on background overlay closes modal
      if (e.target === modal) {
        closeChainModal();
      }
    });

    // Slider input delegation
    modal.addEventListener('input', (e) => {
      if (e.target.matches('.chain-slider')) {
        const catId = e.target.dataset.slider;
        _onSliderChange(catId, e.target.value);
      }
    });
  });
})();

/* outside click handler removed — pill strip has no dropdown to close */

function handleCollectionChange() { /* legacy no-op */ }

const typeOptions = document.querySelectorAll('.type-option');
typeOptions.forEach(opt => {
  opt.addEventListener('click', () => {
    typeOptions.forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
  });
});

const durOptions = document.querySelectorAll('.dur-option');
durOptions.forEach(opt => {
  opt.addEventListener('click', () => {
    durOptions.forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
  });
});

async function submitNewVideo(event) {
  event.preventDefault();
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'Creando…';

  const mode = document.querySelector('input[name="mode"]:checked').value;
  const video_type = document.querySelector('input[name="video_type"]:checked').value;
  const duration = document.querySelector('input[name="duration"]:checked').value;

  // Only include completed reference videos (status === 'ok')
  const completedRefs = referenceVideos.filter(v => v.status === 'ok');
  const reference_transcripts = completedRefs.length > 0
    ? JSON.stringify(completedRefs.map(v => ({ url: v.url, title: v.title, transcript: v.transcript })))
    : null;

  // Resolve collection value (stock mode only)
  let collection = 'general';
  if (mode === 'stock') {
    collection = _selectedCollection || 'general';
  }

  const payload = {
    title: document.getElementById('title').value.trim(),
    mode,
    video_type,
    duration,
    collection,
    reference_character: mode === 'animated' ? document.getElementById('referenceCharacter').value.trim() || null : null,
    reference_transcripts,
  };

  try {
    const project = await apiFetch('/api/projects/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    showToast(`Video "${project.title}" creado y en cola`, 'success');
    document.getElementById('newVideoForm').reset();
    document.getElementById('collectionGroup').style.display = 'none';
    _selectedCollection = 'general';
    renderCollectionPills();
    modeOptions[0].click(); // reset to animated
    typeOptions[0].click(); // reset to top10
    durOptions[0].click();  // reset to 6-8 min
    // Reset reference videos
    referenceVideos.length = 0;
    renderReferenceList();
    showView('detail', project.id);
  } catch (e) {
    showToast('Error al crear proyecto: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '🚀 Crear Video';
  }
}

// ── Chunk card toggle ─────────────────────────────────────────────────────
function toggleChunkCard(num) {
  const body = document.getElementById(`chunk-body-${num}`);
  const icon = document.getElementById(`toggle-${num}`);
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (open) _openChunks.add(num); else _openChunks.delete(num);
  if (icon) {
    icon.textContent = open ? '▲' : '▼';
    icon.classList.toggle('open', open);
  }
}

function toggleChunkPrompt(num) {
  const body = document.getElementById(`prompt-body-${num}`);
  const icon = document.getElementById(`prompt-toggle-${num}`);
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (open) _openPrompts.add(num); else _openPrompts.delete(num);
  if (icon) icon.textContent = open ? '▲' : '▼';
}

async function retryChunkImage(chunkNumber) {
  if (!currentProjectId) return;
  const btn = event.target;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Reintentando…';

  try {
    await apiFetch(`/api/projects/${currentProjectId}/retry-chunk-image/${chunkNumber}`, { method: 'POST' });
    showToast(`Reintentando imagen para escena #${chunkNumber}…`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al reintentar: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = origText;
  }
}

async function regenerateImageGenaipro(chunkNumber) {
  if (!currentProjectId) return;
  const btn = event.target;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Regenerando…';

  try {
    await apiFetch(`/api/projects/${currentProjectId}/scenes/${chunkNumber}/regenerate-genaipro`, { method: 'POST' });
    showToast(`⚡ Regenerando imagen de escena #${chunkNumber} con ${_imgProviderName()}…`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al regenerar: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// ── Image & Video Preview Modals ──────────────────────────────────────────
let _videoModalSceneNum = null;
let _isStockGlobal = false;

function openVideoPreview(url, sceneNum) {
  _videoModalSceneNum = sceneNum;
  const prompt = (_chunkData[sceneNum] || {}).motion_prompt || '';
  const modal = document.getElementById('videoPreviewModal');
  const player = document.getElementById('videoPreviewPlayer');
  const label = document.getElementById('videoModalLabel');
  const textarea = document.getElementById('videoModalPrompt');
  const saveBtn = document.getElementById('videoModalSaveBtn');
  const regenBtn = document.getElementById('videoModalRegenBtn');

  if (_isStockGlobal) {
    label.textContent = `Video — Escena #${sceneNum}`;
    textarea.style.display = 'none';
    if (saveBtn) saveBtn.style.display = 'none';
    if (regenBtn) regenBtn.style.display = 'none';
  } else {
    label.textContent = `Prompt de animación — Escena #${sceneNum}`;
    textarea.value = prompt;
    textarea.style.display = '';
    if (saveBtn) saveBtn.style.display = '';
    if (regenBtn) regenBtn.style.display = '';
  }

  // Show modal and stop polling
  modal.style.display = '';
  stopPolling();

  // Simple direct src — no blob, no fetch
  player.src = url;
  player.play().catch(() => {});
}

async function saveMotionPromptFromModal() {
  if (!currentProjectId || !_videoModalSceneNum) return;
  const textarea = document.getElementById('videoModalPrompt');
  if (!textarea) return;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/chunk/${_videoModalSceneNum}/motion-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ motion_prompt: textarea.value })
    });
    showToast(`Prompt de animación #${_videoModalSceneNum} guardado`, 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function saveAndRegenerateVideo() {
  if (!currentProjectId || !_videoModalSceneNum) return;
  const textarea = document.getElementById('videoModalPrompt');
  if (textarea) {
    try {
      await apiFetch(`/api/projects/${currentProjectId}/chunk/${_videoModalSceneNum}/motion-prompt`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ motion_prompt: textarea.value })
      });
    } catch (e) { /* prompt save failed, still try regenerate */ }
  }
  closeVideoModal();
  try {
    await apiFetch(`/api/projects/${currentProjectId}/start-animation`, { method: 'POST' });
    showToast(`🔄 Re-animando escena #${_videoModalSceneNum} con Meta AI…`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 5000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

function closeVideoModal(e) {
  if (e && e.target !== e.currentTarget && !e.target.classList.contains('video-modal-close')) return;
  const modal = document.getElementById('videoPreviewModal');
  const player = document.getElementById('videoPreviewPlayer');
  player.pause();
  player.removeAttribute('src');
  modal.style.display = 'none';
  // Resume polling after closing the modal
  if (currentProjectId) {
    stopPolling();
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 8000);
  }
}

function openImagePreview(url, sceneNum) {
  const data = _chunkData[sceneNum] || {};
  const prompt = data.image_prompt || '';
  const sceneText = data.scene_text || '';
  const displayPrompt = prompt || sceneText;
  const missingPrompt = !prompt && sceneText;
  const overlay = document.createElement('div');
  overlay.className = 'image-modal';

  const promptSection = _isStockGlobal ? '' : `
      <div class="modal-prompt-section">
        <label class="modal-prompt-label">Prompt de imagen — Escena #${sceneNum}${missingPrompt ? ' <span style="color:var(--yellow);font-weight:400">(usando texto de escena — sin prompt guardado)</span>' : ''}</label>
        <textarea id="modal_image_prompt_${sceneNum}" class="modal-prompt-textarea" rows="3">${escHtml(displayPrompt)}</textarea>
        <div class="modal-prompt-actions">
          <button class="btn btn-ghost btn-sm" onclick="saveImagePromptFromModal(${sceneNum})">💾 Guardar prompt</button>
          <button class="btn btn-primary btn-sm" onclick="saveAndRegenerateImage(${sceneNum})">🔄 Regenerar imagen</button>
        </div>
      </div>`;

  overlay.innerHTML = `
    <div class="media-modal-content" onclick="event.stopPropagation()">
      <button class="video-modal-close" onclick="this.closest('.image-modal').remove()">&times;</button>
      <img src="${url}" alt="Escena #${sceneNum}" style="width:100%;border-radius:8px;" />
      ${promptSection}
    </div>
  `;
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  document.body.appendChild(overlay);
}

async function saveImagePromptFromModal(chunkNumber) {
  if (!currentProjectId) return;
  const textarea = document.getElementById(`modal_image_prompt_${chunkNumber}`);
  if (!textarea) return;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/chunk/${chunkNumber}/image-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_prompt: textarea.value })
    });
    showToast(`Prompt de imagen #${chunkNumber} guardado`, 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function saveAndRegenerateImage(chunkNumber) {
  if (!currentProjectId) return;
  const textarea = document.getElementById(`modal_image_prompt_${chunkNumber}`);
  if (textarea) {
    try {
      await apiFetch(`/api/projects/${currentProjectId}/chunk/${chunkNumber}/image-prompt`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_prompt: textarea.value })
      });
    } catch (e) { /* prompt save failed, still try regenerate */ }
  }
  // Close modal
  document.querySelectorAll('.image-modal').forEach(m => m.remove());
  // Regenerate
  try {
    await apiFetch(`/api/projects/${currentProjectId}/scenes/${chunkNumber}/regenerate-genaipro`, { method: 'POST' });
    showToast(`🔄 Regenerando imagen #${chunkNumber}…`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al regenerar: ' + e.message, 'error');
  }
}

// Close video modal with Escape key
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const vm = document.getElementById('videoPreviewModal');
    if (vm && vm.style.display !== 'none') closeVideoModal();
    document.querySelectorAll('.image-modal').forEach(m => m.remove());
  }
});

async function retryMetaAnimation(chunkNumber) {
  if (!currentProjectId) return;
  try {
    // Clear video_path and re-trigger animation for this chunk
    await apiFetch(`/api/projects/${currentProjectId}/start-animation`, { method: 'POST' });
    showToast(`Re-animando escena #${chunkNumber} con Meta AI…`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 5000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function regenerateAllGenaipro() {
  if (!currentProjectId) return;
  const btn = document.getElementById('regenAllGenaipro');
  const origText = btn ? btn.textContent : '⚠️ Regenerar TODAS las imágenes (Imagen 4)';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando regeneración masiva…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/regenerate-all-genaipro`, { method: 'POST' });
    showToast(`⚡ Regenerando TODAS las imágenes con ${_imgProviderName()} en segundo plano. Revisa los logs.`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

// ── Reference Images (Character + Style) ──────────────────────────────────
async function uploadReferenceCharacter(input) {
  if (!currentProjectId || !input.files[0]) return;
  const formData = new FormData();
  formData.append('file', input.files[0]);
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-character`, {
      method: 'POST', body: formData, isFormData: true,
    });
    showToast('Imagen de personaje subida', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al subir personaje: ' + e.message, 'error');
  }
  input.value = '';
}

async function deleteReferenceCharacter() {
  if (!currentProjectId) return;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-character`, { method: 'DELETE' });
    showToast('Imagen de personaje eliminada', 'info');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function uploadReferenceStyle(input) {
  if (!currentProjectId || !input.files[0]) return;
  const formData = new FormData();
  formData.append('file', input.files[0]);
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-style`, {
      method: 'POST', body: formData, isFormData: true,
    });
    showToast('Imagen de estilo subida', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al subir estilo: ' + e.message, 'error');
  }
  input.value = '';
}

async function deleteReferenceStyle() {
  if (!currentProjectId) return;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-style`, { method: 'DELETE' });
    showToast('Imagen de estilo eliminada', 'info');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ── Client-side sentence-break splitter (mirrors Python logic) ────────────
function _findSentenceBreak(text, target) {
  const start = Math.min(target, text.length - 1);
  for (let i = start; i >= Math.max(start - 400, 0); i--) {
    if ('.?!'.includes(text[i]) && (i + 1 >= text.length || ' \n\t\r'.includes(text[i + 1]))) {
      return i + 1;
    }
  }
  for (let i = start; i < Math.min(start + 400, text.length); i++) {
    if ('.?!'.includes(text[i]) && (i + 1 >= text.length || ' \n\t\r'.includes(text[i + 1]))) {
      return i + 1;
    }
  }
  return target;
}

function countChunks(text, targetSize) {
  let count = 0;
  let remaining = text.trim();
  while (remaining.length > 0) {
    count++;
    if (remaining.length <= targetSize) break;
    const breakAt = _findSentenceBreak(remaining, targetSize);
    remaining = remaining.substring(breakAt).trim();
  }
  return count;
}

function updateChunkPreview() {
  const ta = document.getElementById('approvalTextarea');
  const sizeInput = document.getElementById('chunkSizeInput');
  const preview = document.getElementById('chunkPreview');
  if (!ta || !sizeInput || !preview) return;
  const text = ta.value.trim();
  const size = parseInt(sizeInput.value, 10) || 1500;
  if (!text) { preview.textContent = ''; return; }
  const n = countChunks(text, size);
  preview.textContent = `→ ~${n} chunk${n !== 1 ? 's' : ''} para este script`;
}

// ── Script Approval ───────────────────────────────────────────────────────

// Mark textarea as user-edited so polling won't overwrite the content
document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('approvalTextarea');
  if (ta) {
    ta.addEventListener('input', () => { ta.dataset.edited = '1'; });
  }
});

async function approveScript() {
  if (!currentProjectId) return;
  const ta = document.getElementById('approvalTextarea');
  const script_final = ta.value.trim();
  if (!script_final) {
    showToast('El script no puede estar vacío.', 'error');
    return;
  }
  const sizeInput = document.getElementById('chunkSizeInput');
  const target_chunk_size = parseInt(sizeInput?.value || '1500', 10) || 1500;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/approve-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script_final, target_chunk_size }),
    });
    ta.dataset.edited = '';
    showToast('Script aprobado. Dividiendo en chunks…', 'success');
    // Resume polling
    if (!pollInterval) {
      pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
    }
    refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al aprobar: ' + e.message, 'error');
  }
}

async function resplitChunks() {
  if (!currentProjectId) return;
  const input = document.getElementById('resplitInput');
  const target_chunk_size = parseInt(input?.value || '1500', 10) || 1500;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/resplit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_chunk_size }),
    });
    showToast(`Re-dividiendo en chunks de ${target_chunk_size} chars…`, 'info');
    setTimeout(() => refreshDetail(currentProjectId), 1200);
  } catch (e) {
    showToast('Error al re-dividir: ' + e.message, 'error');
  }
}

async function regenerateScript() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Regenerar el script desde el outline actual? Se perderá el script actual.')) return;
  try {
    const ta = document.getElementById('approvalTextarea');
    ta.dataset.edited = '';
    await apiFetch(`/api/projects/${currentProjectId}/regenerate-script`, { method: 'POST' });
    showToast('Regenerando script…', 'info');
    if (!pollInterval) {
      pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
    }
    refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al regenerar: ' + e.message, 'error');
  }
}

// ── Edit Script Modal ──────────────────────────────────────────────────────

function openEditScriptModal() {
  const modal = document.getElementById('editScriptModal');
  if (!modal) return;
  modal.style.display = '';
  const ta = document.getElementById('editScriptPrompt');
  if (ta) { ta.value = ''; ta.focus(); }
}

function closeEditScriptModal() {
  const modal = document.getElementById('editScriptModal');
  if (modal) modal.style.display = 'none';
}

function setEditPrompt(text) {
  const ta = document.getElementById('editScriptPrompt');
  if (ta) { ta.value = text; ta.focus(); }
}

async function sendEditScriptPrompt() {
  if (!currentProjectId) return;
  const ta = document.getElementById('editScriptPrompt');
  const prompt = ta?.value?.trim();
  if (!prompt) { showToast('Escribe una instrucción primero.', 'error'); return; }

  const btn = document.getElementById('editScriptSendBtn');
  const btnText = document.getElementById('editScriptBtnText');
  if (btn) btn.disabled = true;
  if (btnText) btnText.textContent = '⏳ Procesando…';

  try {
    const data = await apiFetch(`/api/projects/${currentProjectId}/edit-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const scriptTA = document.getElementById('approvalTextarea');
    if (scriptTA) {
      scriptTA.value = data.script || '';
      scriptTA.dataset.edited = '1';
      updateChunkPreview();
      updateWordCount();
    }
    closeEditScriptModal();
    showToast('Guion actualizado por Claude ✓', 'success');
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
    if (btnText) btnText.textContent = '✨ Enviar a Claude';
  }
}

// ── Copy script text ───────────────────────────────────────────────────────

async function copyScriptText() {
  const ta = document.getElementById('approvalTextarea');
  const content = document.getElementById('scriptContent');
  const text = (ta && ta.style.display !== 'none') ? ta.value : (content ? content.textContent : '');
  if (!text.trim()) { showToast('No hay texto para copiar', 'error'); return; }
  try {
    await navigator.clipboard.writeText(text);
    showToast('Texto copiado', 'success');
  } catch (_) {
    showToast('Error al copiar', 'error');
  }
}

// ── Word count ─────────────────────────────────────────────────────────────

function updateWordCount() {
  const ta = document.getElementById('approvalTextarea');
  const countEl = document.getElementById('wordCountVal');
  const charEl = document.getElementById('charCountVal');
  const container = document.getElementById('scriptWordCount');
  if (!ta || !countEl) return;
  const text = ta.value.trim();
  const words = text ? text.split(/\s+/).length : 0;
  const chars = ta.value.length;
  countEl.textContent = words.toLocaleString();
  if (charEl) charEl.textContent = chars.toLocaleString();
  if (container) container.style.display = ta.style.display === 'none' ? 'none' : '';
}

// ── Actions ───────────────────────────────────────────────────────────────
async function deleteProject(id) {
  if (!await showConfirm('¿Borrar este proyecto? Esta acción es irreversible.', 'Borrar')) return;
  try {
    await apiFetch(`/api/projects/${id}`, { method: 'DELETE' });
    showToast('Proyecto borrado', 'info');
    loadDashboard();
  } catch (e) {
    showToast('Error al borrar: ' + e.message, 'error');
  }
}

async function retryProject(id) {
  try {
    await apiFetch(`/api/projects/${id}/retry`, { method: 'POST' });
    showToast('Reintentando…', 'info');
    openDetail(id);
  } catch (e) {
    showToast('Error al reintentar: ' + e.message, 'error');
  }
}

// ── Worker status ─────────────────────────────────────────────────────────
async function updateWorkerStatus() {
  try {
    const workers = await apiFetch('/api/workers/');
    const active = workers.filter(w => w.status === 'busy').length;
    document.getElementById('workerCount').textContent = active;
    const dot = document.querySelector('.worker-dot');
    dot.className = `worker-dot ${active > 0 ? 'active' : 'idle'}`;
  } catch (e) { /* ignore */ }
}

// Refresh worker status every 5s
setInterval(updateWorkerStatus, 5000);

// ── Utilities ─────────────────────────────────────────────────────────────
async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const d = await res.json(); msg = d.detail || JSON.stringify(d); } catch { }
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Generic Confirm Modal ─────────────────────────────────────────────────
let _confirmResolve = null;

function showConfirm(message, okLabel = 'Aceptar') {
  return new Promise(resolve => {
    _confirmResolve = resolve;
    document.getElementById('confirmModalMsg').textContent = message;
    document.getElementById('confirmModalOk').textContent = okLabel;
    document.getElementById('confirmModal').style.display = 'flex';
  });
}

function _confirmOk() {
  document.getElementById('confirmModal').style.display = 'none';
  if (_confirmResolve) { _confirmResolve(true); _confirmResolve = null; }
}

function _confirmCancel() {
  document.getElementById('confirmModal').style.display = 'none';
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
}

function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'fadeOut 0.4s ease forwards';
    setTimeout(() => toast.remove(), 400);
  }, duration);
}

// ── Voice Config ──────────────────────────────────────────────────────────

// ── State ──────────────────────────────────────────────────────────────────
let _allVoices = [];       // raw from API
let _filteredVoices = [];  // after search + filters
let _selectedVoice = { voice_id: '', name: 'Andrew - Smooth, Smart and Clear', description: '', language: '', accent: '', gender: '', age: '', category: '', preview_url: '' };
let _favorites = new Set(JSON.parse(localStorage.getItem('voiceFavorites') || '[]'));
let _activeFilters = {};
let _trendingOrder = [];   // original API order = trending
let _previewAudio = null;  // HTMLAudio for voice preview
let _previewVoiceId = '';  // which voice is currently previewing

// ── Waveform player state ──────────────────────────────────────────────────
let _waveAudio = null;      // HTMLAudio element
let _waveCtx = null;        // AudioContext
let _waveAnalyser = null;
let _waveBuffer = null;     // decoded AudioBuffer
let _waveRafId = null;
let _waveMode = 'test';     // 'test' | 'approval'
let _approvalWaveAudio = null;
let _approvalWaveBuffer = null;
let _approvalRafId = null;

// ── Language code → full name map ──────────────────────────────────────────
const LANG_NAMES = {
  af:'Afrikaans', ar:'Arabic', bg:'Bulgarian', bn:'Bengali', ca:'Catalan',
  cs:'Czech', cy:'Welsh', da:'Danish', de:'German', el:'Greek',
  en:'English', es:'Spanish', et:'Estonian', fa:'Persian', fi:'Finnish',
  fil:'Filipino', fr:'French', gl:'Galician', gu:'Gujarati', he:'Hebrew',
  hi:'Hindi', hr:'Croatian', hu:'Hungarian', hy:'Armenian', id:'Indonesian',
  is:'Icelandic', it:'Italian', ja:'Japanese', ka:'Georgian', kn:'Kannada',
  ko:'Korean', lt:'Lithuanian', lv:'Latvian', mk:'Macedonian', ml:'Malayalam',
  mr:'Marathi', ms:'Malay', mt:'Maltese', nl:'Dutch', no:'Norwegian',
  pa:'Punjabi', pl:'Polish', pt:'Portuguese', ro:'Romanian', ru:'Russian',
  sk:'Slovak', sl:'Slovenian', sq:'Albanian', sr:'Serbian', sv:'Swedish',
  sw:'Swahili', ta:'Tamil', te:'Telugu', th:'Thai', tl:'Filipino',
  tr:'Turkish', uk:'Ukrainian', ur:'Urdu', vi:'Vietnamese',
  zh:'Chinese', 'zh-cn':'Chinese (Simplified)', 'zh-tw':'Chinese (Traditional)',
};

let _langOptions = []; // [{code, name}] populated from _allVoices

// ── Voice normalizer ───────────────────────────────────────────────────────
function normalizeVoice(v) {
  const labels = v.labels || {};
  return {
    voice_id:    v.voice_id || v.id || '',
    name:        v.name || '',
    description: v.description || labels.description || '',
    language:    v.language   || labels.language   || '',
    accent:      v.accent     || labels.accent      || '',
    gender:      (v.gender    || labels.gender      || '').toLowerCase(),
    age:         (v.age       || labels.age         || '').toLowerCase(),
    category:    (v.category  || labels.use_case    || '').toLowerCase().replace(/[\s&]+/g, '_'),
    preview_url: v.preview_url || v.preview || '',
    high_quality: Array.isArray(v.high_quality_base_model_ids) && v.high_quality_base_model_ids.length > 0,
    notice_period: v.notice_period || labels.notice_period || '',
    live_moderation: v.live_moderation != null ? !!v.live_moderation : true,
    _raw: v,
  };
}

// ── Init ───────────────────────────────────────────────────────────────────
async function initVoiceConfig() {
  updateVoiceCard();
  await loadVoicesFromServer();
}

async function loadVoicesFromServer() {
  try {
    const data = await apiFetch('/api/tts/voices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tts_provider: 'genaipro', tts_api_key: '' }),
    });
    const raw = data.voices || [];
    _allVoices = raw.map(normalizeVoice);
    _trendingOrder = _allVoices.map((_, i) => i);

    if (!_selectedVoice.voice_id) {
      const andrew = _allVoices.find(v => v.name.toLowerCase().includes('andrew'));
      const def = andrew || _allVoices[0];
      if (def) _selectedVoice = def;
    }
    updateVoiceCard();
    _filteredVoices = [..._allVoices];
    populateFilterDropdowns();
  } catch (e) {
    console.warn('Could not load voices:', e.message);
  }
}

function populateFilterDropdowns() {
  const langs = [...new Set(_allVoices.map(v => v.language).filter(Boolean))].sort();
  _langOptions = langs.map(code => ({ code, name: LANG_NAMES[code] || code }));
  _langOptions.sort((a, b) => a.name.localeCompare(b.name));
  _updateAccentDropdown(''); // show all accents initially
}

function _renderLangDropdown(query) {
  const dropdown = document.getElementById('vfLangDropdown');
  if (!dropdown) return;
  const q = (query || '').toLowerCase().trim();
  const matches = q
    ? _langOptions.filter(l => l.name.toLowerCase().includes(q) || l.code.toLowerCase().includes(q))
    : _langOptions;
  if (!matches.length) { dropdown.style.display = 'none'; return; }
  dropdown.innerHTML = matches.map(l =>
    `<li data-code="${l.code}" onmousedown="_selectLangOption('${l.code}','${l.name.replace(/'/g,"\\'")}')">
       <span class="vf-lang-name">${l.name}</span>
       <span class="vf-lang-code">${l.code}</span>
     </li>`
  ).join('');
  dropdown.style.display = 'block';
}

function _hideLangDropdown() {
  const dropdown = document.getElementById('vfLangDropdown');
  if (dropdown) dropdown.style.display = 'none';
  // If text input doesn't match a selected lang, clear it
  const input = document.getElementById('vfLanguageInput');
  const hidden = document.getElementById('vfLanguage');
  if (input && hidden && !hidden.value) input.value = '';
}

function _selectLangOption(code, name) {
  const input = document.getElementById('vfLanguageInput');
  const hidden = document.getElementById('vfLanguage');
  if (input) input.value = name;
  if (hidden) hidden.value = code;
  _hideLangDropdown();
  _updateAccentDropdown(code);
}

function _clearLang() {
  const input = document.getElementById('vfLanguageInput');
  const hidden = document.getElementById('vfLanguage');
  if (input) input.value = '';
  if (hidden) hidden.value = '';
  _updateAccentDropdown('');
}

function _updateAccentDropdown(langCode) {
  const voices = langCode ? _allVoices.filter(v => v.language === langCode) : _allVoices;
  const accents = [...new Set(voices.map(v => v.accent).filter(Boolean))].sort();
  const sel = document.getElementById('vfAccent');
  if (!sel) return;
  sel.innerHTML = '<option value="">Select accent</option>';
  accents.forEach(a => {
    const o = document.createElement('option');
    o.value = a;
    o.textContent = a.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    sel.appendChild(o);
  });
}

// ── Voice Card ─────────────────────────────────────────────────────────────
function updateVoiceCard() {
  const nameEl = document.getElementById('voiceCardName');
  const descEl = document.getElementById('voiceCardDesc');
  const favBtn = document.getElementById('voiceFavBtn');
  if (nameEl) nameEl.textContent = _selectedVoice.name || '—';
  if (descEl) descEl.textContent = _selectedVoice.description || (_selectedVoice.language ? `${_selectedVoice.language} · ${_selectedVoice.accent || ''}`.trim().replace(/·\s*$/, '') : 'Sin descripción');
  if (favBtn) favBtn.textContent = _favorites.has(_selectedVoice.voice_id) ? '♥' : '♡';
}

function toggleFavoriteSelected() {
  const vid = _selectedVoice.voice_id;
  if (!vid) return;
  if (_favorites.has(vid)) _favorites.delete(vid); else _favorites.add(vid);
  localStorage.setItem('voiceFavorites', JSON.stringify([..._favorites]));
  updateVoiceCard();
  // refresh modal list if open
  if (document.getElementById('voiceModal')?.style.display !== 'none') renderVoiceModalList(_filteredVoices);
}

// ── Voice Modal ────────────────────────────────────────────────────────────
function openVoiceModal() {
  const modal = document.getElementById('voiceModal');
  if (!modal) return;
  modal.style.display = '';
  const search = document.getElementById('vmSearch');
  if (search) search.value = '';
  _filteredVoices = [..._allVoices];
  renderVoiceModalList(_filteredVoices);
  if (search) search.focus();
}

function closeVoiceModal() {
  const modal = document.getElementById('voiceModal');
  if (modal) modal.style.display = 'none';
  stopPreview();
}

function renderVoiceModalList(voices) {
  const list = document.getElementById('vmVoiceList');
  if (!list) return;
  if (!voices.length) {
    list.innerHTML = '<div class="vm-empty">No se encontraron voces.</div>';
    return;
  }
  list.innerHTML = '';
  const frag = document.createDocumentFragment();
  voices.forEach(v => frag.appendChild(_buildVoiceRow(v)));
  list.appendChild(frag);
}

function _buildVoiceRow(v) {
  const isFav = _favorites.has(v.voice_id);
  const isSelected = v.voice_id === _selectedVoice.voice_id;
  const isPreviewing = v.voice_id === _previewVoiceId;
  const row = document.createElement('div');
  row.className = 'vm-row' + (isSelected ? ' vm-row--selected' : '') + (isPreviewing ? ' vm-row--previewing' : '');
  row.dataset.voiceId = v.voice_id;

  const chips = [v.language, v.accent].filter(Boolean).map(c => `<span class="vm-chip">${escHtml(c)}</span>`).join('');
  const catLabel = v.category ? `<span class="vm-chip vm-chip--cat">${escHtml(categoryLabel(v.category))}</span>` : '';

  row.innerHTML = `
    <div class="vm-row-main" onclick="previewVoiceRow(${escAttr(JSON.stringify(v))})">
      <div class="vm-row-name">${escHtml(v.name)}</div>
      <div class="vm-row-desc">${escHtml(v.description || '')}</div>
      <div class="vm-row-chips">${chips}${catLabel}</div>
    </div>
    <div class="vm-row-actions">
      <button class="vm-icon-btn vm-icon-btn--select${isSelected ? ' vm-icon-btn--selected' : ''}" title="Seleccionar voz" onclick="event.stopPropagation();selectVoice(${escAttr(JSON.stringify(v))})">↩</button>
      <button class="vm-icon-btn${isFav ? ' vm-icon-btn--fav' : ''}" title="Favorito" onclick="event.stopPropagation();toggleFavoriteModal('${escHtml(v.voice_id)}',this)">${isFav ? '♥' : '♡'}</button>
      <button class="vm-icon-btn" title="Copiar Voice ID" onclick="event.stopPropagation();copyVoiceId('${escHtml(v.voice_id)}',this)">📋</button>
    </div>
  `;
  return row;
}

function categoryLabel(cat) {
  const MAP = {
    narrative_story: 'Narrative & Story', conversational: 'Conversational',
    characters_animation: 'Characters & Animation', social_media: 'Social Media',
    entertainment_tv: 'Entertainment & TV', advertisement: 'Advertisement',
    informative_educational: 'Informative & Educational',
  };
  return MAP[cat] || cat;
}

function escAttr(str) { return str.replace(/"/g, '&quot;'); }

function selectVoice(voice) {
  _selectedVoice = typeof voice === 'string' ? JSON.parse(voice) : voice;
  updateVoiceCard();
  closeVoiceModal();
  showToast(`Voz seleccionada: ${_selectedVoice.name}`, 'success');
}

// ── Search & Sort ──────────────────────────────────────────────────────────
let _searchDebounce = null;
let _serverSearchVersion = 0;  // race-condition guard

function searchVoices(query) {
  const q = query.toLowerCase().trim();
  clearTimeout(_searchDebounce);

  if (!q) {
    _filteredVoices = [..._allVoices];
    renderVoiceModalList(_filteredVoices);
    return;
  }

  // Local filter first
  const local = _allVoices.filter(v =>
    v.name.toLowerCase().includes(q) ||
    v.voice_id.toLowerCase().includes(q) ||
    (v.description || '').toLowerCase().includes(q)
  );

  if (local.length > 0) {
    _filteredVoices = _applyFilterMap(local, _activeFilters);
    renderVoiceModalList(_filteredVoices);
    return;
  }

  // No local results — search server (debounced)
  if (q.length >= 3) {
    const list = document.getElementById('vmVoiceList');
    if (list) list.innerHTML = '<div class="vm-empty">Buscando en servidor...</div>';
    _searchDebounce = setTimeout(() => _searchVoicesServer(q), 500);
  } else {
    renderVoiceModalList([]);
  }
}

async function _searchVoicesServer(query) {
  const version = ++_serverSearchVersion;
  console.log('[VoiceSearch] Server search:', query);
  try {
    const data = await apiFetch('/api/tts/voices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tts_provider: 'genaipro', tts_api_key: '', search: query }),
    });
    // Ignore stale responses
    if (version !== _serverSearchVersion) return;

    const raw = data.voices || [];
    console.log('[VoiceSearch] Server returned:', raw.length, 'voices');
    if (!raw.length) {
      const list = document.getElementById('vmVoiceList');
      if (list) list.innerHTML = '<div class="vm-empty">No se encontraron voces.</div>';
      return;
    }
    const newVoices = raw.map(normalizeVoice);
    // Merge into _allVoices so subsequent local searches find them
    for (const v of newVoices) {
      if (!_allVoices.find(e => e.voice_id === v.voice_id)) {
        _allVoices.push(v);
      }
    }
    _filteredVoices = newVoices;
    renderVoiceModalList(_filteredVoices);
  } catch (e) {
    console.warn('[VoiceSearch] Server search failed:', e.message);
    if (version !== _serverSearchVersion) return;
    const list = document.getElementById('vmVoiceList');
    if (list) list.innerHTML = '<div class="vm-empty">Error buscando voces.</div>';
  }
}

function sortTrending() {
  _filteredVoices = [..._filteredVoices].sort((a, b) => {
    const ia = _allVoices.findIndex(v => v.voice_id === a.voice_id);
    const ib = _allVoices.findIndex(v => v.voice_id === b.voice_id);
    return ia - ib;
  });
  renderVoiceModalList(_filteredVoices);
}

// ── Favorites ──────────────────────────────────────────────────────────────
function toggleFavoriteModal(voiceId, btn) {
  if (_favorites.has(voiceId)) { _favorites.delete(voiceId); btn.textContent = '♡'; btn.classList.remove('vm-icon-btn--fav'); }
  else { _favorites.add(voiceId); btn.textContent = '♥'; btn.classList.add('vm-icon-btn--fav'); }
  localStorage.setItem('voiceFavorites', JSON.stringify([..._favorites]));
  if (_selectedVoice.voice_id === voiceId) updateVoiceCard();
}

// ── Copy Voice ID ──────────────────────────────────────────────────────────
function copyVoiceId(voiceId, btn) {
  navigator.clipboard.writeText(voiceId).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }).catch(() => showToast('No se pudo copiar', 'error'));
}

// ── Voice Preview ──────────────────────────────────────────────────────────

// Preview bar state
let _pbAudio = null;       // HTMLAudio for preview bar
let _pbVoice = null;       // voice object currently in bar
let _pbBuffer = null;      // decoded AudioBuffer (for waveform)
let _pbRafId = null;       // requestAnimationFrame id

function previewVoiceRow(voice) {
  const v = typeof voice === 'string' ? JSON.parse(voice) : voice;
  // Toggle: click same voice = pause/play
  if (_previewVoiceId === v.voice_id && _pbAudio) {
    if (_pbAudio.paused) { _pbAudio.play().catch(() => {}); }
    else { _pbAudio.pause(); }
    return;
  }
  stopPreview();
  if (!v.preview_url) {
    showToast('Esta voz no tiene muestra de audio disponible.', 'info');
    return;
  }
  _previewVoiceId = v.voice_id;
  _pbVoice = v;
  _pbAudio = new Audio(v.preview_url);
  _pbAudio.crossOrigin = 'anonymous';

  // Update row highlights
  document.querySelectorAll('.vm-row--previewing').forEach(r => r.classList.remove('vm-row--previewing'));
  const activeRow = document.querySelector(`.vm-row[data-voice-id="${CSS.escape(v.voice_id)}"]`);
  if (activeRow) activeRow.classList.add('vm-row--previewing');

  // Show bar immediately (before audio loads)
  showPreviewBar(v);

  _pbAudio.onplay  = () => { _updatePbBtn(true);  _startPbProgress(); };
  _pbAudio.onpause = () => { _updatePbBtn(false); cancelAnimationFrame(_pbRafId); };
  _pbAudio.onended = () => {
    _updatePbBtn(false);
    cancelAnimationFrame(_pbRafId);
    _previewVoiceId = '';
    document.querySelectorAll('.vm-row--previewing').forEach(r => r.classList.remove('vm-row--previewing'));
    _drawPbWave(0); // reset to full unplayed
  };

  _pbAudio.play().catch(e => showToast('No se pudo reproducir preview: ' + e.message, 'error'));

  // Async: fetch + decode for waveform (non-blocking)
  fetch(v.preview_url)
    .then(r => r.arrayBuffer())
    .then(buf => {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      return ctx.decodeAudioData(buf).then(ab => { ctx.close(); return ab; });
    })
    .then(ab => { _pbBuffer = ab; _drawPbWave(_pbAudio ? _pbAudio.currentTime / (_pbAudio.duration || 1) : 0); })
    .catch(() => {}); // waveform is optional, don't break on error
}

function showPreviewBar(voice) {
  const bar = document.getElementById('vmPreviewBar');
  if (!bar) return;
  bar.style.display = '';
  const nameEl = document.getElementById('vmPbName');
  const langEl = document.getElementById('vmPbLang');
  if (nameEl) nameEl.textContent = voice.name || '—';
  if (langEl) langEl.textContent = [voice.language, voice.accent].filter(Boolean).join(' · ');
  _pbBuffer = null; // reset until async decode finishes
  _drawPbWave(0);
}

function togglePreviewBarPlayback() {
  if (!_pbAudio) return;
  if (_pbAudio.paused) _pbAudio.play().catch(() => {}); else _pbAudio.pause();
}

function seekPreview(seconds) {
  if (!_pbAudio || !_pbAudio.duration) return;
  _pbAudio.currentTime = Math.max(0, Math.min(_pbAudio.duration, _pbAudio.currentTime + seconds));
}

function seekPreviewByClick(event, canvas) {
  if (!_pbAudio || !_pbAudio.duration) return;
  const pct = event.offsetX / canvas.offsetWidth;
  _pbAudio.currentTime = pct * _pbAudio.duration;
}

function _updatePbBtn(playing) {
  const btn = document.getElementById('vmPbPlayBtn');
  if (btn) btn.textContent = playing ? '⏸' : '▶';
}

function _startPbProgress() {
  cancelAnimationFrame(_pbRafId);
  const tick = () => {
    if (!_pbAudio || _pbAudio.paused) return;
    const prog = _pbAudio.duration ? _pbAudio.currentTime / _pbAudio.duration : 0;
    _drawPbWave(prog);
    const fmt = s => { const m = Math.floor(s/60); return `${m}:${String(Math.floor(s%60)).padStart(2,'0')}`; };
    const tl = document.getElementById('vmPbTimeLeft');
    const tr = document.getElementById('vmPbTimeRight');
    if (tl) tl.textContent = fmt(_pbAudio.currentTime);
    if (tr) tr.textContent = fmt(_pbAudio.duration || 0);
    _pbRafId = requestAnimationFrame(tick);
  };
  _pbRafId = requestAnimationFrame(tick);
}

function _drawPbWave(progress) {
  const canvas = document.getElementById('vmPbCanvas');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth || 800;
  const H = canvas.offsetHeight || 48;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const barW = 2;
  const gap  = 1.5;
  const cols = Math.floor(W / (barW + gap));
  const playedColor   = '#6c63ff';
  const unplayedColor = '#3a3a55';

  if (_pbBuffer) {
    // Real waveform from decoded audio
    const data = _pbBuffer.getChannelData(0);
    const samplesPerCol = Math.ceil(data.length / cols);
    for (let i = 0; i < cols; i++) {
      let max = 0;
      const start = i * samplesPerCol;
      for (let j = start; j < start + samplesPerCol && j < data.length; j++) {
        const vv = Math.abs(data[j]);
        if (vv > max) max = vv;
      }
      const barH = Math.max(3, max * H * 0.85);
      const x = i * (barW + gap);
      const y = (H - barH) / 2;
      ctx.fillStyle = (i / cols) < progress ? playedColor : unplayedColor;
      ctx.beginPath(); ctx.roundRect(x, y, barW, barH, 1); ctx.fill();
    }
  } else {
    // Placeholder: pseudo-random looking waveform
    const seed = (_pbVoice?.voice_id || 'x').split('').reduce((a,c) => a + c.charCodeAt(0), 0);
    for (let i = 0; i < cols; i++) {
      const pseudo = Math.abs(Math.sin((i + seed) * 0.4) * 0.5 + Math.sin((i + seed) * 1.1) * 0.3 + 0.2);
      const barH = Math.max(3, pseudo * H * 0.85);
      const x = i * (barW + gap);
      const y = (H - barH) / 2;
      ctx.fillStyle = (i / cols) < progress ? playedColor : unplayedColor;
      ctx.beginPath(); ctx.roundRect(x, y, barW, barH, 1); ctx.fill();
    }
  }
}

function stopPreview() {
  if (_pbAudio) { _pbAudio.pause(); _pbAudio = null; }
  cancelAnimationFrame(_pbRafId);
  _previewVoiceId = '';
  _pbVoice = null;
  _pbBuffer = null;
  document.querySelectorAll('.vm-row--previewing').forEach(r => r.classList.remove('vm-row--previewing'));
  const bar = document.getElementById('vmPreviewBar');
  if (bar) bar.style.display = 'none';
}

// ── Filter Modal ───────────────────────────────────────────────────────────
function openFilterModal() {
  const modal = document.getElementById('voiceFilterModal');
  if (modal) modal.style.display = '';
}

function closeFilterModal() {
  const modal = document.getElementById('voiceFilterModal');
  if (modal) modal.style.display = 'none';
}

function toggleFilterChip(btn) {
  btn.classList.toggle('vf-chip--active');
}

function selectToggle(btn) {
  const group = btn.dataset.group;
  document.querySelectorAll(`.vf-toggle[data-group="${group}"]`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function applyFilters() {
  const filters = {};

  // Language (sent to server)
  const lang = document.getElementById('vfLanguage')?.value;
  if (lang) filters.language = lang;

  // Accent (client-side)
  const accent = document.getElementById('vfAccent')?.value;
  if (accent) filters.accent = accent;

  // Category chips (client-side)
  const cats = [...document.querySelectorAll('#vfCategoryChips .vf-chip--active')].map(b => b.dataset.val);
  if (cats.length) filters.categories = cats;

  // Toggle groups
  const toggleVal = (group) => document.querySelector(`.vf-toggle.active[data-group="${group}"]`)?.dataset.val || '';
  const quality = toggleVal('quality');
  if (quality) filters.quality = quality;
  const gender = toggleVal('gender'); // sent to server
  if (gender) filters.gender = gender;
  const age = toggleVal('age');
  if (age) filters.age = age;

  _activeFilters = filters;
  const query = document.getElementById('vmSearch')?.value || '';
  const base = query
    ? _allVoices.filter(v => v.name.toLowerCase().includes(query.toLowerCase()) || v.voice_id.toLowerCase().includes(query.toLowerCase()))
    : [..._allVoices];
  _filteredVoices = _applyFilterMap(base, _activeFilters);
  renderVoiceModalList(_filteredVoices);
  closeFilterModal();
}

function _applyFilterMap(voices, filters) {
  return voices.filter(v => {
    if (filters.language && v.language !== filters.language) return false;
    if (filters.accent && v.accent !== filters.accent) return false;
    if (filters.categories && filters.categories.length && !filters.categories.includes(v.category)) return false;
    if (filters.quality === 'high' && !v.high_quality) return false;
    if (filters.gender && v.gender !== filters.gender) return false;
    if (filters.age && v.age !== filters.age) return false;
    return true;
  });
}

function resetFilters() {
  _activeFilters = {};
  document.querySelectorAll('.vf-chip--active').forEach(b => b.classList.remove('vf-chip--active'));
  document.querySelectorAll('.vf-toggle').forEach(b => {
    b.classList.remove('active');
    if (b.dataset.val === '') b.classList.add('active');
  });
  _clearLang();
  const accentSel = document.getElementById('vfAccent');
  if (accentSel) accentSel.value = '';
  _filteredVoices = [..._allVoices];
  renderVoiceModalList(_filteredVoices);
  closeFilterModal();
}

// ── Voice config collection ────────────────────────────────────────────────
function _collectVoiceConfig() {
  const modelId  = document.getElementById('ttsModelId')?.value  || 'eleven_multilingual_v2';
  const speed    = parseFloat(document.getElementById('ttsSpeed')?.value    || '1.0');
  const stability = parseFloat(document.getElementById('ttsStability')?.value || '0.5');
  const similarity = parseFloat(document.getElementById('ttsSimilarity')?.value || '0.75');
  const style    = parseFloat(document.getElementById('ttsStyle')?.value    || '0.0');

  const extraConfig = { model_id: modelId, speed, stability, similarity, style };
  if (_selectedVoice.name) extraConfig['voice_name'] = _selectedVoice.name;

  return {
    tts_provider:  'genaipro',
    tts_api_key:   '',
    tts_voice_id:  _selectedVoice.voice_id || null,
    tts_config:    JSON.stringify(extraConfig),
  };
}

async function testVoice() {
  if (!currentProjectId) return;
  const cfg = _collectVoiceConfig();
  if (!cfg.tts_voice_id) {
    showToast('Selecciona una voz primero.', 'error');
    return;
  }

  const btn = document.querySelector('.voice-config-actions-top .btn-ghost');
  const origText = btn ? btn.textContent : '🔊 Probar Voz';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Generando…'; }

  try {
    const resp = await fetch(`/api/projects/${currentProjectId}/test-voice`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }

    const blob = await resp.blob();
    const label = document.getElementById('wavePlayerLabel');
    if (label) label.textContent = '🔊 Vista previa (200 chars)';
    await renderWaveformPlayer(blob, 'test');
    showToast('Vista previa generada ✓', 'success');
  } catch (e) {
    showToast('Error al probar voz: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function generateVoiceover() {
  if (!currentProjectId) return;
  const cfg = _collectVoiceConfig();
  if (!cfg.tts_voice_id) {
    showToast('Selecciona una voz primero.', 'error');
    return;
  }

  const btn = document.querySelector('.voice-config-actions-top .btn-primary');
  const origText = btn ? btn.textContent : '🎙️ Generar Voiceover Completo';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/generate-voiceover`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    showToast('Generando voiceover… revisa los logs para ver el progreso.', 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

// ── Waveform Player ────────────────────────────────────────────────────────

async function renderWaveformPlayer(blob, mode) {
  _waveMode = mode || 'test';
  const canvasId  = mode === 'approval' ? 'approvalWaveCanvas' : 'waveCanvas';
  const playBtnId = mode === 'approval' ? 'approvalWavePlayBtn' : 'wavePlayBtn';
  const timeId    = mode === 'approval' ? 'approvalWaveTime' : 'waveTime';
  const playerId  = mode === 'approval' ? 'approvalWavePlayer' : 'waveformPlayer';

  const player = document.getElementById(playerId);
  if (player) player.style.display = '';

  // Decode audio
  const arrayBuf = await blob.arrayBuffer();
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const audioBuf = await ctx.decodeAudioData(arrayBuf);

  if (mode === 'approval') {
    _approvalWaveBuffer = audioBuf;
    if (_approvalWaveAudio) { _approvalWaveAudio.pause(); URL.revokeObjectURL(_approvalWaveAudio.src); }
    _approvalWaveAudio = new Audio(URL.createObjectURL(blob));
  } else {
    _waveBuffer = audioBuf;
    if (_waveAudio) { _waveAudio.pause(); URL.revokeObjectURL(_waveAudio.src); }
    _waveAudio = new Audio(URL.createObjectURL(blob));
  }

  await ctx.close();

  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const buf = mode === 'approval' ? _approvalWaveBuffer : _waveBuffer;
  drawWaveformStatic(canvas, buf, 0);

  const audio = mode === 'approval' ? _approvalWaveAudio : _waveAudio;
  const playBtn = document.getElementById(playBtnId);
  const timeEl  = document.getElementById(timeId);

  function fmt(s) { const m = Math.floor(s / 60); return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`; }

  audio.ontimeupdate = () => {
    const prog = audio.duration ? audio.currentTime / audio.duration : 0;
    drawWaveformStatic(canvas, buf, prog);
    if (timeEl) timeEl.textContent = `${fmt(audio.currentTime)} / ${fmt(audio.duration || 0)}`;
  };
  audio.onended = () => { if (playBtn) playBtn.textContent = '▶'; };
  audio.onplay  = () => { if (playBtn) playBtn.textContent = '⏸'; };
  audio.onpause = () => { if (playBtn) playBtn.textContent = '▶'; };
}

function drawWaveformStatic(canvas, audioBuf, progress) {
  if (!canvas || !audioBuf) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth || 600;
  const H = canvas.offsetHeight || 64;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const data = audioBuf.getChannelData(0);
  const step = Math.ceil(data.length / W);
  const barW = 2;
  const gap  = 1;
  const cols = Math.floor(W / (barW + gap));
  const samplesPerCol = Math.ceil(data.length / cols);
  const playedColor = '#7c3aed';
  const unplayedColor = '#3f3f46';

  for (let i = 0; i < cols; i++) {
    let max = 0;
    const start = i * samplesPerCol;
    for (let j = start; j < start + samplesPerCol && j < data.length; j++) {
      const v = Math.abs(data[j]);
      if (v > max) max = v;
    }
    const barH = Math.max(2, max * H * 0.85);
    const x = i * (barW + gap);
    const y = (H - barH) / 2;
    ctx.fillStyle = (i / cols) < progress ? playedColor : unplayedColor;
    ctx.beginPath();
    ctx.roundRect(x, y, barW, barH, 1);
    ctx.fill();
  }
}

function toggleWavePlayback() {
  const audio = _waveAudio;
  if (!audio) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

function toggleApprovalPlayback() {
  const audio = _approvalWaveAudio;
  if (!audio) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

// Seek on canvas click
document.addEventListener('click', (e) => {
  const canvas = e.target;
  if (canvas.id === 'waveCanvas' && _waveAudio) {
    const pct = e.offsetX / canvas.offsetWidth;
    _waveAudio.currentTime = pct * _waveAudio.duration;
  }
  if (canvas.id === 'approvalWaveCanvas' && _approvalWaveAudio) {
    const pct = e.offsetX / canvas.offsetWidth;
    _approvalWaveAudio.currentTime = pct * _approvalWaveAudio.duration;
  }
});

// ── Audio Approval ────────────────────────────────────────────────────────

async function approveAudio() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Aprobar el voiceover? Después podrás continuar con la generación de escenas.', 'Aprobar')) return;

  const btn = document.querySelector('#voiceoverApprovalSection .btn-success');
  const origText = btn ? btn.textContent : '✅ Aprobar Audio';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Aprobando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/approve-audio`, { method: 'POST' });
    showToast('¡Audio aprobado! Haz clic en "Continuar con Escenas" para procesar el video.', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al aprobar: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function continueWithScenes() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Continuar con la generación de escenas? Se dividirá el SRT en escenas de 5 segundos y se iniciará la generación de video.', 'Continuar')) return;

  const btn = document.getElementById('voiceoverContinueActions')?.querySelector('button');
  const origText = btn ? btn.textContent : '▶️ Continuar con Escenas';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Creando escenas…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/create-scenes-from-srt`, { method: 'POST' });
    showToast('Escenas creadas. Iniciando generación de video…', 'success');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function resetToAudioApproved() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Reintentar desde audio aprobado? Se limpiarán los chunks con error para que puedas continuar con las escenas.', 'Reintentar')) return;

  const btn = document.getElementById('voiceoverRetryActions')?.querySelector('button');
  const origText = btn ? btn.textContent : '🔄 Reintentar desde Audio Aprobado';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Reseteando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/reset-to-audio-approved`, { method: 'POST' });
    showToast('Proyecto reseteado. Ahora haz clic en "Continuar con Escenas".', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function generateImages() {
  if (!currentProjectId) return;

  const btn = document.getElementById('generateImagesBtn');
  const origText = btn ? btn.textContent : '🎨 Generar Imágenes';
  if (btn) { btn.disabled = true; btn.textContent = `⏳ Iniciando ${_imgProviderName()}…`; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/generate-images`, { method: 'POST' });
    showToast(`🎨 ${_imgProviderName()} iniciado — generando imagen por escena. Revisa los logs.`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al generar imágenes: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function continueWithVideo() {
  showToast('Próximamente — integración con NCA para renderizado de video.', 'info');
}

async function searchStockAssets() {
  if (!currentProjectId) return;
  const btn = document.getElementById('btnSearchStockAssets');
  const origText = btn ? btn.textContent : '🔍 Buscar Assets de Stock';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Buscando assets…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/search-stock-assets`, { method: 'POST' });
    showToast('🔍 Búsqueda de assets iniciada — revisa los logs.', 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al buscar assets: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function recalibrateAudio() {
  if (!currentProjectId) return;
  const btn = document.getElementById('btnRecalibrateAudio');
  const origText = btn ? btn.textContent : '🎯 Recalibrar Audio';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Recalibrando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/recalibrate-timestamps`, { method: 'POST' });
    showToast('🎯 Recalibración con Whisper iniciada — puede tardar 30-60s…', 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al recalibrar: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function retryStockSearch(chunkNumber) {
  if (!currentProjectId) return;
  showToast(`🔍 Rebuscando asset para escena ${chunkNumber}… (puede tardar 30-60s)`, 'info');
  try {
    // Get current state BEFORE retry so we can detect when it changes
    const beforeResp = await apiFetch(`/api/projects/${currentProjectId}`);
    const beforeChunk = (beforeResp.chunks || []).find(c => c.chunk_number === chunkNumber);
    const beforeVideoPath = beforeChunk ? beforeChunk.video_path : null;
    const beforeImagePath = beforeChunk ? beforeChunk.image_path : null;
    const beforeUpdated = beforeChunk ? beforeChunk.updated_at : null;

    // Fire the retry (runs in background)
    await apiFetch(`/api/projects/${currentProjectId}/retry-chunk-image/${chunkNumber}`, { method: 'POST' });

    // Poll until the chunk changes (new file or status change)
    let attempts = 0;
    const maxAttempts = 120; // 120 × 3s = 360s max wait (clip_bank downloads can be slow)
    const pollInterval = 3000;

    const pollForChange = async () => {
      let sawPending = false;
      while (attempts < maxAttempts) {
        attempts++;
        await new Promise(r => setTimeout(r, pollInterval));
        try {
          const resp = await apiFetch(`/api/projects/${currentProjectId}`);
          const chunk = (resp.chunks || []).find(c => c.chunk_number === chunkNumber);
          if (!chunk) break;

          // Track if we ever saw a 'pending' state (means backend started processing)
          if (chunk.status === 'pending' || chunk.status === 'searching') {
            sawPending = true;
            continue;
          }

          // If status is done/error AND we either saw pending or updated_at changed
          const isFinished = chunk.status === 'done' || chunk.status === 'error';
          const hasChanged = chunk.updated_at !== beforeUpdated ||
                             chunk.video_path !== beforeVideoPath ||
                             chunk.image_path !== beforeImagePath;

          if (isFinished && (sawPending || hasChanged)) {
            await refreshDetail(currentProjectId);
            if (chunk.status === 'error') {
              showToast(`❌ Escena ${chunkNumber}: no se encontró un clip diferente`, 'error');
            } else {
              showToast(`✅ Escena ${chunkNumber}: nuevo clip encontrado!`, 'success');
            }
            return;
          }
        } catch (_) { /* ignore poll errors */ }
      }
      // Timeout — refresh anyway
      await refreshDetail(currentProjectId);
      showToast(`⏰ Escena ${chunkNumber}: tiempo agotado, revisa el estado`, 'warning');
    };

    // Start polling (don't await — let it run in background)
    pollForChange();

    // Immediate refresh to show "pending" status
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al rebuscar: ' + e.message, 'error');
  }
}

function renderAssetTypeFilters() {
  const container = document.getElementById('assetTypeFilters');
  if (!container) return;
  container.innerHTML = _visiblePills.map(t => {
    const active = _activeAssetTypes.has(t.key) ? 'active' : '';
    return `<span class="atf-chip ${t.key} ${active}" data-type="${t.key}" onclick="toggleAssetFilter('${t.key}')">${t.icon} ${t.label}</span>`;
  }).join('');
}

function toggleAssetFilter(key) {
  if (_activeAssetTypes.has(key)) {
    if (_activeAssetTypes.size < 1) return; // allow deselecting all
    _activeAssetTypes.delete(key);
  } else {
    _activeAssetTypes.add(key);
  }
  renderAssetTypeFilters();
}

async function planScenes() {
  if (!currentProjectId) return;
  const btn = document.getElementById('btnPlanScenesTop');
  const origText = btn ? btn.textContent : '🧠 Planificar';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Planificando…'; }

  try {
    const allowedTypes = [..._activeAssetTypes];
    await apiFetch(`/api/projects/${currentProjectId}/plan-scenes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allowed_types: allowedTypes }),
    });
    showToast('🧠 Planificación iniciada — Claude analiza cada escena…', 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al planificar escenas: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

function toggleAssetDropdown(badge, projectId, chunkNumber) {
  // Close any existing dropdown
  const existing = document.querySelector('.asset-dropdown');
  if (existing) { existing.remove(); return; }

  const types = [
    { id: 'title_card', icon: '📝', label: 'Titulo' },
    { id: 'web_image', icon: '🌐', label: 'Imagen Animada' },
    { id: 'web_image_full', icon: '🖼️', label: 'Imagen Completa' },
    { id: 'stock_video', icon: '📹', label: 'Stock Video' },
    { id: 'ai_image', icon: '🤖', label: 'AI Image' },
    { id: 'archive_footage', icon: '🏛️', label: 'Archivo' },
  ];

  const dd = document.createElement('div');
  dd.className = 'asset-dropdown';
  dd.innerHTML = types.map(t =>
    `<div class="asset-dropdown-item" data-type="${t.id}">${t.icon} ${t.label}</div>`
  ).join('');

  badge.style.position = 'relative';
  badge.appendChild(dd);

  dd.addEventListener('click', async (e) => {
    const item = e.target.closest('.asset-dropdown-item');
    if (!item) return;
    const newType = item.dataset.type;
    dd.remove();
    try {
      await apiFetch(`/api/projects/${projectId}/chunk/${chunkNumber}/asset-type`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asset_type: newType }),
      });
      await refreshDetail(projectId);
    } catch (err) {
      showToast('Error al cambiar tipo: ' + err.message, 'error');
    }
  });

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', function _close(ev) {
      if (!dd.contains(ev.target)) { dd.remove(); document.removeEventListener('click', _close); }
    });
  }, 10);
}

async function startVeo3Animation() {
  if (!currentProjectId) return;
  const btn = document.getElementById('startVeo3AnimationBtn');
  const origText = btn ? btn.textContent : '🤖 Animar con Meta AI';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando Meta AI…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/start-animation`, { method: 'POST' });
    showToast('🤖 Animación con Meta AI iniciada (5 navegadores paralelos). Revisa los logs.', 'success');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 5000);
  } catch (e) {
    showToast('Error al iniciar animación: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

// ── Editing View ─────────────────────────────────────────────────────────
let editingPollInterval = null;
let _editingChunks = [];   // current chunk order for drag-and-drop
let _dragSrcIdx = null;    // index being dragged
let _canvasPlayer = null;  // CanvasPlayer instance

async function openEditing(projectId) {
  currentProjectId = projectId;
  stopPolling();
  if (editingPollInterval) { clearInterval(editingPollInterval); editingPollInterval = null; }
  await refreshEditing(projectId);
  // Only start log stream if actively rendering (not on every open)
}

let _editingLogSource = null;

function startEditingLogStream(projectId) {
  const container = document.getElementById('editingLogsContainer');
  const card = document.getElementById('editingLogsCard');
  if (!container) return;
  // Only stream if the logs card is visible (i.e., during rendering)
  if (card && card.style.display === 'none') return;

  // Stop previous SSE if any
  if (_editingLogSource) { _editingLogSource.close(); _editingLogSource = null; }

  container.innerHTML = '';

  // Use SSE for real-time log streaming
  _editingLogSource = new EventSource(`/api/logs/${projectId}/stream`);
  _editingLogSource.onmessage = (e) => {
    try {
      const l = JSON.parse(e.data);
      const div = document.createElement('div');
      div.className = `log-line ${l.level || 'info'}`;
      const ts = new Date(l.timestamp).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      div.innerHTML = `<span class="log-ts">${ts}</span><span class="log-stage">[${l.stage || ''}]</span><span class="log-msg">${escHtml(l.message)}</span>`;
      container.appendChild(div);
      // Keep max 200 lines
      while (container.children.length > 200) container.removeChild(container.firstChild);
      container.scrollTop = container.scrollHeight;
    } catch (_) {}
  };
  _editingLogSource.onerror = () => {
    // Reconnect after a delay
    if (_editingLogSource) _editingLogSource.close();
    _editingLogSource = null;
  };
}

async function refreshEditing(projectId) {
  try {
    const p = await apiFetch(`/api/projects/${projectId}`);
    const chunks = p.chunks || [];
    _editingChunks = chunks.slice(); // copy for reorder

    // Header
    document.getElementById('editingTitle').textContent = `Edición — ${p.title}`;
    const badge = document.getElementById('editingBadge');
    if (badge) { badge.textContent = p.status.toUpperCase(); badge.className = `badge badge-${p.status}`; }

    // Stats
    const clipCount = document.getElementById('editingClipCount');
    const doneVids = chunks.filter(c => c.video_path).length;
    const doneImgs = chunks.filter(c => c.image_path).length;
    const totalDurMs = chunks.reduce((s, c) => s + ((c.end_ms || 0) - (c.start_ms || 0)), 0);
    const totalDurStr = _fmtDuration(totalDurMs);
    if (clipCount) clipCount.textContent = `${chunks.length} clips · ${doneVids} vid · ${doneImgs} img · ${totalDurStr}`;

    // Build timeline
    _buildTimeline(p, chunks);

    // Initialize or update canvas player
    _initCanvasPlayer(p.id, chunks);
    _initPlayerControls();

    // Render button state
    const renderBtn = document.getElementById('editingRenderBtn');
    const renderHint = document.getElementById('editingRenderHint');
    const finalPreview = document.getElementById('editingFinalPreview');

    const progressDiv = document.getElementById('editingRenderProgress');
    const progressFill = document.getElementById('editingProgressFill');
    const progressPct = document.getElementById('editingProgressPct');
    const progressStage = document.getElementById('editingProgressStage');

    // Cancel render button
    let cancelBtn = document.getElementById('editingCancelRender');

    if (p.status === 'rendering' || p.status === 'queued') {
      renderBtn.disabled = true;
      renderBtn.textContent = '⏳ Renderizando…';
      renderHint.textContent = '';
      if (finalPreview) finalPreview.style.display = 'none';

      // Show cancel button
      if (!cancelBtn) {
        cancelBtn = document.createElement('button');
        cancelBtn.id = 'editingCancelRender';
        cancelBtn.className = 'btn btn-sm';
        cancelBtn.style.cssText = 'background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid #ef4444;margin-left:8px;';
        cancelBtn.textContent = '✕ Cancelar render';
        cancelBtn.onclick = () => cancelRender();
        renderBtn.parentElement.appendChild(cancelBtn);
      }

      // Show progress bar
      const pct = p.render_progress || 0;
      if (progressDiv) {
        progressDiv.style.display = '';
        progressFill.style.width = pct + '%';
        progressPct.textContent = pct + '%';
        // Stage label based on percentage
        if (pct < 60) progressStage.textContent = 'Preparando clips…';
        else if (pct < 90) progressStage.textContent = 'Aplicando transiciones…';
        else if (pct < 98) progressStage.textContent = 'Mezclando audio…';
        else progressStage.textContent = 'Finalizando…';
      }

      // Show logs panel and start log stream during active render
      const logsCard = document.getElementById('editingLogsCard');
      if (logsCard) logsCard.style.display = '';
      startEditingLogStream(projectId);

      if (!editingPollInterval) {
        editingPollInterval = setInterval(() => {
          refreshEditing(projectId);
        }, 3000);
      }
    } else if (p.final_video_path) {
      // Stop log stream and hide logs panel when not rendering
      if (_editingLogSource) { _editingLogSource.close(); _editingLogSource = null; }
      const logsCard2 = document.getElementById('editingLogsCard');
      if (logsCard2) logsCard2.style.display = 'none';
      if (cancelBtn) cancelBtn.remove();
      renderBtn.textContent = '🎬 Renderizar Video';
      renderBtn.disabled = false;
      renderBtn.style.display = '';
      renderHint.textContent = '✅ Listo';
      if (progressDiv) progressDiv.style.display = 'none';
      if (finalPreview) {
        finalPreview.style.display = '';
        const player = document.getElementById('editingVideoPlayer');
        const dl = document.getElementById('editingVideoDownload');
        const headerDl = document.getElementById('editingHeaderDownload');
        const videoUrl = `/api/projects/${p.id}/final-video?t=${Date.now()}`;
        if (player) player.src = videoUrl;
        if (dl) dl.href = videoUrl;
        if (headerDl) { headerDl.href = videoUrl; headerDl.download = 'final_video.mp4'; headerDl.style.display = ''; }
        // Also show in main preview
        const mainPlayer = document.getElementById('editingPreviewPlayer');
        const placeholder = document.getElementById('editingPreviewPlaceholder');
        if (mainPlayer) { mainPlayer.src = videoUrl; mainPlayer.style.display = ''; }
        if (placeholder) placeholder.style.display = 'none';
      }
      if (editingPollInterval) { clearInterval(editingPollInterval); editingPollInterval = null; }
    } else {
      if (_editingLogSource) { _editingLogSource.close(); _editingLogSource = null; }
      const logsCard3 = document.getElementById('editingLogsCard');
      if (logsCard3) logsCard3.style.display = 'none';
      if (cancelBtn) cancelBtn.remove();
      renderBtn.style.display = '';
      renderBtn.disabled = false;
      renderBtn.textContent = '🎬 Renderizar Video';
      renderHint.textContent = `${doneVids} clips · ${totalDurStr}`;
      if (progressDiv) progressDiv.style.display = 'none';
      if (finalPreview) finalPreview.style.display = 'none';
      // Reset main preview
      const mainPlayer = document.getElementById('editingPreviewPlayer');
      const placeholder = document.getElementById('editingPreviewPlaceholder');
      if (mainPlayer) mainPlayer.style.display = 'none';
      if (placeholder) placeholder.style.display = '';
      if (editingPollInterval) { clearInterval(editingPollInterval); editingPollInterval = null; }
    }
  } catch (e) {
    console.error('refreshEditing error:', e);
  }
}

function _fmtDuration(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

// ── Transition definitions (CapCut-style labels) ─────────────────────────
const TRANSITIONS = [
  { id: 'fade',        label: 'Combinar',     icon: '🔄' },
  { id: 'fadeblack',   label: 'Oscurecer',    icon: '⬛' },
  { id: 'fadewhite',   label: 'Destello',     icon: '⬜' },
  { id: 'dissolve',    label: 'Disolver',     icon: '💫' },
  { id: 'wipeleft',    label: 'Borrar ←',     icon: '◀️' },
  { id: 'wiperight',   label: 'Borrar →',     icon: '▶️' },
  { id: 'slideleft',   label: 'Deslizar ←',   icon: '⏪' },
  { id: 'slideright',  label: 'Deslizar →',   icon: '⏩' },
  { id: 'slideup',     label: 'Deslizar ↑',   icon: '⏫' },
  { id: 'slidedown',   label: 'Deslizar ↓',   icon: '⏬' },
  { id: 'circleopen',  label: 'Círculo abrir', icon: '⭕' },
  { id: 'circleclose', label: 'Círculo cerrar',icon: '🔴' },
  { id: 'radial',      label: 'Radial',       icon: '🌀' },
  { id: 'smoothleft',  label: 'Suave ←',      icon: '🌊' },
  { id: 'smoothright', label: 'Suave →',      icon: '🌊' },
  { id: 'zoomin',      label: 'Acercar',      icon: '🔍' },
];

let _activeTransitionPopup = null;

// ── Virtual-scrolling timeline ─────────────────────────────────────────────
// Pre-computed layout data for all items (clips + transition markers)
let _tlLayout = [];       // [{type:'clip'|'marker', idx, left, width, chunk}]
let _tlTotalWidth = 0;
let _tlProject = null;
let _tlChunks = [];
let _tlScrollHandler = null;
const _TL_BUFFER = 300;   // px buffer outside viewport to pre-render
const _TL_MARKER_W = 22;  // transition marker width in px
const PX_PER_SEC = 12;

function _buildTimeline(project, chunks) {
  const timeline = document.getElementById('editingTimeline');
  const ruler = document.getElementById('editingTimeRuler');
  if (ruler) ruler.innerHTML = '';
  _closeTransitionPopup();

  _tlProject = project;
  _tlChunks = chunks;

  // ── 1. Pre-compute layout positions ───────────────────────────────────
  _tlLayout = [];
  let left = 0;
  let accMs = 0;

  chunks.forEach((c, idx) => {
    const durMs = (c.start_ms != null && c.end_ms != null) ? (c.end_ms - c.start_ms) : 3800;
    const durSec = Math.max(durMs / 1000, 0.5);
    const width = Math.max(Math.round(durSec * PX_PER_SEC), 30);

    // Transition marker before this clip (not first)
    if (idx > 0) {
      _tlLayout.push({ type: 'marker', idx, left, width: _TL_MARKER_W, chunk: c, accMs });
      left += _TL_MARKER_W + 2; // 2px gap
    }

    _tlLayout.push({ type: 'clip', idx, left, width, chunk: c, accMs, durSec });
    left += width + 2; // 2px gap

    // Ruler tick
    if (ruler) {
      const tick = document.createElement('span');
      if (idx > 0) {
        const spacer = document.createElement('span');
        spacer.style.width = _TL_MARKER_W + 'px';
        spacer.textContent = '';
        ruler.appendChild(spacer);
      }
      tick.style.width = width + 'px';
      tick.textContent = _fmtDuration(accMs);
      ruler.appendChild(tick);
    }
    accMs += durMs;
  });

  _tlTotalWidth = left;

  // ── 2. Setup virtual-scroll container ─────────────────────────────────
  // Replace inner HTML with a single spacer + absolutely positioned items
  timeline.innerHTML = '';
  timeline.style.position = 'relative';

  const spacer = document.createElement('div');
  spacer.style.width = _tlTotalWidth + 'px';
  spacer.style.height = '1px';
  spacer.style.pointerEvents = 'none';
  spacer.className = 'tl-spacer';
  timeline.appendChild(spacer);

  // ── 3. Event delegation ───────────────────────────────────────────────
  // Remove old handler if any
  if (_tlScrollHandler) {
    timeline.removeEventListener('scroll', _tlScrollHandler);
  }
  // Remove old delegated listeners
  timeline.removeEventListener('click', _tlOnClick);
  timeline.removeEventListener('dragstart', _tlOnDragStart);
  timeline.removeEventListener('dragover', _tlOnDragOver);
  timeline.removeEventListener('dragleave', _tlOnDragLeave);
  timeline.removeEventListener('drop', _tlOnDrop);
  timeline.removeEventListener('dragend', _tlOnDragEnd);

  // Add delegated listeners
  timeline.addEventListener('click', _tlOnClick);
  timeline.addEventListener('dragstart', _tlOnDragStart);
  timeline.addEventListener('dragover', _tlOnDragOver);
  timeline.addEventListener('dragleave', _tlOnDragLeave);
  timeline.addEventListener('drop', _tlOnDrop);
  timeline.addEventListener('dragend', _tlOnDragEnd);

  // ── 4. Scroll-driven rendering ────────────────────────────────────────
  _tlScrollHandler = () => _tlRenderVisible(timeline);
  timeline.addEventListener('scroll', _tlScrollHandler, { passive: true });

  // Initial render
  _tlRenderVisible(timeline);
}

// Render only visible items in the timeline viewport
function _tlRenderVisible(timeline) {
  const scrollLeft = timeline.scrollLeft;
  const viewWidth = timeline.clientWidth;
  const lo = scrollLeft - _TL_BUFFER;
  const hi = scrollLeft + viewWidth + _TL_BUFFER;

  // Collect which layout items are visible
  const visibleSet = new Set();
  for (let i = 0; i < _tlLayout.length; i++) {
    const item = _tlLayout[i];
    const itemRight = item.left + item.width;
    if (itemRight >= lo && item.left <= hi) {
      visibleSet.add(i);
    }
  }

  // Remove DOM nodes no longer visible
  const existing = timeline.querySelectorAll('[data-tl-i]');
  existing.forEach(el => {
    const i = parseInt(el.dataset.tlI);
    if (!visibleSet.has(i)) {
      el.remove();
    } else {
      visibleSet.delete(i); // already rendered
    }
  });

  // Create DOM nodes for newly visible items
  visibleSet.forEach(i => {
    const item = _tlLayout[i];
    const el = _tlCreateElement(item);
    el.dataset.tlI = i;
    timeline.appendChild(el);
  });
}

// Create a DOM element for a layout item
function _tlCreateElement(item) {
  if (item.type === 'marker') {
    return _tlCreateMarker(item);
  } else {
    return _tlCreateClip(item);
  }
}

function _tlCreateMarker(item) {
  const c = item.chunk;
  const marker = document.createElement('div');
  marker.className = 'transition-marker' + (c.transition ? ' has-transition' : '');
  marker.dataset.chunkNumber = c.chunk_number;
  marker.dataset.idx = item.idx;
  marker.style.position = 'absolute';
  marker.style.left = item.left + 'px';
  marker.style.top = '0';
  marker.style.height = '100%';
  marker.title = c.transition
    ? `Transición: ${_transitionLabel(c.transition)}`
    : 'Agregar transición';

  if (c.transition) {
    const tr = TRANSITIONS.find(t => t.id === c.transition);
    marker.innerHTML = `<span class="transition-marker-icon">${tr ? tr.icon : '🔄'}</span>`;
  } else {
    marker.innerHTML = `<span class="transition-marker-icon">+</span>`;
  }
  return marker;
}

function _tlCreateClip(item) {
  const c = item.chunk;
  const hasVid = !!c.video_path;
  const hasImg = !!c.image_path;

  const clip = document.createElement('div');
  clip.className = `timeline-clip ${hasVid ? 'has-video' : hasImg ? '' : 'no-media'}`;
  clip.style.width = item.width + 'px';
  clip.style.position = 'absolute';
  clip.style.left = item.left + 'px';
  clip.style.top = '0';
  clip.draggable = true;
  clip.dataset.idx = item.idx;

  let mediaHtml = '';
  if (hasVid) {
    mediaHtml = `<div style="width:100%;height:40px;background:#1a3a1a;display:flex;align-items:center;justify-content:center;font-size:10px;color:#6f6;">&#9654;</div>`;
  } else if (hasImg) {
    mediaHtml = `<div style="width:100%;height:40px;background:#1a1a3a;display:flex;align-items:center;justify-content:center;font-size:10px;color:#66f;">&#9632;</div>`;
  } else {
    mediaHtml = `<div style="width:100%;height:40px;background:var(--bg4);"></div>`;
  }

  clip.innerHTML = `
    <span class="timeline-clip-num">${c.chunk_number}</span>
    ${mediaHtml}
    <div class="timeline-clip-info"><span>${item.durSec.toFixed(1)}s</span></div>
  `;
  return clip;
}

// ── Delegated event handlers for timeline ─────────────────────────────────
function _tlFindClip(e) {
  return e.target.closest('.timeline-clip');
}
function _tlFindMarker(e) {
  return e.target.closest('.transition-marker');
}

function _tlOnClick(e) {
  const marker = _tlFindMarker(e);
  if (marker) {
    e.stopPropagation();
    const idx = parseInt(marker.dataset.idx);
    const c = _tlChunks[idx];
    if (c && _tlProject) _openTransitionPopup(marker, _tlProject.id, c);
    return;
  }
  const clip = _tlFindClip(e);
  if (clip) {
    const idx = parseInt(clip.dataset.idx);
    const c = _tlChunks[idx];
    if (c && _tlProject) _previewClip(_tlProject.id, c);
  }
}

function _tlOnDragStart(e) {
  const clip = _tlFindClip(e);
  if (!clip) return;
  _dragSrcIdx = parseInt(clip.dataset.idx);
  clip.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', _dragSrcIdx);
}

function _tlOnDragOver(e) {
  const clip = _tlFindClip(e);
  if (!clip) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  clip.classList.remove('drag-over-left', 'drag-over-right');
  const rect = clip.getBoundingClientRect();
  const midX = rect.left + rect.width / 2;
  if (e.clientX < midX) clip.classList.add('drag-over-left');
  else clip.classList.add('drag-over-right');
}

function _tlOnDragLeave(e) {
  const clip = _tlFindClip(e);
  if (clip) clip.classList.remove('drag-over-left', 'drag-over-right');
}

function _tlOnDrop(e) {
  const clip = _tlFindClip(e);
  if (!clip) return;
  e.preventDefault();
  clip.classList.remove('drag-over-left', 'drag-over-right');
  const dstIdx = parseInt(clip.dataset.idx);
  if (_dragSrcIdx === null || _dragSrcIdx === dstIdx) return;

  const rect = clip.getBoundingClientRect();
  const midX = rect.left + rect.width / 2;
  let insertIdx = e.clientX < midX ? dstIdx : dstIdx + 1;
  if (_dragSrcIdx < insertIdx) insertIdx--;

  const [moved] = _editingChunks.splice(_dragSrcIdx, 1);
  _editingChunks.splice(insertIdx, 0, moved);
  _saveClipOrder();
  const projectId = currentProjectId;
  apiFetch(`/api/projects/${projectId}`).then(p => {
    _buildTimeline(p, _editingChunks);
  });
}

function _tlOnDragEnd(e) {
  const clip = _tlFindClip(e);
  if (clip) clip.classList.remove('dragging');
  document.querySelectorAll('.timeline-clip').forEach(el => {
    el.classList.remove('drag-over-left', 'drag-over-right');
  });
  _dragSrcIdx = null;
}

// Scroll timeline to show a specific clip index
function _tlScrollToClip(idx) {
  const timeline = document.getElementById('editingTimeline');
  if (!timeline) return;
  // Find the clip layout item
  const item = _tlLayout.find(l => l.type === 'clip' && l.idx === idx);
  if (!item) return;
  const viewWidth = timeline.clientWidth;
  const targetScroll = item.left - viewWidth / 2 + item.width / 2;
  timeline.scrollTo({ left: Math.max(0, targetScroll), behavior: 'smooth' });
}

function _transitionLabel(id) {
  const t = TRANSITIONS.find(t => t.id === id);
  return t ? t.label : id;
}

function _closeTransitionPopup() {
  if (_activeTransitionPopup) {
    _activeTransitionPopup.remove();
    _activeTransitionPopup = null;
  }
}

function _openTransitionPopup(marker, projectId, chunk) {
  _closeTransitionPopup();

  const popup = document.createElement('div');
  popup.className = 'transition-popup';

  // Header
  const header = document.createElement('div');
  header.className = 'transition-popup-header';
  header.innerHTML = `<span>Transiciones</span>`;

  // Remove button if transition exists
  if (chunk.transition) {
    const removeBtn = document.createElement('button');
    removeBtn.className = 'btn-transition-remove';
    removeBtn.textContent = '✕ Quitar';
    removeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _setTransition(projectId, chunk.chunk_number, null, 500);
    });
    header.appendChild(removeBtn);
  }
  popup.appendChild(header);

  // Duration slider
  const durRow = document.createElement('div');
  durRow.className = 'transition-dur-row';
  const currentDur = chunk.transition_duration || 500;
  durRow.innerHTML = `
    <label>Duración: <strong id="trDurLabel">${(currentDur / 1000).toFixed(1)}s</strong></label>
    <input type="range" id="trDurSlider" min="200" max="2000" step="100" value="${currentDur}" />
  `;
  popup.appendChild(durRow);

  // Wire up slider label
  setTimeout(() => {
    const slider = document.getElementById('trDurSlider');
    const label = document.getElementById('trDurLabel');
    if (slider && label) {
      slider.addEventListener('input', () => {
        label.textContent = (parseInt(slider.value) / 1000).toFixed(1) + 's';
      });
    }
  }, 0);

  // Grid of transitions
  const grid = document.createElement('div');
  grid.className = 'transition-grid';

  TRANSITIONS.forEach(tr => {
    const item = document.createElement('div');
    item.className = 'transition-item' + (chunk.transition === tr.id ? ' active' : '');
    item.innerHTML = `
      <div class="transition-item-icon">${tr.icon}</div>
      <div class="transition-item-label">${tr.label}</div>
    `;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      const dur = parseInt(document.getElementById('trDurSlider')?.value || '500');
      _setTransition(projectId, chunk.chunk_number, tr.id, dur);
    });
    grid.appendChild(item);
  });
  popup.appendChild(grid);

  // Position popup fixed above marker (avoid overflow clipping)
  const rect = marker.getBoundingClientRect();
  popup.style.position = 'fixed';
  popup.style.left = Math.max(10, rect.left - 120) + 'px';
  popup.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
  popup.style.zIndex = '200';

  document.body.appendChild(popup);
  _activeTransitionPopup = popup;

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', _handleTransitionOutsideClick);
  }, 0);
}

function _handleTransitionOutsideClick(e) {
  if (_activeTransitionPopup && !_activeTransitionPopup.contains(e.target) &&
      !e.target.closest('.transition-marker')) {
    _closeTransitionPopup();
    document.removeEventListener('click', _handleTransitionOutsideClick);
  }
}

async function _setTransition(projectId, chunkNumber, transition, duration) {
  try {
    await apiFetch(`/api/projects/${projectId}/chunk/${chunkNumber}/transition`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transition, duration }),
    });
    const label = transition ? _transitionLabel(transition) : 'ninguna';
    showToast(`Transición: ${label}`, 'success');
    _closeTransitionPopup();
    document.removeEventListener('click', _handleTransitionOutsideClick);
    // Refresh to rebuild timeline
    await refreshEditing(projectId);
    // Play transition preview in canvas player
    if (_canvasPlayer && transition) {
      _canvasPlayer.seekToTransition(chunkNumber);
      _showPlayerControls();
    }
  } catch (e) {
    showToast('Error al guardar transición: ' + e.message, 'error');
  }
}

// ── Bulk transition popup ──────────────────────────────────────────────────
let _bulkTransitionPopup = null;

function openBulkTransitionPopup() {
  if (!currentProjectId) return;
  closeBulkTransitionPopup();

  const btn = document.getElementById('btnBulkTransition');
  const popup = document.createElement('div');
  popup.className = 'bulk-transition-popup';

  let html = `<div class="transition-popup-header">
    <span>🔀 Transición para todos</span>
    <button class="btn-transition-remove" onclick="applyBulkTransition(null, 500)">✕ Quitar todas</button>
  </div>`;

  html += `<div class="transition-dur-row">
    <label>Duración:</label>
    <input type="range" id="bulkTransDur" min="200" max="2000" step="100" value="500" />
    <span id="bulkTransDurLabel">500ms</span>
  </div>`;

  html += `<div class="transition-grid">`;
  TRANSITIONS.forEach(tr => {
    html += `<div class="transition-item" onclick="applyBulkTransition('${tr.id}', document.getElementById('bulkTransDur').value)">
      <span class="transition-item-icon">${tr.icon}</span>
      <span class="transition-item-label">${tr.label}</span>
    </div>`;
  });
  html += `</div>`;

  popup.innerHTML = html;

  // Position below the button
  const rect = btn.getBoundingClientRect();
  const container = btn.closest('.timeline-toolbar') || btn.parentElement;
  popup.style.position = 'fixed';
  popup.style.top = (rect.bottom + 4) + 'px';
  popup.style.left = rect.left + 'px';

  document.body.appendChild(popup);
  _bulkTransitionPopup = popup;

  // Duration slider label
  const slider = popup.querySelector('#bulkTransDur');
  const label = popup.querySelector('#bulkTransDurLabel');
  slider.addEventListener('input', () => { label.textContent = slider.value + 'ms'; });

  // Close on outside click
  setTimeout(() => document.addEventListener('click', _handleBulkTransitionOutside), 10);
}

function closeBulkTransitionPopup() {
  if (_bulkTransitionPopup) {
    _bulkTransitionPopup.remove();
    _bulkTransitionPopup = null;
  }
  document.removeEventListener('click', _handleBulkTransitionOutside);
}

function _handleBulkTransitionOutside(e) {
  if (_bulkTransitionPopup && !_bulkTransitionPopup.contains(e.target) &&
      e.target.id !== 'btnBulkTransition') {
    closeBulkTransitionPopup();
  }
}

async function applyBulkTransition(transition, duration) {
  if (!currentProjectId) return;
  try {
    const data = await apiFetch(`/api/projects/${currentProjectId}/bulk-transitions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transition, duration: parseInt(duration) || 500 }),
    });
    const label = transition ? _transitionLabel(transition) : 'ninguna';
    showToast(`Transición "${label}" aplicada a ${data.updated} clips`, 'success');
    closeBulkTransitionPopup();
    await refreshEditing(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

function _previewClip(projectId, chunk) {
  const n = chunk.chunk_number;

  // Use canvas player if available
  if (_canvasPlayer) {
    const idx = _editingChunks.findIndex(c => c.chunk_number === n);
    if (idx >= 0) {
      _canvasPlayer.play(idx);
      _showPlayerControls();
      // Auto-scroll timeline to show the playing clip
      _tlScrollToClip(idx);
    }
  }

  // Highlight selected clip (only among currently rendered clips)
  document.querySelectorAll('.timeline-clip.selected').forEach(el => el.classList.remove('selected'));
  document.querySelectorAll('.timeline-clip').forEach(el => {
    if (parseInt(el.dataset.idx) === _editingChunks.findIndex(c => c.chunk_number === n)) {
      el.classList.add('selected');
    }
  });
}

// ── Canvas Player Integration ────────────────────────────────────────────

async function _initCanvasPlayer(projectId, chunks) {
  const canvas = document.getElementById('editingCanvas');
  if (!canvas) return;

  // Stop & destroy previous player
  if (_canvasPlayer) {
    _canvasPlayer.destroy();
  }

  // Check if preview.mp4 is available — use PreviewPlayer for instant playback
  try {
    const status = await apiFetch(`/api/projects/${projectId}/preview-status`);
    if (status.ready) {
      const previewUrl = `/api/projects/${projectId}/preview-video`;
      _canvasPlayer = new PreviewPlayer(canvas, projectId, chunks, previewUrl);
      _hidePreviewBuildUI();
      return;
    }
  } catch (_) { /* fall through to CanvasPlayer */ }

  // Fallback: multi-clip player
  _canvasPlayer = new CanvasPlayer(canvas, projectId, chunks);
  _showPreviewBuildUI(projectId);
}

let _previewPollInterval = null;

function _showPreviewBuildUI(projectId) {
  const container = document.getElementById('editingPreviewScreen');
  if (!container) return;
  // Show "Generate Preview" button if not already present
  let btn = document.getElementById('buildPreviewBtn');
  if (!btn) {
    btn = document.createElement('button');
    btn.id = 'buildPreviewBtn';
    btn.className = 'btn btn-primary';
    btn.style.cssText = 'position:absolute;bottom:10px;right:10px;z-index:20;font-size:12px;padding:6px 14px;';
    btn.textContent = '🎞️ Generar Preview';
    btn.onclick = () => _buildPreview(projectId);
    container.appendChild(btn);
  }
}

function _hidePreviewBuildUI() {
  const btn = document.getElementById('buildPreviewBtn');
  if (btn) btn.remove();
  const bar = document.getElementById('previewProgressBar');
  if (bar) bar.remove();
  if (_previewPollInterval) { clearInterval(_previewPollInterval); _previewPollInterval = null; }
}

async function _buildPreview(projectId) {
  const btn = document.getElementById('buildPreviewBtn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Generando…'; }

  try {
    await apiFetch(`/api/projects/${projectId}/build-preview`, { method: 'POST' });
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = '🎞️ Generar Preview'; }
    return;
  }

  // Show progress bar
  const container = document.getElementById('editingPreviewScreen');
  let bar = document.getElementById('previewProgressBar');
  if (!bar && container) {
    bar = document.createElement('div');
    bar.id = 'previewProgressBar';
    bar.style.cssText = 'position:absolute;bottom:40px;left:10px;right:10px;z-index:20;height:6px;background:#333;border-radius:3px;';
    bar.innerHTML = '<div id="previewProgressFill" style="height:100%;width:0%;background:var(--primary,#4f8cff);border-radius:3px;transition:width 0.3s;"></div>';
    container.appendChild(bar);
  }

  // Poll for progress
  if (_previewPollInterval) clearInterval(_previewPollInterval);
  _previewPollInterval = setInterval(async () => {
    try {
      const status = await apiFetch(`/api/projects/${projectId}/preview-status`);
      const fill = document.getElementById('previewProgressFill');
      if (fill) fill.style.width = status.progress + '%';
      if (btn) btn.textContent = `⏳ ${status.progress}%`;

      if (status.ready) {
        clearInterval(_previewPollInterval);
        _previewPollInterval = null;
        showToast('✅ Preview listo', 'success');
        // Re-init with PreviewPlayer
        _initCanvasPlayer(projectId, _editingChunks);
        _initPlayerControls();
      } else if (status.error) {
        clearInterval(_previewPollInterval);
        _previewPollInterval = null;
        showToast('Error generando preview', 'error');
        if (btn) { btn.disabled = false; btn.textContent = '🎞️ Reintentar Preview'; }
        if (bar) bar.remove();
      }
    } catch (_) {}
  }, 2000);
}

// ── Player Controls Wiring ────────────────────────────────────────
let _currentPlaybackMs = 0;
let _totalPlaybackMs = 0;
let _userSeeking = false;
let _isPlaying = false;
let _keyboardListenerAdded = false;

// FIX #1: Helper to clone element, removing ALL old event listeners
function _cloneClean(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  const clone = el.cloneNode(true);
  el.parentNode.replaceChild(clone, el);
  return clone;
}

function _initPlayerControls() {
  if (!_canvasPlayer) return;

  // FIX #1: Clone all control elements to strip any old listeners
  const playBtn   = _cloneClean('playerPlayBtn');
  const backBtn   = _cloneClean('playerBackBtn');
  const fwdBtn    = _cloneClean('playerFwdBtn');
  const seekBar   = _cloneClean('playerSeekBar');
  const speedSelect = _cloneClean('playerSpeedSelect');
  const timeDisplay = document.getElementById('playerTimeDisplay');

  if (!playBtn) return;

  // FIX #6: Hide legacy overlay controls so they can't interfere
  const overlay = document.getElementById('editingPlayerControlsOverlay');
  if (overlay) overlay.style.display = 'none';

  // Calculate total duration
  _totalPlaybackMs = (_editingChunks || []).reduce((sum, c) => {
    return sum + ((c.end_ms != null && c.start_ms != null) ? (c.end_ms - c.start_ms) : 3800);
  }, 0);

  // Wire up canvas player callbacks (safe to re-set each time)
  _canvasPlayer.onTimeUpdate = (globalMs, totalMs) => {
    _currentPlaybackMs = globalMs;
    if (!_userSeeking && _totalPlaybackMs > 0) {
      seekBar.value = (globalMs / _totalPlaybackMs) * 1000;
    }
    _updateTimeDisplay(timeDisplay);

    // Update scrubber bar
    const pct = totalMs > 0 ? Math.min(globalMs / totalMs, 1) : 0;
    const fill = document.getElementById('timelineScrubberFill');
    const head = document.getElementById('timelinePlayhead');
    const timeEl = document.getElementById('timelineScrubberTime');
    if (fill) fill.style.width = (pct * 100) + '%';
    if (head) head.style.left = (pct * 100) + '%';
    if (timeEl) timeEl.textContent = _fmtDuration(globalMs);

    // Move vertical playhead line over clips
    const clipHead = document.getElementById('timelineClipPlayhead');
    const clipTrack = document.querySelector('.timeline-track-clips');
    if (clipHead && clipTrack && totalMs > 0) {
      const trackW = clipTrack.scrollWidth;
      const offsetPx = Math.round(pct * trackW);
      clipHead.style.display = '';
      clipHead.style.left = (70 + offsetPx) + 'px';
    }
  };

  _canvasPlayer.onStateChange = (state) => {
    _isPlaying = (state === 'playing');
    playBtn.innerHTML = _isPlaying ? '&#9646;&#9646;' : '&#9654;';
  };

  _canvasPlayer.onEnd = () => {
    _isPlaying = false;
    playBtn.innerHTML = '&#9654;';
  };

  _canvasPlayer.onChunkChange = (idx) => {
    document.querySelectorAll('.timeline-clip.selected').forEach(el => el.classList.remove('selected'));
    document.querySelectorAll('.timeline-clip').forEach(el => {
      if (parseInt(el.dataset.idx) === idx) el.classList.add('selected');
    });
    _tlScrollToClip(idx);
  };

  // Fresh listeners on cloned elements (guaranteed no duplicates)
  playBtn.addEventListener('click', () => {
    if (!_canvasPlayer) return;
    _canvasPlayer.togglePlayPause();
  });

  backBtn.addEventListener('click', () => {
    if (!_canvasPlayer) return;
    const newMs = Math.max(0, _currentPlaybackMs - 5000);
    _canvasPlayer.seekTo(newMs);
  });

  fwdBtn.addEventListener('click', () => {
    if (!_canvasPlayer) return;
    const newMs = Math.min(_totalPlaybackMs, _currentPlaybackMs + 5000);
    _canvasPlayer.seekTo(newMs);
  });

  seekBar.addEventListener('mousedown', () => { _userSeeking = true; });
  seekBar.addEventListener('touchstart', () => { _userSeeking = true; }, { passive: true });
  seekBar.addEventListener('mouseup', () => { _userSeeking = false; });
  seekBar.addEventListener('touchend', () => { _userSeeking = false; });
  seekBar.addEventListener('input', () => {
    if (_canvasPlayer && _totalPlaybackMs > 0) {
      const ms = (seekBar.value / 1000) * _totalPlaybackMs;
      _canvasPlayer.seekTo(ms);
      _currentPlaybackMs = ms;
      _updateTimeDisplay(timeDisplay);
    }
  });

  speedSelect.addEventListener('change', () => {
    if (_canvasPlayer) {
      _canvasPlayer.playbackRate = parseFloat(speedSelect.value);
    }
  });

  // Keyboard shortcuts — only add once (can't clone document)
  if (!_keyboardListenerAdded) {
    _keyboardListenerAdded = true;
    document.addEventListener('keydown', (e) => {
      const editingView = document.getElementById('editingView');
      if (!editingView || editingView.style.display === 'none') return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

      if (e.code === 'Space') {
        e.preventDefault();
        document.getElementById('playerPlayBtn')?.click();
      } else if (e.code === 'ArrowLeft') {
        e.preventDefault();
        document.getElementById('playerBackBtn')?.click();
      } else if (e.code === 'ArrowRight') {
        e.preventDefault();
        document.getElementById('playerFwdBtn')?.click();
      }
    });
  }
}

function _updateTimeDisplay(el) {
  if (!el) return;
  const cur = _formatTime(_currentPlaybackMs);
  const tot = _formatTime(_totalPlaybackMs);
  el.textContent = cur + ' / ' + tot;
}

function _formatTime(ms) {
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return min + ':' + (sec < 10 ? '0' : '') + sec;
}

function _showPlayerControls() {
  const controls = document.getElementById('editingPlayerControls');
  if (controls) controls.style.display = '';
}

function timelineScrubberSeek(event) {
  if (!_canvasPlayer) return;
  const bar = document.getElementById('timelineScrubber');
  if (!bar) return;
  const rect = bar.getBoundingClientRect();
  const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  const totalMs = _canvasPlayer.totalDurMs || 0;
  if (totalMs > 0) _canvasPlayer.seekTo(pct * totalMs);
}

function toggleCanvasPlay() {
  if (_canvasPlayer) {
    _canvasPlayer.togglePlayPause();
    _showPlayerControls();
  }
}

// ── Drag & Drop (delegated handlers in _buildTimeline) ──────────────────

async function _saveClipOrder() {
  if (!currentProjectId) return;
  const order = _editingChunks.map((c, i) => ({ chunk_id: c.id, new_number: i + 1 }));
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reorder-chunks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order }),
    });
    showToast('Orden actualizado', 'success');
  } catch (e) {
    showToast('Error al reordenar: ' + e.message, 'error');
  }
}

async function renderFinalVideo() {
  if (!currentProjectId) return;
  const btn = document.getElementById('editingRenderBtn');
  const origText = btn ? btn.textContent : '🎬 Renderizar Video';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando render…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/render`, { method: 'POST' });
    showToast('🎬 Renderizado iniciado con FFmpeg.', 'success');
    await refreshEditing(currentProjectId);
    startEditingLogStream(currentProjectId);
  } catch (e) {
    showToast('Error al renderizar: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function cancelRender() {
  if (!currentProjectId) return;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/cancel-render`, { method: 'POST' });
    showToast('Render cancelado. Puedes reiniciarlo.', 'success');
    if (editingPollInterval) { clearInterval(editingPollInterval); editingPollInterval = null; }
    await refreshEditing(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function saveMotionPrompt(chunk_number) {
  if (!currentProjectId) return;
  const val = document.getElementById(`motion_prompt_${chunk_number}`).value;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/chunk/${chunk_number}/motion-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ motion_prompt: val })
    });
    showToast(`Prompt de movimiento #${chunk_number} guardado`, 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function regenerateVoiceover() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Descartar el voiceover actual y volver a configurar la voz?', 'Descartar')) return;

  try {
    await apiFetch(`/api/projects/${currentProjectId}/regenerate-voiceover`, { method: 'POST' });
    showToast('Voiceover descartado. Configura la voz y genera de nuevo.', 'info');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ── Init ──────────────────────────────────────────────────────────────────
_fetchSettings();
loadDashboard();
