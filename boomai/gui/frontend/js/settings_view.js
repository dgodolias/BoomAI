/**
 * Settings view — loads and syncs toggle states.
 */
const SettingsView = {
    settings: {},

    async load() {
        this.settings = await API.getSettings();

        // API key status (welcome screen)
        this._updateApiStatus(this.settings.api_key_set, this.settings.api_key_masked);

        // Toggles
        this._syncToggle('toggle-comments', this.settings.scan_comments);
        this._syncToggle('toggle-debug', this.settings.scan_debug);

        // Profile toggle
        this._syncToggle('toggle-deep', this.settings.scan_profile === 'deep');

        this._syncToggle('toggle-cost-report', this.settings.cost_reporting_enabled);

        // Show/hide API key input on welcome
        if (!this.settings.api_key_set) {
            document.getElementById('api-key-setup').style.display = 'block';
            document.getElementById('btn-open-folder').style.opacity = '0.5';
        }

        // Model dropdowns
        this._populateModelSelect('select-strong-model', this.settings.strong_candidates || []);
        this._populateModelSelect('select-weak-model', this.settings.weak_candidates || []);

        const strongLabel = document.getElementById('strong-mode-label');
        const weakLabel = document.getElementById('weak-mode-label');
        if (strongLabel) strongLabel.textContent = (this.settings.strong_mode || 'auto').toUpperCase();
        if (weakLabel) weakLabel.textContent = (this.settings.weak_mode || 'auto').toUpperCase();
    },

    _populateModelSelect(selectId, candidates) {
        const select = document.getElementById(selectId);
        if (!select) return;
        select.innerHTML = '<option value="">AUTO</option>';
        for (const c of candidates) {
            const opt = document.createElement('option');
            opt.value = c.model_id;
            opt.textContent = c.display_name;
            if (c.current) opt.selected = true;
            select.appendChild(opt);
        }
    },

    _updateApiStatus(isSet, masked) {
        // Welcome screen
        const dot = document.getElementById('api-status-dot');
        const text = document.getElementById('api-status-text');
        if (dot) {
            dot.className = `status-dot ${isSet ? 'green' : 'red'}`;
        }
        if (text) {
            text.textContent = isSet ? `API Key: ${masked}` : 'API Key not set';
        }

        // Browser screen
        const sdot = document.getElementById('settings-api-dot');
        const stext = document.getElementById('settings-api-text');
        if (sdot) sdot.className = `status-dot ${isSet ? 'green' : 'red'}`;
        if (stext) stext.textContent = isSet ? masked : 'Not configured';
    },

    _syncToggle(id, value) {
        const el = document.getElementById(id);
        if (!el) return;
        if (value) el.classList.add('active');
        else el.classList.remove('active');
    },

    isToggleActive(id) {
        const el = document.getElementById(id);
        return el ? el.classList.contains('active') : false;
    },

    getProfile() {
        return this.isToggleActive('toggle-deep') ? 'deep' : 'default';
    },

    isShallow() {
        return false;
    },

    isComments() {
        return this.isToggleActive('toggle-comments');
    },
};
