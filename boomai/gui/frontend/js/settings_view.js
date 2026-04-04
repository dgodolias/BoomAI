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
        this._syncToggle('toggle-shallow', false);
        this._syncToggle('toggle-comments', this.settings.scan_comments);
        this._syncToggle('toggle-debug', this.settings.scan_debug);

        // Profile pill
        if (this.settings.scan_profile === 'deep') {
            document.getElementById('pill-default').classList.remove('active');
            document.getElementById('pill-deep').classList.add('active');
        } else {
            document.getElementById('pill-default').classList.add('active');
            document.getElementById('pill-deep').classList.remove('active');
        }

        // Show/hide API key input on welcome
        if (!this.settings.api_key_set) {
            document.getElementById('api-key-setup').style.display = 'block';
            document.getElementById('btn-open-folder').style.opacity = '0.5';
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
        return document.getElementById('pill-deep').classList.contains('active') ? 'deep' : 'default';
    },

    isShallow() {
        return this.isToggleActive('toggle-shallow');
    },

    isComments() {
        return this.isToggleActive('toggle-comments');
    },
};
