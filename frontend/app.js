/**
 * Royal Road TTS — Frontend Application
 *
 * Single-page app with library view, novel detail, and persistent audio player.
 */

// ===== State =====
const state = {
    novels: [],
    currentNovel: null,
    // Library organization
    libraryTab: location.hash === '#favorites' ? 'favorites' : 'all',
    librarySort: localStorage.getItem('librarySort') || 'added',
    libraryView: localStorage.getItem('libraryView') || 'grid',
    exportSpeed: 1.0,
    _dragging: false,
    _suppressClick: false,
    chapters: [],
    chapterPage: 1,
    chapterTotalPages: 1,
    chapterTotal: 0,
    // Player (decoupled from the browsed novel so navigation can't hijack playback)
    playback: {
        novel: null,      // novel being played
        chapters: [],     // full ascending chapter list for that novel
        chapter: null,    // chapter currently loaded in the player
    },
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
    // Kick the server-side favorites sync (new chapters + pre-downloads).
    // If it actually starts, watch for it to finish and re-render the
    // library so fresh chapter/unread counts show without a reload.
    api('POST', '/api/library/refresh-favorites')
        .then(res => { if (res && res.started) watchFavoritesSync(); })
        .catch(() => {});

    await loadSettings();
    applyTheme(state.settings.theme);
    await loadVoices();
    await loadLibrary();
    setupEventListeners();
    setupAudioEvents();
    applyPlaybackRate();
    updateAddNovelVisibility();
    applyLibraryTab();
    document.getElementById('library-sort').value = state.librarySort;
    applyLibraryView();
    startExportsPolling(); // stops itself when no jobs are active
});

window.addEventListener('hashchange', () => {
    state.libraryTab = location.hash === '#favorites' ? 'favorites' : 'all';
    applyLibraryTab();
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

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
async function watchFavoritesSync() {
    // Poll while the favorites sync runs (it yields to playback, so allow a
    // generous window), then refresh the library once for new unread counts.
    for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 3000));
        try {
            const s = await api('GET', '/api/library/sync-status');
            if (!s.running) {
                await loadLibrary();
                return;
            }
        } catch (e) {
            return; // server unreachable — a page reload will catch up
        }
    }
}

async function loadLibrary() {
    try {
        state.novels = await api('GET', '/api/novels');
        renderLibrary();
    } catch (e) {
        console.error('Failed to load library:', e);
    }
}

function setLibraryTab(tab) {
    state.libraryTab = tab;
    history.replaceState(null, '', tab === 'favorites' ? '#favorites' : location.pathname);
    applyLibraryTab();
}

function applyLibraryTab() {
    document.getElementById('tab-all').classList.toggle('active', state.libraryTab === 'all');
    document.getElementById('tab-favorites').classList.toggle('active', state.libraryTab === 'favorites');
    renderLibrary();
}

function sortedNovels() {
    let list = state.novels.slice();
    if (state.libraryTab === 'favorites') {
        list = list.filter(n => n.favorite);
    }
    const comparators = {
        listened: (a, b) => (b.progress_updated_at || '').localeCompare(a.progress_updated_at || ''),
        added: (a, b) => (b.created_at || '').localeCompare(a.created_at || ''),
        title: (a, b) => a.title.localeCompare(b.title),
        custom: (a, b) => (a.sort_order ?? Infinity) - (b.sort_order ?? Infinity),
    };
    list.sort(comparators[state.librarySort] || comparators.added);
    if (state.libraryTab === 'all') {
        // Favorites group first; sort() is stable so order within groups holds
        list.sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0));
    }
    return list;
}

function applyLibraryView() {
    const isList = state.libraryView === 'list';
    const btn = document.getElementById('library-view-toggle');
    btn.textContent = isList ? '▦' : '☰';
    btn.title = isList ? 'Switch to grid view' : 'Switch to list view';
    document.getElementById('novel-grid').classList.toggle('novel-list', isList);
}

function unreadCount(novel) {
    // Chapters beyond the one you're on; 0 for never-started novels
    // (their total already says it all)
    if (!novel.progress_chapter) return 0;
    return Math.max(0, (novel.total_chapters || 0) - novel.progress_chapter);
}

function novelCardHtml(novel) {
    const unread = unreadCount(novel);
    return `
        <div class="novel-card" data-id="${novel.id}">
            <button class="novel-card-fav${novel.favorite ? ' is-fav' : ''}" data-id="${novel.id}" title="${novel.favorite ? 'Unfavorite' : 'Favorite'}">${novel.favorite ? '⭐' : '☆'}</button>
            <button class="novel-card-delete" data-id="${novel.id}" title="Remove">✕</button>
            <div class="novel-card-cover-wrap">
                ${novel.cover_url
                    ? `<img class="novel-card-cover" src="${escapeHtml(novel.cover_url)}" alt="${escapeHtml(novel.title)}" loading="lazy" draggable="false">`
                    : `<div class="novel-card-cover" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:2rem;">📖</div>`
                }
                ${unread > 0 ? `<span class="unread-blob" title="${unread} unread chapters">${unread > 99 ? '99+' : unread}</span>` : ''}
            </div>
            <div class="novel-card-body">
                <div class="novel-card-title">${escapeHtml(novel.title)}</div>
                <div class="novel-card-author">${escapeHtml(novel.author)}</div>
                <div class="novel-card-progress">
                    <span>${novel.total_chapters} chapters</span>
                    ${novel.progress_chapter
                        ? `<span class="progress-badge" data-novel-id="${novel.id}" title="Resume from here">▶ Ch. ${novel.progress_chapter}</span>`
                        : ''
                    }
                </div>
            </div>
        </div>
    `;
}

function novelRowHtml(novel) {
    const unread = unreadCount(novel);
    return `
        <div class="novel-card novel-card--row" data-id="${novel.id}">
            ${novel.cover_url
                ? `<img class="novel-row-cover" src="${escapeHtml(novel.cover_url)}" alt="" loading="lazy" draggable="false">`
                : `<div class="novel-row-cover novel-row-cover--empty">📖</div>`
            }
            <div class="novel-row-text">
                <div class="novel-card-title">${escapeHtml(novel.title)}</div>
                <div class="novel-card-author">${escapeHtml(novel.author)}</div>
            </div>
            <div class="novel-row-meta">
                ${unread > 0 ? `<span class="unread-count">${unread} unread</span>` : ''}
                ${novel.progress_chapter
                    ? `<span class="progress-badge" data-novel-id="${novel.id}" title="Resume from here">▶ Ch. ${novel.progress_chapter}</span>`
                    : `<span class="novel-row-chapters">${novel.total_chapters} chs</span>`
                }
                <button class="novel-card-fav${novel.favorite ? ' is-fav' : ''}" data-id="${novel.id}" title="${novel.favorite ? 'Unfavorite' : 'Favorite'}">${novel.favorite ? '⭐' : '☆'}</button>
                <button class="novel-card-delete" data-id="${novel.id}" title="Remove">✕</button>
            </div>
        </div>
    `;
}

function renderLibrary() {
    const grid = document.getElementById('novel-grid');
    const empty = document.getElementById('library-empty');
    const novels = sortedNovels();

    if (novels.length === 0) {
        grid.innerHTML = '';
        empty.innerHTML = state.libraryTab === 'favorites'
            ? '<p>No favorites yet.</p><p>Tap the ☆ on a novel to add it here.</p>'
            : '<p>Your library is empty.</p><p>Click <strong>+ Add Novel</strong> to get started.</p>';
        empty.style.display = 'block';
        return;
    }

    empty.style.display = 'none';
    const isList = state.libraryView === 'list';
    grid.innerHTML = novels.map(n => isList ? novelRowHtml(n) : novelCardHtml(n)).join('');

    // Card click → open novel (suppressed right after a drag)
    grid.querySelectorAll('.novel-card').forEach(card => {
        card.addEventListener('click', (e) => {
            if (state._suppressClick) return;
            if (e.target.closest('.novel-card-delete') || e.target.closest('.novel-card-fav')) return;
            openNovel(parseInt(card.dataset.id));
        });
        setupCardDrag(card);
    });

    // Favorite stars
    grid.querySelectorAll('.novel-card-fav').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleFavorite(parseInt(btn.dataset.id));
        });
    });

    // Progress badges → resume shortcut
    grid.querySelectorAll('.progress-badge').forEach(badge => {
        badge.addEventListener('click', (e) => {
            e.stopPropagation();
            openNovel(parseInt(badge.dataset.novelId), { resume: true });
        });
    });

    // Delete buttons
    grid.querySelectorAll('.novel-card-delete').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = parseInt(btn.dataset.id);
            const novel = state.novels.find(n => n.id === id);
            const msg = novel?.source === 'epub'
                ? 'Remove this book? Its EPUB file will also be deleted from the EPUBs folder.'
                : 'Remove this novel from your library?';
            if (confirm(msg)) {
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

// ===== Supported sites =====
async function openScrapersModal() {
    // One modal at a time: this is reached from Add Novel, which would
    // otherwise stack on top of us
    document.getElementById('modal-add').style.display = 'none';
    document.getElementById('modal-scrapers').style.display = 'flex';
    const el = document.getElementById('scraper-list');
    el.innerHTML = '<p class="hint">Loading…</p>';
    try {
        const data = await api('GET', '/api/scrapers');
        const scrapers = data.scrapers || [];
        el.innerHTML = scrapers.length
            ? scrapers.map(s => `
                <div class="scraper-row">
                    <strong>${escapeHtml(s.name)}</strong>
                    ${s.patterns && s.patterns.length
                        ? `<code class="scraper-pattern">${escapeHtml(s.patterns[0])}</code>`
                        : ''}
                </div>`).join('')
            : '<p class="hint">No scrapers installed — the app can\'t fetch from any site yet. Add one (below) to get started.</p>';
    } catch (e) {
        el.innerHTML = `<p class="hint">Failed to load scrapers: ${escapeHtml(e.message)}</p>`;
    }
}

function closeScrapersModal() {
    document.getElementById('modal-scrapers').style.display = 'none';
    openAddModal(); // return to where the link was clicked
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

async function uploadEpub(file) {
    const errorEl = document.getElementById('add-error');
    const loadingEl = document.getElementById('add-loading');
    errorEl.style.display = 'none';
    loadingEl.textContent = 'Uploading EPUB…';
    loadingEl.style.display = 'block';
    try {
        const form = new FormData();
        form.append('file', file);
        const resp = await fetch('/api/epubs/upload', { method: 'POST', body: form });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        closeAddModal();
        showToast('Book added!');
        await loadLibrary();
    } catch (e) {
        errorEl.textContent = e.message;
        errorEl.style.display = 'block';
    } finally {
        loadingEl.style.display = 'none';
        loadingEl.textContent = 'Fetching novel info...';
    }
}

// ===== Novel Detail =====
// Which chapter-list page (50/page) holds a given chapter order, honoring sort.
function pageForOrder(order, total, sort, perPage = 50) {
    if (!order) return 1;
    return sort === 'desc'
        ? Math.max(1, Math.ceil(((total || order) - order + 1) / perPage))
        : Math.max(1, Math.ceil(order / perPage));
}

async function openNovel(novelId, opts = {}) {
    const novel = state.novels.find(n => n.id === novelId);
    if (!novel) return;

    state.currentNovel = novel;
    // Open to the page holding the chapter you're on, not always page 1.
    const _sort = novel.effective_settings?.chapter_sort || state.settings.chapter_sort || 'asc';
    state.chapterPage = pageForOrder(novel.progress_chapter, novel.total_chapters, _sort);

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

    // Description: collapsed to 4 lines; show "Read more" only if it overflows
    const descEl = document.getElementById('novel-description');
    descEl.textContent = novel.description || '';
    descEl.classList.add('clamped');
    const descToggle = document.getElementById('desc-toggle');
    descToggle.textContent = 'Read more';
    requestAnimationFrame(() => {
        descToggle.style.display =
            descEl.scrollHeight > descEl.clientHeight + 2 ? '' : 'none';
    });

    updateFavoriteButton();

    // Source site link (opens the scraped page in a new tab). EPUBs have an
    // epub:// pseudo-URL with no web page, so show a plain label instead.
    const isWebNovel = /^https?:\/\//.test(novel.rr_url || '');
    document.getElementById('novel-source').innerHTML = novel.source
        ? (isWebNovel
            ? `From <a href="${escapeHtml(novel.rr_url)}" target="_blank" rel="noopener">${escapeHtml(novel.source)} ↗</a>`
            : `From ${escapeHtml(novel.source)}`)
        : '';

    // Auto-refresh chapters on open — favorites only; non-favorites are
    // binge reads, refreshed manually via the ↻ button
    if (novel.favorite) {
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
    }

    await loadChapters();
    const progress = await updateResumeButton();
    if (opts.resume && progress?.chapter_id) {
        await resumeNovel(novel, progress.chapter_id);
    }
}

async function updateResumeButton() {
    const btn = document.getElementById('btn-resume');
    btn.style.display = 'none';
    if (!state.currentNovel) return null;
    try {
        const progress = await api('GET', `/api/progress/${state.currentNovel.id}`);
        if (!progress.chapter_id) return null;
        const pos = progress.position_seconds > 5 ? ` (${formatTime(progress.position_seconds)})` : '';
        btn.textContent = `▶ Resume — Ch. ${progress.chapter_order}${pos}`;
        btn.style.display = '';
        const novel = state.currentNovel;
        btn.onclick = () => resumeNovel(novel, progress.chapter_id);
        return progress;
    } catch (e) {
        return null;
    }
}

async function resumeNovel(novel, chapterId) {
    try {
        const queue = await loadPlaybackQueue(novel.id);
        const target = queue.find(c => c.id === chapterId);
        if (!target) {
            showToast('Saved chapter not found');
            return;
        }
        state.playback.novel = novel;
        state.playback.chapters = queue;
        await playChapter(target, novel);
    } catch (e) {
        showToast('Resume failed: ' + e.message);
    }
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
        state.chapterTotal = data.total;
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
            <span class="chapter-title-text">${escapeHtml(ch.title)}</span>
            ${ch.word_count ? `<span class="chapter-meta">${(ch.word_count / 1000).toFixed(1)}k words</span>` : ''}
            ${ch.is_current ? '<span class="current-badge">Current</span>' : ''}
            <button class="chapter-play-btn" data-id="${ch.id}" title="Play">▶</button>
        </div>
    `).join('');

    // Click handlers
    list.querySelectorAll('.chapter-row').forEach(row => {
        row.addEventListener('click', () => {
            playChapter(state.chapters.find(c => c.id === parseInt(row.dataset.id)));
        });
    });

    list.querySelectorAll('.chapter-play-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            playChapter(state.chapters.find(c => c.id === parseInt(btn.dataset.id)));
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

    const options = Array.from({ length: state.chapterTotalPages }, (_, i) => {
        const p = i + 1;
        return `<option value="${p}" ${p === state.chapterPage ? 'selected' : ''}>Page ${p} of ${state.chapterTotalPages}</option>`;
    }).join('');

    el.innerHTML =
        `<button ${state.chapterPage <= 1 ? 'disabled' : ''} data-page="${state.chapterPage - 1}">‹ Prev</button>` +
        `<select id="page-select" title="Jump to page">${options}</select>` +
        `<button ${state.chapterPage >= state.chapterTotalPages ? 'disabled' : ''} data-page="${state.chapterPage + 1}">Next ›</button>`;

    el.querySelectorAll('button[data-page]').forEach(btn => {
        btn.addEventListener('click', () => {
            const p = parseInt(btn.dataset.page);
            if (p >= 1 && p <= state.chapterTotalPages) {
                state.chapterPage = p;
                loadChapters();
            }
        });
    });
    el.querySelector('#page-select').addEventListener('change', (e) => {
        state.chapterPage = parseInt(e.target.value);
        loadChapters();
    });
}

async function refreshNovel() {
    if (!state.currentNovel) return;
    const btn = document.getElementById('btn-refresh');
    btn.disabled = true;
    btn.innerHTML = '↻<span class="btn-label"> Refreshing...</span>';

    try {
        const result = await api('POST', `/api/novels/${state.currentNovel.id}/refresh`);
        if (result.new_chapters > 0) {
            showToast(`${result.new_chapters} new chapter${result.new_chapters > 1 ? 's' : ''} found!`);
            state.currentNovel.total_chapters = result.total_chapters;
            document.getElementById('novel-stats').textContent = `${result.total_chapters} chapters`;
            loadLibrary(); // background: keep home-screen unread counts current
        } else {
            showToast('Already up to date');
        }
        await loadChapters();
    } catch (e) {
        showToast('Refresh failed: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '↻<span class="btn-label"> Refresh</span>';
    }
}

// ===== Audio Playback =====

function stopInstantPlay() {
    state._instantActive = false;
}

async function loadPlaybackQueue(novelId) {
    const data = await api('GET', `/api/novels/${novelId}/chapters?page=1&per_page=10000`);
    // Queue is always ascending regardless of display sort
    return data.chapters.slice().sort((a, b) => a.order - b.order);
}

function playbackSetting(key) {
    // Per-novel override of the playing novel, else global default
    const override = state.playback.novel?.settings?.[key];
    return override != null ? override : state.settings[key];
}

function markCurrentChapter(chapterId) {
    if (state.currentNovel?.id !== state.playback.novel?.id) return;
    state.chapters.forEach(c => { c.is_current = c.id === chapterId; });
    document.querySelectorAll('#chapter-list .chapter-row').forEach(row => {
        const isCur = parseInt(row.dataset.id) === chapterId;
        row.classList.toggle('current', isCur);
        const badge = row.querySelector('.current-badge');
        if (isCur && !badge) {
            const b = document.createElement('span');
            b.className = 'current-badge';
            b.textContent = 'Current';
            row.insertBefore(b, row.querySelector('.chapter-play-btn'));
        } else if (!isCur && badge) {
            badge.remove();
        }
    });
}

async function playChapter(chapter, novel = state.currentNovel) {
    if (!chapter || !novel) return;
    const chapterId = chapter.id;

    // Load the playback queue when switching novels (or on first play)
    if (state.playback.novel?.id !== novel.id) {
        try {
            state.playback.chapters = await loadPlaybackQueue(novel.id);
        } catch (e) {
            showToast('Failed to load chapter list: ' + e.message);
            return;
        }
        state.playback.novel = novel;
    }
    state.playback.chapter = chapter;

    // Stop any current playback
    stopInstantPlay();
    state.audio.pause();
    state.audio.removeAttribute('src');
    state.audio.load();
    state.isPlaying = false;
    state._instantSwapped = false;

    markCurrentChapter(chapterId);

    // Show player
    const player = document.getElementById('mini-player');
    player.style.display = 'flex';

    document.getElementById('player-novel-title').textContent = novel.title;
    document.getElementById('player-chapter-title').textContent = chapter.title;

    // "Open on website" link — real web chapters only, not local EPUBs.
    const chapterLink = document.getElementById('player-chapter-link');
    if (chapter.rr_url && /^https?:\/\//.test(chapter.rr_url)) {
        chapterLink.href = chapter.rr_url;
        chapterLink.style.display = '';
    } else {
        chapterLink.removeAttribute('href');
        chapterLink.style.display = 'none';
    }
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

    if (state.playback.chapter?.id !== chapterId) return;

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

    // Poll until full file is ready
    let ready = false;
    while (!ready) {
        await new Promise(r => setTimeout(r, 1500));
        if (state.playback.chapter?.id !== chapterId) return;
        try {
            const status = await api('GET', `/api/chapters/${chapterId}/status`);
            if (status.ready) {
                ready = true;
                if (status.duration_seconds) {
                    document.getElementById('player-duration').textContent = formatTime(status.duration_seconds);
                }
            }
        } catch (e) {
            showToast('Synthesis check failed: ' + e.message);
            loadingEl.style.display = 'none';
            state.isSynthesizing = false;
            return;
        }
    }

    state.isSynthesizing = false;
    loadingEl.style.display = 'none';
    if (state.playback.chapter?.id !== chapterId) return;
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

    if (state.playback.chapter?.id !== chapterId) return;

    applyPlaybackRate();

    // Restore saved progress
    const playingNovelId = state.playback.novel?.id;
    if (playingNovelId) {
        try {
            const progress = await api('GET', `/api/progress/${playingNovelId}`);
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

// Safari (macOS/iOS) plays HLS natively in <audio>; a growing EVENT playlist
// gives seamless segment transitions plus background/lock-screen playback.
const supportsNativeHls = new Audio().canPlayType('application/vnd.apple.mpegurl') !== '';

async function playInstant(chapterId) {
    if (supportsNativeHls) {
        await playInstantHls(chapterId);
    } else {
        await playInstantSegments(chapterId);
    }
}

async function playInstantHls(chapterId) {
    const loadingEl = document.getElementById('player-loading');
    const scrubbar = document.getElementById('player-scrubbar');
    const durationEl = document.getElementById('player-duration');

    loadingEl.style.display = 'inline';
    loadingEl.textContent = 'Starting...';

    // Wait for the first AAC segment (or fall back if the chapter is already
    // synthesized, or AAC encoding is unavailable on the server)
    while (state.playback.chapter?.id === chapterId) {
        let segData;
        try {
            segData = await api('GET', `/api/chapters/${chapterId}/segments`);
        } catch (e) {
            showToast('Streaming failed: ' + e.message);
            loadingEl.style.display = 'none';
            state.isSynthesizing = false;
            return;
        }
        if (segData.segment_count === 0 && segData.file_ready) {
            state.isSynthesizing = false;
            loadingEl.style.display = 'none';
            await playFullFile(chapterId);
            return;
        }
        if (segData.aac_count > 0) break;
        if (segData.segment_count >= 2) {
            // WAV segments exist but no AAC — ffmpeg missing/failing on server
            await playInstantSegments(chapterId);
            return;
        }
        await new Promise(r => setTimeout(r, 400));
    }
    if (state.playback.chapter?.id !== chapterId) return;

    // Duration is Infinity while the playlist grows; the durationchange
    // handler restores the scrubbar once #EXT-X-ENDLIST lands.
    scrubbar.style.display = 'none';
    durationEl.textContent = 'Streaming...';

    state.audio.src = `/api/chapters/${chapterId}/hls.m3u8`;
    applyPlaybackRate();
    state.audio.load();
    try {
        await state.audio.play();
    } catch (e) {}
    if (state.playback.chapter?.id !== chapterId) return;

    state.isSynthesizing = false;
    loadingEl.style.display = 'none';
    updateMediaSession();
    saveProgress();
    startProgressSaving();
}

async function playInstantSegments(chapterId) {
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
    while (state._instantActive && state.playback.chapter?.id === chapterId) {
        // Poll for segment availability
        let segData;
        try {
            segData = await api('GET', `/api/chapters/${chapterId}/segments`);
        } catch (e) { break; }

        if (state.playback.chapter?.id !== chapterId || !state._instantActive) break;

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

            if (state.playback.chapter?.id !== chapterId || !state._instantActive) break;

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

                    if (state.playback.chapter?.id !== chapterId) return;

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
    if (state.audio.paused) {
        state.audio.play().catch(() => {});
    } else {
        state.audio.pause();
    }
}

function seekRelative(seconds) {
    if (!state.audio.src) return;
    const max = isFinite(state.audio.duration) ? state.audio.duration : Infinity;
    state.audio.currentTime = Math.max(0, Math.min(max, state.audio.currentTime + seconds));
}

async function playAdjacentChapter(direction) {
    const { chapter, chapters, novel } = state.playback;
    if (!chapter || !novel) return;

    const target = chapters.find(c => c.order === chapter.order + direction);
    if (!target) {
        showToast(direction > 0 ? 'No next chapter' : 'No previous chapter');
        return;
    }
    await playChapter(target, novel);
    followPlaybackPage(target);
}

async function followPlaybackPage(target) {
    // If the user is viewing the playing novel and the new chapter is on a
    // different page, follow it so the visible list tracks playback.
    if (state.currentNovel?.id !== state.playback.novel?.id) return;
    if (state.chapters.some(c => c.id === target.id)) return;
    const sort = state.currentNovel.effective_settings?.chapter_sort || state.settings.chapter_sort;
    state.chapterPage = pageForOrder(target.order, state.chapterTotal, sort);
    await loadChapters();
    markCurrentChapter(target.id);
}

function setupAudioEvents() {
    const audio = state.audio;
    const scrubbar = document.getElementById('player-scrubbar');
    const currentTime = document.getElementById('player-current-time');
    const duration = document.getElementById('player-duration');
    const loadingEl = document.getElementById('player-loading');

    // Single source of truth for play/pause UI and the OS media session —
    // required for iOS to keep the Now Playing session claimable while paused.
    audio.addEventListener('play', () => {
        state.isPlaying = true;
        document.getElementById('btn-play-pause').textContent = '⏸';
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
        updatePositionState();
    });

    audio.addEventListener('pause', () => {
        state.isPlaying = false;
        if (!state.isSynthesizing) {
            document.getElementById('btn-play-pause').textContent = '▶';
        }
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
        saveProgress();
    });

    audio.addEventListener('timeupdate', () => {
        if (state._instantActive) return;  // scrubbar hidden during segment playback
        if (!audio.duration) return;
        currentTime.textContent = formatTime(audio.currentTime);
        if (isFinite(audio.duration)) {
            scrubbar.value = (audio.currentTime / audio.duration) * 100;
        }
    });

    audio.addEventListener('loadedmetadata', () => {
        if (state._instantActive) return;  // don't show segment duration
        if (audio.duration && isFinite(audio.duration)) {
            duration.textContent = formatTime(audio.duration);
            scrubbar.max = 100;
        }
        updatePositionState();
    });

    audio.addEventListener('durationchange', () => {
        if (state._instantActive) return;
        if (audio.duration && isFinite(audio.duration)) {
            duration.textContent = formatTime(audio.duration);
            scrubbar.style.display = '';
        }
        updatePositionState();
    });

    audio.addEventListener('seeked', updatePositionState);
    audio.addEventListener('ratechange', updatePositionState);

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

        // Auto-play next chapter (per-novel override wins)
        if (playbackSetting('auto_play')) {
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

    const title = state.playback.chapter?.title || 'Chapter';
    const novel = state.playback.novel?.title || 'Novel TTS';

    navigator.mediaSession.metadata = new MediaMetadata({
        title: title,
        artist: novel,
        album: state.playback.novel?.author || '',
        artwork: state.playback.novel?.cover_url
            ? [{ src: state.playback.novel.cover_url, sizes: '512x512', type: 'image/jpeg' }]
            : [],
    });

    // Explicit handlers — a toggle here desyncs when iOS's idea of the state
    // differs from ours, which broke resume from the lock screen.
    navigator.mediaSession.setActionHandler('play', () => {
        state.audio.play().catch(() => {});
    });
    navigator.mediaSession.setActionHandler('pause', () => {
        state.audio.pause();
    });
    // Fixed skip amounts matching the in-app buttons. iOS draws its own icon
    // (often "10") but the page controls the actual jump.
    navigator.mediaSession.setActionHandler('seekbackward', () => seekRelative(-15));
    navigator.mediaSession.setActionHandler('seekforward', () => seekRelative(30));
    navigator.mediaSession.setActionHandler('previoustrack', () => playAdjacentChapter(-1));
    navigator.mediaSession.setActionHandler('nexttrack', () => playAdjacentChapter(1));
    // Without a seekto handler, the lock-screen progress bar is read-only
    try {
        navigator.mediaSession.setActionHandler('seekto', (details) => {
            if (details.seekTime == null) return;
            if (details.fastSeek && 'fastSeek' in state.audio) {
                state.audio.fastSeek(details.seekTime);
            } else {
                state.audio.currentTime = details.seekTime;
            }
            updatePositionState();
        });
    } catch (e) { /* seekto unsupported on this browser */ }
}

function updatePositionState() {
    if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
    const { duration, currentTime, playbackRate } = state.audio;
    if (!isFinite(duration) || !duration) return;
    try {
        navigator.mediaSession.setPositionState({
            duration: duration,
            playbackRate: playbackRate,
            position: Math.min(currentTime, duration),
        });
    } catch (e) {}
}

// ===== Progress Saving =====
async function saveProgress() {
    const novelId = state.playback.novel?.id;
    const chapterId = state.playback.chapter?.id;
    if (!novelId || !chapterId) return;
    try {
        await api('PUT', `/api/progress/${novelId}`, {
            chapter_id: chapterId,
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
        `<option value="${escapeHtml(v.id)}" ${v.id === state.settings.voice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`
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

    // Audiobook export / Plex
    document.getElementById('audiobook-dir').value = state.settings.audiobook_dir || '';
    document.getElementById('plex-url').value = state.settings.plex_url || '';
    document.getElementById('plex-token').value = state.settings.plex_token || '';
    const sec = document.getElementById('plex-section');
    sec.innerHTML = state.settings.plex_section_id
        ? `<option value="${escapeHtml(state.settings.plex_section_id)}" selected>Library #${escapeHtml(state.settings.plex_section_id)} (saved)</option>`
        : '<option value="">— load libraries first —</option>';

    switchSettingsTab('playback');
    renderVoiceDemoList();
}

function switchSettingsTab(name) {
    document.querySelectorAll('.settings-tab').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tab === name));
    ['playback', 'export', 'voices'].forEach(tab => {
        document.getElementById(`settings-panel-${tab}`).style.display = tab === name ? '' : 'none';
    });
}

// ===== Voice demos =====
let demoAudio = null;

function renderVoiceDemoList() {
    const el = document.getElementById('voice-demo-list');
    el.innerHTML = state.voices.map(v => `
        <div class="voice-demo-row">
            <button class="small-btn voice-demo-play" data-voice="${escapeHtml(v.id)}" title="Play demo">▶</button>
            <span class="voice-demo-label">${escapeHtml(v.label)}${v.id === state.settings.voice ? ' <span class="voice-current">✓ current</span>' : ''}</span>
            <button class="secondary-btn btn-small voice-demo-use" data-voice="${escapeHtml(v.id)}">Use</button>
        </div>`).join('');

    el.querySelectorAll('.voice-demo-play').forEach(btn =>
        btn.addEventListener('click', () => playVoiceDemo(btn)));
    el.querySelectorAll('.voice-demo-use').forEach(btn =>
        btn.addEventListener('click', async () => {
            await updateSetting('voice', btn.dataset.voice);
            renderVoiceDemoList();
        }));
}

function stopVoiceDemo() {
    if (demoAudio) {
        demoAudio.pause();
        demoAudio = null;
    }
    document.querySelectorAll('.voice-demo-play').forEach(b => {
        b.textContent = '▶';
        b.disabled = false;
    });
}

async function playVoiceDemo(btn) {
    const wasPlaying = btn.textContent === '■';
    stopVoiceDemo();
    if (wasPlaying) return; // toggled off

    btn.textContent = '…'; // generating/loading
    const audio = new Audio(`/api/voices/${encodeURIComponent(btn.dataset.voice)}/demo`);
    demoAudio = audio;
    audio.addEventListener('playing', () => {
        if (demoAudio === audio) btn.textContent = '■';
    });
    audio.addEventListener('ended', () => {
        if (demoAudio === audio) stopVoiceDemo();
    });
    audio.addEventListener('error', () => {
        if (demoAudio === audio) {
            stopVoiceDemo();
            showToast('Demo failed to load', 4000);
        }
    });
    try {
        await audio.play();
    } catch (e) {
        if (demoAudio === audio) stopVoiceDemo();
    }
}

async function loadPlexLibraries() {
    try {
        const data = await api('GET', '/api/plex/libraries');
        const sec = document.getElementById('plex-section');
        sec.innerHTML = '<option value="">— choose —</option>' + data.libraries.map(l =>
            `<option value="${escapeHtml(l.id)}" ${l.id === state.settings.plex_section_id ? 'selected' : ''}>${escapeHtml(l.title)} (${escapeHtml(l.type)})</option>`
        ).join('');
        showToast('Libraries loaded — pick your audiobook library');
    } catch (e) {
        showToast(e.message, 5000);
    }
}

function applyPlaybackRate() {
    state.audio.playbackRate = playbackSetting('speed');
}

function closeSettings() {
    stopVoiceDemo();
    document.getElementById('modal-settings').style.display = 'none';
}

// ===== Favorites =====
async function toggleFavorite(novelId) {
    const novel = state.novels.find(n => n.id === novelId);
    if (!novel) return;
    try {
        const result = await api('PATCH', `/api/novels/${novelId}/settings`, { favorite: !novel.favorite });
        novel.favorite = result.favorite;
        renderLibrary();
        updateFavoriteButton();
        showToast(novel.favorite ? '⭐ Added to favorites' : 'Removed from favorites');
    } catch (e) {
        showToast('Failed: ' + e.message);
    }
}

function updateFavoriteButton() {
    const btn = document.getElementById('btn-favorite');
    const fav = !!state.currentNovel?.favorite;
    btn.textContent = fav ? '⭐' : '☆';
    btn.title = fav ? 'Unfavorite' : 'Favorite';
}

// ===== Drag-to-reorder =====
// Touch/pen: 400ms long-press (movement first = scroll, not drag).
// Mouse: no timer — press and move past a small threshold drags immediately;
// press-and-release without movement stays a click.
function setupCardDrag(card) {
    let pressTimer = null;
    let dragging = false;
    let mouseArmed = false;
    let startX = 0, startY = 0;

    const startDrag = (pointerId) => {
        dragging = true;
        state._dragging = true;
        card.classList.add('dragging');
        try { card.setPointerCapture(pointerId); } catch (err) {}
    };

    card.addEventListener('pointerdown', (e) => {
        if (e.target.closest('button')) return;
        startX = e.clientX;
        startY = e.clientY;
        if (e.pointerType === 'mouse') {
            if (e.button !== 0) return;
            mouseArmed = true;
            return;
        }
        pressTimer = setTimeout(() => startDrag(e.pointerId), 400);
    });

    card.addEventListener('pointermove', (e) => {
        if (!dragging) {
            if (mouseArmed && (Math.abs(e.clientX - startX) > 8 || Math.abs(e.clientY - startY) > 8)) {
                startDrag(e.pointerId);
            } else if (pressTimer && (Math.abs(e.clientX - startX) > 10 || Math.abs(e.clientY - startY) > 10)) {
                // Movement before the long-press fires = scrolling, not dragging
                clearTimeout(pressTimer);
                pressTimer = null;
            }
            if (!dragging) return;
        }
        const target = document.elementFromPoint(e.clientX, e.clientY)?.closest('.novel-card');
        if (!target || target === card) return;
        const cards = [...document.querySelectorAll('#novel-grid .novel-card')];
        if (cards.indexOf(card) < cards.indexOf(target)) {
            target.after(card);
        } else {
            target.before(card);
        }
        // Moving the card in the DOM disconnects it briefly, which releases
        // pointer capture — re-grab it so the drag survives multiple swaps.
        try { card.setPointerCapture(e.pointerId); } catch (err) {}
    });

    const finish = () => {
        clearTimeout(pressTimer);
        pressTimer = null;
        mouseArmed = false;
        if (!dragging) return;
        dragging = false;
        state._dragging = false;
        card.classList.remove('dragging');
        state._suppressClick = true;
        setTimeout(() => { state._suppressClick = false; }, 150);
        saveCustomOrder();
    };
    card.addEventListener('pointerup', finish);
    card.addEventListener('pointercancel', finish);
}

async function saveCustomOrder() {
    const ids = [...document.querySelectorAll('#novel-grid .novel-card')].map(c => parseInt(c.dataset.id));
    try {
        await api('PUT', '/api/novels/order', { ids });
        ids.forEach((id, i) => {
            const n = state.novels.find(nv => nv.id === id);
            if (n) n.sort_order = i;
        });
        state.librarySort = 'custom';
        localStorage.setItem('librarySort', 'custom');
        document.getElementById('library-sort').value = 'custom';
        showToast('Custom order saved');
    } catch (e) {
        showToast('Failed to save order: ' + e.message);
    }
}

// ===== Per-Novel Settings =====
function openNovelSettings() {
    const novel = state.currentNovel;
    if (!novel) return;
    const ov = novel.settings || {};

    document.getElementById('ns-novel-name').textContent = novel.title;

    const globalVoiceLabel = state.voices.find(v => v.id === state.settings.voice)?.label || state.settings.voice;
    document.getElementById('ns-voice').innerHTML =
        `<option value="">Default (${escapeHtml(globalVoiceLabel)})</option>` +
        state.voices.map(v =>
            `<option value="${escapeHtml(v.id)}" ${v.id === ov.voice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`
        ).join('');

    document.getElementById('ns-speed-value').textContent =
        ov.speed != null ? `${ov.speed.toFixed(2)}x` : `Default (${state.settings.speed.toFixed(2)}x)`;

    document.getElementById('ns-autoplay').value = ov.auto_play == null ? '' : String(ov.auto_play);
    document.getElementById('ns-sort').value = ov.chapter_sort ?? '';

    document.getElementById('modal-novel-settings').style.display = 'flex';
}

function closeNovelSettings() {
    document.getElementById('modal-novel-settings').style.display = 'none';
}

async function updateNovelSetting(field, value) {
    const novel = state.currentNovel;
    if (!novel) return;
    try {
        const result = await api('PATCH', `/api/novels/${novel.id}/settings`, { [field]: value });
        novel.settings = result.settings;
        novel.effective_settings = result.effective_settings;
        novel.favorite = result.favorite;
        // Keep the playing novel's object in sync so playbackSetting() sees it
        if (state.playback.novel?.id === novel.id) {
            state.playback.novel.settings = result.settings;
            state.playback.novel.effective_settings = result.effective_settings;
            applyPlaybackRate();
        }
        if (field === 'chapter_sort') {
            state.chapterPage = 1;
            await loadChapters();
        }
        openNovelSettings(); // refresh displayed values
    } catch (e) {
        showToast('Failed to save: ' + e.message);
    }
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

// ===== Save to Plex exports =====
let exportsPollTimer = null;
let lastJobStatuses = {};

function openExportModal() {
    const novel = state.currentNovel;
    if (!novel) return;
    if (!(state.settings.audiobook_dir || '').trim()) {
        showToast('Set your audiobook folder in Settings first', 5000);
        return;
    }
    const startSel = document.getElementById('export-start');
    const endSel = document.getElementById('export-end');
    startSel.innerHTML = endSel.innerHTML = '<option value="">Loading chapters…</option>';
    startSel.disabled = endSel.disabled = true;
    api('GET', `/api/novels/${novel.id}/chapters?page=1&per_page=10000`).then(data => {
        if (state.currentNovel?.id !== novel.id) return; // modal context changed
        const chs = (data.chapters || []).slice().sort((a, b) => a.order - b.order);
        if (!chs.length) return;
        const opts = chs.map(c =>
            `<option value="${c.order}">${c.order}. ${escapeHtml(c.title)}</option>`).join('');
        startSel.innerHTML = opts;
        endSel.innerHTML = opts;
        startSel.value = String(chs[0].order);
        endSel.value = String(chs[chs.length - 1].order);
        startSel.disabled = endSel.disabled = false;
        updateExportNamePreview();
    }).catch(e => showToast('Failed to load chapters: ' + e.message, 5000));
    const eff = novel.effective_settings || {};
    const voiceSel = document.getElementById('export-voice');
    voiceSel.innerHTML = state.voices.map(v =>
        `<option value="${escapeHtml(v.id)}" ${v.id === eff.voice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`
    ).join('');
    // Same +/− stepper as everywhere else, defaulting to the novel's
    // effective speed (0.05 steps, clamped 0.5–2.0)
    state.exportSpeed = eff.speed ?? 1.0;
    updateExportSpeedDisplay();
    updateExportNamePreview();
    document.getElementById('modal-export').style.display = 'flex';
}

function updateExportNamePreview() {
    if (!state.currentNovel) return;
    const s = document.getElementById('export-start').value || '?';
    const e = document.getElementById('export-end').value || '?';
    document.getElementById('export-name-preview').textContent =
        `${state.currentNovel.title} - Chapters ${s} - ${e}.m4b`;
}

function updateExportSpeedDisplay() {
    document.getElementById('export-speed-value').textContent =
        `${state.exportSpeed.toFixed(2)}x`;
}

function closeExportModal() {
    document.getElementById('modal-export').style.display = 'none';
}

async function startExport() {
    const novel = state.currentNovel;
    if (!novel) return;
    try {
        await api('POST', `/api/novels/${novel.id}/export`, {
            start_order: parseInt(document.getElementById('export-start').value, 10),
            end_order: parseInt(document.getElementById('export-end').value, 10),
            voice: document.getElementById('export-voice').value,
            speed: state.exportSpeed,
        });
        closeExportModal();
        showToast('Export queued');
        startExportsPolling();
    } catch (e) {
        showToast('Export failed to start: ' + e.message, 5000);
    }
}

async function refreshExports() {
    let data;
    try {
        data = await api('GET', '/api/exports');
    } catch (e) {
        return;
    }
    const jobs = data.jobs || [];
    const active = jobs.filter(j => j.status === 'queued' || j.status === 'running');

    const badge = document.getElementById('exports-badge');
    badge.style.display = active.length ? '' : 'none';
    if (active.length) {
        const running = active.find(j => j.status === 'running');
        document.getElementById('exports-badge-count').textContent = running
            ? `${running.chapters_done}/${running.chapters_total}`
            : `${active.length} queued`;
    }

    for (const j of jobs) {
        const prev = lastJobStatuses[j.id];
        if (prev && prev !== j.status) {
            if (j.status === 'completed') showToast(`✅ Export done: ${j.novel_title}`, 6000);
            if (j.status === 'failed') showToast(`❌ Export failed: ${j.error || 'see Exports panel'}`, 8000);
        }
        lastJobStatuses[j.id] = j.status;
    }

    renderExportsList(jobs);

    if (!active.length) stopExportsPolling();
}

function renderExportsList(jobs) {
    const el = document.getElementById('exports-list');
    if (!el) return;
    if (!jobs.length) {
        el.innerHTML = '<p class="hint">No exports yet.</p>';
        return;
    }
    el.innerHTML = jobs.map(j => `
        <div class="export-row">
          <div>
            <strong>${escapeHtml(j.novel_title)}</strong> — Ch ${j.start_order}–${j.end_order}
            <span class="export-status export-${j.status}">${escapeHtml(j.status)}</span>
            <div class="hint">${j.status === 'running' ? `${j.chapters_done}/${j.chapters_total} · ` : ''}${escapeHtml(j.detail || j.error || '')}</div>
          </div>
          <div>
            ${(j.status === 'queued' || j.status === 'running') ? `<button class="secondary-btn btn-small" data-cancel-id="${j.id}">Cancel</button>` : ''}
            ${(j.status === 'failed' || j.status === 'interrupted' || j.status === 'canceled') ? `<button class="secondary-btn btn-small" data-retry-id="${j.id}">Retry</button>` : ''}
          </div>
        </div>`).join('');

    el.querySelectorAll('[data-cancel-id]').forEach(btn => {
        btn.addEventListener('click', () => cancelExport(parseInt(btn.dataset.cancelId, 10)));
    });
    el.querySelectorAll('[data-retry-id]').forEach(btn => {
        btn.addEventListener('click', () => retryExport(parseInt(btn.dataset.retryId, 10)));
    });
}

async function cancelExport(id) {
    try {
        await api('POST', `/api/exports/${id}/cancel`);
        refreshExports();
    } catch (e) {
        showToast(e.message);
    }
}

async function retryExport(id) {
    try {
        await api('POST', `/api/exports/${id}/retry`);
        startExportsPolling();
    } catch (e) {
        showToast(e.message);
    }
}

function startExportsPolling() {
    refreshExports();
    if (!exportsPollTimer) exportsPollTimer = setInterval(refreshExports, 3000);
}

function stopExportsPolling() {
    if (exportsPollTimer) {
        clearInterval(exportsPollTimer);
        exportsPollTimer = null;
    }
}

function openExportsPanel() {
    document.getElementById('modal-exports').style.display = 'flex';
    refreshExports();
}

function closeExportsPanel() {
    document.getElementById('modal-exports').style.display = 'none';
}

// ===== Event Listeners =====
function setupEventListeners() {
    // Add novel
    document.getElementById('btn-add-novel').addEventListener('click', openAddModal);
    document.getElementById('btn-add-cancel').addEventListener('click', closeAddModal);
    document.getElementById('btn-add-confirm').addEventListener('click', addNovel);
    document.getElementById('btn-upload-epub').addEventListener('click', () => {
        document.getElementById('input-epub-file').click();
    });
    document.getElementById('input-epub-file').addEventListener('change', (e) => {
        if (e.target.files.length) uploadEpub(e.target.files[0]);
        e.target.value = '';   // allow re-selecting the same file after an error
    });
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

    // Library tabs + sort
    document.getElementById('tab-all').addEventListener('click', () => setLibraryTab('all'));
    document.getElementById('tab-favorites').addEventListener('click', () => setLibraryTab('favorites'));
    document.getElementById('library-sort').addEventListener('change', (e) => {
        state.librarySort = e.target.value;
        localStorage.setItem('librarySort', state.librarySort);
        renderLibrary();
    });
    document.getElementById('library-view-toggle').addEventListener('click', () => {
        state.libraryView = state.libraryView === 'grid' ? 'list' : 'grid';
        localStorage.setItem('libraryView', state.libraryView);
        applyLibraryView();
        renderLibrary();
    });
    // Native image drag would hijack pointer-based reordering with a mouse
    document.getElementById('novel-grid').addEventListener('dragstart', (e) => e.preventDefault());

    // Block page scroll while a card is being dragged (iOS)
    document.addEventListener('touchmove', (e) => {
        if (state._dragging) e.preventDefault();
    }, { passive: false });

    // Novel detail
    document.getElementById('btn-back').addEventListener('click', closeNovel);
    document.getElementById('btn-refresh').addEventListener('click', refreshNovel);
    document.getElementById('btn-favorite').addEventListener('click', () => {
        if (state.currentNovel) toggleFavorite(state.currentNovel.id);
    });

    // Per-novel settings
    document.getElementById('btn-novel-settings').addEventListener('click', openNovelSettings);
    document.getElementById('btn-ns-close').addEventListener('click', closeNovelSettings);
    document.getElementById('modal-novel-settings').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeNovelSettings();
    });
    document.getElementById('ns-voice').addEventListener('change', (e) => {
        updateNovelSetting('voice', e.target.value || null);
    });
    document.getElementById('ns-autoplay').addEventListener('change', (e) => {
        updateNovelSetting('auto_play', e.target.value === '' ? null : e.target.value === 'true');
    });
    document.getElementById('ns-sort').addEventListener('change', (e) => {
        updateNovelSetting('chapter_sort', e.target.value || null);
    });
    document.getElementById('ns-speed-down').addEventListener('click', () => {
        const base = state.currentNovel?.settings?.speed ?? state.settings.speed;
        updateNovelSetting('speed', Math.max(0.5, Math.round((base - 0.05) * 100) / 100));
    });
    document.getElementById('ns-speed-up').addEventListener('click', () => {
        const base = state.currentNovel?.settings?.speed ?? state.settings.speed;
        updateNovelSetting('speed', Math.min(2.0, Math.round((base + 0.05) * 100) / 100));
    });
    document.getElementById('ns-speed-reset').addEventListener('click', () => {
        updateNovelSetting('speed', null);
    });

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

    document.getElementById('audiobook-dir').addEventListener('change', (e) => {
        updateSetting('audiobook_dir', e.target.value);
    });

    document.getElementById('plex-url').addEventListener('change', (e) => {
        updateSetting('plex_url', e.target.value);
    });

    document.getElementById('plex-token').addEventListener('change', (e) => {
        updateSetting('plex_token', e.target.value);
    });

    document.getElementById('plex-section').addEventListener('change', (e) => {
        updateSetting('plex_section_id', e.target.value);
    });

    document.getElementById('btn-load-libraries').addEventListener('click', loadPlexLibraries);

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

    // Save to Plex
    document.getElementById('btn-save-plex').addEventListener('click', openExportModal);
    document.getElementById('btn-export-cancel').addEventListener('click', closeExportModal);
    document.getElementById('btn-export-confirm').addEventListener('click', startExport);
    document.getElementById('export-start').addEventListener('change', updateExportNamePreview);
    document.getElementById('export-end').addEventListener('change', updateExportNamePreview);
    document.getElementById('export-speed-down').addEventListener('click', () => {
        state.exportSpeed = Math.max(0.5, Math.round((state.exportSpeed - 0.05) * 100) / 100);
        updateExportSpeedDisplay();
    });
    document.getElementById('export-speed-up').addEventListener('click', () => {
        state.exportSpeed = Math.min(2.0, Math.round((state.exportSpeed + 0.05) * 100) / 100);
        updateExportSpeedDisplay();
    });
    document.querySelectorAll('.settings-tab').forEach(btn =>
        btn.addEventListener('click', () => switchSettingsTab(btn.dataset.tab)));
    document.getElementById('desc-toggle').addEventListener('click', () => {
        const descEl = document.getElementById('novel-description');
        const clamped = descEl.classList.toggle('clamped');
        document.getElementById('desc-toggle').textContent = clamped ? 'Read more' : 'Show less';
    });
    document.getElementById('link-supported-sites').addEventListener('click', (e) => {
        e.preventDefault();
        openScrapersModal();
    });
    document.getElementById('btn-scrapers-close').addEventListener('click', closeScrapersModal);
    document.getElementById('modal-scrapers').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeScrapersModal();
    });
    document.getElementById('modal-export').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeExportModal();
    });

    // Exports badge/panel
    document.getElementById('exports-badge').addEventListener('click', openExportsPanel);
    document.getElementById('btn-exports-close').addEventListener('click', closeExportsPanel);
    document.getElementById('modal-exports').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeExportsPanel();
    });
}
