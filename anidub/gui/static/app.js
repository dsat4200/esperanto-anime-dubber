let currentClip = null;
let characters = {};
let timelineClips = [];
let episodes = [];
let activeStem = null;
let dataAnimeName = '';

let selectedEpisodes = new Set();
let batchTrackAudioIdx = null;
let batchTrackSubIdx = null;
let pendingBatchType = null;

let timelineCtx = null;
let timelineVP = { startSec: 0, pxPerSec: 50 };
let timelineDrag = null;
let timelineCtxMenuClipId = null;

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
        sel.innerHTML = '<option value="">-- load --</option>' +
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
    showOverlay('Loading project...');
    try {
        await api('/api/open', { method: 'POST', body: { project_dir: path } });
        hideOverlay();
        await loadEpisodes();
    } catch (e) {
        hideOverlay();
        alert('Load failed: ' + e.message);
    }
}

async function openAnime() {
    const name = document.getElementById('anime-name').value.trim();
    if (!name) return;
    showOverlay('Creating project...');
    try {
        await api('/api/open', { method: 'POST', body: { anime: name } });
        hideOverlay();
        await loadEpisodes();
    } catch (e) {
        hideOverlay();
        alert('Create failed: ' + e.message);
    }
}

// ── Episodes ──────────────────────────────────

async function loadEpisodes() {
    const data = await api('/api/episodes');
    episodes = data.episodes;
    activeStem = data.active_stem;
    dataAnimeName = data.anime_name || '';
    renderEpisodeHome();
    if (activeStem) {
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
    document.getElementById('home-title').textContent = dataAnimeName || 'Episodes';
    grid.innerHTML = episodes.map(ep => {
        const pct = ep.progress_pct || 0;
        const tPct = ep.translation_pct || 0;
        const cPct = ep.clone_pct || 0;
        return `<div class="ep-card" data-color="${ep.color}" data-stem="${ep.stem}"
                     onclick="onEpisodeClick(event, '${ep.stem}')"
                     ondblclick="openEpisode('${ep.stem}')">
            <div class="ep-num">#${ep.number}</div>
            <div class="ep-title">${escHtml(ep.title || ep.stem)}</div>
            <div class="ep-bars">
                <div class="ep-bar ep-bar-tr"><div class="ep-bar-fill" style="width:${tPct}%"></div><span>Tr ${tPct}%</span></div>
                <div class="ep-bar ep-bar-cl"><div class="ep-bar-fill" style="width:${cPct}%"></div><span>Cl ${cPct}%</span></div>
                <div class="ep-bar ep-bar-ac"><div class="ep-bar-fill" style="width:${pct}%"></div><span>Ac ${pct}%</span></div>
            </div>
        </div>`;
    }).join('');
    updateSelectionUI();
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
    showOverlay('Loading episode...');
    try {
        await api('/api/episodes/select', { method: 'POST', body: { stem } });
        activeStem = stem;
        hideOverlay();
        document.getElementById('home-panel').style.display = 'none';
        document.getElementById('editor-panel').style.display = 'flex';
        await setupEditor();
    } catch (e) {
        hideOverlay();
        alert('Failed to open episode: ' + e.message);
    }
}

async function goHome() {
    document.getElementById('editor-panel').style.display = 'none';
    document.getElementById('home-panel').style.display = 'flex';
    selectedEpisodes.clear();
    await loadEpisodes();
}

async function setupEditorForActive() {
    const tracks = await api('/api/tracks');
    if (!tracks.demucs_done) {
        await runDemucsFlow();
    }
    document.getElementById('home-panel').style.display = 'none';
    document.getElementById('editor-panel').style.display = 'flex';
    await setupEditor();
}

async function setupEditor() {
    const tracks = await api('/api/tracks');
    if (!tracks.demucs_done) {
        await runDemucsFlow();
    }
    initTimelineCanvas();
    await loadCharacters();
    await loadTimeline();
    document.getElementById('video-player').addEventListener('timeupdate', onVideoTimeUpdate);
    populateEpisodeDropdown();
    const first = await getFirstUnaccepted();
    await loadClip(first);
}

async function runDemucsFlow() {
    showOverlay('Running Demucs (may take a few minutes)...');
    try {
        await api('/api/demucs', { method: 'POST' });
    } catch (e) {
        alert('Demucs failed: ' + e.message);
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
    showOverlay('Switching episode...');
    try {
        await api('/api/episodes/select', { method: 'POST', body: { stem } });
        activeStem = stem;
        hideOverlay();
        await setupEditor();
    } catch (e) {
        hideOverlay();
        alert('Switch failed: ' + e.message);
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
        `Clip ${c.clip_id}  ${fmtTs(c.start_sec)} → ${fmtTs(c.end_sec)}`;
    document.getElementById('original-text').textContent = c.original_text;
    document.getElementById('translation-text').value = c.translated_text || '';
    document.getElementById('pronunciation-text').value = c.pronunciation_override || '';
    document.getElementById('instruct-extra').value = c.instruct_extra || '';

    const sel = document.getElementById('char-select');
    sel.innerHTML = '<option value="">-- none --</option>' +
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

    const info = [];
    if (c.status === 'non_dub') info.push('Original audio only');
    if (c.status === 'sign') info.push('Sign/No audio');
    if (c.clone_ms) info.push(`Clone: ${(c.clone_ms / 1000).toFixed(1)}s`);
    if (c.attempts) info.push(`Attempts: ${c.attempts}`);
    if (c.audio_offset_ms) info.push(`Offset: ${c.audio_offset_ms.toFixed(0)}ms`);
    info.push(`Status: ${c.status}`);
    document.getElementById('clone-info').textContent = info.join('  |  ');

    const nd = c.status === 'non_dub' || c.status === 'sign';
    document.querySelectorAll('.clone-only').forEach(el => el.style.display = nd ? 'none' : '');
    document.querySelectorAll('.accept-only').forEach(el => el.style.display = nd ? 'none' : '');
}

async function autoProcess(clipId) {
    showOverlay('Processing...');
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
    showOverlay('Translating...');
    try {
        const resp = await api(`/api/clips/${c.clip_id}/translate`, { method: 'POST' });
        document.getElementById('translation-text').value = resp.translated_text;
        currentClip.translated_text = resp.translated_text;
        currentClip.status = 'translated';
        renderClip();
        loadTimeline();
    } catch (e) { alert('Translate failed: ' + e.message); }
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

    await api(`/api/clips/${clipId}/translate`, { method: 'POST', body: { text_override: translation } });
    await api(`/api/clips/${clipId}/pronunciation`, { method: 'POST', body: { pronunciation_override: pronunciation } });
    await api(`/api/clips/${clipId}/instruct`, { method: 'POST', body: { instruct_extra: instructExtra } });
    await api(`/api/clips/${clipId}/character`, { method: 'POST', body: { character, mood } });
    await api(`/api/clips/${clipId}/speed`, { method: 'POST', body: { speed_factor: speedFactor } });

    currentClip.translated_text = translation;
    currentClip.status = 'translated';
    currentClip.pronunciation_override = pronunciation;
    currentClip.instruct_extra = instructExtra;
    currentClip.character = character;
    currentClip.character_mood = mood;
    currentClip.speed_factor = speedFactor;
    renderClip();
    loadTimeline();
}

async function cloneCurrent() {
    const c = currentClip;
    if (!c) return;
    showOverlay('Cloning...');
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
    } catch (e) { alert('Clone failed: ' + e.message); }
    hideOverlay();
}

async function previewCurrent() {
    const c = currentClip;
    if (!c) return;
    showOverlay('Generating preview...');
    try {
        const resp = await api(`/api/clips/${c.clip_id}/preview`, { method: 'POST' });
        const video = document.getElementById('video-player');
        video.src = resp.url + '?t=' + Date.now();
        video.load();
        video.play();
    } catch (e) { alert('Preview failed: ' + e.message); }
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
    } catch (e) { alert('Accept failed: ' + e.message); }
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
    document.getElementById('modal-audio-tracks').innerHTML = '<h4>Audio</h4>' +
        data.audio.map((t, i) => `<label><input type="radio" name="m-audio" value="${i}" ${i===0?'checked':''}> ${t.language||'?'} (${t.codec}, ${t.channels}ch)</label>`).join('');
    document.getElementById('modal-sub-tracks').innerHTML = '<h4>Subtitles</h4>' +
        data.subtitle.map((t, i) => `<label><input type="radio" name="m-sub" value="${i}" ${i===0?'checked':''}> ${t.language||'?'} (${t.codec})</label>`).join('');
    document.getElementById('track-modal').style.display = 'flex';
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

async function runBatch(type) {
    const stems = [...selectedEpisodes];
    if (!stems.length) return;
    const key = type === 'translate' ? 'batch-translate' : 'batch-clone';
    const endpoint = type === 'translate' ? '/api/episodes/batch-translate' : '/api/episodes/batch-clone';

    showOverlay(`${type === 'translate' ? 'Translating' : 'Cloning'} episodes...`);
    try {
        await api(endpoint, {
            method: 'POST',
            body: { stems, audio_idx: batchTrackAudioIdx, sub_idx: batchTrackSubIdx },
        });
        await pollBatchProgress(key);
        selectedEpisodes.clear();
        await loadEpisodes();
    } catch (e) { alert(`Batch ${type} failed: ` + e.message); }
    hideOverlay();
}

async function pollBatchProgress(key) {
    let done = false;
    while (!done) {
        await sleep(300);
        try {
            const p = await api(`/api/episodes/${key}/progress`);
            document.getElementById('overlay-msg').textContent = p.message || `${key}...`;
            document.getElementById('bulk-status').textContent = p.message || '';
            if (p.done) done = true;
        } catch (e) { done = true; }
    }
    document.getElementById('bulk-status').textContent = 'Complete.';
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
        if (hit.type === 'handle-start' || hit.type === 'handle-end' || hit.type === 'audio-handle') e.target.style.cursor = 'ew-resize';
        else if (hit.type === 'clip') e.target.style.cursor = 'pointer';
        else e.target.style.cursor = 'grab';
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
    e.target.style.cursor = 'grab';
    if (!timelineDrag) return;
    const drag = timelineDrag; timelineDrag = null;
    if (drag.type === 'pan') return;
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
        sel.innerHTML = '<option value="">-- none --</option>' +
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
            `<div>${mood} <button onclick="deleteCharacter('${name}','${mood}')">del</button></div>`
        ).join('')
    ).join('<hr>');
}
async function saveCharacter() {
    const c = currentClip;
    if (!c) return;
    const name = document.getElementById('char-select').value || prompt('Character name:');
    if (!name) return;
    const mood = document.getElementById('mood-select').value || 'normal';
    showOverlay('Saving...');
    try {
        await api('/api/characters', { method: 'POST', body: { name, mood, clip_id: c.clip_id } });
        await loadCharacters(); renderClip();
    } catch (e) { alert('Save failed: ' + e.message); }
    hideOverlay();
}
async function deleteCharacter(name, mood) {
    await api(`/api/characters/${name}/${mood}`, { method: 'DELETE' });
    await loadCharacters(); renderCharPanel(); renderClip();
}
async function addCharacter() {
    const name = document.getElementById('new-char-name').value.trim();
    const mood = document.getElementById('new-char-mood').value.trim() || 'normal';
    if (!name) return alert('Enter a name');
    const c = currentClip; if (!c) return;
    try {
        await api('/api/characters', { method: 'POST', body: { name, mood, clip_id: c.clip_id } });
        await loadCharacters(); renderCharPanel(); renderClip();
        document.getElementById('new-char-name').value = '';
        document.getElementById('new-char-mood').value = '';
    } catch (e) { alert('Add failed: ' + e.message); }
}

// ── Bulk ─────────────────────────────────────

async function translateAll() {
    if (!confirm('Translate all pending clips?')) return;
    showOverlay('Translating all...');
    try {
        await api('/api/translate-all', { method: 'POST' });
        await pollProgress('translate-all', 'Translating');
        loadTimeline();
        if (currentClip) loadClip(currentClip.clip_id);
    } catch (e) { alert('Translate all failed: ' + e.message); }
    hideOverlay();
}
async function cloneAll() {
    if (!confirm('Clone all translated clips?')) return;
    showOverlay('Cloning all...');
    try {
        await api('/api/clone-all', { method: 'POST' });
        await pollProgress('clone-all', 'Cloning');
        loadTimeline();
        if (currentClip) loadClip(currentClip.clip_id);
    } catch (e) { alert('Clone all failed: ' + e.message); }
    hideOverlay();
}
async function pollProgress(key, label) {
    let done = false;
    while (!done) {
        await sleep(200);
        try {
            const p = await api(`/api/${key}/progress`);
            document.getElementById('overlay-msg').textContent = `${label}... ${p.current}/${p.total}`;
            document.getElementById('bulk-status').textContent = p.message;
            if (p.done) done = true;
        } catch (e) { done = true; }
    }
    document.getElementById('bulk-status').textContent = `${label} complete.`;
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function saveProject() {
    await api('/api/save', { method: 'POST' });
    document.getElementById('bulk-status').textContent = 'Saved.';
}
async function assembleFull() {
    if (!confirm('Assemble full episode?')) return;
    showOverlay('Assembling...');
    try {
        const resp = await api('/api/assemble', { method: 'POST' });
        alert('Done: ' + resp.final_path);
    } catch (e) { alert('Assemble failed: ' + e.message); }
    hideOverlay();
}
function fmtTs(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = (sec % 60).toFixed(1);
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(4, '0')}`;
}

loadProjectPicker();
