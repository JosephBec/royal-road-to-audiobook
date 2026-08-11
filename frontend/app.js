/**
 * Royal Road TTS — Frontend Application
 *
 * Single-page app with library view, novel detail, and persistent audio player.
 */

// ===== State =====
const state = {
    novels: [],
    libraryLoaded: false,
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
    settings: { engine: 'kokoro', voice: 'af_heart', speed: 1.0, playback_mode: 'full', auto_play: true, theme: 'dark', chapter_sort: 'asc' },
    voices: [],
    engines: [],
    // Instant Play state
    _instantActive: false,    // whether instant play loop is running
    _instantSwapped: false,   // whether we've swapped to full file
    _instantElapsed: 0,       // cumulative seconds played across segments
};

// ===== Init =====
// Run a startup step without letting its failure take the rest down. The
// boot sequence used to be a bare chain of awaits, so one slow or failed
// request — the server is briefly busy right after a restart — meant
// loadLibrary() and setupEventListeners() never ran and the page rendered as
// a bare header until you reloaded.
async function bootStep(label, fn) {
    try {
        await fn();
    } catch (e) {
        console.error(`Startup step "${label}" failed:`, e);
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    // Kick the server-side favorites sync (new chapters + pre-downloads).
    // If it actually starts, watch for it to finish and re-render the
    // library so fresh chapter/unread counts show without a reload.
    api('POST', '/api/library/refresh-favorites')
        .then(res => { if (res && res.started) watchFavoritesSync(); })
        .catch(() => {});

    // Wire the interface up first. Buttons that do nothing are worse than
    // buttons with no data behind them yet, and this cannot fail on a
    // network hiccup.
    setupEventListeners();
    setupAudioEvents();
    updateAddNovelVisibility();
    applyLibraryTab();
    document.getElementById('library-sort').value = state.librarySort;
    applyLibraryView();

    await bootStep('settings', loadSettings);
    await bootStep('theme', () => applyTheme(state.settings.theme));
    await bootStep('playback rate', () => applyPlaybackRate());
    await bootStep('voices', loadVoices);
    await bootStep('library', loadLibrary);
    await bootStep('exports', () => startExportsPolling());
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
        state.libraryLoaded = true;
        renderLibrary();
    } catch (e) {
        console.error('Failed to load library:', e);
        // Say so and offer a way out. Silence here looked like an empty
        // library, which is indistinguishable from a broken one.
        const grid = document.getElementById('novel-grid');
        const empty = document.getElementById('library-empty');
        if (empty) empty.style.display = 'none';
        if (grid) {
            grid.innerHTML =
                '<div class="library-error">'
                + '<p>Could not reach the server.</p>'
                + '<button id="btn-library-retry" class="primary-btn">Try again</button>'
                + '</div>';
            const retry = document.getElementById('btn-library-retry');
            if (retry) retry.addEventListener('click', () => loadLibrary());
        }
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
    document.getElementById('tab-archived').classList.toggle('active', state.libraryTab === 'archived');
    renderLibrary();
}

function sortedNovels() {
    let list = state.novels.slice();
    if (state.libraryTab === 'favorites') {
        list = list.filter(n => n.favorite);
    } else if (state.libraryTab === 'archived') {
        list = list.filter(n => n.archived);
    } else {
        // Archived novels are finished or paused; keep them out of the way
        // without deleting them or their progress.
        list = list.filter(n => !n.archived);
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

    if (novels.length === 0 && !state.libraryLoaded) {
        // Nothing fetched yet: say nothing rather than claiming the library
        // is empty, which is a different and alarming statement.
        grid.innerHTML = '';
        empty.style.display = 'none';
        return;
    }

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
    // Drop the previous novel's chapters before the view switches. They used
    // to stay on screen until the new fetch returned, so opening a second
    // book briefly showed the first one's table of contents.
    state.chapters = [];
    state.chapterTotal = 0;
    state.chapterTotalPages = 1;
    showChapterListLoading();
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

function showChapterListLoading() {
    const list = document.getElementById('chapter-list');
    if (!list) return;
    list.innerHTML = Array.from({ length: 6 }, () =>
        '<div class="chapter-row chapter-row--skeleton">'
        + '<span class="skeleton-bar skeleton-bar--num"></span>'
        + '<span class="skeleton-bar skeleton-bar--title"></span>'
        + '</div>').join('');
    const pager = document.getElementById('chapter-pagination');
    if (pager) pager.innerHTML = '';
}

async function loadChapters() {
    if (!state.currentNovel) return;
    // Tag the request with the novel it belongs to. Clicking through books
    // quickly can land an earlier response after a later one, which would
    // paint the wrong chapters over the right ones.
    const requestedId = state.currentNovel.id;

    try {
        const data = await api('GET', `/api/novels/${requestedId}/chapters?page=${state.chapterPage}&per_page=50`);
        if (state.currentNovel?.id !== requestedId) return;   // superseded
        state.chapters = data.chapters;
        state.chapterTotalPages = data.total_pages;
        state.chapterTotal = data.total;
        renderChapters();
    } catch (e) {
        console.error('Failed to load chapters:', e);
        if (state.currentNovel?.id === requestedId) {
            document.getElementById('chapter-list').innerHTML =
                '<p class="hint">Could not load chapters.</p>';
        }
    }
}

function renderChapters() {
    const list = document.getElementById('chapter-list');

    list.innerHTML = state.chapters.map(ch => `
        <div class="chapter-row ${ch.is_current ? 'current' : ''}" data-id="${ch.id}">
            <span class="chapter-number">${ch.chapter_number ?? ch.order}</span>
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
                // Clamp: a position beyond the audio's length can only be
                // wrong, and silently seeking there drops you most of the way
                // through a chapter you have never heard.
                const dur = state.audio.duration;
                const pos = progress.position_seconds;
                state.audio.currentTime = (isFinite(dur) && dur > 0)
                    ? Math.min(pos, Math.max(0, dur - 1))
                    : pos;
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

    // Safari starts a growing HLS playlist near its live edge, so a fresh
    // chapter began wherever rendering had reached. Place the playhead
    // ourselves once metadata lands — at the saved position if this is a
    // resume, otherwise at the beginning.
    let startAt = 0;
    try {
        const novelId = state.playback.novel?.id;
        if (novelId) {
            const progress = await api('GET', `/api/progress/${novelId}`);
            if (progress.chapter_id === chapterId && progress.position_seconds > 0) {
                startAt = progress.position_seconds;
            }
        }
    } catch (e) {}

    await new Promise(resolve => {
        if (state.audio.readyState >= 1) return resolve();
        const done = () => { state.audio.removeEventListener('loadedmetadata', done); resolve(); };
        state.audio.addEventListener('loadedmetadata', done);
        setTimeout(done, 3000);   // don't hang if metadata never arrives
    });
    if (state.playback.chapter?.id !== chapterId) return;
    try {
        const seekable = state.audio.seekable;
        const end = seekable && seekable.length ? seekable.end(seekable.length - 1) : 0;
        state.audio.currentTime = Math.max(0, Math.min(startAt, Math.max(0, end - 1)));
    } catch (e) {}

    // The seek above does not survive play(). For a playlist that is still
    // growing, Safari commits its own start position when playback begins —
    // the live edge — and silently discards wherever the playhead was put
    // before. The cache rework made this deterministic instead of occasional:
    // every chapter in the reading window holds exactly its first two
    // minutes, so pressing play on a fresh chapter started at 2:00 sharp.
    //
    // The position therefore has to be enforced after playback starts, and
    // the enforcement must be armed BEFORE play() is called. The first
    // attempt at this fix subscribed to 'playing' after awaiting play(), and
    // on iOS the event had already fired by the time the promise callback
    // ran — the listener waited forever and the bug sailed through. Armed
    // here, it cannot miss: whichever of 'playing' or 'timeupdate' arrives
    // first does the correction, and it stands down only after seeing the
    // playhead where it belongs twice in a row.
    enforceStartPosition(chapterId, startAt);

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

// How far the playhead may land from where we put it before we call it
// Safari's doing and put it back. Segments are one sentence (~5s), so 2s
// cleanly separates "the engine moved us to the live edge" from seek
// granularity.
const START_DRIFT_TOLERANCE_S = 2;
const START_ENFORCE_ATTEMPTS = 5;
// Consecutive observations at the right position before standing down.
const START_STABLE_CONFIRMATIONS = 2;

// Keep the playhead where playback was meant to start, against an HLS engine
// that moves it to the live edge of a growing playlist when play() begins.
//
// Must be called BEFORE play(): it listens for both 'playing' and
// 'timeupdate', so whichever the engine emits first triggers the check, and a
// listener attached late cannot miss the moment (subscribing to 'playing'
// after awaiting play() did exactly that — on iOS the event beat the promise
// callback and the enforcement never ran).
//
// The target re-clamps to the seekable range on every check, so a resume
// aimed past what has rendered settles at the end of what exists. Stands down
// after the playhead is seen in place START_STABLE_CONFIRMATIONS times, or
// after START_ENFORCE_ATTEMPTS corrections — never a seek war mid-listen.
function enforceStartPosition(chapterId, startAt) {
    let attempts = 0;
    let stable = 0;
    const detach = () => {
        state.audio.removeEventListener('playing', onEvent);
        state.audio.removeEventListener('timeupdate', onEvent);
    };
    const onEvent = () => {
        if (state.playback.chapter?.id !== chapterId) return detach();
        if (state.audio.paused) return;   // not actually playing yet
        let target = startAt;
        try {
            const seekable = state.audio.seekable;
            if (seekable && seekable.length) {
                target = Math.min(target, Math.max(0, seekable.end(seekable.length - 1) - 0.5));
            }
        } catch (e) {}
        if (Math.abs(state.audio.currentTime - target) <= START_DRIFT_TOLERANCE_S) {
            stable += 1;
            if (stable >= START_STABLE_CONFIRMATIONS) detach();
            return;
        }
        stable = 0;
        attempts += 1;
        if (attempts > START_ENFORCE_ATTEMPTS) return detach();
        try { state.audio.currentTime = target; } catch (e) {}
    };
    state.audio.addEventListener('playing', onEvent);
    state.audio.addEventListener('timeupdate', onEvent);
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

                    // Account for inter-segment silence in the full file.
                    // The gap is per engine+voice (0.7s on a tuned Chatterbox
                    // voice, 0.3s on Kokoro), so ask rather than assume —
                    // hardcoding 0.3 drifted further with every segment.
                    const silencePerGap = freshData.segment_gap ?? 0.3;
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
    const { chapter, novel } = state.playback;
    if (!chapter || !novel) return;

    // Step by position in the ascending queue, not by order arithmetic — orders
    // can have gaps/duplicates after a stub+refresh, which would break order±1.
    let chapters = state.playback.chapters;
    let idx = chapters.findIndex(c => c.id === chapter.id);
    let target = idx === -1 ? null : chapters[idx + direction];

    // No next chapter in the queue? It may be stale (chapters added since it
    // loaded). Reload once and retry before giving up.
    if (!target && direction > 0) {
        try {
            state.playback.chapters = await loadPlaybackQueue(novel.id);
            chapters = state.playback.chapters;
            idx = chapters.findIndex(c => c.id === chapter.id);
            target = idx === -1 ? null : chapters[idx + direction];
        } catch (e) { /* keep the original "no next chapter" message below */ }
    }

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
        // Build the keepalive's AudioContext here, inside a guaranteed user
        // gesture, so resuming it later from a lock-screen handler — where
        // gesture rules are murkier — only has to resume, never create.
        prepareSilentKeepalive();
        stopSilentKeepalive();
        updatePositionState();
    });

    audio.addEventListener('pause', () => {
        // A finished element fires pause before ended (spec), so during
        // Instant Play every segment boundary lands here. Those are not
        // pauses: the loop plays the next segment immediately. Doing the
        // paused-state bookkeeping for them stamped paused/playing onto the
        // lock screen and saved progress once per sentence.
        if (state._instantActive && state.audio.ended) return;
        state.isPlaying = false;
        if (!state.isSynthesizing) {
            document.getElementById('btn-play-pause').textContent = '▶';
        }
        // The keepalive is Web Audio, not a second media element, so starting
        // it does not change what iOS considers the Now Playing item — this
        // element stays it, and "paused" is simply true.
        startSilentKeepalive();
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
        updatePositionState();   // pin the frozen timeline at the paused spot
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
        // Instant Play swaps audio.src between segments and polls for not-yet-
        // ready ones; those transient errors are handled by the segment loop,
        // so don't surface a toast for them.
        if (state._instantActive) return;
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

// One definition for the in-app buttons and the lock screen, so the two can
// never drift apart.
const SKIP_BACK_SECONDS = 15;
const SKIP_FORWARD_SECONDS = 30;

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

    // Transport commands, interpreted against our own paused state and nothing
    // else. This used to be a timing heuristic: a second <audio> element (the
    // old keepalive) was the playing element during a pause, so iOS routed the
    // headset toggle to it as "pause", and a timer tried to guess whether each
    // pause command was the user or iOS settling. The timer failed both ways —
    // presses inside the window were swallowed, and any pause iOS emitted
    // after it (Siri, a call, another app taking audio focus) started playback
    // nobody asked for, hours later. With the keepalive out of the media
    // element world entirely there is nothing to route wrongly: paused means
    // paused, and a pause command while paused can only be a toggle press
    // meaning resume.
    navigator.mediaSession.setActionHandler('play', () => {
        stopSilentKeepalive();
        state.audio.play().catch(() => {});
    });
    navigator.mediaSession.setActionHandler('pause', () => {
        if (state.audio.paused) {
            stopSilentKeepalive();
            state.audio.play().catch(() => {});
        } else {
            state.audio.pause();
        }
    });
    // Fixed skip amounts matching the in-app buttons. iOS draws its own icon
    // (often "10") but the page controls the actual jump.
    navigator.mediaSession.setActionHandler('seekbackward', () => seekRelative(-SKIP_BACK_SECONDS));
    navigator.mediaSession.setActionHandler('seekforward', () => seekRelative(SKIP_FORWARD_SECONDS));
    // Track buttons skip within the chapter, they do not change chapter.
    //
    // A double-press on a headset is "next track", and mapping that to the
    // next chapter meant a mis-press threw away your place in a chapter that
    // may have taken twenty minutes to render — with no way to undo it from
    // the headset. Seeking is what someone reaching for a button mid-listen
    // means, and the worst case is being thirty seconds out.
    navigator.mediaSession.setActionHandler('previoustrack', () => seekRelative(-SKIP_BACK_SECONDS));
    navigator.mediaSession.setActionHandler('nexttrack', () => seekRelative(SKIP_FORWARD_SECONDS));
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

// ===== Keeping the iOS lock-screen session alive =====
//
// Safari deactivates the audio session a few seconds after a pause. Once it
// has, the Now Playing controls stop routing to this page: play does nothing,
// which is why resume only works if you press it almost immediately.
//
// The keepalive holds the session open with Web Audio — a looping buffer of
// silence — and specifically NOT with a second <audio> element. The previous
// design used one, and it was the root of every resume bug this app has had:
// iOS routes transport commands to whichever media element is playing, so
// during a pause every button press was aimed at the silent loop instead of
// the chapter, and the page was reduced to guessing what each command meant
// from its timing. Web Audio keeps the session alive without ever appearing
// in the media element world, so the paused chapter remains the Now Playing
// item, its frozen timeline stays honest, and a button press means exactly
// what it says.
//
// Still opt-in and stored per device: a silent render loop costs a trickle of
// battery, and only iOS needs it.

const KEEPALIVE_KEY = 'iosKeepSessionAlive';
// Stop holding the session open after this long. The bug only bites for a
// listener coming back to a paused book; keeping the session alive all night
// to serve that is a battery cost with no benefit.
const KEEPALIVE_MAX_MS = 45 * 60 * 1000;
let keepaliveCtx = null;       // AudioContext with a looping silent source
let keepaliveExpiry = null;

function keepaliveEnabled() {
    return localStorage.getItem(KEEPALIVE_KEY) === '1';
}

function prepareSilentKeepalive() {
    // Build the context inside a known user gesture (the play that starts the
    // chapter) and leave it suspended. iOS only lets audio start under a
    // gesture, and whether a lock-screen action handler counts as one is
    // exactly the kind of ambiguity this rewrite exists to remove — a resume()
    // on an existing context is the least it could ever need to do.
    if (!keepaliveEnabled() || keepaliveCtx) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    try {
        keepaliveCtx = new Ctx();
        const source = keepaliveCtx.createBufferSource();
        // One second of silence, looped. Zeroed samples — nothing to hear at
        // any volume.
        source.buffer = keepaliveCtx.createBuffer(1, keepaliveCtx.sampleRate,
                                                  keepaliveCtx.sampleRate);
        source.loop = true;
        source.connect(keepaliveCtx.destination);
        source.start();
        keepaliveCtx.suspend().catch(() => {});
    } catch (e) {
        keepaliveCtx = null;
    }
}

function startSilentKeepalive() {
    if (!keepaliveEnabled()) return;
    prepareSilentKeepalive();
    if (!keepaliveCtx) return;
    keepaliveCtx.resume().catch(() => {});
    if (keepaliveExpiry) clearTimeout(keepaliveExpiry);
    keepaliveExpiry = setTimeout(stopSilentKeepalive, KEEPALIVE_MAX_MS);
}

function stopSilentKeepalive() {
    if (keepaliveExpiry) { clearTimeout(keepaliveExpiry); keepaliveExpiry = null; }
    if (keepaliveCtx) keepaliveCtx.suspend().catch(() => {});
}

function reviveMediaSession() {
    // Handlers and metadata can be dropped when the page is frozen and thawed.
    if (!state.playback.chapter) return;
    updateMediaSession();
    if ('mediaSession' in navigator) {
        navigator.mediaSession.playbackState = state.isPlaying ? 'playing' : 'paused';
    }
    updatePositionState();
    // An interruption (a call, Siri) can suspend the context while the page is
    // backgrounded; coming back to the page is the earliest chance to restart
    // it. Idempotent when it is already running.
    if (!state.isPlaying) startSilentKeepalive();
}

function updatePositionState() {
    if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
    // During Instant Play the element's duration is one sentence's, and
    // publishing it drew a fresh sliver of a progress bar on the lock screen
    // every few seconds. Say nothing until the full-file swap gives the
    // element the chapter's real timeline.
    if (state._instantActive) return;
    // Published while playing and while paused alike. The chapter element is
    // the Now Playing item in both states now, so its real position is the
    // honest thing to show — a frozen timeline at the paused spot, not a
    // cleared one.
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
    // Engines carry their own voice lists; state.voices always holds the ones
    // belonging to the currently selected engine so every existing consumer
    // (settings, per-novel, export, demos) keeps working unchanged.
    try {
        const data = await api('GET', '/api/engines');
        state.engines = data.engines || [];
        state.voices = voicesForEngine(state.settings.engine);
        return;
    } catch (e) {
        console.warn('No /api/engines — falling back to the single-engine voice list:', e);
    }
    // The server process can be older than the assets it serves: index.html and
    // app.js are read from disk per request, but the Python is whatever was
    // loaded at startup. Without this fallback that window leaves the voice
    // pickers completely empty rather than merely missing the model switcher.
    try {
        const data = await api('GET', '/api/voices');
        state.voices = data.voices || [];
        state.engines = [];
    } catch (e) {
        console.error('Failed to load voices:', e);
    }
}

function engineByName(name) {
    return (state.engines || []).find(e => e.name === name)
        || (state.engines || [])[0] || null;
}

function voicesForEngine(name) {
    return engineByName(name)?.voices || [];
}

function engineOptionsHtml(selected, { inheritLabel = null } = {}) {
    const opts = (state.engines || []).map(e => {
        const label = e.available ? e.label : `${e.label} — ${e.unavailable_reason}`;
        return `<option value="${escapeHtml(e.name)}" ${e.name === selected ? 'selected' : ''} ${e.available ? '' : 'disabled'}>${escapeHtml(label)}</option>`;
    });
    if (inheritLabel) {
        opts.unshift(`<option value="" ${!selected ? 'selected' : ''}>${escapeHtml(inheritLabel)}</option>`);
    }
    return opts.join('');
}

function renderEngineHint() {
    const hint = document.getElementById('engine-hint');
    if (!hint) return;
    const eng = engineByName(state.settings.engine);
    if (!eng) { hint.textContent = ''; return; }
    const bits = [];
    if (eng.supports_custom_voices) {
        bits.push('Drop a 5s+ WAV in the voices/ folder to add a custom voice.');
    }
    if (!eng.supports_speed) {
        bits.push('Playback speed still works; exports are time-stretched.');
    }
    hint.textContent = bits.join(' ');
}

function openSettings() {
    document.getElementById('modal-settings').style.display = 'flex';

    // Populate model dropdown. With no engines reported (a server older than
    // these assets) hide the row rather than showing an empty, dead select.
    const engineSelect = document.getElementById('setting-engine');
    engineSelect.closest('.setting-row').style.display =
        state.engines.length ? '' : 'none';
    engineSelect.innerHTML = engineOptionsHtml(state.settings.engine);

    // Populate voice dropdown for the selected model
    const voiceSelect = document.getElementById('setting-voice');
    voiceSelect.innerHTML = state.voices.map(v =>
        `<option value="${escapeHtml(v.id)}" ${v.id === state.settings.voice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`
    ).join('');
    renderEngineHint();

    // Speed
    document.getElementById('speed-value').textContent = `${state.settings.speed.toFixed(2)}x`;

    // Mode
    document.getElementById('setting-mode').value = state.settings.playback_mode;

    // Auto-play
    document.getElementById('setting-autoplay').checked = state.settings.auto_play;
    document.getElementById('setting-keepalive').checked = keepaliveEnabled();

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
    // Per-voice tuning lives inline on each row rather than behind another
    // modal — one flat list, no menu inside a menu.
    const tunable = engineByName(state.settings.engine)?.supports_voice_settings;

    el.innerHTML = state.voices.map(v => {
        const pause = v.settings?.sentence_pause;
        return `
        <div class="voice-demo-row">
            <button class="small-btn voice-demo-play" data-voice="${escapeHtml(v.id)}" title="Play demo">▶</button>
            <span class="voice-demo-label">${escapeHtml(v.label)}${v.id === state.settings.voice ? ' <span class="voice-current">✓ current</span>' : ''}</span>
            ${tunable && pause != null ? `
            <span class="voice-pause" title="Silence added at the end of every sentence">
                <button class="small-btn voice-pause-down" data-voice="${escapeHtml(v.id)}">−</button>
                <span class="voice-pause-value" data-voice="${escapeHtml(v.id)}">${pause.toFixed(2)}s</span>
                <button class="small-btn voice-pause-up" data-voice="${escapeHtml(v.id)}">+</button>
            </span>` : ''}
            <button class="secondary-btn btn-small voice-demo-use" data-voice="${escapeHtml(v.id)}">Use</button>
        </div>`;
    }).join('');

    el.querySelectorAll('.voice-demo-play').forEach(btn =>
        btn.addEventListener('click', () => playVoiceDemo(btn)));
    el.querySelectorAll('.voice-demo-use').forEach(btn =>
        btn.addEventListener('click', async () => {
            await updateSetting('voice', btn.dataset.voice);
            renderVoiceDemoList();
        }));
    el.querySelectorAll('.voice-pause-down').forEach(btn =>
        btn.addEventListener('click', () => stepVoicePause(btn.dataset.voice, -0.05)));
    el.querySelectorAll('.voice-pause-up').forEach(btn =>
        btn.addEventListener('click', () => stepVoicePause(btn.dataset.voice, +0.05)));
}

async function stepVoicePause(voiceId, delta) {
    const voice = state.voices.find(v => v.id === voiceId);
    if (!voice) return;
    const s = voice.settings || {};
    const next = Math.min(s.max ?? 3, Math.max(s.min ?? 0,
        Math.round(((s.sentence_pause ?? 0.7) + delta) * 100) / 100));
    try {
        const result = await api('PATCH',
            `/api/voices/${encodeURIComponent(voiceId)}/settings?engine=${encodeURIComponent(state.settings.engine)}`,
            { sentence_pause: next });
        voice.settings = result.settings;
        renderVoiceDemoList();
        // The pause is baked into rendered audio, so cached chapters were
        // dropped server-side; say so rather than letting it look like nothing
        // happened until the next chapter re-renders.
        showToast(`Sentence pause ${next.toFixed(2)}s — cached audio for this voice cleared`);
    } catch (e) {
        showToast('Could not save pause: ' + e.message);
    }
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
    // Voice ids are engine-scoped; without the engine the server resolves
    // against the default one and 404s on every Chatterbox voice.
    const audio = new Audio(`/api/voices/${encodeURIComponent(btn.dataset.voice)}/demo`
        + `?engine=${encodeURIComponent(state.settings.engine)}`);
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
    updateArchiveButton();
}

function updateArchiveButton() {
    const btn = document.getElementById('btn-archive');
    if (!btn) return;
    // Closed box = filed away, open box = ready to take back out.
    const archived = !!state.currentNovel?.archived;
    btn.textContent = archived ? '📂' : '📦';
    btn.title = archived ? 'Unarchive' : 'Archive';
    btn.classList.toggle('is-archived', archived);
}

async function toggleArchive() {
    const novel = state.currentNovel;
    if (!novel) return;
    const next = !novel.archived;
    try {
        const result = await api('PATCH', `/api/novels/${novel.id}/settings`, { archived: next });
        novel.archived = result.archived;
        updateArchiveButton();
        await loadLibrary();
        showToast(next
            ? 'Archived — hidden from the library and no longer pre-rendered'
            : 'Restored to the library');
    } catch (e) {
        showToast('Could not archive: ' + e.message);
    }
}


// ===== Pronunciation & text rules =====
//
// Two problems share one screen because they share a mechanism: both rewrite
// chapter text before it is spoken. Scanning finds words the narrator has no
// pronunciation for; rules are the general form of the same thing.

let pronPreviewAudio = null;

// Chapter range pickers. The export modal and the pronunciation scan both ask
// for "from chapter X to chapter Y", and a number box means knowing the order
// number of the chapter you want — which is the thing you opened the panel to
// find out. One list, one behaviour, both places.
//
// defaultSpan is how many chapters to preselect; null means all of them.
function populateChapterRange(startId, endId, novel, defaultSpan, onReady) {
    const startSel = document.getElementById(startId);
    const endSel = document.getElementById(endId);
    startSel.innerHTML = endSel.innerHTML = '<option value="">Loading chapters…</option>';
    startSel.disabled = endSel.disabled = true;
    return api('GET', `/api/novels/${novel.id}/chapters?page=1&per_page=10000`)
        .then(data => {
            if (state.currentNovel?.id !== novel.id) return;   // modal context changed
            const chs = (data.chapters || []).slice().sort((a, b) => a.order - b.order);
            if (!chs.length) {
                startSel.innerHTML = endSel.innerHTML =
                    '<option value="">No chapters yet</option>';
                return;
            }
            const opts = chs.map(c =>
                `<option value="${c.order}">${c.order}. ${escapeHtml(c.title)}</option>`).join('');
            startSel.innerHTML = opts;
            endSel.innerHTML = opts;
            startSel.value = String(chs[0].order);
            const lastIndex = defaultSpan
                ? Math.min(defaultSpan, chs.length) - 1
                : chs.length - 1;
            endSel.value = String(chs[lastIndex].order);
            startSel.disabled = endSel.disabled = false;
            if (onReady) onReady();
        })
        .catch(e => showToast('Failed to load chapters: ' + e.message, 5000));
}

function openPronunciation() {
    const novel = state.currentNovel;
    if (!novel) return;
    document.getElementById('pron-novel-name').textContent = novel.title;
    populateChapterRange('pron-start', 'pron-end', novel, 10);
    document.getElementById('pron-results').innerHTML = '';
    document.getElementById('rule-preview-out').innerHTML = '';
    switchPronTab('scan');
    loadRules();
    refreshTextCoverage();
    document.getElementById('modal-pronunciation').style.display = 'flex';
}

function closePronunciation() {
    stopPronPreview();
    document.getElementById('modal-pronunciation').style.display = 'none';
}

function switchPronTab(name) {
    document.querySelectorAll('.pron-tab').forEach(b =>
        b.classList.toggle('active', b.dataset.ptab === name));
    ['scan', 'rules'].forEach(t => {
        document.getElementById('pron-panel-' + t).style.display = t === name ? '' : 'none';
    });
}

function stopPronPreview() {
    if (pronPreviewAudio) { pronPreviewAudio.pause(); pronPreviewAudio = null; }
}

// Say the word in a short fixed frame — never bare, never the book's sentence.
//
// Bare fails: Chatterbox is autoregressive with no phoneme input, so a
// one-word prompt gives it almost nothing to predict a stop from. Measured
// over five renders of "Aether" alone it spoke the word twice in two of them,
// and once fell into a stuck loop transcribing as "As-S-A-S-A-S-A-S...".
//
// The book's own sentence fixes that but overshoots: asked to hear
// "Ephesians" it narrated the surrounding cross-reference to Job 31, which is
// not what "hear this word" should do.
//
// This frame was picked by measurement, not taste: four renders of the word
// that destabilises worst, checked against a transcript, with no doubling.
function pronDemoPhrase(word) {
    return 'The word is ' + String(word).trim().replace(/[.!?]+$/, '') + '.';
}

// Hear a word as written, or as a respelling would make it. This is the only
// way to judge a respelling — the phonemes are not the point, the sound is.
async function speakPhrase(text, button) {
    stopPronPreview();
    const original = button ? button.textContent : null;
    if (button) { button.textContent = '...'; button.disabled = true; }
    try {
        const resp = await fetch('/api/pronunciation/speak', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text }),
        });
        if (!resp.ok) {
            let detail = resp.statusText;
            try { detail = (await resp.json()).detail || detail; } catch (err) {}
            throw new Error(detail);
        }
        const url = URL.createObjectURL(await resp.blob());
        pronPreviewAudio = new Audio(url);
        pronPreviewAudio.play().catch(() => {});
    } catch (e) {
        showToast('Could not speak that: ' + e.message);
    } finally {
        if (button) { button.textContent = original; button.disabled = false; }
    }
}

async function scanPronunciation() {
    const novel = state.currentNovel;
    if (!novel) return;
    const status = document.getElementById('pron-scan-status');
    const results = document.getElementById('pron-results');
    const start = parseInt(document.getElementById('pron-start').value, 10) || null;
    const end = parseInt(document.getElementById('pron-end').value, 10) || null;

    status.textContent = 'Scanning… this reads every chapter in the range.';
    results.innerHTML = '';
    try {
        const data = await api('POST', '/api/novels/' + novel.id + '/pronunciation/scan',
                               { start_order: start, end_order: end });
        status.textContent = data.words.length + ' word(s) with no known pronunciation across '
            + data.chapters_scanned + ' chapter(s). '
            + data.already_handled + ' already respelled.';
        renderPronResults(data.words);
    } catch (e) {
        status.textContent = 'Scan failed: ' + e.message;
    }
}

function renderPronResults(words) {
    const el = document.getElementById('pron-results');
    if (!words.length) {
        el.innerHTML = '<p class="hint">Nothing unpronounceable found in that range.</p>';
        return;
    }
    el.innerHTML = words.map(function (w) {
        return '<div class="pron-row" data-word="' + escapeHtml(w.word) + '">'
            + '<div class="pron-word"><strong>' + escapeHtml(w.word) + '</strong>'
            + '<span class="pron-count">x' + w.count + '</span></div>'
            + '<div class="pron-example">' + escapeHtml(w.example || '') + '</div>'
            + '<div class="pron-actions">'
            + '<button class="secondary-btn btn-small pron-hear">&#9654; as written</button>'
            // No worked example. Chatterbox has no phoneme input, so there is
            // no notation to teach — you just spell it how it should sound, and
            // an invented sample word implied a convention that does not exist.
            + '<input type="text" class="pron-respell" placeholder="spell it how it should sound">'
            + '<button class="secondary-btn btn-small pron-hear">&#9654; respelling</button>'
            + '<button class="secondary-btn btn-small pron-save">Save</button>'
            + '</div></div>';
    }).join('');

    el.querySelectorAll('.pron-row').forEach(function (row) {
        const word = row.dataset.word;
        const input = row.querySelector('.pron-respell');
        row.querySelectorAll('.pron-hear')[0].addEventListener('click', function (e) {
            speakPhrase(pronDemoPhrase(word), e.currentTarget);
        });
        row.querySelectorAll('.pron-hear')[1].addEventListener('click', function (e) {
            const value = input.value.trim();
            if (!value) { showToast('Type a respelling first'); return; }
            speakPhrase(pronDemoPhrase(value), e.currentTarget);
        });
        row.querySelector('.pron-save').addEventListener('click', async function () {
            const value = input.value.trim();
            if (!value) { showToast('Type a respelling first'); return; }
            try {
                await api('POST', '/api/novels/' + state.currentNovel.id + '/text-rules', {
                    kind: 'literal', pattern: word, replacement: value,
                    note: 'pronunciation', sort_order: 100,
                });
                // Solved words leave the list, and a rescan won't surface them again.
                row.remove();
                showToast('"' + word + '" will be spoken as "' + value + '"');
                loadRules();
            } catch (e) {
                showToast('Could not save: ' + e.message);
            }
        });
    });
}

async function loadRules() {
    const el = document.getElementById('pron-rules-list');
    if (!state.currentNovel) return;
    try {
        const data = await api('GET', '/api/novels/' + state.currentNovel.id + '/text-rules');
        const shared = data.global_rules.map(function (r) {
            return Object.assign({}, r, { shared: true });
        });
        const all = data.rules.concat(shared);
        if (!all.length) {
            el.innerHTML = '<p class="hint">No rules yet for this novel.</p>';
            return;
        }
        el.innerHTML = all.map(function (r) {
            return '<div class="rule-row" data-id="' + r.id + '">'
                + '<span class="rule-kind">' + escapeHtml(r.kind) + '</span>'
                + '<code class="rule-pattern">' + escapeHtml(r.pattern) + '</code>'
                + '<span class="rule-arrow">&rarr;</span>'
                + '<code class="rule-replacement">' + escapeHtml(r.replacement) + '</code>'
                + (r.shared ? '<span class="rule-shared">global</span>' : '')
                + '<button class="small-btn rule-delete" title="Delete">&#10005;</button>'
                + '</div>';
        }).join('');
        el.querySelectorAll('.rule-delete').forEach(function (btn) {
            btn.addEventListener('click', async function () {
                const id = btn.closest('.rule-row').dataset.id;
                try {
                    await api('DELETE', '/api/text-rules/' + id);
                    loadRules();
                    showToast('Rule deleted — cached audio cleared');
                } catch (e) { showToast('Could not delete: ' + e.message); }
            });
        });
    } catch (e) {
        el.innerHTML = '<p class="hint">Could not load rules.</p>';
    }
}

async function previewRule() {
    const out = document.getElementById('rule-preview-out');
    const kind = document.getElementById('rule-kind').value;
    const pattern = document.getElementById('rule-pattern').value;
    const replacement = document.getElementById('rule-replacement').value;
    // Preview against a real chapter: a pattern that looks right often isn't.
    const chapter = state.chapters.length ? state.chapters[0] : null;
    try {
        const body = { kind: kind, pattern: pattern, replacement: replacement };
        if (chapter) { body.chapter_id = chapter.id; }
        else { body.text = 'Stealth V and Blade Mastery 4 > 5.'; }
        const data = await api('POST', '/api/text-rules/preview', body);
        out.innerHTML = '<p class="hint">' + data.match_count + ' match(es)</p>'
            + data.examples.map(function (e) {
                return '<div class="rule-example"><code>' + escapeHtml(e.before) + '</code>'
                    + '<span class="rule-arrow">&rarr;</span>'
                    + '<code>' + escapeHtml(e.after) + '</code></div>';
            }).join('');
    } catch (e) {
        out.innerHTML = '<p class="error-msg">' + escapeHtml(e.message) + '</p>';
    }
}

async function saveRule() {
    const kind = document.getElementById('rule-kind').value;
    const pattern = document.getElementById('rule-pattern').value.trim();
    const replacement = document.getElementById('rule-replacement').value;
    const note = document.getElementById('rule-note').value.trim();
    if (!pattern) { showToast('A pattern is required'); return; }
    try {
        await api('POST', '/api/novels/' + state.currentNovel.id + '/text-rules',
                  { kind: kind, pattern: pattern, replacement: replacement,
                    note: note || null, sort_order: 50 });
        document.getElementById('rule-pattern').value = '';
        document.getElementById('rule-replacement').value = '';
        document.getElementById('rule-note').value = '';
        document.getElementById('rule-preview-out').innerHTML = '';
        loadRules();
        showToast('Rule saved — cached audio cleared');
    } catch (e) {
        showToast('Could not save: ' + e.message);
    }
}


// ===== Text backfill =====
//
// Chapter text is only stored as a side effect of playing or prefetching, so
// everything behind your position was never fetched. Scanning a whole novel
// for pronunciation should not require listening to it first. Text is about
// 20 KB a chapter, so caching a whole book costs a few megabytes.


function formatBytes(n) {
    if (!n) return '0 KB';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

// Coverage is still worth stating — it tells you whether a scan can see the
// whole range. There is no longer a button, because backfilling is not a
// decision: the library refresh fetches whatever text is missing.
async function refreshTextCoverage() {
    const el = document.getElementById('pron-coverage');
    if (!el || !state.currentNovel) return;
    try {
        const d = await api('GET', '/api/novels/' + state.currentNovel.id + '/text-coverage');
        if (d.missing === 0) {
            el.textContent = 'All ' + d.total_chapters + ' chapters have text ('
                + formatBytes(d.cached_bytes) + ').';
        } else {
            el.textContent = d.cached + ' of ' + d.total_chapters
                + ' chapters have text — the rest are being fetched in the background.';
        }
    } catch (e) {
        el.textContent = '';
    }
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

    const globalEngineLabel = engineByName(state.settings.engine)?.label || state.settings.engine;
    const nsEngine = document.getElementById('ns-engine');
    nsEngine.closest('.setting-row').style.display = state.engines.length ? '' : 'none';
    nsEngine.innerHTML =
        engineOptionsHtml(ov.engine || '', { inheritLabel: `Default (${globalEngineLabel})` });

    // Voices belong to whichever engine is in effect for THIS novel, which may
    // be its own override rather than the global one.
    const novelEngine = ov.engine || state.settings.engine;
    const novelVoices = voicesForEngine(novelEngine);
    const inheritedVoice = novel.effective_settings?.voice ?? state.settings.voice;
    const globalVoiceLabel = novelVoices.find(v => v.id === inheritedVoice)?.label || inheritedVoice;
    document.getElementById('ns-voice').innerHTML =
        `<option value="">Default (${escapeHtml(globalVoiceLabel)})</option>` +
        novelVoices.map(v =>
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
    populateChapterRange('export-start', 'export-end', novel, null,
                         updateExportNamePreview);
    const eff = novel.effective_settings || {};
    const voiceSel = document.getElementById('export-voice');
    voiceSel.innerHTML = voicesForEngine(eff.engine || state.settings.engine).map(v =>
        `<option value="${escapeHtml(v.id)}" ${v.id === eff.voice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`
    ).join('');
    // Same +/− stepper as everywhere else, defaulting to the novel's
    // effective speed (0.05 steps, clamped 0.5–2.0)
    state.exportSpeed = eff.speed ?? 1.0;
    updateExportSpeedDisplay();
    updateExportNamePreview();
    document.getElementById('modal-export').style.display = 'flex';
}

// Measured on this machine's RTX 2070: seconds of audio produced per second
// of rendering. Chatterbox is ~35x slower than Kokoro, so a range that takes
// hours on one takes days on the other — worth saying before you start it.
//
// Chatterbox was 1.9 here, which was optimistic and made every export estimate
// read about 25% short. 1.43 is what four consecutive chapters actually
// averaged end to end, including the scraping between them.
const ENGINE_THROUGHPUT = { kokoro: 50, chatterbox: 1.43 };
const WORDS_PER_MINUTE = 155;  // typical narration pace

function updateExportNamePreview() {
    if (!state.currentNovel) return;
    const s = document.getElementById('export-start').value || '?';
    const e = document.getElementById('export-end').value || '?';
    document.getElementById('export-name-preview').textContent =
        `${state.currentNovel.title} - Chapters ${s} - ${e}.m4b`;
    updateExportEstimate();
}

function humanDuration(seconds) {
    if (seconds < 90) return `${Math.round(seconds)}s`;
    const mins = seconds / 60;
    if (mins < 90) return `${Math.round(mins)} min`;
    const hours = mins / 60;
    if (hours < 36) return `${hours.toFixed(1)} hours`;
    return `${(hours / 24).toFixed(1)} days`;
}

function updateExportEstimate() {
    const el = document.getElementById('export-estimate');
    if (!el || !state.currentNovel) return;
    const start = parseInt(document.getElementById('export-start').value, 10);
    const end = parseInt(document.getElementById('export-end').value, 10);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
        el.textContent = '';
        return;
    }

    const chapters = end - start + 1;
    const words = state.chapters.length
        ? state.chapters.reduce((a, c) => a + (c.word_count || 0), 0) / state.chapters.length
        : 2500;
    const audioSeconds = chapters * (words / WORDS_PER_MINUTE) * 60;

    const engineName = state.currentNovel.effective_settings?.engine || state.settings.engine;
    const rate = ENGINE_THROUGHPUT[engineName] ?? 2;
    const renderSeconds = audioSeconds / rate;

    const engineLabel = engineByName(engineName)?.label || engineName;
    let msg = `${chapters} chapters ≈ ${humanDuration(audioSeconds)} of audio · `
            + `~${humanDuration(renderSeconds)} to render on ${engineLabel}`;
    if (renderSeconds > 6 * 3600) {
        msg += '. Runs in the background at lowest priority — playback always wins,'
             + ' and it resumes where it left off if you restart.';
    }
    el.textContent = msg;
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
    document.getElementById('tab-archived').addEventListener('click', () => setLibraryTab('archived'));
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
    document.getElementById('btn-pronunciation').addEventListener('click', openPronunciation);
    document.getElementById('btn-pron-close').addEventListener('click', closePronunciation);
    document.getElementById('modal-pronunciation').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closePronunciation();
    });
    document.querySelectorAll('.pron-tab').forEach(b =>
        b.addEventListener('click', () => switchPronTab(b.dataset.ptab)));
    document.getElementById('btn-pron-scan').addEventListener('click', scanPronunciation);
    document.getElementById('btn-rule-preview').addEventListener('click', previewRule);
    document.getElementById('btn-rule-save').addEventListener('click', saveRule);

    document.getElementById('btn-archive').addEventListener('click', toggleArchive);

    // Per-novel settings
    document.getElementById('btn-novel-settings').addEventListener('click', openNovelSettings);
    document.getElementById('btn-ns-close').addEventListener('click', closeNovelSettings);
    document.getElementById('modal-novel-settings').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeNovelSettings();
    });
    document.getElementById('ns-engine').addEventListener('change', async (e) => {
        // Clear any voice override at the same time: a voice id from the old
        // engine is meaningless to the new one, and leaving it set would just
        // fall back silently on the server.
        const next = e.target.value || null;
        if (state.currentNovel?.settings?.voice) {
            await updateNovelSetting('voice', null);
        }
        await updateNovelSetting('engine', next);
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

    // iOS freezes backgrounded pages; handlers and metadata can be lost on
    // thaw, so re-establish them whenever we come back.
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') reviveMediaSession();
    });
    window.addEventListener('pageshow', reviveMediaSession);

    // iOS only lets an AudioContext start under a user gesture, and playback
    // can begin without one (autoplay chaining into the next chapter). Any
    // tap will do, so take the first one on offer. Cheap no-op once built.
    document.addEventListener('click', prepareSilentKeepalive, true);

    document.getElementById('setting-keepalive').addEventListener('change', (e) => {
        localStorage.setItem(KEEPALIVE_KEY, e.target.checked ? '1' : '0');
        if (e.target.checked && !state.isPlaying) startSilentKeepalive();
        if (!e.target.checked) stopSilentKeepalive();
        showToast(e.target.checked
            ? 'Lock-screen controls will stay active while paused'
            : 'Lock-screen keepalive off');
    });

    // Settings
    document.getElementById('btn-settings').addEventListener('click', openSettings);
    document.getElementById('btn-settings-close').addEventListener('click', closeSettings);

    document.getElementById('setting-engine').addEventListener('change', async (e) => {
        // The server snaps the voice to one the new engine has; re-read it
        // rather than guessing, then repopulate the voice list to match.
        await updateSetting('engine', e.target.value);
        state.voices = voicesForEngine(state.settings.engine);
        document.getElementById('setting-voice').innerHTML = state.voices.map(v =>
            `<option value="${escapeHtml(v.id)}" ${v.id === state.settings.voice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`
        ).join('');
        renderEngineHint();
        showToast(`Model: ${engineByName(state.settings.engine)?.label || e.target.value}`);
    });

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
    document.getElementById('btn-back-15').addEventListener('click', () => seekRelative(-SKIP_BACK_SECONDS));
    document.getElementById('btn-fwd-30').addEventListener('click', () => seekRelative(SKIP_FORWARD_SECONDS));
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
