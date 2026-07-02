/**
 * Royal Road TTS — Frontend Application
 *
 * Single-page app with library view, novel detail, and persistent audio player.
 */

// ===== State =====
const state = {
    novels: [],
    currentNovel: null,
    chapters: [],
    chapterPage: 1,
    chapterTotalPages: 1,
    // Player
    currentChapterId: null,
    currentNovelId: null,
    isPlaying: false,
    isSynthesizing: false,
    audio: new Audio(),
    progressInterval: null,
    saveInterval: null,
    // Settings
    settings: { voice: 'af_heart', speed: 1.0, playback_mode: 'full', auto_play: true, theme: 'dark', chapter_sort: 'asc' },
    voices: [],
    // Instant Play state
    _instantActive: false,    // whether instant play loop is running
    _instantSwapped: false,   // whether we've swapped to full file
    _instantElapsed: 0,       // cumulative seconds played across segments
};

// ===== Init =====
document.addEventListener('DOMContentLoaded', async () => {
    await loadSettings();
    applyTheme(state.settings.theme);
    await loadVoices();
    await loadLibrary();
    setupEventListeners();
    setupAudioEvents();
    applyPlaybackRate();
    updateAddNovelVisibility();
});

// ===== Theme =====
function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('btn-theme-toggle');
    btn.textContent = theme === 'dark' ? '\u{1F319}' : '\u{2600}\u{FE0F}';
}

function toggleTheme() {
    const newTheme = state.settings.theme === 'dark' ? 'light' : 'dark';
    state.settings.theme = newTheme;
    applyTheme(newTheme);
    updateSetting('theme', newTheme);
}

// ===== API Helpers =====
async function api(method, path, body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(path, opts);
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    if (resp.status === 204) return null;
    return resp.json();
}

function formatTime(seconds) {
    if (!seconds || isNaN(seconds)) return '--:--';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function showToast(msg, duration = 3000) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, duration);
}

function updateAddNovelVisibility() {
    const btn = document.getElementById('btn-add-novel');
    const isHome = document.getElementById('library-view').classList.contains('active');
    btn.style.display = isHome ? '' : 'none';
}

function goHome() {
    if (state.currentNovel) {
        closeNovel();
    }
}

// ===== Library =====
async function loadLibrary() {
    try {
        state.novels = await api('GET', '/api/novels');
        renderLibrary();
    } catch (e) {
        console.error('Failed to load library:', e);
    }
}

function renderLibrary() {
    const grid = document.getElementById('novel-grid');
    const empty = document.getElementById('library-empty');

    if (state.novels.length === 0) {
        grid.innerHTML = '';
        empty.style.display = 'block';
        return;
    }

    empty.style.display = 'none';
    grid.innerHTML = state.novels.map(novel => `
        <div class="novel-card" data-id="${novel.id}">
            <button class="novel-card-delete" data-id="${novel.id}" title="Remove">✕</button>
            ${novel.cover_url
                ? `<img class="novel-card-cover" src="${novel.cover_url}" alt="${novel.title}" loading="lazy">`
                : `<div class="novel-card-cover" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:2rem;">📖</div>`
            }
            <div class="novel-card-body">
                <div class="novel-card-title">${novel.title}</div>
                <div class="novel-card-author">${novel.author}</div>
                <div class="novel-card-progress">
                    <span>${novel.total_chapters} chapters</span>
                    ${novel.progress_chapter
                        ? `<span class="progress-badge">Ch. ${novel.progress_chapter}</span>`
                        : ''
                    }
                </div>
            </div>
        </div>
    `).join('');

    // Card click → open novel
    grid.querySelectorAll('.novel-card').forEach(card => {
        card.addEventListener('click', (e) => {
            if (e.target.closest('.novel-card-delete')) return;
            openNovel(parseInt(card.dataset.id));
        });
    });

    // Delete buttons
    grid.querySelectorAll('.novel-card-delete').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = parseInt(btn.dataset.id);
            if (confirm('Remove this novel from your library?')) {
                try {
                    await api('DELETE', `/api/novels/${id}`);
                    showToast('Novel removed');
                    await loadLibrary();
                } catch (err) {
                    showToast('Error: ' + err.message);
                }
            }
        });
    });
}

// ===== Add Novel =====
function openAddModal() {
    document.getElementById('modal-add').style.display = 'flex';
    document.getElementById('input-novel-url').value = '';
    document.getElementById('add-error').style.display = 'none';
    document.getElementById('add-loading').style.display = 'none';
    document.getElementById('input-novel-url').focus();
}

function closeAddModal() {
    document.getElementById('modal-add').style.display = 'none';
}

async function addNovel() {
    const url = document.getElementById('input-novel-url').value.trim();
    if (!url) return;

    const errorEl = document.getElementById('add-error');
    const loadingEl = document.getElementById('add-loading');
    const confirmBtn = document.getElementById('btn-add-confirm');

    errorEl.style.display = 'none';
    loadingEl.style.display = 'block';
    confirmBtn.disabled = true;

    try {
        await api('POST', '/api/novels', { url });
        closeAddModal();
        showToast('Novel added!');
        await loadLibrary();
    } catch (e) {
        errorEl.textContent = e.message;
        errorEl.style.display = 'block';
    } finally {
        loadingEl.style.display = 'none';
        confirmBtn.disabled = false;
    }
}

// ===== Novel Detail =====
async function openNovel(novelId) {
    const novel = state.novels.find(n => n.id === novelId);
    if (!novel) return;

    state.currentNovel = novel;
    state.chapterPage = 1;

    // Switch views
    document.getElementById('library-view').classList.remove('active');
    document.getElementById('novel-view').classList.add('active');
    updateAddNovelVisibility();

    // Populate header
    document.getElementById('novel-title').textContent = novel.title;
    document.getElementById('novel-author').textContent = `by ${novel.author}`;
    document.getElementById('novel-stats').textContent = `${novel.total_chapters} chapters`;
    const cover = document.getElementById('novel-cover');
    cover.src = novel.cover_url || '';
    cover.style.display = novel.cover_url ? 'block' : 'none';

    // Description
    const descEl = document.getElementById('novel-description');
    descEl.textContent = novel.description || '';

    // Auto-refresh chapters from Royal Road
    try {
        const result = await api('POST', `/api/novels/${novel.id}/refresh`);
        if (result.new_chapters > 0) {
            showToast(`${result.new_chapters} new chapter${result.new_chapters > 1 ? 's' : ''} found!`);
            novel.total_chapters = result.total_chapters;
            document.getElementById('novel-stats').textContent = `${result.total_chapters} chapters`;
        }
    } catch (e) {
        // Non-critical, just load existing chapters
    }

    await loadChapters();
}

function closeNovel() {
    document.getElementById('novel-view').classList.remove('active');
    document.getElementById('library-view').classList.add('active');
    state.currentNovel = null;
    updateAddNovelVisibility();
    loadLibrary();
}

async function loadChapters() {
    if (!state.currentNovel) return;

    try {
        const data = await api('GET', `/api/novels/${state.currentNovel.id}/chapters?page=${state.chapterPage}&per_page=50`);
        state.chapters = data.chapters;
        state.chapterTotalPages = data.total_pages;
        renderChapters();
    } catch (e) {
        console.error('Failed to load chapters:', e);
    }
}

function renderChapters() {
    const list = document.getElementById('chapter-list');

    list.innerHTML = state.chapters.map(ch => `
        <div class="chapter-row ${ch.is_current ? 'current' : ''}" data-id="${ch.id}">
            <span class="chapter-number">${ch.order}</span>
            <span class="chapter-title-text">${ch.title}</span>
            ${ch.word_count ? `<span class="chapter-meta">${(ch.word_count / 1000).toFixed(1)}k words</span>` : ''}
            ${ch.is_current ? '<span class="current-badge">Current</span>' : ''}
            <button class="chapter-play-btn" data-id="${ch.id}" title="Play">▶</button>
        </div>
    `).join('');

    // Click handlers
    list.querySelectorAll('.chapter-row').forEach(row => {
        row.addEventListener('click', () => playChapter(parseInt(row.dataset.id)));
    });

    list.querySelectorAll('.chapter-play-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            playChapter(parseInt(btn.dataset.id));
        });
    });

    renderPagination();
}

function renderPagination() {
    const el = document.getElementById('chapter-pagination');
    if (state.chapterTotalPages <= 1) {
        el.innerHTML = '';
        return;
    }

    let html = '';
    html += `<button ${state.chapterPage <= 1 ? 'disabled' : ''} data-page="${state.chapterPage - 1}">‹ Prev</button>`;
    for (let i = 1; i <= state.chapterTotalPages; i++) {
        if (state.chapterTotalPages > 7 && Math.abs(i - state.chapterPage) > 2 && i !== 1 && i !== state.chapterTotalPages) {
            if (i === state.chapterPage - 3 || i === state.chapterPage + 3) html += '<button disabled>…</button>';
            continue;
        }
        html += `<button class="${i === state.chapterPage ? 'active' : ''}" data-page="${i}">${i}</button>`;
    }
    html += `<button ${state.chapterPage >= state.chapterTotalPages ? 'disabled' : ''} data-page="${state.chapterPage + 1}">Next ›</button>`;

    el.innerHTML = html;
    el.querySelectorAll('button[data-page]').forEach(btn => {
        btn.addEventListener('click', () => {
            const p = parseInt(btn.dataset.page);
            if (p >= 1 && p <= state.chapterTotalPages) {
                state.chapterPage = p;
                loadChapters();
            }
        });
    });
}

async function refreshNovel() {
    if (!state.currentNovel) return;
    const btn = document.getElementById('btn-refresh');
    btn.disabled = true;
    btn.textContent = '↻ Refreshing...';

    try {
        const result = await api('POST', `/api/novels/${state.currentNovel.id}/refresh`);
        if (result.new_chapters > 0) {
            showToast(`${result.new_chapters} new chapter${result.new_chapters > 1 ? 's' : ''} found!`);
            state.currentNovel.total_chapters = result.total_chapters;
            document.getElementById('novel-stats').textContent = `${result.total_chapters} chapters`;
        } else {
            showToast('Already up to date');
        }
        await loadChapters();
    } catch (e) {
        showToast('Refresh failed: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '↻ Refresh';
    }
}

// ===== Audio Playback =====

function stopInstantPlay() {
    state._instantActive = false;
}

async function playChapter(chapterId) {
    const chapter = state.chapters.find(c => c.id === chapterId);
    if (!chapter) return;

    // Stop any current playback
    stopInstantPlay();
    state.audio.pause();
    state.audio.removeAttribute('src');
    state.audio.load();
    state.isPlaying = false;
    state._instantSwapped = false;

    state.currentChapterId = chapterId;
    state.currentNovelId = state.currentNovel?.id || null;

    // Show player
    const player = document.getElementById('mini-player');
    player.style.display = 'flex';

    document.getElementById('player-novel-title').textContent = state.currentNovel?.title || '';
    document.getElementById('player-chapter-title').textContent = chapter.title;
    document.getElementById('player-current-time').textContent = '0:00';
    document.getElementById('player-duration').textContent = '--:--';
    document.getElementById('player-scrubbar').value = 0;

    const loadingEl = document.getElementById('player-loading');
    const playBtn = document.getElementById('btn-play-pause');

    const mode = state.settings.playback_mode;

    // Start synthesis
    loadingEl.style.display = 'inline';
    loadingEl.textContent = (mode === 'instant') ? 'Starting...' : 'Synthesizing...';
    playBtn.textContent = '⏳';
    state.isSynthesizing = true;

    let synthResult;
    try {
        synthResult = await api('POST', `/api/chapters/${chapterId}/synthesize`);
    } catch (e) {
        showToast('Synthesis failed: ' + e.message);
        loadingEl.style.display = 'none';
        state.isSynthesizing = false;
        return;
    }

    if (state.currentChapterId !== chapterId) return;

    if (synthResult && synthResult.ready) {
        // Already synthesized — play full file directly
        state.isSynthesizing = false;
        loadingEl.style.display = 'none';
        await playFullFile(chapterId);
        return;
    }

    if (mode === 'instant') {
        await playInstant(chapterId);
    } else {
        await playFull(chapterId);
    }
}

async function playFull(chapterId) {
    const loadingEl = document.getElementById('player-loading');
    const playBtn = document.getElementById('btn-play-pause');

    // Poll until full file is ready
    let ready = false;
    while (!ready) {
        await new Promise(r => setTimeout(r, 1500));
        if (state.currentChapterId !== chapterId) return;
        try {
            const status = await api('GET', `/api/chapters/${chapterId}/status`);
            if (status.ready) {
                ready = true;
                if (status.duration_seconds) {
                    document.getElementById('player-duration').textContent = formatTime(status.duration_seconds);
                }
            }
        } catch (e) { break; }
    }

    state.isSynthesizing = false;
    loadingEl.style.display = 'none';
    if (state.currentChapterId !== chapterId) return;
    await playFullFile(chapterId);
}

async function playFullFile(chapterId) {
    const playBtn = document.getElementById('btn-play-pause');

    state.audio.src = `/api/chapters/${chapterId}/stream?t=${Date.now()}`;
    state.audio.load();

    await new Promise(resolve => {
        const onCanPlay = () => { state.audio.removeEventListener('canplay', onCanPlay); state.audio.removeEventListener('error', onErr); resolve(); };
        const onErr = () => { state.audio.removeEventListener('canplay', onCanPlay); state.audio.removeEventListener('error', onErr); resolve(); };
        state.audio.addEventListener('canplay', onCanPlay);
        state.audio.addEventListener('error', onErr);
    });

    if (state.currentChapterId !== chapterId) return;

    applyPlaybackRate();

    // Restore saved progress
    if (state.currentNovelId) {
        try {
            const progress = await api('GET', `/api/progress/${state.currentNovelId}`);
            if (progress.chapter_id === chapterId && progress.position_seconds > 0) {
                state.audio.currentTime = progress.position_seconds;
            }
        } catch (e) {}
    }

    try {
        await state.audio.play();
        state.isPlaying = true;
        playBtn.textContent = '⏸';
    } catch (e) {
        playBtn.textContent = '▶';
        state.isPlaying = false;
    }

    updateMediaSession();
    saveProgress();
    startProgressSaving();
}

async function playInstant(chapterId) {
    const loadingEl = document.getElementById('player-loading');
    const playBtn = document.getElementById('btn-play-pause');
    const durationEl = document.getElementById('player-duration');

    state._instantActive = true;
    state._instantSwapped = false;
    state._instantElapsed = 0;

    let nextSeg = 0;
    let totalDuration = 0;
    let segCount = 0;

    // Hide scrubbar during segment playback
    const scrubbar = document.getElementById('player-scrubbar');
    const currentTimeEl = document.getElementById('player-current-time');
    scrubbar.style.display = 'none';
    currentTimeEl.textContent = '';
    durationEl.textContent = 'Streaming...';

    // Helper: play a single segment via state.audio, returns a promise that
    // resolves when the segment finishes playing (or rejects on error)
    function playSegmentAudio(segUrl) {
        return new Promise((resolve, reject) => {
            state.audio.src = segUrl;
            applyPlaybackRate();
            state.audio.load();

            const onEnded = () => { cleanup(); resolve('ended'); };
            const onError = () => { cleanup(); reject(new Error('segment error')); };
            const onCanPlay = () => {
                state.audio.removeEventListener('canplaythrough', onCanPlay);
                state.audio.play().then(() => {
                    state.isPlaying = true;
                    playBtn.textContent = '⏸';
                    loadingEl.style.display = 'none';
                    state.isSynthesizing = false;
                    updateMediaSession();
                }).catch(() => {});
            };

            function cleanup() {
                state.audio.removeEventListener('ended', onEnded);
                state.audio.removeEventListener('error', onError);
                state.audio.removeEventListener('canplaythrough', onCanPlay);
            }

            state.audio.addEventListener('ended', onEnded);
            state.audio.addEventListener('error', onError);
            state.audio.addEventListener('canplaythrough', onCanPlay);
        });
    }

    // Main loop: poll for segments, play them one by one
    while (state._instantActive && state.currentChapterId === chapterId) {
        // Poll for segment availability
        let segData;
        try {
            segData = await api('GET', `/api/chapters/${chapterId}/segments`);
        } catch (e) { break; }

        if (state.currentChapterId !== chapterId || !state._instantActive) break;

        totalDuration = segData.total_duration || totalDuration;
        segCount = segData.segment_count;

        // If no segments but file is ready (chapter was pre-synthesized), swap immediately
        if (segData.segment_count === 0 && segData.file_ready) {
            state._instantSwapped = true;
            stopInstantPlay();
            scrubbar.style.display = '';
            loadingEl.style.display = 'none';
            state.isSynthesizing = false;
            await playFullFile(chapterId);
            return;
        }

        // Play any available segments we haven't played yet
        if (nextSeg < segData.segment_count) {
            const segUrl = `/api/chapters/${chapterId}/segments/${nextSeg}`;
            const segDur = segData.segment_durations[nextSeg] || 0;
            nextSeg++;

            try {
                await playSegmentAudio(segUrl);
                // Segment finished playing
                state._instantElapsed += segDur;
            } catch (e) {
                console.error('Segment play error:', e);
                break;
            }

            if (state.currentChapterId !== chapterId || !state._instantActive) break;

            // Show brief loading between segments
            loadingEl.style.display = 'inline';
            loadingEl.textContent = 'Loading next...';

            // After playing a segment, check if file is ready now
            try {
                const freshData = await api('GET', `/api/chapters/${chapterId}/segments`);
                if (freshData.file_ready && freshData.complete) {
                    // Swap to full file at current position
                    state._instantSwapped = true;
                    stopInstantPlay();

                    // Account for inter-segment silence in full file
                    const silencePerGap = 0.3;
                    const numGaps = Math.max(0, nextSeg - 1);
                    const seekTo = state._instantElapsed + (numGaps * silencePerGap);

                    state.audio.src = `/api/chapters/${chapterId}/stream?t=${Date.now()}`;
                    applyPlaybackRate();
                    state.audio.load();

                    await new Promise(resolve => {
                        const onReady = () => { state.audio.removeEventListener('canplaythrough', onReady); state.audio.removeEventListener('error', onErr); resolve(); };
                        const onErr = () => { state.audio.removeEventListener('canplaythrough', onReady); state.audio.removeEventListener('error', onErr); resolve(); };
                        state.audio.addEventListener('canplaythrough', onReady);
                        state.audio.addEventListener('error', onErr);
                    });

                    if (state.currentChapterId !== chapterId) return;

                    // Restore scrubbar now that full file is loaded
                    scrubbar.style.display = '';
                    state.audio.currentTime = Math.max(0, seekTo);
                    try {
                        await state.audio.play();
                        state.isPlaying = true;
                        playBtn.textContent = '⏸';
                        showToast('Switched to full file — screen off safe');
                    } catch (e) {
                        playBtn.textContent = '▶';
                        state.isPlaying = false;
                    }

                    updateMediaSession();
                    saveProgress();
                    startProgressSaving();
                    return;
                }
            } catch (e) {}

            // Continue to next segment immediately (no poll delay needed)
            continue;
        }

        // No new segments available yet — wait and poll again
        await new Promise(r => setTimeout(r, 300));
    }

    // Cleanup if we exit without swapping
    scrubbar.style.display = '';
    if (!state._instantSwapped) {
        stopInstantPlay();
    }
}

function togglePlayPause() {
    if (!state.audio.src) return;

    if (state.isPlaying) {
        state.audio.pause();
        state.isPlaying = false;
        document.getElementById('btn-play-pause').textContent = '▶';
        saveProgress();
    } else {
        state.audio.play().then(() => {
            state.isPlaying = true;
            document.getElementById('btn-play-pause').textContent = '⏸';
        }).catch(() => {});
    }
}

function seekRelative(seconds) {
    if (!state.audio.src) return;
    state.audio.currentTime = Math.max(0, Math.min(state.audio.duration || 0, state.audio.currentTime + seconds));
}

async function playAdjacentChapter(direction) {
    if (!state.currentNovelId || !state.currentChapterId) return;

    const current = state.chapters.find(c => c.id === state.currentChapterId);
    if (!current) return;

    const targetOrder = current.order + direction;
    let target = state.chapters.find(c => c.order === targetOrder);

    // If not on current page, fetch from API
    if (!target) {
        try {
            const data = await api('GET', `/api/novels/${state.currentNovelId}/chapters?page=1&per_page=10000`);
            target = data.chapters.find(c => c.order === targetOrder);
        } catch (e) {
            showToast(`Couldn't load chapter list: ${e.message}`);
            return;
        }
    }

    if (target) {
        playChapter(target.id);
    } else {
        showToast(direction > 0 ? 'No next chapter' : 'No previous chapter');
    }
}

function setupAudioEvents() {
    const audio = state.audio;
    const scrubbar = document.getElementById('player-scrubbar');
    const currentTime = document.getElementById('player-current-time');
    const duration = document.getElementById('player-duration');
    const loadingEl = document.getElementById('player-loading');

    audio.addEventListener('timeupdate', () => {
        if (state._instantActive) return;  // scrubbar hidden during segment playback
        if (!audio.duration) return;
        currentTime.textContent = formatTime(audio.currentTime);
        scrubbar.value = (audio.currentTime / audio.duration) * 100;
    });

    audio.addEventListener('loadedmetadata', () => {
        if (state._instantActive) return;  // don't show segment duration
        if (audio.duration && isFinite(audio.duration)) {
            duration.textContent = formatTime(audio.duration);
            scrubbar.max = 100;
        }
    });

    audio.addEventListener('canplay', () => {
        if (state._instantActive) return;  // handled by segment logic
        loadingEl.style.display = 'none';
    });

    audio.addEventListener('ended', async () => {
        // During Instant Play, segments end individually — don't trigger auto-play
        if (state._instantActive) return;

        state.isPlaying = false;
        document.getElementById('btn-play-pause').textContent = '▶';
        saveProgress();

        // Auto-play next chapter
        if (state.settings.auto_play) {
            await playAdjacentChapter(1);
        }
    });

    audio.addEventListener('error', () => {
        loadingEl.style.display = 'none';
        showToast('Audio playback error');
    });

    // Scrubbar interaction
    scrubbar.addEventListener('input', () => {
        if (audio.duration) {
            audio.currentTime = (scrubbar.value / 100) * audio.duration;
        }
    });
}

// ===== Media Session (lock screen controls) =====
function updateMediaSession() {
    if (!('mediaSession' in navigator)) return;

    const title = state.chapters?.find(c => c.id === state.currentChapterId)?.title || 'Chapter';
    const novel = state.currentNovel?.title || 'Royal Road TTS';

    navigator.mediaSession.metadata = new MediaMetadata({
        title: title,
        artist: novel,
        album: state.currentNovel?.author || '',
        artwork: state.currentNovel?.cover_url
            ? [{ src: state.currentNovel.cover_url, sizes: '512x512', type: 'image/jpeg' }]
            : [],
    });

    navigator.mediaSession.setActionHandler('play', () => togglePlayPause());
    navigator.mediaSession.setActionHandler('pause', () => togglePlayPause());
    navigator.mediaSession.setActionHandler('seekbackward', (details) => {
        seekRelative(-(details.seekOffset || 15));
    });
    navigator.mediaSession.setActionHandler('seekforward', (details) => {
        seekRelative(details.seekOffset || 30);
    });
    navigator.mediaSession.setActionHandler('previoustrack', () => playAdjacentChapter(-1));
    navigator.mediaSession.setActionHandler('nexttrack', () => playAdjacentChapter(1));
}

// ===== Progress Saving =====
async function saveProgress() {
    if (!state.currentNovelId || !state.currentChapterId) return;
    try {
        await api('PUT', `/api/progress/${state.currentNovelId}`, {
            chapter_id: state.currentChapterId,
            position_seconds: state.audio.currentTime || 0,
        });
    } catch (e) {
        console.error('Failed to save progress:', e);
    }
}

function startProgressSaving() {
    if (state.saveInterval) clearInterval(state.saveInterval);
    state.saveInterval = setInterval(() => {
        if (state.isPlaying) saveProgress();
    }, 10000);
}

// ===== Settings =====
async function loadSettings() {
    try {
        state.settings = await api('GET', '/api/settings');
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
}

async function loadVoices() {
    try {
        const data = await api('GET', '/api/voices');
        state.voices = data.voices || [];
    } catch (e) {
        console.error('Failed to load voices:', e);
    }
}

function openSettings() {
    document.getElementById('modal-settings').style.display = 'flex';

    // Populate voice dropdown
    const voiceSelect = document.getElementById('setting-voice');
    voiceSelect.innerHTML = state.voices.map(v =>
        `<option value="${v.id}" ${v.id === state.settings.voice ? 'selected' : ''}>${v.label}</option>`
    ).join('');

    // Speed
    document.getElementById('speed-value').textContent = `${state.settings.speed.toFixed(2)}x`;

    // Mode
    document.getElementById('setting-mode').value = state.settings.playback_mode;

    // Auto-play
    document.getElementById('setting-autoplay').checked = state.settings.auto_play;

    // Theme
    document.getElementById('setting-theme').value = state.settings.theme;

    // Chapter sort
    document.getElementById('setting-chapter-sort').value = state.settings.chapter_sort;
}

function applyPlaybackRate() {
    state.audio.playbackRate = state.settings.speed;
}

function closeSettings() {
    document.getElementById('modal-settings').style.display = 'none';
}

async function updateSetting(key, value) {
    try {
        state.settings = await api('PUT', '/api/settings', { [key]: value });
        // Apply live playback rate change
        if (key === 'speed') {
            applyPlaybackRate();
            document.getElementById('speed-value').textContent = `${state.settings.speed.toFixed(2)}x`;
        }
        // Reload chapters if sort order changed
        if (key === 'chapter_sort' && state.currentNovel) {
            await loadChapters();
        }
    } catch (e) {
        showToast('Failed to save setting: ' + e.message);
    }
}

// ===== Event Listeners =====
function setupEventListeners() {
    // Add novel
    document.getElementById('btn-add-novel').addEventListener('click', openAddModal);
    document.getElementById('btn-add-cancel').addEventListener('click', closeAddModal);
    document.getElementById('btn-add-confirm').addEventListener('click', addNovel);
    document.getElementById('input-novel-url').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') addNovel();
    });

    // Close modal on backdrop click
    document.getElementById('modal-add').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeAddModal();
    });
    document.getElementById('modal-settings').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeSettings();
    });

    // Novel detail
    document.getElementById('btn-back').addEventListener('click', closeNovel);
    document.getElementById('btn-refresh').addEventListener('click', refreshNovel);

    // Settings
    document.getElementById('btn-settings').addEventListener('click', openSettings);
    document.getElementById('btn-settings-close').addEventListener('click', closeSettings);

    document.getElementById('setting-voice').addEventListener('change', (e) => {
        updateSetting('voice', e.target.value);
    });

    document.getElementById('setting-mode').addEventListener('change', (e) => {
        updateSetting('playback_mode', e.target.value);
    });

    document.getElementById('setting-autoplay').addEventListener('change', (e) => {
        updateSetting('auto_play', e.target.checked);
    });

    document.getElementById('setting-theme').addEventListener('change', (e) => {
        state.settings.theme = e.target.value;
        applyTheme(e.target.value);
        updateSetting('theme', e.target.value);
    });

    document.getElementById('setting-chapter-sort').addEventListener('change', (e) => {
        updateSetting('chapter_sort', e.target.value);
    });

    document.getElementById('speed-down').addEventListener('click', () => {
        const newSpeed = Math.max(0.5, Math.round((state.settings.speed - 0.05) * 100) / 100);
        updateSetting('speed', newSpeed);
    });

    document.getElementById('speed-up').addEventListener('click', () => {
        const newSpeed = Math.min(2.0, Math.round((state.settings.speed + 0.05) * 100) / 100);
        updateSetting('speed', newSpeed);
    });

    // Theme toggle
    document.getElementById('btn-theme-toggle').addEventListener('click', toggleTheme);

    // Home button
    document.getElementById('btn-home').addEventListener('click', goHome);

    // Player controls
    document.getElementById('btn-play-pause').addEventListener('click', togglePlayPause);
    document.getElementById('btn-back-15').addEventListener('click', () => seekRelative(-15));
    document.getElementById('btn-fwd-30').addEventListener('click', () => seekRelative(30));
    document.getElementById('btn-prev-chapter').addEventListener('click', () => playAdjacentChapter(-1));
    document.getElementById('btn-next-chapter').addEventListener('click', () => playAdjacentChapter(1));

    // Player settings button opens settings modal
    document.getElementById('btn-player-settings').addEventListener('click', openSettings);
}
