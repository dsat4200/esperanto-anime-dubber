let currentClip = null;
let totalClips = 0;
let characters = {};
let timelineData = [];
let episodes = [];
let activeStem = null;

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
function hideOverlay() {
    document.getElementById('overlay').style.display = 'none';
}

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

    const sel = document.getElementById('episode-select');
    sel.innerHTML = episodes.map(ep => {
        const marker = ep.status === 'done' ? ' ✓' : ep.status === 'in_progress' ? ' …' : '';
        return `<option value="${ep.stem}" ${ep.stem === activeStem ? 'selected' : ''}>${ep.stem}${marker} (${ep.clip_count || '?'})</option>`;
    }).join('');

    if (activeStem) {
        const tracks = await api('/api/tracks');
        if (!tracks.demucs_done) {
            document.getElementById('setup-panel').style.display = 'flex';
            document.getElementById('editor-panel').style.display = 'none';
            await loadTracks();
        } else {
            document.getElementById('setup-panel').style.display = 'none';
            document.getElementById('editor-panel').style.display = 'flex';
            await loadCharacters();
            await loadTimeline();
            document.getElementById('video-player').addEventListener('timeupdate', onVideoTimeUpdate);
            await loadClip(await getFirstUnaccepted());
        }
    } else if (episodes.length > 0) {
        await switchEpisode(episodes[0].stem);
    }
}

async function switchEpisode(stem) {
    if (!stem || stem === activeStem) return;
    showOverlay('Switching episode...');
    try {
        await api('/api/episodes/select', { method: 'POST', body: { stem } });
        activeStem = stem;
        hideOverlay();

        const tracks = await api('/api/tracks');
        if (!tracks.demucs_done) {
            document.getElementById('setup-panel').style.display = 'flex';
            document.getElementById('editor-panel').style.display = 'none';
            await loadTracks();
        } else {
            document.getElementById('setup-panel').style.display = 'none';
            document.getElementById('editor-panel').style.display = 'flex';
            await loadCharacters();
            await loadTimeline();
            document.getElementById('video-player').addEventListener('timeupdate', onVideoTimeUpdate);
            await loadClip(await getFirstUnaccepted());
        }
    } catch (e) {
        hideOverlay();
        alert('Switch failed: ' + e.message);
    }
}

// ── Setup ────────────────────────────────────

async function loadTracks() {
    const data = await api('/api/tracks');
    document.getElementById('tracks-section').style.display = 'block';

    const adiv = document.getElementById('audio-tracks');
    adiv.innerHTML = data.audio.map((t, i) =>
        `<label><input type="radio" name="audio" value="${i}" ${
            i === 0 ? 'checked' : ''
        } onchange="previewTrack('audio', ${i})"> ${t.language || '?'} (${t.codec}, ${t.channels}ch)</label>`
    ).join('<br>');

    const sdiv = document.getElementById('sub-tracks');
    sdiv.innerHTML = data.subtitle.map((t, i) =>
        `<label><input type="radio" name="sub" value="${i}" ${
            i === 0 ? 'checked' : ''
        } onchange="previewTrack('sub', ${i})"> ${t.language || '?'} (${t.codec})</label>`
    ).join('<br>');

    if (data.demucs_done) {
        document.getElementById('btn-demucs').style.display = 'none';
        document.getElementById('btn-start').style.display = 'inline';
        document.getElementById('demucs-status').textContent = 'Demucs already done.';
    }
}

async function runDemucs() {
    const audioIdx = parseInt(document.querySelector('input[name="audio"]:checked')?.value || '0');
    const subIdx = parseInt(document.querySelector('input[name="sub"]:checked')?.value || '0');
    await api('/api/audio-track', { method: 'POST', body: { index: audioIdx } });
    await api('/api/sub-track', { method: 'POST', body: { index: subIdx } });

    showOverlay('Running Demucs (may take a few minutes)...');
    try {
        await api('/api/demucs', { method: 'POST' });
        hideOverlay();
        document.getElementById('btn-demucs').style.display = 'none';
        document.getElementById('btn-start').style.display = 'inline';
        document.getElementById('demucs-status').textContent = 'Demucs complete.';
    } catch (e) {
        hideOverlay();
        alert('Demucs failed: ' + e.message);
    }
}

async function previewTrack(type, index) {
    try {
        const resp = await api('/api/preview-sample',
            { method: 'POST', body: { type, index } });
        // This returns audio/video directly — not JSON
        // Use fetch directly to handle binary response
        const blob = await fetch('/api/preview-sample', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type, index }),
        }).then(r => r.blob());
        const url = URL.createObjectURL(blob);
        const video = document.getElementById('video-player');
        if (type === 'audio') {
            video.src = url;
            video.load();
            video.play();
        } else {
            video.src = url;
            video.load();
            video.play();
        }
    } catch (e) {
        console.error('previewTrack:', e);
    }
}

async function startEditing() {
    document.getElementById('setup-panel').style.display = 'none';
    document.getElementById('editor-panel').style.display = 'flex';
    await loadEpisodes();
}

async function getFirstUnaccepted() {
    const data = await api('/api/stats');
    totalClips = data.total;
    for (let i = 1; i <= totalClips; i++) {
        const clip = await api('/api/clips/' + i);
        if (clip.status !== 'accepted' && clip.status !== 'non_dub') return i;
    }
    return 1;
}

// ── Editor ───────────────────────────────────

async function loadClip(n) {
    if (!n || n < 1 || n > totalClips) return;
    try {
        const clip = await api('/api/clips/' + n);
        currentClip = clip;
        renderClip();
        if (clip.status === 'non_dub') {
            loadRawPreview(clip.start_sec, clip.end_sec);
        } else if (clip.needs_processing) {
            await autoProcess(n);
        } else if (clip.clone_path) {
            await previewCurrent();
        }
    } catch (e) {
        console.error('loadClip failed:', e);
    }
}

function renderClip() {
    const c = currentClip;
    if (!c) return;
    document.getElementById('clip-title').textContent =
        `Clip ${c.index}/${totalClips}  ${fmtTs(c.start_sec)} → ${fmtTs(c.end_sec)}`;
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

    document.getElementById('offset-slider').value = c.offset_ms;
    document.getElementById('offset-val').textContent = Math.round(c.offset_ms);

    document.getElementById('speed-slider').value = Math.round((c.speed_factor || 1.0) * 100);
    document.getElementById('speed-val').textContent = (c.speed_factor || 1.0).toFixed(2);

    const info = [];
    if (c.status === 'non_dub') {
        info.push('Original audio only');
    }
    if (c.clone_ms) info.push(`Clone: ${(c.clone_ms/1000).toFixed(1)}s`);
    if (c.attempts) info.push(`Attempts: ${c.attempts}`);
    info.push(`Status: ${c.status}`);
    document.getElementById('clone-info').textContent = info.join('  |  ');

    // Hide clone/accept/reject for non-dub
    const nd = c.status === 'non_dub';
    document.querySelectorAll('.clone-only').forEach(el => el.style.display = nd ? 'none' : '');
    document.querySelectorAll('.accept-only').forEach(el => el.style.display = nd ? 'none' : '');

    renderTimelineHighlight();
}

async function autoProcess(n) {
    showOverlay('Processing...');
    try {
        const char = document.getElementById('char-select').value || undefined;
        const mood = document.getElementById('mood-select').value || 'normal';
        const result = await api(`/api/clips/${n}/process`,
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
    } catch (e) {
        console.error('autoProcess:', e);
    }
    hideOverlay();
}

async function prevClip() {
    const resp = await api('/api/clips/' + (currentClip ? currentClip.index - 1 : 1)).catch(() => null);
    if (resp && !resp.error) loadClip(resp.index);
}

async function nextClip() {
    const resp = await api('/api/clips/' + (currentClip ? currentClip.index + 1 : 1)).catch(() => null);
    if (resp && !resp.error) loadClip(resp.index);
}

async function translateCurrent() {
    const c = currentClip;
    if (!c) return;
    showOverlay('Translating...');
    try {
        const override = document.getElementById('translation-text').value.trim() || undefined;
        const resp = await api(`/api/clips/${c.index}/translate`,
            { method: 'POST', body: { text_override: override } });
        document.getElementById('translation-text').value = resp.translated_text;
        currentClip.translated_text = resp.translated_text;
        currentClip.status = 'translated';
        renderClip();
        loadTimeline();
    } catch (e) {
        alert('Translate failed: ' + e.message);
    }
    hideOverlay();
}

async function cloneCurrent() {
    const c = currentClip;
    if (!c) return;
    showOverlay('Cloning...');
    try {
        const char = document.getElementById('char-select').value || undefined;
        const mood = document.getElementById('mood-select').value || 'normal';
        const resp = await api(`/api/clips/${c.index}/clone`,
            { method: 'POST', body: { character: char, mood } });
        currentClip.clone_ms = resp.inference_ms;
        currentClip.status = 'cloned';
        currentClip.attempts = (currentClip.attempts || 0) + 1;
        renderClip();
        loadTimeline();
        await previewCurrent();
    } catch (e) {
        alert('Clone failed: ' + e.message);
    }
    hideOverlay();
}

async function previewCurrent() {
    const c = currentClip;
    if (!c) return;
    showOverlay('Generating preview...');
    try {
        const resp = await api(`/api/clips/${c.index}/preview`, { method: 'POST' });
        const video = document.getElementById('video-player');
        video.src = resp.url + '?t=' + Date.now();
        video.load();
        video.play();
    } catch (e) {
        alert('Preview failed: ' + e.message);
    }
    hideOverlay();
}

async function loadRawPreview(start_sec, end_sec) {
    try {
        const blob = await fetch('/api/preview-raw', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start_sec, end_sec }),
        }).then(r => r.blob());
        const video = document.getElementById('video-player');
        video.src = URL.createObjectURL(blob);
        video.load();
        video.play();
    } catch (e) {
        console.error('loadRawPreview:', e);
    }
}

async function acceptCurrent() {
    const c = currentClip;
    if (!c) return;
    if (c.needs_processing) await autoProcess(c.index);
    try {
        const resp = await api(`/api/clips/${c.index}/accept`, { method: 'POST' });
        currentClip.status = 'accepted';
        loadTimeline();
        loadEpisodes();
        if (resp.next_index) {
            loadClip(resp.next_index);
        } else if (resp.done) {
            alert('All clips accepted!');
        }
    } catch (e) {
        alert('Accept failed: ' + e.message);
    }
}

async function rejectCurrent() {
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.index}/reject`, { method: 'POST' });
    currentClip.status = 'rejected';
    renderClip();
    loadTimeline();
}

async function resetCurrent() {
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.index}/reset`, { method: 'POST' });
    currentClip.status = 'pending';
    currentClip.translated_text = null;
    currentClip.clone_ms = null;
    renderClip();
    loadTimeline();
}

async function onOffsetChange(val) {
    const ms = parseFloat(val);
    document.getElementById('offset-val').textContent = Math.round(ms);
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.index}/offset`, { method: 'POST', body: { offset_ms: ms } });
    currentClip.offset_ms = ms;
}

async function onSpeedChange(val) {
    const pct = parseInt(val) / 100;
    document.getElementById('speed-val').textContent = pct.toFixed(2);
    const c = currentClip;
    if (!c) return;
    await api(`/api/clips/${c.index}/speed`, { method: 'POST', body: { speed_factor: pct } });
    currentClip.speed_factor = pct;
}

async function onCharacterChange() {
    const c = currentClip;
    if (!c) return;
    const char = document.getElementById('char-select').value || null;
    const mood = document.getElementById('mood-select').value || 'normal';
    await api(`/api/clips/${c.index}/character`, { method: 'POST', body: { character: char, mood } });
    currentClip.character = char;
    currentClip.character_mood = mood;
    renderClip();
}

async function saveInstruct() {
    const c = currentClip;
    if (!c) return;
    const extra = document.getElementById('instruct-extra').value.trim() || null;
    await api(`/api/clips/${c.index}/instruct`, { method: 'POST', body: { instruct_extra: extra } });
    currentClip.instruct_extra = extra;
}

async function savePronunciation() {
    const c = currentClip;
    if (!c) return;
    const override = document.getElementById('pronunciation-text').value.trim() || null;
    await api(`/api/clips/${c.index}/pronunciation`, { method: 'POST', body: { pronunciation_override: override } });
    currentClip.pronunciation_override = override;
}

// ── Timeline ─────────────────────────────────

async function loadTimeline() {
    try {
        const data = await api('/api/timeline');
        timelineData = data;
        renderTimelineBar();
    } catch (e) { console.error(e); }
}

function renderTimelineBar() {
    const bar = document.getElementById('timeline-inner');
    if (!timelineData.length) { bar.innerHTML = ''; return; }

    const totalDur = timelineData.reduce((s, r) => s + r.duration, 0);
    bar.innerHTML = timelineData.map(r => {
        const pct = (r.duration / totalDur * 100).toFixed(2);
        const cls = r.kind === 'clip'
            ? `tl-${r.status || 'pending'}${r.clip_index === (currentClip?.index) ? ' current' : ''}`
            : `tl-${r.kind}`;
        const label = r.kind === 'clip' ? `#${r.clip_index}` : r.kind.toUpperCase();
        return `<div class="${cls}" style="width:${pct}%"
                     onclick="${r.kind === 'clip' ? `loadClip(${r.clip_index})` : ''}"
                     title="${label} ${fmtTs(r.start_sec)}→${fmtTs(r.end_sec)}">${label}</div>`;
    }).join('');
}

function renderTimelineHighlight() {
    const divs = document.querySelectorAll('#timeline-inner div');
    divs.forEach(d => d.classList.remove('current'));
    if (!currentClip) return;
    divs.forEach(d => {
        if (d.textContent === '#' + currentClip.index) d.classList.add('current');
    });
}

function onVideoTimeUpdate() {}

// ── Characters ───────────────────────────────

async function loadCharacters() {
    try {
        characters = await api('/api/characters');
        const sel = document.getElementById('char-select');
        sel.innerHTML = '<option value="">-- none --</option>' +
            Object.keys(characters).map(name =>
                `<option value="${name}">${name}</option>`
            ).join('');
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
        `<strong>${name}</strong><br>` +
        Object.entries(moods).map(([mood]) =>
            `<div>${mood}
                <button onclick="deleteCharacter('${name}','${mood}')">del</button>
            </div>`
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
        await api('/api/characters', { method: 'POST', body: { name, mood, clip_index: c.index } });
        await loadCharacters();
        renderClip();
    } catch (e) { alert('Save failed: ' + e.message); }
    hideOverlay();
}

async function deleteCharacter(name, mood) {
    await api(`/api/characters/${name}/${mood}`, { method: 'DELETE' });
    await loadCharacters();
    renderCharPanel();
    renderClip();
}

async function addCharacter() {
    const name = document.getElementById('new-char-name').value.trim();
    const mood = document.getElementById('new-char-mood').value.trim() || 'normal';
    if (!name) return alert('Enter a name');
    const c = currentClip;
    if (!c) return;
    try {
        await api('/api/characters', { method: 'POST', body: { name, mood, clip_index: c.index } });
        await loadCharacters();
        renderCharPanel();
        renderClip();
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
        if (currentClip) loadClip(currentClip.index);
    } catch (e) { alert('Translate all failed: ' + e.message); }
    hideOverlay();
}

async function cloneAll() {
    if (!confirm('Clone all translated clips?')) return;
    showOverlay('Cloning all...');
    try {
        const resp = await api('/api/clone-all', { method: 'POST' });
        await pollProgress('clone-all', 'Cloning');
        loadTimeline();
        if (currentClip) loadClip(currentClip.index);
    } catch (e) { alert('Clone all failed: ' + e.message); }
    hideOverlay();
}

async function pollProgress(key, label) {
    let done = false;
    while (!done) {
        await sleep(200);
        try {
            const p = await api(`/api/${key}/progress`);
            document.getElementById('overlay-msg').textContent =
                `${label}... ${p.current}/${p.total}`;
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

// ── Init ─────────────────────────────────────

loadProjectPicker();
