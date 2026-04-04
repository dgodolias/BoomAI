/**
 * Results screen — findings list with selective fix application.
 */
const ResultsView = {
    results: null,
    selectedFixes: new Set(),

    show(results) {
        this.results = results;
        this.selectedFixes = new Set();

        // Summary card
        const summary = document.getElementById('results-summary');
        const elapsed = this._formatTime(results.elapsed || 0);
        const cost = results.usage
            ? `$${((results.usage.prompt_tokens * 1.25 + results.usage.completion_tokens * 10) / 1_000_000).toFixed(4)}`
            : '--';

        summary.innerHTML = `
            <div class="estimate-item">
                <div class="estimate-label">Issues Found</div>
                <div class="estimate-value">${results.findings.length}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">Fixes Applied</div>
                <div class="estimate-value" style="color:var(--success)">${results.applied_count}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">Time</div>
                <div class="estimate-value">${elapsed}</div>
            </div>
            <div class="estimate-item">
                <div class="estimate-label">API Calls</div>
                <div class="estimate-value">${results.usage ? results.usage.api_calls : '--'}</div>
            </div>
        `;

        // Summary text
        if (results.summary) {
            const summaryText = document.createElement('div');
            summaryText.style.cssText = 'margin-top:16px; padding-top:16px; border-top:1px solid var(--border); font-size:13px; color:var(--text-secondary); line-height:1.6;';
            summaryText.textContent = results.summary;
            summary.parentElement.appendChild(summaryText);
        }

        // Findings list
        const list = document.getElementById('findings-list');
        list.innerHTML = '';

        if (results.findings.length === 0) {
            list.innerHTML = `
                <div style="text-align:center; padding:40px; color:var(--text-dim);">
                    <div style="font-size:32px; margin-bottom:12px;">✨</div>
                    <div style="font-family:var(--font-heading); font-size:18px;">No issues found!</div>
                    <div style="font-size:13px; margin-top:8px;">Your code looks clean.</div>
                </div>
            `;
            document.getElementById('btn-apply-fixes').style.display = 'none';
            return;
        }

        // Count fixable
        const fixable = results.findings.filter(f => f.fixable);

        // Select actions
        const selectActions = document.getElementById('results-select-actions');
        if (fixable.length > 0) {
            selectActions.innerHTML = `
                <button class="btn btn-secondary" style="font-size:12px; padding:6px 14px;" onclick="ResultsView.selectAllFixes()">Select All Fixes</button>
                <button class="btn btn-secondary" style="font-size:12px; padding:6px 14px;" onclick="ResultsView.deselectAllFixes()">Deselect All</button>
            `;
            document.getElementById('btn-apply-fixes').style.display = '';
        } else {
            selectActions.innerHTML = '';
            document.getElementById('btn-apply-fixes').style.display = 'none';
        }

        // Render findings
        for (const f of results.findings) {
            const card = document.createElement('div');
            card.className = 'finding-card fade-in-up';
            card.style.animationDelay = `${results.findings.indexOf(f) * 50}ms`;

            const bodyLines = (f.body || '').split('\n').slice(0, 4).join('\n');

            let fixHtml = '';
            if (f.fixable) {
                fixHtml = `
                    <label style="display:flex; align-items:center; gap:6px; margin-top:10px; cursor:pointer; font-size:12px; color:var(--success);">
                        <input type="checkbox" class="tree-checkbox fix-checkbox"
                               data-index="${f.index}"
                               ${this.selectedFixes.has(f.index) ? 'checked' : ''}
                               onchange="ResultsView.toggleFix(${f.index}, this.checked)">
                        Include fix
                    </label>
                `;
            }

            card.innerHTML = `
                <div class="finding-header">
                    <span class="severity-badge ${f.severity}">${f.severity}</span>
                    <span class="finding-file">${f.file}:${f.line}</span>
                    ${f.fixable ? '<span class="fix-badge">FIX</span>' : ''}
                    ${f.category ? `<span style="font-size:11px; color:var(--text-dim);">${f.category}</span>` : ''}
                </div>
                <div class="finding-body">${this._escapeHtml(bodyLines)}</div>
                ${fixHtml}
            `;

            list.appendChild(card);
        }
    },

    toggleFix(index, checked) {
        if (checked) this.selectedFixes.add(index);
        else this.selectedFixes.delete(index);
        this._updateApplyButton();
    },

    selectAllFixes() {
        if (!this.results) return;
        for (const f of this.results.findings) {
            if (f.fixable) this.selectedFixes.add(f.index);
        }
        document.querySelectorAll('.fix-checkbox').forEach(cb => cb.checked = true);
        this._updateApplyButton();
    },

    deselectAllFixes() {
        this.selectedFixes.clear();
        document.querySelectorAll('.fix-checkbox').forEach(cb => cb.checked = false);
        this._updateApplyButton();
    },

    _updateApplyButton() {
        const btn = document.getElementById('btn-apply-fixes');
        if (this.selectedFixes.size > 0) {
            btn.textContent = `Apply ${this.selectedFixes.size} Fix(es)`;
            btn.style.display = '';
        } else {
            btn.style.display = 'none';
        }
    },

    getSelectedIndices() {
        return Array.from(this.selectedFixes);
    },

    _formatTime(seconds) {
        const s = Math.round(seconds);
        if (s < 60) return `${s}s`;
        return `${Math.floor(s / 60)}m ${s % 60}s`;
    },

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
};
