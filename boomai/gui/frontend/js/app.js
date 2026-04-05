/**
 * Main app controller — screen navigation, state, user actions.
 */
const App = {
    currentScreen: 'welcome',
    repoPath: null,
    estimateData: null,

    async init() {
        await SettingsView.load();
        this._loadRecentProjects();
        this._showCwd();
    },

    async _showCwd() {
        const result = await API.getCwd();
        if (result && result.path) {
            this._cwd = result.path;
            const display = result.path.replace(/\\/g, '/');
            document.getElementById('cwd-path').textContent = display;
            document.getElementById('cwd-card').style.display = 'block';
        }
    },

    async useCwd() {
        if (!await this._ensureApiKey()) return;
        if (!this._cwd) return;
        this._openProject(this._cwd);
    },

    // ── Navigation ────────────────────────────────

    goTo(screenId) {
        const current = document.getElementById(`screen-${this.currentScreen}`);
        const next = document.getElementById(`screen-${screenId}`);
        if (!current || !next || this.currentScreen === screenId) return;

        current.classList.remove('active');
        current.classList.add('exit-left');
        setTimeout(() => current.classList.remove('exit-left'), 400);

        next.classList.add('active');
        this.currentScreen = screenId;
    },

    // ── Welcome ───────────────────────────────────

    async openFolder() {
        if (!await this._ensureApiKey()) return;
        const result = await API.selectFolder();
        if (!result || !result.path) return;
        this._openProject(result.path);
    },

    async openRecentProject(path) {
        if (!await this._ensureApiKey()) return;
        this._openProject(path);
    },

    _openProject(path) {
        this.repoPath = path;
        document.getElementById('project-name').textContent =
            path.replace(/\\/g, '/').split('/').pop();
        this.goTo('browser');
        this._loadFileTree();
    },

    async _loadFileTree() {
        const treeData = await API.getFileTree(this.repoPath);
        if (treeData.error) {
            alert(`Error loading files: ${treeData.error}`);
            return;
        }
        FileBrowser.render(treeData);
    },

    async _loadRecentProjects() {
        const recent = await API.getRecentProjects();
        const container = document.getElementById('recent-projects');
        if (!recent || recent.length === 0) {
            container.innerHTML = '';
            return;
        }

        container.innerHTML = `
            <div class="section-title" style="text-align:center;">Recent Projects</div>
            <ul class="recent-list">
                ${recent.map(p => `
                    <li class="recent-item" onclick="App.openRecentProject('${p.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')">
                        <span style="color:var(--accent-purple);">📁</span>
                        <span class="recent-path">${p.replace(/\\/g, '/')}</span>
                    </li>
                `).join('')}
            </ul>
        `;
    },

    // ── API Key ───────────────────────────────────

    async saveApiKey() {
        const input = document.getElementById('api-key-input');
        const key = input.value.trim();
        if (!key) return;

        await API.setApiKey(key);
        document.getElementById('api-key-setup').style.display = 'none';
        document.getElementById('btn-open-folder').style.opacity = '1';
        await SettingsView.load();
    },

    async updateApiKey() {
        const input = document.getElementById('settings-api-input');
        const key = input.value.trim();
        if (!key) return;
        await API.setApiKey(key);
        input.value = '';
        await SettingsView.load();
    },

    async _ensureApiKey() {
        const status = await API.getApiKeyStatus();
        if (!status.configured) {
            document.getElementById('api-key-setup').style.display = 'block';
            document.getElementById('api-key-input').focus();
            return false;
        }
        return true;
    },

    // ── Settings ──────────────────────────────────

    setProfile(profile) {
        const el = document.getElementById('toggle-deep');
        if (el) {
            if (profile === 'deep') el.classList.add('active');
            else el.classList.remove('active');
        }
    },

    toggleSetting(el) {
        el.classList.toggle('active');
    },

    async changeModel(role, modelId) {
        await API.setModel(role, modelId);
        await SettingsView.load();
    },

    async refreshCatalog() {
        const btn = document.querySelector('[onclick="App.refreshCatalog()"]');
        const original = btn.textContent;
        btn.textContent = 'Refreshing...';
        btn.disabled = true;
        btn.style.opacity = '0.6';

        const result = await API.refreshCatalog();
        if (result.error) {
            btn.textContent = 'Error!';
            btn.style.color = 'var(--error)';
            setTimeout(() => {
                btn.textContent = original;
                btn.style.color = '';
                btn.style.opacity = '';
                btn.disabled = false;
            }, 2000);
        } else {
            btn.textContent = 'Refreshed!';
            btn.style.color = 'var(--success)';
            btn.style.borderColor = 'var(--success)';
            await SettingsView.load();
            setTimeout(() => {
                btn.textContent = original;
                btn.style.color = '';
                btn.style.borderColor = '';
                btn.style.opacity = '';
                btn.disabled = false;
            }, 1500);
        }
    },

    // ── Estimation ────────────────────────────────

    async estimate() {
        const files = FileBrowser.getSelectedFiles();
        if (files.length === 0) {
            alert('No files selected!');
            return;
        }

        const profile = SettingsView.getProfile();
        const shallow = SettingsView.isShallow();

        // Show loading state
        document.getElementById('est-cost').textContent = 'Calculating...';
        this.goTo('estimate');

        const est = await API.estimate(this.repoPath, files, profile, shallow);
        if (est.error) {
            document.getElementById('est-cost').textContent = `Error: ${est.error}`;
            return;
        }

        this.estimateData = est;

        // Fill estimate grid
        const grid = document.getElementById('estimate-grid');
        grid.innerHTML = `
            <div class="estimate-item">
                <div class="estimate-label">Profile</div>
                <div class="estimate-value">${est.profile}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">Model</div>
                <div class="estimate-value" style="font-size:14px;">${est.model}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">Files</div>
                <div class="estimate-value">${est.file_count.toLocaleString()}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">Chunks</div>
                <div class="estimate-value">${est.chunk_count}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">API Calls</div>
                <div class="estimate-value">${est.api_calls_low}–${est.api_calls_high}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">Total Chars</div>
                <div class="estimate-value">${(est.total_chars / 1000).toFixed(0)}K</div>
            </div>
        `;

        // Cost
        document.getElementById('est-cost').textContent =
            `$${est.cost_min.toFixed(2)} – $${est.cost_max.toFixed(2)}`;

        // Time
        document.getElementById('est-time').textContent =
            `${this._formatTime(est.time_min)} – ${this._formatTime(est.time_max)}`;

        // Learned
        const learnedEl = document.getElementById('est-learned');
        if (est.learned_samples > 0) {
            learnedEl.textContent = `Calibrated from ${est.learned_samples} previous run(s)`;
        } else {
            learnedEl.textContent = '';
        }
    },

    // ── Scan ──────────────────────────────────────

    async startScan() {
        const files = FileBrowser.getSelectedFiles();
        const profile = SettingsView.getProfile();
        const comments = SettingsView.isComments();
        const shallow = SettingsView.isShallow();

        ScanView.reset();
        this.goTo('scan');

        const result = await API.startScan(this.repoPath, files, profile, comments, shallow);
        if (result.error) {
            alert(`Failed to start scan: ${result.error}`);
            return;
        }

        ScanView.startPolling();
    },

    async cancelScan() {
        if (confirm('Cancel the running scan?')) {
            await API.cancelScan();
            ScanView.stopPolling();
            this.goTo('browser');
        }
    },

    // ── Results ───────────────────────────────────

    async applyFixes() {
        const indices = ResultsView.getSelectedIndices();
        if (indices.length === 0) return;

        const result = await API.applyFixes(this.repoPath, indices);
        if (result.error) {
            alert(`Error: ${result.error}`);
            return;
        }

        alert(`Applied ${result.applied} fix(es).\n\nRun 'git diff' to see changes.`);
        document.getElementById('btn-apply-fixes').style.display = 'none';
    },

    newScan() {
        this.goTo('browser');
    },

    // ── Helpers ───────────────────────────────────

    _formatTime(seconds) {
        const s = Math.round(seconds);
        if (s < 60) return `${s}s`;
        return `${Math.floor(s / 60)}m ${s % 60}s`;
    },
};

// Boot
document.addEventListener('DOMContentLoaded', () => {
    // Wait for pywebview to be ready
    if (window.pywebview) {
        App.init();
    } else {
        window.addEventListener('pywebviewready', () => App.init());
    }
});
