/**
 * Scan progress polling and character animation control.
 */
const ScanView = {
    _polling: false,
    _totalPapers: 5,

    startPolling() {
        this._polling = true;
        this._poll();
    },

    stopPolling() {
        this._polling = false;
    },

    async _poll() {
        if (!this._polling) return;

        try {
            const status = await API.getScanStatus();
            this._updateUI(status);

            if (status.state === 'done') {
                this._polling = false;
                this._onComplete();
                return;
            }

            if (status.state === 'error') {
                this._polling = false;
                this._onError(status.error);
                return;
            }
        } catch (e) {
            console.error('Poll error:', e);
        }

        if (this._polling) {
            setTimeout(() => this._poll(), 600);
        }
    },

    _updateUI(status) {
        // Progress bar
        const pct = Math.round(status.progress * 100);
        document.getElementById('scan-progress-fill').style.width = `${pct}%`;
        document.getElementById('scan-percent').textContent = `${pct}%`;

        // Stage label — use estimated_files for smooth display
        if (status.stage) {
            let stage = status.stage;
            if (status.estimated_files > 0 && status.total_files > 0 && stage.includes('Reviewing')) {
                stage = `Reviewing files (est. ${status.estimated_files}/${status.total_files})...`;
            }
            document.getElementById('scan-stage').textContent = stage;
        }

        // Log messages
        if (status.messages && status.messages.length > 0) {
            const log = document.getElementById('scan-log');
            for (const msg of status.messages) {
                const entry = document.createElement('div');
                entry.className = 'scan-log-entry';
                entry.textContent = msg;
                log.appendChild(entry);
            }
            log.scrollTop = log.scrollHeight;
        }

        // Paper stacks animation
        this._updatePaperStacks(status.progress);
    },

    _updatePaperStacks(progress) {
        const inbox = document.getElementById('stack-inbox');
        const done = document.getElementById('stack-done');

        // Calculate how many papers in each stack
        const totalPapers = this._totalPapers;
        const donePapers = Math.round(progress * totalPapers);
        const inboxPapers = totalPapers - donePapers;

        // Rebuild inbox stack
        const inboxLabel = inbox.querySelector('.stack-label');
        inbox.innerHTML = '';
        for (let i = 0; i < inboxPapers; i++) {
            const paper = document.createElement('div');
            paper.className = 'paper';
            inbox.appendChild(paper);
        }
        inbox.appendChild(inboxLabel || (() => {
            const l = document.createElement('div');
            l.className = 'stack-label';
            l.textContent = 'To Review';
            return l;
        })());

        // Rebuild done stack
        const doneLabel = done.querySelector('.stack-label');
        done.innerHTML = '';
        for (let i = 0; i < donePapers; i++) {
            const paper = document.createElement('div');
            paper.className = 'paper';
            paper.style.background = '#E8F5E9';
            paper.style.borderColor = '#81C784';
            done.appendChild(paper);
        }
        done.appendChild(doneLabel || (() => {
            const l = document.createElement('div');
            l.className = 'stack-label';
            l.textContent = 'Done';
            return l;
        })());
    },

    reset() {
        document.getElementById('scan-progress-fill').style.width = '0%';
        document.getElementById('scan-percent').textContent = '0%';
        document.getElementById('scan-stage').textContent = 'Initializing...';
        document.getElementById('scan-log').innerHTML = '';
        this._updatePaperStacks(0);
    },

    async _onComplete() {
        document.getElementById('scan-percent').textContent = '100%';
        document.getElementById('scan-progress-fill').style.width = '100%';
        document.getElementById('scan-stage').textContent = 'Complete!';

        // Character celebration
        const charImg = document.querySelector('.character-img');
        if (charImg) charImg.classList.add('celebrate');

        // Show results after brief delay
        setTimeout(async () => {
            try {
                const results = await API.getScanResults();
                if (results && !results.error) {
                    ResultsView.show(results);
                    App.goTo('results');
                } else {
                    document.getElementById('scan-stage').textContent =
                        `Error loading results: ${results?.error || 'unknown'}`;
                }
            } catch (e) {
                document.getElementById('scan-stage').textContent = `Error: ${e.message}`;
            }
        }, 1200);
    },

    _onError(error) {
        document.getElementById('scan-stage').textContent = `Error: ${error || 'Unknown error'}`;
        document.getElementById('scan-stage').style.color = 'var(--error)';
    },
};
