let currentClip = null;
let totalClips = 0;
let characters = {};
let timelineData = [];

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

// ── Setup ────────────────────────────────────

async function openMkv() {
    const path = document.getElementById('mkv-path').value.trim();
    if (!path) return;
    const name = document.getElementById('project-name').value.trim() || undefined;
    showOverlay('Opening...');
    try {
        await api('/api/open', { method: 'POST', body: { mkv_path: path, project_name: name } });
        hideOverlay();
        await loadTracks();
    } catch (e) {
        hideOverlay();
        alert('Open failed: ' + e.message);
    }
}

async function openProject() {
    const dir = document.getElementById('project-dir').value.trim();
    if (!dir) return;
    showOverlay('Loading project...');
    try {
        await api('/api/open', { method: 'POST', body: { project_dir: dir } });
        hideOverlay();
        await loadTracks();
        if (await checkDemucs()) { startEditing(); }
    } catch (e) {
        hideOverlay();
        alert('Load failed: ' + e.message);
    }
}

async function loadTracks() {
    const data = await api('/api/tracks');
    document.getElementById('tracks-section').style.display = 'block';

    const adiv = document.getElementById('audio-tracks');
    adiv.innerHTML = data.audio.map((t, i) =>
        `<label><input type="radio" name="audio" value="${i}" ${
            i === 0 ? 'checked' : ''
        }> ${t.language || '?'} (${t.codec}, ${t.channels}ch)</label>`
    ).join('<br>');

    const sdiv = document.getElementById('sub-tracks');
    sdiv.innerHTML = data.subtitle.map((t, i) =>
        `<label><input type="radio" name="sub" value="${i}" ${
            i === 0 ? 'checked' : ''
        }> ${t.language || '?'} (${t.codec})</label>`
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

async function checkDemucs() {
    const data = await api('/api/tracks');
    return data.demucs_done;
}

// ── Editor ───────────────────────────────────

async function startEditing() {
    document.getElementById('setup-panel').style.display = 'none';
    document.getElementById('editor-panel').style.display = 'flex';
    await loadCharacters();
    await loadTimeline();
    document.getElementById('video-player').addEventListener('timeupdate', onVideoTimeUpdate);
    loadClip(await getFirstUnaccepted());
}

function getCheckedRadio(name) {
    const el = document.querySelector(`input[name="${name}"]:checked`);
    return el ? parseInt(el.value) : 0;
}

async function getFirstUnaccepted() {
    const data = await api('/api/stats');
    totalClips = data.total;
    for (let i = 1; i <= totalClips; i++) {
        const clip = await api('/api/clips/' + i);
        if (clip.status !== 'accepted') return i;
    }
    return 1;
}

async function loadClip(n) {
    if (!n || n < 1 || n > totalClips) return;
    try {
        const clip = await api('/api/clips/' + n);
        currentClip = clip;
        renderClip();
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

    const info = [];
    if (c.clone_ms) info.push(`Clone: ${(c.clone_ms/1000).toFixed(1)}s`);
    if (c.attempts) info.push(`Attempts: ${c.attempts}`);
    info.push(`Status: ${c.status}`);
    document.getElementById('clone-info').textContent = info.join('  |  ');

    renderTimelineHighlight();
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

async function acceptCurrent() {
    const c = currentClip;
    if (!c) return;
    try {
        const resp = await api(`/api/clips/${c.index}/accept`, { method: 'POST' });
        currentClip.status = 'accepted';
        loadTimeline();
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
    await api(`/api/clips/${c.index}/offset`,
        { method: 'POST', body: { offset_ms: ms } });
    currentClip.offset_ms = ms;
}

async function onCharacterChange() {
    const c = currentClip;
    if (!c) return;
    const char = document.getElementById('char-select').value || null;
    const mood = document.getElementById('mood-select').value || 'normal';
    await api(`/api/clips/${c.index}/character`,
        { method: 'POST', body: { character: char, mood } });
    currentClip.character = char;
    currentClip.character_mood = mood;
    renderClip();
}

// ── Timeline ─────────────────────────────────

async function loadTimeline() {
    const data = await api('/api/timeline');
    timelineData = data;
    renderTimelineBar();
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

function onVideoTimeUpdate() {
    // Could sync timeline highlight to video position — deferred
}

// ── Characters ───────────────────────────────

async function loadCharacters() {
    try {
        characters = await api('/api/characters');
        const sel = document.getElementById('char-select');
        sel.innerHTML = '<option value="">-- none --</option>' +
            Object.keys(characters).map(name =>
                `<option value="${name}">${name}</option>`
            ).join('');
    } catch (e) {
        characters = {};
    }
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
        Object.entries(moods).map(([mood, path]) =>
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
        await api('/api/characters',
            { method: 'POST', body: { name, mood, clip_index: c.index } });
        await loadCharacters();
        renderClip();
    } catch (e) {
        alert('Save failed: ' + e.message);
    }
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
        await api('/api/characters',
            { method: 'POST', body: { name, mood, clip_index: c.index } });
        await loadCharacters();
        renderCharPanel();
        renderClip();
        document.getElementById('new-char-name').value = '';
        document.getElementById('new-char-mood').value = '';
    } catch (e) {
        alert('Add failed: ' + e.message);
    }
}

// ── Bulk ─────────────────────────────────────

async function translateAll() {
    if (!confirm('Translate all pending clips?')) return;
    showOverlay('Translating all...');
    try {
        const resp = await api('/api/translate-all', { method: 'POST' });
        document.getElementById('bulk-status').textContent =
            `Translated ${resp.processed}/${resp.total}`;
        loadTimeline();
        if (currentClip) loadClip(currentClip.index);
    } catch (e) {
        alert('Translate all failed: ' + e.message);
    }
    hideOverlay();
}

async function cloneAll() {
    const stats = await api('/api/stats');
    const count = (stats.translated || 0) + (stats.cloned || 0) + (stats.rejected || 0);
    if (count === 0) return alert('No translated clips to clone.');
    if (!confirm(`Clone ${count} clips? This may take a while.`)) return;
    showOverlay('Cloning all...');
    document.getElementById('bulk-status').textContent = 'Cloning...';
    try {
        const resp = await api('/api/clone-all', { method: 'POST' });
        document.getElementById('bulk-status').textContent =
            `Cloned ${resp.processed}/${resp.total}`;
        loadTimeline();
        if (currentClip) loadClip(currentClip.index);
    } catch (e) {
        alert('Clone all failed: ' + e.message);
    }
    hideOverlay();
}

// ── Save / Assemble ──────────────────────────

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
    } catch (e) {
        alert('Assemble failed: ' + e.message);
    }
    hideOverlay();
}

// ── Utilities ────────────────────────────────

function fmtTs(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = (sec % 60).toFixed(1);
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(4, '0')}`;
}

// ── Auto-load ────────────────────────────────

if (window._AUTO_LOAD) {
    (async () => {
        try {
            await api('/api/open', { method: 'POST', body: { project_dir: window._AUTO_LOAD } });
            await loadTracks();
            if (await checkDemucs()) startEditing();
        } catch (e) { console.error('Auto-load failed:', e); }
    })();
}
