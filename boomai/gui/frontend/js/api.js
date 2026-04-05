/**
 * Thin wrapper around window.pywebview.api — ensures pywebview is ready.
 */
const API = {
    _ready: false,
    _queue: [],

    async _waitReady() {
        if (this._ready) return;
        return new Promise(resolve => {
            if (window.pywebview && window.pywebview.api) {
                this._ready = true;
                resolve();
            } else {
                window.addEventListener('pywebviewready', () => {
                    this._ready = true;
                    resolve();
                });
            }
        });
    },

    async call(method, ...args) {
        await this._waitReady();
        return window.pywebview.api[method](...args);
    },

    selectFolder()              { return this.call('select_folder'); },
    getCwd()                    { return this.call('get_cwd'); },
    getFileTree(path)           { return this.call('get_file_tree', path); },
    getRecentProjects()         { return this.call('get_recent_projects'); },
    getSettings()               { return this.call('get_settings'); },
    setApiKey(key)              { return this.call('set_api_key', key); },
    saveSetting(key, value)     { return this.call('save_setting', key, value); },
    getApiKeyStatus()           { return this.call('get_api_key_status'); },
    estimate(path, files, profile, shallow)
        { return this.call('estimate', path, files, profile, shallow); },
    startScan(path, files, profile, comments, shallow)
        { return this.call('start_scan', path, files, profile, comments, shallow); },
    getScanStatus()             { return this.call('get_scan_status'); },
    cancelScan()                { return this.call('cancel_scan'); },
    getScanResults()            { return this.call('get_scan_results'); },
    applyFixes(path, indices)   { return this.call('apply_fixes', path, indices); },
};
