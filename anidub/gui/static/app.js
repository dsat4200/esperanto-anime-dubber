let currentClip = null;
let characters = {};
let timelineClips = [];
let episodes = [];
let activeStem = null;
let dataAnimeName = '';
let currentJobKey = null;

let selectedEpisodes = new Set();
let batchTrackAudioIdx = null;
let batchTrackSubIdx = null;
let pendingBatchType = null;

let timelineCtx = null;
let timelineVP = { startSec: 0, pxPerSec: 50 };
let timelineDrag = null;
let timelineCtxMenuClipId = null;

// ── i18n ─────────────────────────────────────
let i18n = {};

async function loadI18n(lang) {
    try {
        const resp = await fetch('/static/i18n/' + lang + '.json', { cache: 'no-cache' });
        i18n = await resp.json();
    } catch (e) {
        console.error('Failed to load i18n ' + lang + ':', e);
        i18n = {};
    }
}

function t(key) {
    return i18n[key] != null ? i18n[key] : key;
}

function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        const val = i18n[key];
        if (val != null) el.textContent = val;
    });
    document.querySelectorAll('[data-i18n-ph]').forEach(el => {
        const key = el.getAttribute('data-i18n-ph');
        const val = i18n[key];
        if (val != null) el.placeholder = val;
    });
}

async function onLangChange(lang) {
    localStorage.setItem('anidub.lang', lang);
    await loadI18n(lang);
    applyI18n();
    // Re-render dynamic UI strings so translated content appears.
    if (currentClip) renderClip();
    if (document.getElementById('home-episodes').style.display !== 'none') renderEpisodeHome();
    loadProjectPicker();
    drawTimeline();
    refreshGpuStats();
}

const STATUS_COLORS = {
    pending: '#2a2a3e', translating: '#4a4060', translated: '#0f3460',
    cloned: '#c8a000', accepted: '#0a4', rejected: '#a30',
    skipped: '#555', non_dub: '#3a3a4e', sign: '#1a5a5a',
};
const RULER_H = 24, CLIP_TOP = 30, CLIP_MIN_H = 28;

async function api(url, opts = {}) {
    const defaults = { headers: { 'Content-Type': 'application/json' } };
    if (opts.body && typeof opts.body === 'object') {
        opts.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(url, { ...defaults, ...opts });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || resp.statusText);
    }
    return resp.json();
}

function showOverlay(msg) {
    document.getElementById('overlay').style.display = 'flex';
    document.getElementById('overlay-msg').textContent = msg;
}
function hideOverlay() { document.getElementById('overlay').style.display = 'none'; }

// ── Project discovery ─────────────────────────

async function loadProjectPicker() {
    try {
        const projects = await api('/api/projects');
        const sel = document.getElementById('project-picker');
        sel.innerHTML = '<option value="">' + escHtml(t('home.picker_load_default')) + '</option>' +
            projects.map(p => `<option value="${p.path}">${p.anime_name} (${p.episode_count} eps)</option>`).join('');
    } catch (e) { console.error('project picker:', e); }
}

function onProjectPicker() {
    const sel = document.getElementById('project-picker');
    document.getElementById('anime-name').value = sel.selectedOptions[0]?.text.split(' (')[0] || '';
}

async function loadSelectedProject() {
    const path = document.getElementById('project-picker').value;
    if (!path) return;
    showOverlay(t('overlay.loading_project'));
    try {
        await api('/api/open', { method: 'POST', body: { project_dir: path } });
        hideOverlay();
        await loadEpisodes();
    } catch (e) {
        hideOverlay();
        alert(t('alert.load_failed') + e.message);
    }
}

async function openAnime() {
    const name = document.getElementById('anime-name').value.trim();
    if (!name) return;
    showOverlay(t('overlay.creating_project'));
    try {
        await api('/api/open', { method: 'POST', body: { anime: name } });
        hideOverlay();
        await loadEpisodes();
    } catch (e) {
        hideOverlay();
        alert(t('alert.create_failed') + e.message);
    }
}

// ── Episodes ──────────────────────────────────

async function loadEpisodes(autoOpen = true) {
    const data = await api('/api/episodes');
    episodes = data.episodes;
    activeStem = data.active_stem;
    dataAnimeName = data.anime_name || '';
    renderEpisodeHome();
    checkRunningJobs();
    if (autoOpen && activeStem) {
        await setupEditorForActive();
    }
}

function renderEpisodeHome() {
    const wrapper = document.getElementById('home-episodes');
    const grid = document.getElementById('episode-grid');
    if (!episodes.length) {
        wrapper.style.display = 'none';
        return;
    }
    wrapper.style.display = 'block';
    document.getElementById('home-title').textContent = dataAnimeName || t('home.title_fallback');
    grid.innerHTML = episodes.map(ep => {
        const pct = ep.progress_pct || 0;
        const tPct = ep.translation_pct || 0;
        const cPct = ep.clone_pct || 0;
        const trLbl = t('home.tr_label').replace('{pct}', tPct);
        const clLbl = t('home.cl_label').replace('{pct}', cPct);
        const acLbl = t('home.ac_label').replace('{pct}', pct);
        return `<div class="ep-card" data-color="${ep.color}" data-stem="${ep.stem}"
                     onclick="onEpisodeClick(event, '${ep.stem}')"
                     ondblclick="openEpisode('${ep.stem}')">
            <div class="ep-num">#${ep.number}</div>
            <div class="ep-title">${escHtml(ep.title || ep.stem)}</div>
            <div class="ep-bars">
                <div class="ep-bar ep-bar-tr"><div class="ep-bar-fill" style="width:${tPct}%"></div><span>${trLbl}</span></div>
                <div class="ep-bar ep-bar-cl"><div class="ep-bar-fill" style="width:${cPct}%"></div><span>${clLbl}</span></div>
                <div class="ep-bar ep-bar-ac"><div class="ep-bar-fill" style="width:${pct}%"></div><span>${acLbl}</span></div>
            </div>
        </div>`;
    }).join('');
    updateSelectionUI();
}

// Update just the Tr / Cl / Ac bars on existing episode cards without a
// full re-render — avoids flicker and keeps selection state.
async function refreshEpisodeBars() {
    try {
        const data = await api('/api/episodes');
        for (const ep of data.episodes) {
            const card = document.querySelector(`.ep-card[data-stem="${ep.stem}"]`);
            if (!card) continue;
            const tPct = ep.translation_pct || 0;
            const cPct = ep.clone_pct || 0;
            const pct = ep.progress_pct || 0;
            const bars = card.querySelectorAll('.ep-bar');
            if (bars[0]) {
                bars[0].querySelector('.ep-bar-fill').style.width = tPct + '%';
                bars[0].querySelector('span').textContent = t('home.tr_label').replace('{pct}', tPct);
            }
            if (bars[1]) {
                bars[1].querySelector('.ep-bar-fill').style.width = cPct + '%';
                bars[1].querySelector('span').textContent = t('home.cl_label').replace('{pct}', cPct);
            }
            if (bars[2]) {
                bars[2].querySelector('.ep-bar-fill').style.width = pct + '%';
                bars[2].querySelector('span').textContent = t('home.ac_label').replace('{pct}', pct);
            }
        }
    } catch (e) {}
}

function fillColor(color) {
    const m = { cyan: '#0ff', green: '#0a4', lime: '#9acd32', yellow: '#cc0', orange: '#e80', red: '#e30', darkgrey: '#555', lightgrey: '#444' };
    return m[color] || '#444';
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function onEpisodeClick(e, stem) {
    if (e.shiftKey) {
        toggleSelection(stem);
    } else {
        if (!selectedEpisodes.has(stem)) {
            selectedEpisodes.clear();
            selectedEpisodes.add(stem);
        } else if (selectedEpisodes.size === 1) {
            selectedEpisodes.clear();
        }
        updateSelectionUI();
    }
}

function toggleSelection(stem) {
    if (selectedEpisodes.has(stem)) selectedEpisodes.delete(stem);
    else selectedEpisodes.add(stem);
    updateSelectionUI();
}

function updateSelectionUI() {
    const cards = document.querySelectorAll('.ep-card');
    cards.forEach(c => {
        const stem = c.dataset.stem;
        c.classList.toggle('selected', selectedEpisodes.has(stem));
    });
    const ba = document.getElementById('batch-actions');
    const count = selectedEpisodes.size;
    ba.style.display = count > 0 ? 'flex' : 'none';
    document.querySelectorAll('.sel-count').forEach(s => s.textContent = count);
}

// ── Open / Home ───────────────────────────────

async function openEpisode(stem) {
    showOverlay(t('overlay.loading_episode'));
    try {
        await api('/api/episodes/select', { method: 'POST', body: { stem } });
        activeStem = stem;
        hideOverlay();
        document.getElementById('home-panel').style.display = 'none';
        document.getElementById('editor-panel').style.display = 'flex';
        await setupEditor();
    } catch (e) {
        hideOverlay();
        alert(t('alert.open_episode_failed') + e.message);
    }
}

async function goHome() {
    document.getElementById('editor-panel').style.display = 'none';
    document.getElementById('home-panel').style.display = 'flex';
    selectedEpisodes.clear();
    await api('/api/cleanup', { method: 'POST' });
    document.getElementById('job-bar').style.display = 'none';
    currentJobKey = null;
    await loadEpisodes(false);
}

async function setupEditorForActive() {
    const tracks = await api('/api/tracks');
    if (!tracks.tracks_confirmed) {
        if (tracks.subtitle_none) {
            showNoSubsModal(tracks);
        } else {
            showEpisodeTrackPicker(tracks);
        }
        return;
    }
    if (!tracks.demucs_done) {
        await runDemucsFlow();
    }
    document.getElementById('home-panel').style.display = 'none';
    document.getElementById('editor-panel').style.display = 'flex';
    await setupEditor();
}

async function setupEditor() {
    const tracks = await api('/api/tracks');
    if (!tracks.tracks_confirmed) {
        if (tracks.subtitle_none) {
            showNoSubsModal(tracks);
        } else {
            showEpisodeTrackPicker(tracks);
        }
        return;
    }
    if (!tracks.demucs_done) {
        await runDemucsFlow();
    }
    initTimelineCanvas();
    await loadCharacters();
    await loadTimeline();
    document.getElementById('video-player').addEventListener('timeupdate', onVideoTimeUpdate);
    populateEpisodeDropdown();
    if (playback.mode === 'on') {
        await refreshPlaybackPlan();
    }
    const first = await getFirstUnaccepted();
    await loadClip(first);
}

async function runDemucsFlow() {
    showOverlay(t('overlay.running_demucs'));
    try {
        await api('/api/demucs', { method: 'POST' });
    } catch (e) {
        alert(t('alert.demucs_failed') + e.message);
    }
    hideOverlay();
}

function populateEpisodeDropdown() {
    const sel = document.getElementById('episode-select');
    sel.innerHTML = episodes.map(ep =>
        `<option value="${ep.stem}" ${ep.stem === activeStem ? 'selected' : ''}>${ep.title || ep.stem}</option>`
    ).join('');
}

async function switchEpisode(stem) {
    if (!stem || stem === activeStem) return;
    showOverlay(t('overlay.switching_episode'));
    try {
        await api('/api/episodes/select', { method: 'POST', body: { stem } });
        activeStem = stem;
        hideOverlay();
        await setupEditor();
    } catch (e) {
        hideOverlay();
        alert(t('alert.switch_failed') + e.message);
    }
}

async function getFirstUnaccepted() {
    if (!timelineClips.length) return null;
    for (const c of timelineClips) {
        if (c.status !== 'accepted' && c.status !== 'non_dub' && c.status !== 'sign') return c.clip_id;
    }
    return timelineClips[0]?.clip_id || null;
}

// ── Editor ───────────────────────────────────

async function loadClip(clipId) {
    if (!clipId) return;
    try {
        const clip = await api('/api/clips/' + clipId);
        currentClip = clip;
        renderClip();
        if (playback.mode === 'on') {
            const idx = playback.plan.findIndex(s => s.clip_id === clipId);
            if (idx >= 0) seekPlaybackTo(playback.plan[idx].start);
            drawTimeline();
            return;
        }
        if (clip.status === 'non_dub') {
            loadRawPreview(clip.start_sec, clip.end_sec);
        } else if (clip.needs_processing) {
            await autoProcess(clipId);
        } else if (clip.clone_path) {
            await previewCurrent();
        }
        drawTimeline();
    } catch (e) { console.error('loadClip failed:', e); }
}

function renderClip() {
    const c = currentClip;
    if (!c) return;
    document.getElementById('clip-title').textContent =
        t('editor.clip_title_template')
            .replace('{id}', c.clip_id)
            .replace('{start}', fmtTs(c.start_sec))
            .replace('{end}', fmtTs(c.end_sec));
    document.getElementById('original-text').textContent = c.original_text;
    document.getElementById('translation-text').value = c.translated_text || '';
    document.getElementById('pronunciation-text').value = c.pronunciation_override || '';
    document.getElementById('instruct-extra').value = c.instruct_extra || '';

    const sel = document.getElementById('char-select');
    sel.innerHTML = '<option value="">' + escHtml(t('editor.character_none_option')) + '</option>' +
        Object.keys(characters).map(name =>
            `<option value="${name}" ${c.character === name ? 'selected' : ''}>${name}</option>`
        ).join('');
    const msel = document.getElementById('mood-select');
    if (c.character && characters[c.character]) {
        msel.innerHTML = Object.keys(characters[c.character]).map(m =>
            `<option value="${m}" ${c.character_mood === m ? 'selected' : ''}>${m}</option>`
        ).join('');
    }
    document.getElementById('speed-slider').value = Math.round((c.speed_factor || 1.0) * 100);
    document.getElementById('speed-val').textContent = (c.speed_factor || 1.0).toFixed(2);

    document.getElementById('audio-shift-slider').value = Math.round(c.audio_offset_ms || 0);
    document.getElementById('audio-shift-val').textContent = Math.round(c.audio_offset_ms || 0);

    const info = [];
    if (c.status === 'non_dub') info.push(t('info.original_audio_only'));
    if (c.status === 'sign') info.push(t('info.sign_no_audio'));
    if (c.clone_ms) info.push(t('info.clone_ms_template').replace('{sec}', (c.clone_ms / 1000).toFixed(1)));
    if (c.attempts) info.push(t('info.attempts_template').replace('{n}', c.attempts));
    if (c.audio_offset_ms) info.push(t('info.offset_template').replace('{ms}', c.audio_offset_ms.toFixed(0)));
    info.push(t('info.status_prefix') + t('status.' + c.status));
    document.getElementById('clone-info').textContent = info.join('  |  ');

    const nd = c.status === 'non_dub' || c.status === 'sign';
    document.querySelectorAll('.clone-only').forEach(el => el.style.display = nd ? 'none' : '');
    document.querySelectorAll('.accept-only').forEach(el => el.style.display = nd ? 'none' : '');

    const btnSign = document.getElementById('btn-toggle-sign');
    btnSign.textContent = c.status === 'sign' ? t('editor.mark_vocal') : t('editor.mark_sign');
    btnSign.style.display = c.status === 'non_dub' ? 'none' : '';
}

async function autoProcess(clipId) {
    showOverlay(t('overlay.processing'));
    try {
        const char = document.getElementById('char-select').value || undefined;
        const mood = document.getElementById('mood-select').value || 'normal';
        const result = await api(`/api/clips/${clipId}/process`,
            { method: 'POST', body: { character: char, mood } });
        currentClip.status = result.status;
        currentClip.translated_text = result.translated_text || currentClip.translated_text;
        loadTimeline();
        renderClip();
        if (result.preview_url) {
            const video = document.getElementById('video-player');
            video.src = result.preview_url + '?t=' + Date.now();
            video.load();
            video.play();
        }
    } catch (e) { console.error('autoProcess:', e); }
    hideOverlay();
}

async function prevClip() {
    if (!currentClip || !currentClip.clip_id) return;
    const idx = timelineClips.findIndex(c => c.clip_id === currentClip.clip_id);
    if (idx > 0) loadClip(timelineClips[idx - 1].clip_id);
}

async function nextClip() {
    if (!currentClip || !currentClip.clip_id) return;
    const idx = timelineClips.findIndex(c => c.clip_id === currentClip.clip_id);
    if (idx >= 0 && idx < timelineClips.length - 1) loadClip(timelineClips[idx + 1].clip_id);
}

async function restoreTranslation() {
    const c = currentClip;
    if (!c) return;
    showOverlay(t('overlay.translating'));
    try {
        const resp = await api(`/api/clips/${c.clip_id}/translate`, { method: 'POST' });
        document.getElementById('translation-text').value = resp.translated_text;
        currentClip.translated_text = resp.translated_text;
        currentClip.status = 'translated';
        renderClip();
        loadTimeline();
    } catch (e) { alert(t('alert.translate_failed') + e.message); }
    hideOverlay();
}

async function saveSettings() {
    const c = currentClip;
    if (!c) return;
    const clipId = c.clip_id;
    const translation = document.getElementById('translation-text').value.trim() || undefined;
    const pronunciation = document.getElementById('pronunciation-text').value.trim() || null;
    const instructExtra = document.getElementById('instruct-extra').value.trim() || null;
    const character = document.getElementById('char-select').value || null;
    const mood = document.getElementById('mood-select').value || 'normal';
    const speedFactor = parseInt(document.getElementById('speed-slider').value) / 100;
    const audioOffset = parseFloat(document.getElementById('audio-shift-slider').value);

    await api(`/api/clips/${clipId}/translate`, { method: 'POST', body: { text_override: translation } });
    await api(`/api/clips/${clipId}/pronunciation`, { method: 'POST', body: { pronunciation_override: pronunciation } });
    await api(`/api/clips/${clipId}/instruct`, { method: 'POST', body: { instruct_extra: instructExtra } });
    await api(`/api/clips/${clipId}/character`, { method: 'POST', body: { character, mood } });
    await api(`/api/clips/${clipId}/speed`, { method: 'POST', body: { speed_factor: speedFactor } });
    await api(`/api/clips/${clipId}/audio-offset`, { method: 'POST', body: { offset_ms: audioOffset } });

    currentClip.translated_text = translation;
    currentClip.status = 'translated';
    currentClip.pronunciation_override = pronunciation;
    currentClip.instruct_extra = instructExtra;
    currentClip.character = character;
    currentClip.character_mood = mood;
    currentClip.speed_factor = speedFactor;
    currentClip.audio_offset_ms = audioOffset;
    renderClip();
    loadTimeline();
}

async function cloneCurrent() {
    const c = currentClip;
    if (!c) return;
    showOverlay(t('overlay.cloning'));
    try {
        const char = document.getElementById('char-select').value || undefined;
        const mood = document.getElementById('mood-select').value || 'normal';
        const resp = await api(`/api/clips/${c.clip_id}/clone`,
            { method: 'POST', body: { character: char, mood } });
        currentClip.clone_ms = resp.inference_ms;
        currentClip.status = 'cloned';
        currentClip.attempts = (currentClip.attempts || 0) + 1;
        renderClip();
        loadTimeline();
        await previewCurrent();
    } catch (e) { alert(t('alert.clone_failed') + e.message); }
    hideOverlay();
}

async function previewCurrent() {
    const c = currentClip;
    if (!c) return;
    if (c.status === 'non_dub') {
        loadRawPreview(c.start_sec, c.end_sec);
        return;
    }
    showOverlay(t('overlay.generating_preview'));
    try {
        const resp = await api(`/api/clips/${c.clip_id}/preview`, { method: 'POST' });
        const video = document.getElementById('video-player');
        video.src = resp.url + '?t=' + Date.now();
        video.load();
        video.play();
    } catch (e) { alert(t('alert.preview_failed') + e.message); }
    hideOverlay();
}

async function loadRawPreview(start_sec, end_sec) {
    try {
        const blob = await fetch('/api/preview-raw', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start_sec, end_sec }),
        }).then(r => r.blob());
        const video = document.getElementById('video-player');
        video.src = URL.createObjectURL(blob);
        video.load();
        video.play();
    } catch (e) { console.error('loadRawPreview:', e); }
}

async function toggleSign() {
    const c = currentClip;
    if (!c) return;
    const newStatus = c.status === 'sign' ? 'pending' : 'sign';
    await api(`/api/clips/${c.clip_id}/status`, { method: 'POST', body: { status: newStatus } });
    currentClip.status = newStatus;
    renderClip();
    loadTimeline();
}

async function acceptCurrent() {
    const c = currentClip;
    if (!c) return;
    if (c.needs_processing) await autoProcess(c.clip_id);
    try {
        await api(`/api/clips/${c.clip_id}/accept`, { method: 'POST' });
        currentClip.status = 'accepted';
        loadTimeline();
        loadEpisodes();
        nextClip();
    } catch (e) { alert(t('alert.accept_failed') + e.message); }
}

async function rejectCurrent() {
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.clip_id}/reject`, { method: 'POST' });
    currentClip.status = 'rejected';
    renderClip();
    loadTimeline();
}

async function resetCurrent() {
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.clip_id}/reset`, { method: 'POST' });
    currentClip.status = 'pending';
    currentClip.translated_text = null;
    currentClip.clone_ms = null;
    renderClip();
    loadTimeline();
}

async function onSpeedChange(val) {
    const pct = parseInt(val) / 100;
    document.getElementById('speed-val').textContent = pct.toFixed(2);
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.clip_id}/speed`, { method: 'POST', body: { speed_factor: pct } });
    currentClip.speed_factor = pct;
}

async function onAudioShiftChange(val) {
    const ms = parseFloat(val);
    document.getElementById('audio-shift-val').textContent = Math.round(ms);
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.clip_id}/audio-offset`, { method: 'POST', body: { offset_ms: ms } });
    currentClip.audio_offset_ms = ms;
    loadTimeline();
}

async function onCharacterChange() {
    const c = currentClip;
    if (!c) return;
    const char = document.getElementById('char-select').value || null;
    const mood = document.getElementById('mood-select').value || 'normal';
    await api(`/api/clips/${c.clip_id}/character`, { method: 'POST', body: { character: char, mood } });
    currentClip.character = char;
    currentClip.character_mood = mood;
    renderClip();
}

// ── Batch Operations ──────────────────────────

async function batchTranslate() {
    pendingBatchType = 'translate';
    if (batchTrackAudioIdx === null) {
        await showTrackModal();
        return;
    }
    await runBatch('translate');
}

async function batchClone() {
    pendingBatchType = 'clone';
    if (batchTrackAudioIdx === null) {
        await showTrackModal();
        return;
    }
    await runBatch('clone');
}

async function showTrackModal() {
    const stem = selectedEpisodes.values().next().value;
    if (!stem) return;
    const data = await api('/api/tracks');
    document.getElementById('modal-audio-tracks').innerHTML = '<h4>' + escHtml(t('modal.audio_label')) + '</h4>' +
        data.audio.map((t, i) => `<label><input type="radio" name="m-audio" value="${i}" ${i===0?'checked':''}> ${t.language||'?'} (${t.codec}, ${t.channels}ch)</label>`).join('');
    document.getElementById('modal-sub-tracks').innerHTML = '<h4>' + escHtml(t('modal.subtitles_label')) + '</h4>' +
        data.subtitle.map((t, i) => `<label><input type="radio" name="m-sub" value="${i}" ${i===0?'checked':''}> ${t.language||'?'} (${t.codec})</label>`).join('');
    const modalEl = document.getElementById('track-modal');
    modalEl.querySelector('h3').textContent = t('modal.select_tracks_title');
    modalEl.querySelector('.modal-hint').textContent = t('modal.select_tracks_hint');
    modalEl.querySelector('.modal-btns').innerHTML =
        '<button class="primary" onclick="confirmBatchTracks()" data-i18n="modal.run_button">Run</button>' +
        '<button onclick="cancelBatchTracks()" data-i18n="modal.cancel_button">Cancel</button>';
    modalEl.style.display = 'flex';
}

async function confirmBatchTracks() {
    batchTrackAudioIdx = parseInt(document.querySelector('input[name="m-audio"]:checked')?.value || '0');
    batchTrackSubIdx = parseInt(document.querySelector('input[name="m-sub"]:checked')?.value || '0');
    document.getElementById('track-modal').style.display = 'none';
    if (pendingBatchType === 'translate') await runBatch('translate');
    else await runBatch('clone');
}

function cancelBatchTracks() {
    document.getElementById('track-modal').style.display = 'none';
    pendingBatchType = null;
}

// ── Per-episode track picker ──

function showEpisodeTrackPicker(data) {
    const selAudio = data.selected_audio_idx;
    const selSub = data.selected_sub_idx;
    document.getElementById('modal-audio-tracks').innerHTML = '<h4>' + escHtml(t('modal.audio_label')) + '</h4>' +
        data.audio.map((t, i) => {
            const checked = (selAudio >= 0 && i === selAudio) || (selAudio < 0 && i === 0);
            return `<label><input type="radio" name="m-audio" value="${i}" ${checked ? 'checked' : ''}> ${t.language||'?'} (${t.codec}, ${t.channels}ch)</label>`;
        }).join('');
    document.getElementById('modal-sub-tracks').innerHTML = '<h4>' + escHtml(t('modal.subtitles_label')) + '</h4>' +
        data.subtitle.map((t, i) => {
            const checked = (selSub >= 0 && i === selSub) || (selSub < 0 && i === 0);
            return `<label><input type="radio" name="m-sub" value="${i}" ${checked ? 'checked' : ''}> ${t.language||'?'} (${t.codec})</label>`;
        }).join('');
    const modalEl = document.getElementById('track-modal');
    modalEl.querySelector('h3').textContent = t('modal.select_tracks_episode_title');
    modalEl.querySelector('.modal-hint').textContent = t('modal.select_tracks_episode_hint');
    modalEl.querySelector('.modal-btns').innerHTML =
        '<button class="primary" onclick="confirmEpisodeTracks()" data-i18n="modal.confirm_button">Confirm</button>' +
        '<button onclick="cancelEpisodeTracks()" data-i18n="modal.cancel_button">Cancel</button>';
    modalEl.style.display = 'flex';
}

async function confirmEpisodeTracks() {
    const audioIdx = parseInt(document.querySelector('input[name="m-audio"]:checked')?.value || '0');
    const subIdx = parseInt(document.querySelector('input[name="m-sub"]:checked')?.value || '0');
    document.getElementById('track-modal').style.display = 'none';
    try {
        await api('/api/tracks/confirm', { method: 'POST', body: { audio_idx: audioIdx, sub_idx: subIdx } });
    } catch (e) {
        alert(t('alert.track_confirm_failed') + e.message);
        return;
    }
    await setupEditorContinue();
}

function cancelEpisodeTracks() {
    document.getElementById('track-modal').style.display = 'none';
    goHome();
}

// ── No-subtitle transcription modal ──

const TRANSCRIBE_MODELS = [
    { value: 'openai/whisper-large-v3-turbo', label: 'Large v3 Turbo (most accurate)' },
    { value: 'openai/whisper-medium',           label: 'Medium (accurate)' },
    { value: 'openai/whisper-small',            label: 'Small (balanced)' },
    { value: 'openai/whisper-base',             label: 'Base (fast)' },
    { value: 'openai/whisper-tiny',             label: 'Tiny (fastest)' },
];

const TRANSCRIBE_LANGS = [
    { value: '',        label: 'Auto-detect' },
    { value: 'english',  label: 'English' },
    { value: 'japanese', label: 'Japanese' },
    { value: 'korean',   label: 'Korean' },
    { value: 'chinese',  label: 'Chinese' },
    { value: 'french',   label: 'French' },
    { value: 'german',   label: 'German' },
    { value: 'spanish',  label: 'Spanish' },
    { value: 'portuguese', label: 'Portuguese' },
    { value: 'russian',  label: 'Russian' },
];

function showNoSubsModal(data) {
    const selAudio = data.selected_audio_idx;
    const langOpts = TRANSCRIBE_LANGS.map(l =>
        `<option value="${l.value}"${l.value === '' ? ' selected' : ''}>${l.label}</option>`
    ).join('');
    const modelOpts = TRANSCRIBE_MODELS.map((m, i) =>
        `<option value="${m.value}"${i === 0 ? ' selected' : ''}>${m.label}</option>`
    ).join('');

    document.getElementById('modal-audio-tracks').innerHTML = '<h4>' + escHtml(t('modal.audio_label')) + '</h4>' +
        data.audio.map((t, i) => {
            const checked = (selAudio >= 0 && i === selAudio) || (selAudio < 0 && i === 0);
            return `<label><input type="radio" name="m-audio" value="${i}" ${checked ? 'checked' : ''}> ${t.language||'?'} (${t.codec}, ${t.channels}ch)</label>`;
        }).join('');

    document.getElementById('modal-sub-tracks').innerHTML =
        '<h4>' + escHtml(t('modal.language_label')) + '</h4>' +
        `<select id="transcribe-lang">${langOpts}</select>` +
        '<h4>' + escHtml(t('modal.model_label')) + '</h4>' +
        `<select id="transcribe-model">${modelOpts}</select>`;

    const modalEl = document.getElementById('track-modal');
    modalEl.querySelector('h3').textContent = t('modal.no_subs_title');
    modalEl.querySelector('.modal-hint').textContent = t('modal.no_subs_hint');
    modalEl.querySelector('.modal-btns').innerHTML =
        '<button class="primary" onclick="confirmTranscribe()" data-i18n="modal.generate_button">Generate</button>' +
        '<button onclick="cancelTranscribe()" data-i18n="modal.cancel_button">Cancel</button>';
    modalEl.style.display = 'flex';
}

async function confirmTranscribe() {
    const audioIdx = parseInt(document.querySelector('input[name="m-audio"]:checked')?.value || '0');
    const model = document.getElementById('transcribe-model').value;
    const language = document.getElementById('transcribe-lang').value || null;
    document.getElementById('track-modal').style.display = 'none';

    showOverlay(t('overlay.transcribing'));
    try {
        await api('/api/transcribe', { method: 'POST', body: { audio_idx: audioIdx, model: model, language: language } });
    } catch (e) {
        hideOverlay();
        alert(t('alert.transcribe_failed') + e.message);
        return;
    }
    await pollTranscribe();
}

async function pollTranscribe() {
    for (let i = 0; i < 600; i++) {
        await sleep(2000);
        try {
            const p = await api('/api/transcribe/progress');
            if (p.done) {
                hideOverlay();
                if (p.message && p.message.startsWith('Failed')) {
                    alert(p.message);
                    goHome();
                    return;
                }
                await setupEditorContinue();
                return;
            }
        } catch (e) { /* retry */ }
    }
    hideOverlay();
    alert('Transcription timed out');
    goHome();
}

function cancelTranscribe() {
    document.getElementById('track-modal').style.display = 'none';
    goHome();
}

async function setupEditorContinue() {
    const tracks = await api('/api/tracks');
    if (!tracks.demucs_done) {
        await runDemucsFlow();
    }
    initTimelineCanvas();
    await loadCharacters();
    await loadTimeline();
    document.getElementById('video-player').addEventListener('timeupdate', onVideoTimeUpdate);
    populateEpisodeDropdown();
    if (playback.mode === 'on') {
        await refreshPlaybackPlan();
    }
    const first = await getFirstUnaccepted();
    await loadClip(first);
}

async function runBatch(type) {
    const stems = [...selectedEpisodes];
    if (!stems.length) return;
    const key = type === 'translate' ? 'batch-translate' : 'batch-clone';
    const endpoint = type === 'translate' ? '/api/episodes/batch-translate' : '/api/episodes/batch-clone';

    try {
        await api(endpoint, {
            method: 'POST',
            body: { stems, audio_idx: batchTrackAudioIdx, sub_idx: batchTrackSubIdx },
        });
        startJobPolling(key);
        // Open batch panel to show live progress.
        const panel = document.getElementById('batch-panel');
        if (panel.style.display === 'none' || !panel.style.display) {
            panel.style.display = 'flex';
            if (batchTimer) clearInterval(batchTimer);
            batchTimer = setInterval(refreshBatchJobs, 500);
        }
        // Watch for completion to refresh episode cards.
        watchBatchCompletion(key);
    } catch (e) { alert(t('alert.batch_failed_template').replace('{type}', type) + e.message); }
}

let _batchCompletionWatchers = {};
function watchBatchCompletion(key) {
    if (_batchCompletionWatchers[key]) clearInterval(_batchCompletionWatchers[key]);
    _batchCompletionWatchers[key] = setInterval(async () => {
        try {
            const jobs = await api('/api/jobs');
            const job = jobs[key];
            if (!job || !job.running) {
                clearInterval(_batchCompletionWatchers[key]);
                delete _batchCompletionWatchers[key];
                selectedEpisodes.clear();
                updateSelectionUI();
                await loadEpisodes();
                await refreshEpisodeBars();
                document.getElementById('bulk-status').textContent = t('bulk.status_complete');
            }
        } catch (e) {}
    }, 2000);
}

async function pollBatchProgress(key) {
    let done = false;
    let lastRefresh = 0;
    while (!done) {
        await sleep(300);
        try {
            const p = await api(`/api/episodes/${key}/progress`);
            document.getElementById('overlay-msg').textContent = p.message || `${key}...`;
            document.getElementById('bulk-status').textContent = p.message || '';
            if (p.done) done = true;
            // Refresh episode cards every 5 s so Tr / Cl bars fill during batch.
            const now = Date.now();
            if (now - lastRefresh >= 5000) {
                await refreshEpisodeBars();
                lastRefresh = now;
            }
        } catch (e) { done = true; }
    }
    // Show any failed episodes on the bulk bar so the user sees errors.
    try {
        const p = await api(`/api/episodes/${key}/progress`);
        const failed = p.failed || [];
        if (failed.length) {
            const stems = failed.map(f => f.stem).slice(0, 5).join(', ');
            const more = failed.length > 5 ? ` +${failed.length - 5} more` : '';
            document.getElementById('bulk-status').textContent =
                t('bulk.done_failed_template')
                    .replace('{count}', failed.length)
                    .replace('{stems}', stems)
                    .replace('{more}', more);
        } else {
            document.getElementById('bulk-status').textContent = t('bulk.status_complete');
        }
    } catch (e) {
        document.getElementById('bulk-status').textContent = t('bulk.status_complete');
    }
}

async function startJobPolling(key) {
    currentJobKey = key;
    document.getElementById('job-bar').style.display = 'flex';
    pollJobStatus();
}

async function pollJobStatus() {
    if (!currentJobKey) return;
    try {
        const jobs = await api('/api/jobs');
        if (!jobs[currentJobKey] || !jobs[currentJobKey].running) {
            document.getElementById('job-bar').style.display = 'none';
            currentJobKey = null;
            return;
        }
        document.getElementById('job-msg').textContent = jobs[currentJobKey].message || t('job.running');
    } catch (e) {}
    setTimeout(pollJobStatus, 2000);
}

async function cancelJob() {
    if (!currentJobKey) return;
    await api(`/api/jobs/${currentJobKey}/cancel`, { method: 'POST' });
    document.getElementById('job-msg').textContent = t('job.cancelling');
}

// ── GPU Panel ─────────────────────────────────

let gpuTimer = null;

async function toggleGpuPanel() {
    const panel = document.getElementById('gpu-panel');
    if (panel.style.display === 'none' || !panel.style.display) {
        panel.style.display = 'flex';
        await refreshGpuStats();
        if (gpuTimer) clearInterval(gpuTimer);
        gpuTimer = setInterval(refreshGpuStats, 3000);
    } else {
        panel.style.display = 'none';
        if (gpuTimer) { clearInterval(gpuTimer); gpuTimer = null; }
    }
}

async function refreshGpuStats() {
    try {
        const gpu = await api('/api/gpu');
        const btn = document.getElementById('gpu-btn');
        if (!gpu.available) {
            btn.textContent = t('gpu.na');
            return;
        }
        btn.textContent = `GPU ${gpu.pct_used}%`;
        btn.style.color = gpu.pct_used > 75 ? '#e94560' : '#ccc';
        document.getElementById('gpu-device').textContent = gpu.device;
        document.getElementById('gpu-alloc').textContent = gpu.allocated_mb;
        document.getElementById('gpu-total').textContent = gpu.total_mb;
        document.getElementById('gpu-reserved').textContent = gpu.reserved_mb;
        document.getElementById('gpu-pct').textContent = gpu.pct_used;
        document.getElementById('gpu-bar-fill').style.width = gpu.pct_used + '%';
        const backends = document.getElementById('gpu-backends');
        if (backends) {
            const names = (gpu.live_backends || []).map(
                n => n === '__shared__' ? t('gpu.shared_manual_clones') : n
            );
            backends.textContent = names.length
                ? t('gpu.live_models') + names.join(', ')
                : t('gpu.no_live_models');
        }
    } catch (e) {}
}

async function clearGpuMemory(force) {
    const url = force ? '/api/gpu/clear?force=1' : '/api/gpu/clear';
    await api(url, { method: 'POST' });
    await refreshGpuStats();
}

// ── Batch Jobs Panel ──────────────────────────

let batchTimer = null;

async function toggleBatchPanel() {
    const panel = document.getElementById('batch-panel');
    if (panel.style.display === 'none' || !panel.style.display) {
        panel.style.display = 'flex';
        await refreshBatchJobs();
        if (batchTimer) clearInterval(batchTimer);
        batchTimer = setInterval(refreshBatchJobs, 500);
    } else {
        panel.style.display = 'none';
        if (batchTimer) { clearInterval(batchTimer); batchTimer = null; }
    }
}

async function refreshBatchJobs() {
    try {
        const jobs = await api('/api/batch-jobs');
        const list = document.getElementById('batch-jobs-list');
        const btn = document.getElementById('batch-btn');
        const keys = Object.keys(jobs);
        if (!keys.length) {
            list.innerHTML = '<span style="color:#666">' + escHtml(t('batch.no_jobs')) + '</span>';
            btn.style.color = '#ccc';
            return;
        }
        btn.style.color = '#e94560';
        btn.textContent = t('batch.btn_label') + ' (' + keys.length + ')';
        let anyDone = false;
        list.innerHTML = keys.map(key => {
            const j = jobs[key];
            const p = j.progress || {};
            const isDone = p.done;
            const isFailed = (p.failed || []).length > 0 && isDone;
            const phase = p.phase || '';
            const totalTranslated = p.total_translated || 0;
            const epCurrent = p.episode_current || 0;
            const epTotal = p.episode_total || 0;
            const clipCurrent = p.clip_current || 0;
            const clipTotal = p.clip_total || 0;
            const clipText = p.clip_text || '';
            const episodeName = p.episode_name || '';
            const message = p.message || '';
            const skipped = p.skipped || [];
            const failed = p.failed || [];
            let pct = 0;
            if (epTotal > 0) {
                const epFrac = epCurrent / epTotal;
                const clipFrac = clipTotal > 0 ? clipCurrent / clipTotal : 1;
                pct = Math.round(((epCurrent - 1 + clipFrac) / epTotal) * 100);
            }
            if (isDone) {
                pct = failed.length ? 100 : 100;
                anyDone = true;
            }
            const typeLabel = j.type === 'batch-clone' ? t('batch.label_clone') : t('batch.label_translate');
            let phaseLabel = '';
            if (isDone) {
                if (failed.length) phaseLabel = t('batch.status_failed').replace('{count}', failed.length);
                else phaseLabel = t('batch.status_done').replace('{count}', epTotal);
            } else if (phase === 'demucs') phaseLabel = t('batch.phase_demucs');
            else if (phase === 'translating' || phase === 'cloning') phaseLabel = t('batch.phase_translating');
            else phaseLabel = message;
            const cls = isDone ? (failed.length ? 'batch-job-failed' : 'batch-job-done') : '';
            const spinnerHtml = isDone ? '' : '<span class="spinner"></span> ';
            let lines = '';
            if (!isDone) {
                lines += '<div class="batch-job-episode">' + escHtml(t('batch.status_episode').replace('{current}', epCurrent).replace('{total}', epTotal));
                if (episodeName) lines += ' &mdash; ' + escHtml(episodeName);
                lines += '</div>';
                if (phase === 'translating' && clipTotal > 0) {
                    lines += '<div class="batch-job-clip">' + spinnerHtml + escHtml(t('batch.status_clip').replace('{current}', clipCurrent).replace('{total}', clipTotal));
                    if (clipText) lines += ' &mdash; "' + escHtml(clipText) + '"';
                    lines += '</div>';
                }
            } else {
                if (totalTranslated > 0) lines += '<div class="batch-job-episode">' + totalTranslated + ' lines translated</div>';
                if (failed.length) lines += '<div class="batch-job-episode" style="color:#e30">' + escHtml(t('batch.status_failed').replace('{count}', failed.length)) + '</div>';
                if (skipped.length) lines += '<div class="batch-job-episode" style="color:#888">' + escHtml(t('batch.status_skipped').replace('{count}', skipped.length)) + '</div>';
            }
            return `<div class="batch-job ${cls}">
                <div class="batch-job-header">
                    <span class="batch-job-type">${escHtml(typeLabel)}</span>
                    <span class="batch-job-phase">${escHtml(phaseLabel)}</span>
                </div>
                ${lines}
                <div class="batch-bar"><div class="batch-bar-fill" style="width:${pct}%"></div></div>
                <div class="batch-job-footer">
                    <span>${pct}%</span>
                    ${isDone ? '' : `<button onclick="cancelBatchJob('${key}')">` + escHtml(t('batch.cancel')) + '</button>'}
                </div>
            </div>`;
        }).join('');
        if (anyDone) {
            await refreshEpisodeBars();
        }
    } catch (e) { /* silently ignore */ }
}

async function cancelBatchJob(key) {
    await api(`/api/jobs/${key}/cancel`, { method: 'POST' });
}

async function checkRunningJobs() {
    try {
        const jobs = await api('/api/jobs');
        let hasBatchJob = false;
        for (const [key, job] of Object.entries(jobs)) {
            if (job.running) {
                startJobPolling(key);
                if (key.startsWith('batch-')) hasBatchJob = true;
                break;
            }
        }
        if (hasBatchJob) {
            const panel = document.getElementById('batch-panel');
            panel.style.display = 'flex';
            if (batchTimer) clearInterval(batchTimer);
            batchTimer = setInterval(refreshBatchJobs, 500);
        }
    } catch (e) {}
}

// ── Timeline Canvas ───────────────────────────

function initTimelineCanvas() {
    const canvas = document.getElementById('timeline-canvas');
    timelineCtx = canvas.getContext('2d');
    const resizeCanvas = () => {
        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width * (window.devicePixelRatio || 1);
        canvas.height = rect.height * (window.devicePixelRatio || 1);
        timelineCtx.setTransform(window.devicePixelRatio || 1, 0, 0, window.devicePixelRatio || 1, 0, 0);
        canvas.style.width = rect.width + 'px';
        canvas.style.height = rect.height + 'px';
        drawTimeline();
    };
    resizeCanvas();
    new ResizeObserver(resizeCanvas).observe(canvas.parentElement);
    canvas.addEventListener('mousedown', onTimelineMouseDown);
    canvas.addEventListener('mousemove', onTimelineMouseMove);
    canvas.addEventListener('mouseup', onTimelineMouseUp);
    canvas.addEventListener('mouseleave', onTimelineMouseUp);
    canvas.addEventListener('wheel', onTimelineWheel, { passive: false });
    canvas.addEventListener('contextmenu', onTimelineCtxMenu);
}

function pxToSec(px) { return px / (timelineVP.pxPerSec || 1) + timelineVP.startSec; }
function secToPx(sec) { return (sec - timelineVP.startSec) * timelineVP.pxPerSec; }

function assignLanes(clips) {
    const laneEnds = []; const result = [];
    for (const c of clips) {
        let lane = 0;
        while (lane < laneEnds.length && laneEnds[lane] > c.start_sec) lane++;
        if (lane === laneEnds.length) laneEnds.push(c.end_sec);
        else laneEnds[lane] = c.end_sec;
        result.push({ ...c, lane });
    }
    return { lanes: result, laneCount: Math.max(laneEnds.length, 1) };
}

function drawTimeline() {
    const ctx = timelineCtx;
    const canvas = ctx.canvas;
    if (!canvas) return;
    const W = canvas.width / (window.devicePixelRatio || 1);
    const H = canvas.height / (window.devicePixelRatio || 1);
    ctx.clearRect(0, 0, W, H);
    if (!timelineClips.length) return;

    const totalEnd = Math.max(...timelineClips.map(c => c.end_sec), timelineClips[0].end_sec);
    timelineVP.startSec = Math.max(0, Math.min(timelineVP.startSec, totalEnd - 5));
    const pxPerSec = Math.max(timelineVP.pxPerSec, 2);
    timelineVP.pxPerSec = pxPerSec;

    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, W, RULER_H);
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1;
    const tickStep = pxPerSec > 200 ? 0.5 : pxPerSec > 100 ? 1 : pxPerSec > 50 ? 2 : pxPerSec > 20 ? 5 : pxPerSec > 10 ? 10 : 30;
    let t = Math.floor(timelineVP.startSec / tickStep) * tickStep;
    ctx.fillStyle = '#666';
    ctx.font = '10px monospace';
    while (t <= timelineVP.startSec + W / pxPerSec) {
        const x = secToPx(t);
        ctx.beginPath(); ctx.moveTo(x, RULER_H - 8); ctx.lineTo(x, RULER_H); ctx.stroke();
        ctx.fillText(fmtTs(t), x + 3, RULER_H - 3);
        t += tickStep;
    }

    const { lanes, laneCount } = assignLanes(timelineClips);
    const laneH = Math.max(CLIP_MIN_H, (H - CLIP_TOP - 4) / laneCount);

    if (currentClip) {
        const cx = secToPx(currentClip.start_sec + (currentClip.end_sec - currentClip.start_sec) / 2);
        if (cx >= -10 && cx <= W + 10) {
            ctx.fillStyle = '#fff';
            ctx.beginPath(); ctx.moveTo(cx, CLIP_TOP - 10);
            ctx.lineTo(cx - 6, CLIP_TOP - 2); ctx.lineTo(cx + 6, CLIP_TOP - 2);
            ctx.closePath(); ctx.fill();
        }
    }

    for (const c of lanes) {
        const x = secToPx(c.start_sec);
        const w = Math.max((c.end_sec - c.start_sec) * pxPerSec, 4);
        const y = CLIP_TOP + c.lane * laneH + 2;
        const h = laneH - 4;
        const isCurrent = currentClip && currentClip.clip_id === c.clip_id;

        const color = STATUS_COLORS[c.status] || '#2a2a3e';
        ctx.fillStyle = isCurrent ? brighten(color, 30) : color;
        roundRect(ctx, x, y, w, h, 4); ctx.fill();

        if (c.status === 'sign') {
            ctx.strokeStyle = 'rgba(255,255,255,0.15)'; ctx.lineWidth = 1;
            for (let sx = x + 4; sx < x + w; sx += 8) {
                ctx.beginPath(); ctx.moveTo(sx, y); ctx.lineTo(sx + 8, y + h); ctx.stroke();
            }
        }

        ctx.strokeStyle = isCurrent ? '#fff' : '#0f3460';
        ctx.lineWidth = isCurrent ? 2 : 1;
        roundRect(ctx, x, y, w, h, 4); ctx.stroke();

        ctx.fillStyle = '#ccc';
        ctx.font = `${Math.max(9, Math.min(11, h * 0.4))}px monospace`;
        ctx.textAlign = 'center';
        const label = c.clip_id;
        const textW = ctx.measureText(label).width;
        if (w > textW + 8) ctx.fillText(label, x + w / 2, y + h / 2 + 4);

        const offsetPx = c.audio_offset_ms / 1000 * pxPerSec;
        const audioDur = (c.end_sec - c.start_sec) * pxPerSec;
        const ax = x + offsetPx, aw = Math.max(audioDur, 6);
        ctx.fillStyle = 'rgba(233,69,96,0.25)';
        roundRect(ctx, ax, y + h - 6, aw, 6, 2); ctx.fill();

        if (isCurrent) {
            ctx.fillStyle = '#fff';
            ctx.fillRect(x - 3, y, 6, h);
            ctx.fillRect(x + w - 3, y, 6, h);
            ctx.fillRect(ax + aw - 3, y + h - 6, 6, 6);
        }
    }

    if (playback.mode === 'on') {
        const x = secToPx(playback.currentTime);
        if (x >= 0 && x <= W) {
            ctx.strokeStyle = '#e94560';
            ctx.lineWidth = 2;
            ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
            ctx.fillStyle = '#e94560';
            ctx.beginPath(); ctx.arc(x, RULER_H, 4, 0, Math.PI * 2); ctx.fill();
        }
    }
}

function brighten(hex, amount) {
    const r = Math.min(255, parseInt(hex.slice(1, 3), 16) + amount);
    const g = Math.min(255, parseInt(hex.slice(3, 5), 16) + amount);
    const b = Math.min(255, parseInt(hex.slice(5, 7), 16) + amount);
    return `rgb(${r},${g},${b})`;
}

function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
}

function hitTestTimeline(mx, my) {
    const { lanes, laneCount } = assignLanes(timelineClips);
    const laneH = Math.max(CLIP_MIN_H, (timelineCtx.canvas.height / (window.devicePixelRatio || 1) - CLIP_TOP - 4) / Math.max(laneCount, 1));
    for (const c of lanes) {
        const x = secToPx(c.start_sec);
        const w = Math.max((c.end_sec - c.start_sec) * timelineVP.pxPerSec, 4);
        const y = CLIP_TOP + c.lane * laneH + 2;
        const h = laneH - 4;
        const offsetPx = c.audio_offset_ms / 1000 * timelineVP.pxPerSec;
        const ax = x + offsetPx;
        const aw = Math.max((c.end_sec - c.start_sec) * timelineVP.pxPerSec, 6);

        if (currentClip && currentClip.clip_id === c.clip_id && mx >= x + w - 5 && mx <= x + w + 5 && my >= y && my <= y + h)
            return { type: 'handle-end', clipId: c.clip_id };
        if (currentClip && currentClip.clip_id === c.clip_id && mx >= x - 5 && mx <= x + 5 && my >= y && my <= y + h)
            return { type: 'handle-start', clipId: c.clip_id };
        if (currentClip && currentClip.clip_id === c.clip_id && mx >= ax && mx <= ax + aw && my >= y + h - 8 && my <= y + h)
            return { type: 'audio-handle', clipId: c.clip_id };
        if (mx >= x && mx <= x + w && my >= y && my <= y + h)
            return { type: 'clip', clipId: c.clip_id };
    }
    return { type: 'empty', clipId: null };
}

function onTimelineMouseDown(e) {
    const rect = e.target.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    if (playback.mode === 'on' && my < CLIP_TOP - 2) {
        timelineDrag = { type: 'seek', startX: mx };
        seekPlaybackTo(pxToSec(mx));
        e.target.style.cursor = 'pointer';
        return;
    }
    const hit = hitTestTimeline(mx, my);
    timelineDrag = { type: hit.type, clipId: hit.clipId, startX: mx, startSec: timelineVP.startSec,
                     origStart: null, origEnd: null, origOffset: null };
    if (hit.type === 'handle-start' || hit.type === 'handle-end' || hit.type === 'audio-handle') {
        const c = timelineClips.find(cl => cl.clip_id === hit.clipId);
        if (c) { timelineDrag.origStart = c.start_sec; timelineDrag.origEnd = c.end_sec; timelineDrag.origOffset = c.audio_offset_ms; }
    } else if (hit.type === 'empty') {
        timelineDrag.type = 'pan'; e.target.style.cursor = 'grabbing';
    }
}

function onTimelineMouseMove(e) {
    const rect = e.target.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    if (!timelineDrag) {
        const hit = hitTestTimeline(mx, my);
        if (playback.mode === 'on' && my < CLIP_TOP - 2) e.target.style.cursor = 'pointer';
        else if (hit.type === 'handle-start' || hit.type === 'handle-end' || hit.type === 'audio-handle') e.target.style.cursor = 'ew-resize';
        else if (hit.type === 'clip') e.target.style.cursor = 'pointer';
        else e.target.style.cursor = 'grab';
        return;
    }
    if (timelineDrag.type === 'seek') {
        seekPlaybackTo(pxToSec(mx));
        return;
    }
    if (timelineDrag.type === 'pan') {
        const dx = (mx - timelineDrag.startX) / timelineVP.pxPerSec;
        timelineVP.startSec = Math.max(0, timelineDrag.startSec - dx);
        drawTimeline(); return;
    }
    if (!timelineDrag.clipId) return;
    const clip = timelineClips.find(c => c.clip_id === timelineDrag.clipId);
    if (!clip) return;
    const dt = (mx - timelineDrag.startX) / timelineVP.pxPerSec;
    if (timelineDrag.type === 'handle-start') {
        clip.start_sec = Math.max(0, timelineDrag.origStart + dt);
        if (clip.start_sec >= clip.end_sec - 0.1) clip.start_sec = clip.end_sec - 0.1;
    } else if (timelineDrag.type === 'handle-end') {
        clip.end_sec = Math.max(clip.start_sec + 0.1, timelineDrag.origEnd + dt);
    } else if (timelineDrag.type === 'audio-handle') {
        clip.audio_offset_ms = Math.max(-clip.start_sec * 1000, timelineDrag.origOffset + dt * 1000);
    }
    drawTimeline();
}

async function onTimelineMouseUp(e) {
    if (timelineDrag && timelineDrag.type !== 'pan') e.target.style.cursor = 'grab';
    if (!timelineDrag) return;
    const drag = timelineDrag; timelineDrag = null;
    if (drag.type === 'pan' || drag.type === 'seek') return;
    if (!drag.clipId) return;
    const clip = timelineClips.find(c => c.clip_id === drag.clipId);
    if (!clip) return;
    if (drag.type === 'handle-start' || drag.type === 'handle-end') {
        await api(`/api/clips/${drag.clipId}/resize`, { method: 'POST', body: { start_sec: clip.start_sec, end_sec: clip.end_sec } });
    } else if (drag.type === 'audio-handle') {
        await api(`/api/clips/${drag.clipId}/audio-offset`, { method: 'POST', body: { offset_ms: clip.audio_offset_ms } });
    } else if (drag.type === 'clip') {
        loadClip(drag.clipId);
    }
}

function onTimelineWheel(e) {
    e.preventDefault();
    const rect = e.target.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const secAtCursor = pxToSec(mx);
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    timelineVP.pxPerSec = Math.max(2, Math.min(2000, timelineVP.pxPerSec * factor));
    timelineVP.startSec = Math.max(0, secAtCursor - mx / timelineVP.pxPerSec);
    drawTimeline();
}

function onTimelineCtxMenu(e) {
    e.preventDefault();
    const rect = e.target.getBoundingClientRect();
    const hit = hitTestTimeline(e.clientX - rect.left, e.clientY - rect.top);
    if (!hit.clipId) return;
    timelineCtxMenuClipId = hit.clipId;
    const menu = document.getElementById('timeline-menu');
    menu.style.display = 'block'; menu.style.left = e.clientX + 'px'; menu.style.top = e.clientY + 'px';
    setTimeout(() => document.addEventListener('click', hideCtxMenu, { once: true }), 0);
}
function hideCtxMenu() { document.getElementById('timeline-menu').style.display = 'none'; }

async function ctxDelete() {
    if (!timelineCtxMenuClipId) return;
    await api(`/api/clips/${timelineCtxMenuClipId}/delete`, { method: 'POST' });
    hideCtxMenu(); await loadTimeline();
    if (currentClip && currentClip.clip_id === timelineCtxMenuClipId) nextClip();
}
async function ctxToggleSign() {
    if (!timelineCtxMenuClipId) return;
    const clip = timelineClips.find(c => c.clip_id === timelineCtxMenuClipId);
    const newStatus = (clip && clip.status === 'sign') ? 'pending' : 'sign';
    await api(`/api/clips/${timelineCtxMenuClipId}/status`, { method: 'POST', body: { status: newStatus } });
    hideCtxMenu(); await loadTimeline();
    if (currentClip && currentClip.clip_id === timelineCtxMenuClipId) { currentClip.status = newStatus; renderClip(); }
}

// ── Timeline data ─────────────────────────────

async function loadTimeline() {
    try {
        timelineClips = await api('/api/timeline');
        drawTimeline();
    } catch (e) { console.error(e); }
}
function onVideoTimeUpdate() {}

// ── Characters ───────────────────────────────

async function loadCharacters() {
    try {
        characters = await api('/api/characters');
        const sel = document.getElementById('char-select');
        sel.innerHTML = '<option value="">' + escHtml(t('editor.character_none_option')) + '</option>' +
            Object.keys(characters).map(name => `<option value="${name}">${name}</option>`).join('');
    } catch (e) { characters = {}; }
}
function toggleCharPanel() {
    const panel = document.getElementById('char-panel');
    panel.style.display = panel.style.display === 'none' ? 'flex' : 'none';
    renderCharPanel();
}
function renderCharPanel() {
    const div = document.getElementById('char-list');
    div.innerHTML = Object.entries(characters).map(([name, moods]) =>
        `<strong>${name}</strong><br>` + Object.entries(moods).map(([mood]) =>
            `<div>${mood} <button onclick="deleteCharacter('${name}','${mood}')">${escHtml(t('chars.del'))}</button></div>`
        ).join('')
    ).join('<hr>');
}
async function saveCharacter() {
    const c = currentClip;
    if (!c) return;
    const name = document.getElementById('char-select').value || prompt(t('prompt.character_name'));
    if (!name) return;
    const mood = document.getElementById('mood-select').value || 'normal';
    showOverlay(t('overlay.saving'));
    try {
        await api('/api/characters', { method: 'POST', body: { name, mood, clip_id: c.clip_id } });
        await loadCharacters(); renderClip();
    } catch (e) { alert(t('alert.save_char_failed') + e.message); }
    hideOverlay();
}
async function deleteCharacter(name, mood) {
    await api(`/api/characters/${name}/${mood}`, { method: 'DELETE' });
    await loadCharacters(); renderCharPanel(); renderClip();
}
async function addCharacter() {
    const name = document.getElementById('new-char-name').value.trim();
    const mood = document.getElementById('new-char-mood').value.trim() || 'normal';
    if (!name) return alert(t('alert.enter_name'));
    const c = currentClip; if (!c) return;
    try {
        await api('/api/characters', { method: 'POST', body: { name, mood, clip_id: c.clip_id } });
        await loadCharacters(); renderCharPanel(); renderClip();
        document.getElementById('new-char-name').value = '';
        document.getElementById('new-char-mood').value = '';
    } catch (e) { alert(t('alert.add_char_failed') + e.message); }
}

// ── Bulk ─────────────────────────────────────

async function translateAll() {
    if (!confirm(t('confirm.translate_all'))) return;
    showOverlay(t('overlay.translating_all'));
    try {
        await api('/api/translate-all', { method: 'POST' });
        startJobPolling('translate-all');
        await pollProgress('translate-all', 'overlay.translating', 'bulk.translate_complete');
        loadTimeline();
        if (currentClip) loadClip(currentClip.clip_id);
    } catch (e) { alert(t('alert.translate_all_failed') + e.message); }
    hideOverlay();
}
async function cloneAll() {
    if (!confirm(t('confirm.clone_all'))) return;
    showOverlay(t('overlay.cloning_all'));
    try {
        await api('/api/clone-all', { method: 'POST' });
        startJobPolling('clone-all');
        await pollProgress('clone-all', 'overlay.cloning', 'bulk.clone_complete');
        loadTimeline();
        if (currentClip) loadClip(currentClip.clip_id);
    } catch (e) { alert(t('alert.clone_all_failed') + e.message); }
    hideOverlay();
}
async function pollProgress(key, overlayKey, completeKey) {
    let done = false;
    while (!done) {
        await sleep(200);
        try {
            const p = await api(`/api/${key}/progress`);
            document.getElementById('overlay-msg').textContent = t(overlayKey) + ' ' + p.current + '/' + p.total;
            document.getElementById('bulk-status').textContent = p.message;
            if (p.done) done = true;
        } catch (e) { done = true; }
    }
    document.getElementById('bulk-status').textContent = t(completeKey);
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function saveProject() {
    await api('/api/save', { method: 'POST' });
    document.getElementById('bulk-status').textContent = t('bulk.status_saved');
}
async function assembleFull() {
    if (!confirm(t('confirm.assemble_full'))) return;
    showOverlay(t('overlay.assembling'));
    try {
        const resp = await api('/api/assemble', { method: 'POST' });
        alert(t('alert.assemble_done_prefix') + resp.final_path);
    } catch (e) { alert(t('alert.assemble_failed') + e.message); }
    hideOverlay();
}
async function generatePlaybackPreviews() {
    showOverlay('Generating previews...');
    try {
        const info = await api('/api/playback/generate-previews', { method: 'POST' });
        startJobPolling('playback-previews');
        await pollProgress('playback-previews', '', 'Generated ' + info.total + ' previews');
        document.getElementById('bulk-status').textContent = 'Previews complete.';
        if (playback.mode === 'on') await refreshPlaybackPlan();
    } catch (e) {
        alert('Generate previews failed: ' + e.message);
        document.getElementById('bulk-status').textContent = '';
    }
    hideOverlay();
}
async function deletePlaybackPreviews() {
    if (!confirm('Delete all playback previews? You can regenerate with "Generate Previews".')) return;
    try {
        const resp = await api('/api/playback/delete-previews', { method: 'POST' });
        alert('Deleted ' + resp.deleted + ' previews.');
        if (playback.mode === 'on') await refreshPlaybackPlan();
    } catch (e) { alert('Delete failed: ' + e.message); }
}
function fmtTs(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = (sec % 60).toFixed(1);
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(4, '0')}`;
}

// ── Auto-Play Mode ─────────────────────────────

let playback = {
    mode: 'off',
    running: false,
    plan: [],
    segIndex: -1,
    currentTime: 0,
    v1: null, v2: null,
    activeEl: null,
    standbyIndex: -1,
    totalStart: 0, totalEnd: 0,
    advancing: false,
    raf: null,
    overlaySyncAt: 0,
};

function setPlayBtn(playing) {
    const btn = document.getElementById('play-btn');
    btn.innerHTML = playing ? '\u23F8' : '\u25B6';
    btn.classList.toggle('playing', playing);
}

function pbFmt(sec) {
    sec = Math.max(0, sec || 0);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = (sec % 60).toFixed(1).padStart(4, '0');
    return `${h}:${String(m).padStart(2,'0')}:${s}`;
}

function pbSegURL(seg) {
    if (seg.kind === 'clip') {
        return `/api/playback/segment?clip_id=${encodeURIComponent(seg.clip_id)}&v=${Date.now()}`;
    }
    return `/api/playback/segment?start=${seg.start}&end=${seg.end}`;
}

function showPlaybackError(msg) {
    const el = document.getElementById('bulk-status');
    if (el) el.textContent = msg;
    console.warn('[playback]', msg);
}

function attachVideoErrorListeners() {
    const v1 = playback.v1 || document.getElementById('video-player');
    const v2 = playback.v2 || document.getElementById('video-standby');
    for (const el of [v1, v2]) {
        if (!el || el._pbErrBound) continue;
        el._pbErrBound = true;
        el.addEventListener('error', () => {
            const err = el.error;
            const code = err ? err.code : '?';
            const src = (el.src || '').split('?')[0];
            console.error('[playback] video error code=' + code + ' src=' + src);
            showPlaybackError('Segment failed: ' + src);
        });
        el.addEventListener('stalled', () => {
            console.warn('[playback] stalled:', (el.src || '').split('?')[0]);
        });
    }
}

function playElement(el) {
    el.play().then(() => {
        playback.running = true;
        setPlayBtn(true);
        if (playback.raf) cancelAnimationFrame(playback.raf);
        playback.raf = requestAnimationFrame(playbackTick);
    }).catch((e) => {
        console.warn('[playback] play() rejected:', e && e.name, e && e.message);
        playback.running = false;
        setPlayBtn(false);
        if (playback.raf) cancelAnimationFrame(playback.raf);
        showPlaybackError('Playback blocked by browser — click Play again.');
    });
}

function otherSlot(el) { return el === playback.v1 ? playback.v2 : playback.v1; }

function segIndexForTime(time) {
    for (let i = 0; i < playback.plan.length; i++) {
        const s = playback.plan[i];
        if (s.start <= time && time < s.end) return i;
    }
    if (playback.plan.length && time >= playback.plan[playback.plan.length - 1].end)
        return playback.plan.length - 1;
    return -1;
}

async function refreshPlaybackPlan() {
    const plan = await api('/api/playback/plan');
    playback.plan = plan.segments || [];
    playback.totalStart = plan.total_start || 0;
    playback.totalEnd = plan.total_end || 0;
    // Refresh any currentClip <-> plan link
    if (currentClip) {
        const idx = playback.plan.findIndex(s => s.clip_id === currentClip.clip_id);
        if (idx >= 0 && (playback.segIndex < 0 || playback.segIndex >= playback.plan.length))
            playback.segIndex = idx;
    }
}

function showActiveVideo() {
    if (playback.v1) playback.v1.style.display = (playback.activeEl === playback.v1) ? 'block' : 'none';
    if (playback.v2) playback.v2.style.display = (playback.activeEl === playback.v2) ? 'block' : 'none';
}

function loadSegmentInto(seg, el) {
    el.src = pbSegURL(seg);
    el.load();
    playback.standbyIndex = -1;
}

function prefetchNext() {
    const nextIdx = playback.segIndex + 1;
    if (nextIdx >= playback.plan.length) { playback.standbyIndex = -1; return; }
    if (playback.standbyIndex === nextIdx) return;
    const stand = otherSlot(playback.activeEl);
    const seg = playback.plan[nextIdx];
    stand.src = pbSegURL(seg);
    stand.load();
    stand.style.display = 'none';
    stand.pause();
    playback.standbyIndex = nextIdx;
}

function startSegment(idx, localTime) {
    playback.segIndex = idx;
    const seg = playback.plan[idx];
    if (!seg) return;
    const el = playback.activeEl;
    showActiveVideo();
    loadSegmentInto(seg, el);
    let started = false;
    const onMeta = () => {
        if (started) return; started = true;
        if (localTime > 0 && el.duration > localTime) {
            try { el.currentTime = localTime; } catch (e) {}
        }
        if (playback.running) playElement(el);
    };
    el.addEventListener('loadedmetadata', onMeta, { once: true });
    prefetchNext();
}

function swapToStandby() {
    const stand = otherSlot(playback.activeEl);
    const nextIdx = playback.segIndex + 1;
    const seg = playback.plan[nextIdx];
    if (!seg) {
        playback.advancing = false;
        return;
    }
    if (!stand.duration || stand.duration <= 0) {
        const onCanPlay = () => {
            stand.removeEventListener('canplay', onCanPlay);
            if (stand.duration <= 0) { playback.advancing = false; return; }
            playback.activeEl.pause();
            playback.activeEl.removeAttribute('src');
            playback.activeEl.load();
            playback.activeEl.style.display = 'none';
            playback.activeEl = stand;
            playback.segIndex = nextIdx;
            showActiveVideo();
            if (playback.running) playElement(stand);
            prefetchNext();
            playback.advancing = false;
        };
        stand.addEventListener('canplay', onCanPlay, { once: true });
        return;
    }
    playback.activeEl.pause();
    playback.activeEl.removeAttribute('src');
    playback.activeEl.load();
    playback.activeEl.style.display = 'none';
    playback.activeEl = stand;
    playback.segIndex = nextIdx;
    showActiveVideo();
    if (playback.running) playElement(stand);
    prefetchNext();
}

function advanceSegment() {
    if (playback.advancing) return;
    playback.advancing = true;
    const nextIdx = playback.segIndex + 1;
    if (nextIdx >= playback.plan.length) {
        stopPlayback(true);
        playback.advancing = false;
        return;
    }
    const stand = otherSlot(playback.activeEl);
    if (playback.standbyIndex === nextIdx && stand.readyState >= 3 && stand.src && stand.duration > 0) {
        swapToStandby();
        playback.advancing = false;
    } else if (playback.standbyIndex === nextIdx && stand.readyState >= 2 && stand.src) {
        // Buffer exists but not enough data yet — wait for canplay
        const onCanPlay = () => {
            stand.removeEventListener('canplay', onCanPlay);
            if (stand.duration <= 0) {
                fallbackLoadActive();
                return;
            }
            swapToStandby();
            playback.advancing = false;
        };
        stand.addEventListener('canplay', onCanPlay, { once: true });
    } else {
        fallbackLoadActive();
    }
    function fallbackLoadActive() {
        playback.segIndex = nextIdx;
        const seg = playback.plan[nextIdx];
        const el = playback.activeEl;
        loadSegmentInto(seg, el);
        let started = false;
        const onMeta = () => {
            if (started) return; started = true;
            if (!el.duration || el.duration <= 0) {
                playback.advancing = false;
                return;
            }
            if (playback.running) playElement(el);
            playback.advancing = false;
            prefetchNext();
        };
        el.addEventListener('loadedmetadata', onMeta, { once: true });
        setTimeout(() => { if (playback.advancing) playback.advancing = false; }, 15000);
    }
}

async function toggleAutoPlayMode() {
    if (playback.mode === 'off') await enterAutoPlayMode();
    else exitAutoPlayMode();
}

async function enterAutoPlayMode() {
    if (!timelineClips.length) {
        alert('No clips to play.');
        return;
    }
    playback.mode = 'on';
    playback.running = false;
    playback.v1 = document.getElementById('video-player');
    playback.v2 = document.getElementById('video-standby');
    if (!playback.activeEl) playback.activeEl = playback.v1;
    attachVideoErrorListeners();
    playback.v1.removeAttribute('controls');
    playback.v2.removeAttribute('controls');
    playback.v2.style.display = 'none';
    document.getElementById('playback-time').classList.remove('hidden');
    const toggle = document.getElementById('autoplay-toggle');
    toggle.classList.add('on');
    toggle.textContent = 'Auto Play: On';

    await refreshPlaybackPlan();
    if (!playback.plan.length) { exitAutoPlayMode(); return; }

    let startIdx = 0;
    if (currentClip) {
        const f = playback.plan.findIndex(s => s.clip_id === currentClip.clip_id);
        if (f >= 0) startIdx = f;
    }
    playback.segIndex = startIdx;
    playback.currentTime = playback.plan[startIdx].start;
    if (playback.raf) cancelAnimationFrame(playback.raf);
    playback.raf = null;
    setPlayBtn(false);
    showActiveVideo();
    // Pre-load current segment (paused; user presses Play to start)
    loadSegmentInto(playback.plan[startIdx], playback.activeEl);
    playback.activeEl.addEventListener('loadedmetadata',
        () => { try { playback.activeEl.currentTime = 0; } catch (e) {} },
        { once: true });
    prefetchNext();
updatePlaybackOverlay(playback.plan[startIdx]);
    updatePlaybackTimeUI();
    drawTimeline();
}

function exitAutoPlayMode() {
    playback.mode = 'off';
    stopPlayback(false);
    if (playback.v1) playback.v1.setAttribute('controls', '');
    if (playback.v2) { playback.v2.pause(); playback.v2.removeAttribute('src'); playback.v2.load(); playback.v2.style.display = 'none'; }
    document.getElementById('playback-time').classList.add('hidden');
    const toggle = document.getElementById('autoplay-toggle');
    toggle.classList.remove('on');
    toggle.textContent = 'Auto Play: Off';
    document.getElementById('subtitle-overlay').classList.remove('active');
    if (currentClip) {
        if (currentClip.status === 'non_dub') loadRawPreview(currentClip.start_sec, currentClip.end_sec);
        else if (currentClip.clone_path) previewCurrent();
    }
    drawTimeline();
}

function togglePlayback() {
    if (playback.mode !== 'on') { toggleAutoPlayMode(); return; }
    if (playback.running) pausePlayback();
    else playPlayback();
}

function playPlayback() {
    if (!playback.plan.length) return;
    if (!playback.activeEl.src) {
        playback.running = true;
        startSegment(playback.segIndex, 0);
        return;
    }
    playElement(playback.activeEl);
}

function pausePlayback() {
    playback.running = false;
    if (playback.activeEl) playback.activeEl.pause();
    setPlayBtn(false);
    if (playback.raf) cancelAnimationFrame(playback.raf);
}

function stopPlayback(completed) {
    playback.running = false;
    if (playback.activeEl) playback.activeEl.pause();
    setPlayBtn(false);
    if (playback.raf) cancelAnimationFrame(playback.raf);
    if (completed) {
        const total = playback.totalEnd - playback.totalStart;
        document.getElementById('playback-time').textContent = pbFmt(total) + ' / ' + pbFmt(total);
        document.getElementById('subtitle-overlay').classList.remove('active');
    }
}

function playbackTick() {
    if (!playback.running) return;
    const seg = playback.plan[playback.segIndex];
    if (!seg) { stopPlayback(false); return; }
    const el = playback.activeEl;
    const local = el.currentTime || 0;
    const dur = seg.end - seg.start;
    playback.currentTime = Math.min(seg.start + local, seg.end);
    updatePlaybackOverlay(seg);
    updatePlaybackTimeUI();
    drawTimeline();
    if (seg.kind === 'clip' && (!currentClip || currentClip.clip_id !== seg.clip_id) &&
        Date.now() - playback.overlaySyncAt > 200) {
        playback.overlaySyncAt = Date.now();
        syncCurrentClipFromPlayback(seg.clip_id);
    }
    if (!playback.advancing && (el.ended ||
        (el.duration && el.duration > 0 && local >= dur - 0.05))) {
        advanceSegment();
    }
    playback.raf = requestAnimationFrame(playbackTick);
}

function updatePlaybackOverlay(seg) {
    const overlay = document.getElementById('subtitle-overlay');
    if (!overlay) return;
    if (seg.kind !== 'clip') { overlay.classList.remove('active'); overlay.innerHTML = ''; return; }
    if (seg.clip_id === '__op__' || seg.clip_id === '__ed__') {
        overlay.classList.remove('active'); overlay.innerHTML = ''; return;
    }
    const text = seg.translated_text || seg.original_text || '';
    if (!text.trim() || seg.status === 'non_dub' || seg.status === 'sign') {
        overlay.classList.remove('active'); overlay.innerHTML = ''; return;
    }
    let html = '';
    if (seg.character) html += `<span class="sub-character">${escHtml(seg.character)}</span>`;
    html += escHtml(text);
    overlay.innerHTML = html;
    overlay.classList.add('active');
}

function updatePlaybackTimeUI() {
    const total = playback.totalEnd - playback.totalStart;
    const cur = Math.max(0, playback.currentTime - playback.totalStart);
    document.getElementById('playback-time').textContent = pbFmt(cur) + ' / ' + pbFmt(total);
}

async function syncCurrentClipFromPlayback(clipId) {
    try {
        const clip = await api('/api/clips/' + clipId);
        currentClip = clip;
        renderClip();
        drawTimeline();
    } catch (e) { console.error('syncCurrentClip:', e); }
}

async function seekPlaybackTo(time, reloadIfNeeded) {
    if (reloadIfNeeded === undefined) reloadIfNeeded = true;
    playback.currentTime = time;
    const idx = segIndexForTime(time);
    if (idx < 0) { drawTimeline(); return; }
    const seg = playback.plan[idx];
    const local = Math.max(0, time - seg.start);
    if (idx === playback.segIndex && playback.activeEl.src) {
        try { playback.activeEl.currentTime = local; } catch (e) {}
    } else if (reloadIfNeeded) {
        playback.segIndex = idx;
        loadSegmentInto(seg, playback.activeEl);
        let started = false;
        const onMeta = () => {
            if (started) return; started = true;
            try { playback.activeEl.currentTime = local; } catch (e) {}
            if (playback.running) playElement(playback.activeEl);
        };
        playback.activeEl.addEventListener('loadedmetadata', onMeta, { once: true });
        prefetchNext();
    }
    updatePlaybackOverlay(seg);
    updatePlaybackTimeUI();
    drawTimeline();
}

// ── Boot ─────────────────────────────────────
(async () => {
    const lang = localStorage.getItem('anidub.lang') || 'en';
    document.getElementById('lang-select').value = lang;
    await loadI18n(lang);
    applyI18n();
    loadProjectPicker();
    refreshGpuStats();
})();