/**
 * PreviewPlayer — Plays a single concatenated preview.mp4 with native controls.
 * Tracks which chunk is active based on video.currentTime vs chunk offsets.
 * Same callback interface as CanvasPlayer so app.js works with either.
 */
class PreviewPlayer {
  constructor(canvas, projectId, chunks, previewUrl) {
    this.canvas = canvas;
    this.projectId = projectId;
    this.chunks = chunks;
    this.currentIdx = 0;
    this.state = 'idle';
    this.playbackRate = 1;

    this.container = document.getElementById('editingPreviewScreen');

    this.onStateChange = null;
    this.onTimeUpdate = null;
    this.onChunkChange = null;
    this.onEnd = null;

    // Build offsets (cumulative ms) for chunk tracking
    this._offsets = [];
    let acc = 0;
    for (const c of chunks) {
      this._offsets.push(acc);
      acc += this._durOf(c);
    }
    this.totalDurMs = acc;

    // Create the single video element
    this._video = null;
    this._previewUrl = previewUrl;
    this._setupVideo();
  }

  _durOf(c) {
    return (c.start_ms != null && c.end_ms != null) ? (c.end_ms - c.start_ms) : 3800;
  }

  _setupVideo() {
    if (!this.container) return;

    // Hide placeholder/canvas
    const placeholder = document.getElementById('editingPreviewPlaceholder');
    if (placeholder) placeholder.style.display = 'none';
    if (this.canvas) this.canvas.style.display = 'none';
    const oldVid = document.getElementById('editingPreviewPlayer');
    if (oldVid) oldVid.style.display = 'none';

    // Remove any existing cp-active elements
    this.container.querySelectorAll('video.cp-active, img.cp-active').forEach(el => el.remove());

    const vid = document.createElement('video');
    vid.className = 'cp-active cp-preview';
    vid.controls = true;
    vid.autoplay = false;
    vid.playsInline = true;
    vid.preload = 'auto';
    vid.style.cssText = 'width:100%;height:100%;object-fit:contain;position:absolute;top:0;left:0;z-index:5;background:#000;';
    vid.src = this._previewUrl;
    this.container.appendChild(vid);
    this._video = vid;

    // Track time updates for chunk highlighting
    vid.addEventListener('timeupdate', () => {
      if (!this._video) return;
      const currentMs = this._video.currentTime * 1000;
      if (this.onTimeUpdate) {
        this.onTimeUpdate(currentMs, this.totalDurMs);
      }
      // Find which chunk we're in
      let idx = 0;
      for (let i = this._offsets.length - 1; i >= 0; i--) {
        if (currentMs >= this._offsets[i]) { idx = i; break; }
      }
      if (idx !== this.currentIdx) {
        this.currentIdx = idx;
        if (this.onChunkChange) this.onChunkChange(idx);
      }
    });

    vid.addEventListener('play', () => {
      this.state = 'playing';
      if (this.onStateChange) this.onStateChange(this.state);
    });

    vid.addEventListener('pause', () => {
      if (this._video && this._video.ended) return;
      this.state = 'paused';
      if (this.onStateChange) this.onStateChange(this.state);
    });

    vid.addEventListener('ended', () => {
      this.state = 'idle';
      if (this.onStateChange) this.onStateChange(this.state);
      if (this.onEnd) this.onEnd();
    });
  }

  play(fromIdx = 0) {
    if (!this._video) return;
    const seekMs = this._offsets[fromIdx] || 0;
    this._video.currentTime = seekMs / 1000;
    this._video.playbackRate = this.playbackRate;
    this._video.play().catch(() => {});
    this.currentIdx = fromIdx;
    if (this.onChunkChange) this.onChunkChange(fromIdx);
  }

  pause() {
    if (this._video) this._video.pause();
  }

  resume() {
    if (!this._video) return;
    this._video.playbackRate = this.playbackRate;
    this._video.play().catch(() => {});
  }

  togglePlayPause() {
    if (!this._video) return;
    if (this._video.paused || this._video.ended) {
      this._video.playbackRate = this.playbackRate;
      this._video.play().catch(() => {});
    } else {
      this._video.pause();
    }
  }

  stop() {
    if (this._video) {
      this._video.pause();
      this._video.currentTime = 0;
    }
    this.state = 'idle';
    if (this.onStateChange) this.onStateChange(this.state);
  }

  seekTo(ms) {
    if (!this._video) return;
    this._video.currentTime = Math.max(0, Math.min(ms, this.totalDurMs)) / 1000;
  }

  seekToTransition(chunkNumber) {
    const arrIdx = this.chunks.findIndex(c => c.chunk_number === chunkNumber);
    if (arrIdx <= 0) return;
    const seekMs = Math.max(0, (this._offsets[arrIdx - 1] || 0) + this._durOf(this.chunks[arrIdx - 1]) - 1500);
    this.seekTo(seekMs);
    if (this._video && this._video.paused) {
      this._video.playbackRate = this.playbackRate;
      this._video.play().catch(() => {});
    }
  }

  updateChunks(chunks) {
    this.chunks = chunks;
    this._offsets = [];
    let acc = 0;
    for (const c of chunks) { this._offsets.push(acc); acc += this._durOf(c); }
    this.totalDurMs = acc;
  }

  destroy() {
    if (this._video) {
      this._video.pause();
      this._video.src = '';
      this._video.load();
      this._video.remove();
      this._video = null;
    }
  }
}


/**
 * CanvasPlayer — Fallback sequential clip player (used when preview.mp4 isn't available).
 * Creates FRESH <video> elements per clip with blob caching.
 */
class CanvasPlayer {
  constructor(canvas, projectId, chunks) {
    this.canvas = canvas;
    this.projectId = projectId;
    this.chunks = chunks;
    this.currentIdx = 0;
    this.state = 'idle';
    this._timerInterval = null;
    this._advanceTimeout = null;
    this._loadingMedia = false;

    this.container = document.getElementById('editingPreviewScreen');

    this.audio = null;
    this._audioLoaded = false;
    this.playbackRate = 1;
    this.onStateChange = null;
    this.onTimeUpdate = null;
    this.onChunkChange = null;
    this.onEnd = null;

    // For resume: track when chunk started and how far we were
    this._chunkStartedAt = 0;   // performance.now() when chunk playback began
    this._chunkSeekMs = 0;      // seekMs used when starting this chunk
    this._chunkDurMsCurrent = 0; // duration of current chunk

    // ── Blob cache for instant transitions ──
    this._blobCache = new Map();    // idx → blobUrl
    this._fetching = new Set();     // idx currently being fetched
    this.PREFETCH_AHEAD = 5;        // how many clips to cache ahead
    this.CACHE_BEHIND = 3;          // keep this many behind current

    // ── Offsets for timeline progress ──
    this._offsets = [];
    let acc = 0;
    for (const c of chunks) {
      this._offsets.push(acc);
      acc += this._durOf(c);
    }
    this.totalDurMs = acc;

    this._loadAudio();
  }

  play(fromIdx = 0) {
    this.stop();
    this.currentIdx = fromIdx;
    this.state = 'playing';
    this._fireStateChange();
    this._prefetchAround(fromIdx);
    // Sync audio ONCE at play start — audio is master clock from here on
    const startMs = this.chunks[fromIdx]?.start_ms ?? this._offsets[fromIdx] ?? 0;
    this._syncAudioTo(startMs);
    this._startChunk(fromIdx, 0);
    this._startUITimer(); // Timer runs continuously for entire playback
  }

  pause() {
    if (this.state !== 'playing') return;
    this.state = 'paused';

    const vid = this.container ? this.container.querySelector('video.cp-active') : null;
    if (vid) vid.pause();
    if (this.audio) this.audio.pause();

    this._clearAdvanceTimeout();
    this._stopUITimer();

    this._fireStateChange();
  }

  resume() {
    if (this.state !== 'paused') return;
    this.state = 'playing';

    const vid = this.container ? this.container.querySelector('video.cp-active') : null;
    if (vid) {
      vid.playbackRate = this.playbackRate;
      vid.play().catch(() => {});
    }
    if (this.audio) {
      this.audio.playbackRate = this.playbackRate;
      this.audio.play().catch(() => {});
    }

    // Audio-driven timer handles advancement — no need for advance timeout
    this._startUITimer();

    this._fireStateChange();
  }

  togglePlayPause() {
    if (this.state === 'playing') this.pause();
    else if (this.state === 'paused') this.resume();
    else this.play(0);
  }

  stop() {
    this.state = 'idle';
    this._loadingMedia = false;
    this._clearAdvanceTimeout();
    this._stopUITimer();
    this._removeAllMedia();
    if (this.audio) this.audio.pause();
    this._fireStateChange();
  }

  seekToTransition(chunkNumber) {
    const arrIdx = this.chunks.findIndex(c => c.chunk_number === chunkNumber);
    if (arrIdx <= 0) return;
    const seekMs = Math.max(0, (this._offsets[arrIdx - 1] || 0) + this._durOf(this.chunks[arrIdx - 1]) - 1500);
    this.seekTo(seekMs);
  }

  seekTo(ms) {
    const targetMs = Math.max(0, Math.min(ms, this.totalDurMs));
    let idx = 0;
    for (let i = this._offsets.length - 1; i >= 0; i--) {
      if (targetMs >= this._offsets[i]) { idx = i; break; }
    }
    this.stop();
    this.state = 'playing';
    this._fireStateChange();
    // Sync audio to target position
    this._syncAudioTo(targetMs);
    this._prefetchAround(idx);
    this._startChunk(idx, targetMs - this._offsets[idx]);
    this._startUITimer(); // Timer runs continuously
  }

  updateChunks(chunks) {
    this.chunks = chunks;
    this._offsets = [];
    let acc = 0;
    for (const c of chunks) { this._offsets.push(acc); acc += this._durOf(c); }
    this.totalDurMs = acc;
  }

  destroy() {
    this.stop();
    for (const [, url] of this._blobCache) {
      URL.revokeObjectURL(url);
    }
    this._blobCache.clear();
    this._fetching.clear();
  }

  // ── Audio ─────────────────────────────────────────────────────────────

  _loadAudio() {
    this.audio = new Audio();
    this.audio.preload = 'auto';
    this.audio.src = `/api/projects/${this.projectId}/voiceover/audio`;
    this.audio.addEventListener('canplaythrough', () => { this._audioLoaded = true; }, { once: true });
    this.audio.load();
  }

  // Get current audio position in ms (master clock), or null if unavailable
  _getAudioMs() {
    if (this.audio && this.audio.readyState >= 2 && !isNaN(this.audio.currentTime)) {
      return this.audio.currentTime * 1000;
    }
    return null;
  }

  _syncAudioTo(ms) {
    if (!this.audio) return;
    const doSync = () => {
      this.audio.currentTime = ms / 1000;
      this.audio.playbackRate = this.playbackRate;
      if (this.state === 'playing') {
        this.audio.play().catch(() => {});
      }
    };
    if (this._audioLoaded || this.audio.readyState >= 1) {
      doSync();
    } else {
      // Wait for audio to be ready, then sync
      const onReady = () => {
        this.audio.removeEventListener('canplay', onReady);
        this._audioLoaded = true;
        doSync();
      };
      this.audio.addEventListener('canplay', onReady);
    }
  }

  // ── Blob Cache ──────────────────────────────────────────────────────

  _chunkMediaUrl(chunk) {
    const cb = chunk.updated_at ? `?t=${new Date(chunk.updated_at).getTime()}` : `?t=${Date.now()}`;
    if (chunk.video_path) return `/api/projects/${this.projectId}/chunk/${chunk.chunk_number}/video${cb}`;
    if (chunk.image_path) return `/api/projects/${this.projectId}/chunk/${chunk.chunk_number}/image${cb}`;
    return null;
  }

  // FIX #4: Determine if chunk is image-only (no video_path)
  _isImageOnly(chunk) {
    return !chunk.video_path && !!chunk.image_path;
  }

  _fetchBlob(idx) {
    if (idx < 0 || idx >= this.chunks.length) return;
    if (this._blobCache.has(idx) || this._fetching.has(idx)) return;

    const chunk = this.chunks[idx];
    const url = this._chunkMediaUrl(chunk);
    if (!url) return;

    this._fetching.add(idx);
    fetch(url)
      .then(r => r.blob())
      .then(blob => {
        this._fetching.delete(idx);
        if (!this._blobCache.has(idx)) {
          this._blobCache.set(idx, URL.createObjectURL(blob));
        }
      })
      .catch(() => {
        this._fetching.delete(idx);
      });
  }

  _prefetchAround(idx) {
    for (let i = 0; i < this.PREFETCH_AHEAD; i++) {
      this._fetchBlob(idx + i);
    }
    this._cleanupCache(idx);
  }

  _cleanupCache(currentIdx) {
    for (const [cachedIdx, url] of this._blobCache) {
      if (cachedIdx < currentIdx - this.CACHE_BEHIND) {
        URL.revokeObjectURL(url);
        this._blobCache.delete(cachedIdx);
      }
    }
  }

  _getBestUrl(idx) {
    if (this._blobCache.has(idx)) return this._blobCache.get(idx);
    const chunk = this.chunks[idx];
    return chunk ? this._chunkMediaUrl(chunk) : null;
  }

  // ── Internal ──────────────────────────────────────────────────────────

  _durOf(c) {
    return (c.start_ms != null && c.end_ms != null) ? (c.end_ms - c.start_ms) : 3800;
  }

  _chunkDurMs(idx) {
    return this.chunks[idx] ? this._durOf(this.chunks[idx]) : 0;
  }

  _createVideoEl(url) {
    const vid = document.createElement('video');
    vid.className = 'cp-active';
    vid.controls = false;
    vid.autoplay = false;
    vid.muted = true;
    vid.playsInline = true;
    vid.preload = 'auto';
    vid.style.cssText = 'width:100%;height:100%;object-fit:contain;position:absolute;top:0;left:0;z-index:5;background:#000;';
    vid.src = url;
    return vid;
  }

  // FIX #4: Create <img> element for image-only chunks
  _createImageEl(url) {
    const img = document.createElement('img');
    img.className = 'cp-active';
    img.style.cssText = 'width:100%;height:100%;object-fit:contain;position:absolute;top:0;left:0;z-index:5;background:#000;';
    img.src = url;
    return img;
  }

  // FIX #5: Properly clean up both video and img elements
  _removeAllMedia() {
    if (!this.container) return;
    this.container.querySelectorAll('video.cp-active, img.cp-active').forEach(el => {
      if (el.tagName === 'VIDEO') {
        el.pause();
        el.oncanplay = null;
        el.onended = null;
        el.onerror = null;
        // FIX #5: Use load() to properly reset instead of removeAttribute('src')
        el.src = '';
        el.load();
      }
      el.remove();
    });
  }

  _startChunk(idx, seekMs = 0) {
    this._clearAdvanceTimeout();

    if (idx >= this.chunks.length) {
      if (this.audio) this.audio.pause();
      this.state = 'idle';
      this._stopUITimer();
      this._loadingMedia = false;
      this._fireStateChange();
      if (this.onEnd) this.onEnd();
      return;
    }

    // ── Skip-ahead: if audio already passed this chunk, jump to correct one ──
    const audioNow = this._getAudioMs();
    if (audioNow != null) {
      for (let i = this.chunks.length - 1; i > idx; i--) {
        const cs = this.chunks[i].start_ms ?? this._offsets[i] ?? Infinity;
        if (audioNow >= cs) { idx = i; break; }
      }
    }

    this.currentIdx = idx;
    if (this.onChunkChange) this.onChunkChange(idx);

    const chunk = this.chunks[idx];
    const url = this._getBestUrl(idx);
    const chunkDur = this._chunkDurMs(idx);
    const chunkStartMs = chunk.start_ms ?? this._offsets[idx] ?? 0;

    this._chunkDurMsCurrent = chunkDur;

    if (!this.container || !url) {
      // No media — use fallback timeout, timer handles audio-driven advancement
      this._loadingMedia = false;
      this._chunkStartedAt = performance.now();
      this._chunkSeekMs = 0;
      const remainMs = Math.max(50, chunkDur / this.playbackRate);
      this._advanceTimeout = setTimeout(() => {
        if (this.state === 'playing' && !this._loadingMedia) this._advanceToNext();
      }, remainMs);
      this._prefetchAround(idx + 1);
      return;
    }

    // ── Transition: keep old media for crossfade, remove after animation ──
    const transition = chunk.transition || null;
    const transDur = chunk.transition_duration || 500;
    const oldMedia = this.container.querySelectorAll('video.cp-active, img.cp-active');

    if (transition && oldMedia.length > 0 && seekMs === 0) {
      oldMedia.forEach(el => {
        el.style.zIndex = '4';
        this._applyTransitionOut(el, transition, transDur);
      });
      setTimeout(() => {
        oldMedia.forEach(el => {
          if (el.tagName === 'VIDEO') { el.pause(); el.src = ''; el.load(); }
          el.remove();
        });
      }, transDur + 50);
    } else {
      this._removeAllMedia();
    }

    // Hide placeholder/canvas
    const placeholder = document.getElementById('editingPreviewPlaceholder');
    if (placeholder) placeholder.style.display = 'none';
    if (this.canvas) this.canvas.style.display = 'none';
    const oldVid = document.getElementById('editingPreviewPlayer');
    if (oldVid) oldVid.style.display = 'none';

    const isCached = this._blobCache.has(idx);
    const isImage = this._isImageOnly(chunk);

    this._loadingMedia = true; // Flag: media is loading

    if (isImage) {
      const img = this._createImageEl(url);
      if (transition && seekMs === 0) this._applyTransitionIn(img, transition, transDur);
      this.container.appendChild(img);

      const onImgReady = () => {
        this._loadingMedia = false;
        if (this.state !== 'playing') return;

        // Compute actual seek from audio position (catch up to audio)
        const audioMs = this._getAudioMs();
        let actualSeek = seekMs;
        if (audioMs != null) {
          actualSeek = Math.max(seekMs, audioMs - chunkStartMs);
        }
        this._chunkSeekMs = actualSeek;
        this._chunkStartedAt = performance.now();

        // Fallback advance timeout (audio-driven timer is primary)
        const remainMs = Math.max(100, (chunkDur - actualSeek) / this.playbackRate);
        this._advanceTimeout = setTimeout(() => {
          if (this.state === 'playing') this._advanceToNext();
        }, remainMs);

        this._prefetchAround(idx + 1);
      };

      if (img.complete) {
        onImgReady();
      } else {
        img.onload = onImgReady;
        img.onerror = () => {
          console.error(`[Player] Chunk ${idx} image load error, skipping`);
          this._loadingMedia = false;
          if (isCached) {
            URL.revokeObjectURL(this._blobCache.get(idx));
            this._blobCache.delete(idx);
          }
          setTimeout(() => this._advanceToNext(), 100);
        };
      }
    } else {
      // Video chunk
      const vid = this._createVideoEl(url);
      if (transition && seekMs === 0) this._applyTransitionIn(vid, transition, transDur);
      this.container.appendChild(vid);

      const onReady = () => {
        vid.oncanplay = null;
        this._loadingMedia = false;
        if (this.state !== 'playing') return;

        // Compute actual seek from audio position (catch up to audio)
        const audioMs = this._getAudioMs();
        let actualSeek = seekMs;
        if (audioMs != null) {
          actualSeek = Math.max(seekMs, audioMs - chunkStartMs);
        }
        this._chunkSeekMs = actualSeek;

        if (actualSeek > 0) vid.currentTime = actualSeek / 1000;
        vid.playbackRate = this.playbackRate;
        this._chunkStartedAt = performance.now();

        vid.play().catch(() => {});

        // Fallback advance timeout (audio-driven timer is primary)
        const remainMs = Math.max(100, (chunkDur - actualSeek) / this.playbackRate);
        this._advanceTimeout = setTimeout(() => {
          if (this.state === 'playing') this._advanceToNext();
        }, remainMs);

        this._prefetchAround(idx + 1);
      };

      if (vid.readyState >= 3) {
        onReady();
      } else {
        vid.oncanplay = onReady;
      }

      vid.onended = null;

      vid.onerror = () => {
        console.error(`[Player] Chunk ${idx} video load error, skipping`);
        this._loadingMedia = false;
        if (isCached) {
          URL.revokeObjectURL(this._blobCache.get(idx));
          this._blobCache.delete(idx);
        }
        setTimeout(() => this._advanceToNext(), 100);
      };
    }
  }

  _advanceToNext() {
    if (this._loadingMedia) return; // Don't advance while loading
    this._clearAdvanceTimeout();
    // Don't stop UI timer — it runs continuously
    this._startChunk(this.currentIdx + 1, 0);
  }

  // Audio-driven UI timer: runs CONTINUOUSLY during playback.
  // Uses audio.currentTime as master clock for progress display AND chunk advancement.
  _startUITimer() {
    this._stopUITimer();
    this._timerInterval = setInterval(() => {
      if (this.state !== 'playing') return;

      // Use audio as master clock when available
      const audioMs = this._getAudioMs();
      let globalMs;
      if (audioMs != null) {
        globalMs = audioMs;
      } else {
        // Fallback: offset + elapsed (for when audio hasn't loaded yet)
        const elapsed = (performance.now() - (this._chunkStartedAt || performance.now())) * this.playbackRate;
        globalMs = (this._offsets[this.currentIdx] || 0) + (this._chunkSeekMs || 0) + elapsed;
      }

      // ALWAYS update time display — even during media loading
      if (this.onTimeUpdate) this.onTimeUpdate(Math.min(globalMs, this.totalDurMs), this.totalDurMs);

      // Audio-driven chunk advancement (only when not loading media)
      if (!this._loadingMedia && audioMs != null && this.currentIdx < this.chunks.length - 1) {
        let targetIdx = this.currentIdx;
        for (let i = this.chunks.length - 1; i > this.currentIdx; i--) {
          const chunkStart = this.chunks[i].start_ms ?? this._offsets[i] ?? Infinity;
          if (audioMs >= chunkStart) {
            targetIdx = i;
            break;
          }
        }
        if (targetIdx > this.currentIdx) {
          // Audio has advanced past current chunk — advance visuals to match
          this._clearAdvanceTimeout();
          this._startChunk(targetIdx, 0);
          return;
        }
      }

      // Check for end of playback
      if (globalMs >= this.totalDurMs - 200) {
        this._stopUITimer();
        this._clearAdvanceTimeout();
        this._removeAllMedia();
        if (this.audio) this.audio.pause();
        this.state = 'idle';
        this._loadingMedia = false;
        this._fireStateChange();
        if (this.onEnd) this.onEnd();
      }
    }, 100);
  }

  _stopUITimer() {
    if (this._timerInterval) { clearInterval(this._timerInterval); this._timerInterval = null; }
    // NOTE: Does NOT touch _advanceTimeout anymore!
  }

  _clearAdvanceTimeout() {
    if (this._advanceTimeout) { clearTimeout(this._advanceTimeout); this._advanceTimeout = null; }
  }

  _fireStateChange() {
    if (this.onStateChange) this.onStateChange(this.state);
  }

  // ── Transition animations ────────────────────────────────────────────

  _applyTransitionIn(el, transition, durationMs) {
    const dur = durationMs + 'ms';
    el.style.zIndex = '5';
    switch (transition) {
      case 'fade': case 'dissolve':
        el.style.opacity = '0';
        el.style.transition = `opacity ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.opacity = '1'; }));
        break;
      case 'fadeblack':
        el.style.opacity = '0';
        el.style.transition = `opacity ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.opacity = '1'; }));
        break;
      case 'fadewhite':
        el.style.opacity = '0';
        el.style.transition = `opacity ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.opacity = '1'; }));
        break;
      case 'slideleft': case 'smoothleft':
        el.style.transform = 'translateX(100%)';
        el.style.transition = `transform ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.transform = 'translateX(0)'; }));
        break;
      case 'slideright': case 'smoothright':
        el.style.transform = 'translateX(-100%)';
        el.style.transition = `transform ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.transform = 'translateX(0)'; }));
        break;
      case 'slideup': case 'smoothup':
        el.style.transform = 'translateY(100%)';
        el.style.transition = `transform ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.transform = 'translateY(0)'; }));
        break;
      case 'slidedown': case 'smoothdown':
        el.style.transform = 'translateY(-100%)';
        el.style.transition = `transform ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.transform = 'translateY(0)'; }));
        break;
      case 'zoomin':
        el.style.transform = 'scale(0.3)';
        el.style.opacity = '0';
        el.style.transition = `transform ${dur} ease, opacity ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => {
          el.style.transform = 'scale(1)';
          el.style.opacity = '1';
        }));
        break;
      case 'wipeleft':
        el.style.clipPath = 'inset(0 100% 0 0)';
        el.style.transition = `clip-path ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.clipPath = 'inset(0 0 0 0)'; }));
        break;
      case 'wiperight':
        el.style.clipPath = 'inset(0 0 0 100%)';
        el.style.transition = `clip-path ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.clipPath = 'inset(0 0 0 0)'; }));
        break;
      case 'wipeup':
        el.style.clipPath = 'inset(100% 0 0 0)';
        el.style.transition = `clip-path ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.clipPath = 'inset(0 0 0 0)'; }));
        break;
      case 'wipedown':
        el.style.clipPath = 'inset(0 0 100% 0)';
        el.style.transition = `clip-path ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.clipPath = 'inset(0 0 0 0)'; }));
        break;
      case 'circleopen':
        el.style.clipPath = 'circle(0% at 50% 50%)';
        el.style.transition = `clip-path ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.clipPath = 'circle(75% at 50% 50%)'; }));
        break;
      case 'circleclose':
        el.style.clipPath = 'circle(75% at 50% 50%)';
        el.style.transition = `clip-path ${dur} ease`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.clipPath = 'circle(0% at 50% 50%)'; }));
        break;
      case 'radial':
        el.style.clipPath = 'circle(0% at 50% 50%)';
        el.style.transition = `clip-path ${dur} ease-out`;
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.clipPath = 'circle(75% at 50% 50%)'; }));
        break;
      default:
        // No recognized transition — just show immediately
        el.style.opacity = '1';
        break;
    }
  }

  _applyTransitionOut(el, transition, durationMs) {
    const dur = durationMs + 'ms';
    switch (transition) {
      case 'fade': case 'dissolve':
        el.style.transition = `opacity ${dur} ease`;
        el.style.opacity = '0';
        break;
      case 'fadeblack': case 'fadewhite':
        el.style.transition = `opacity ${dur} ease`;
        el.style.opacity = '0';
        break;
      case 'slideleft': case 'smoothleft':
        el.style.transition = `transform ${dur} ease`;
        el.style.transform = 'translateX(-100%)';
        break;
      case 'slideright': case 'smoothright':
        el.style.transition = `transform ${dur} ease`;
        el.style.transform = 'translateX(100%)';
        break;
      case 'slideup': case 'smoothup':
        el.style.transition = `transform ${dur} ease`;
        el.style.transform = 'translateY(-100%)';
        break;
      case 'slidedown': case 'smoothdown':
        el.style.transition = `transform ${dur} ease`;
        el.style.transform = 'translateY(100%)';
        break;
      case 'zoomin':
        el.style.transition = `transform ${dur} ease, opacity ${dur} ease`;
        el.style.transform = 'scale(1.5)';
        el.style.opacity = '0';
        break;
      case 'wipeleft':
        el.style.transition = `clip-path ${dur} ease`;
        el.style.clipPath = 'inset(0 0 0 100%)';
        break;
      case 'wiperight':
        el.style.transition = `clip-path ${dur} ease`;
        el.style.clipPath = 'inset(0 100% 0 0)';
        break;
      case 'wipeup':
        el.style.transition = `clip-path ${dur} ease`;
        el.style.clipPath = 'inset(0 0 100% 0)';
        break;
      case 'wipedown':
        el.style.transition = `clip-path ${dur} ease`;
        el.style.clipPath = 'inset(100% 0 0 0)';
        break;
      case 'circleopen': case 'circleclose': case 'radial':
        el.style.transition = `clip-path ${dur} ease`;
        el.style.clipPath = 'circle(0% at 50% 50%)';
        break;
      default:
        el.style.transition = `opacity ${dur} ease`;
        el.style.opacity = '0';
        break;
    }
  }
}
