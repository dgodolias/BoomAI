/**
 * File tree rendering with checkboxes.
 */
const FileBrowser = {
    tree: {},
    allFiles: [],
    reviewableFiles: new Set(),
    selectedFiles: new Set(),

    render(treeData) {
        this.tree = treeData.tree || {};
        this.allFiles = [];
        this.reviewableFiles = new Set();
        this.selectedFiles = new Set();

        this._collectFiles(this.tree, '');

        // Auto-select all reviewable files
        for (const f of this.allFiles) {
            if (this.reviewableFiles.has(f)) {
                this.selectedFiles.add(f);
            }
        }

        const container = document.getElementById('file-tree');
        container.innerHTML = '';
        container.appendChild(this._buildNode(this.tree, ''));
        this.updateCount();
    },

    _collectFiles(node, prefix) {
        for (const [name, value] of Object.entries(node)) {
            if (value && value.__file) {
                const path = prefix ? `${prefix}/${name}` : name;
                this.allFiles.push(path);
                if (value.__reviewable) this.reviewableFiles.add(path);
            } else if (typeof value === 'object' && !value.__file) {
                this._collectFiles(value, prefix ? `${prefix}/${name}` : name);
            }
        }
    },

    _buildNode(node, prefix) {
        const fragment = document.createDocumentFragment();

        // Sort: dirs first, then files
        const entries = Object.entries(node).sort(([a, va], [b, vb]) => {
            const aDir = va && !va.__file;
            const bDir = vb && !vb.__file;
            if (aDir && !bDir) return -1;
            if (!aDir && bDir) return 1;
            return a.localeCompare(b);
        });

        for (const [name, value] of entries) {
            if (value && value.__file) {
                const path = prefix ? `${prefix}/${name}` : name;
                const isReviewable = value.__reviewable;
                const fileEl = document.createElement('div');
                fileEl.className = `tree-file ${isReviewable ? '' : 'non-reviewable'}`;

                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.className = 'tree-checkbox';
                cb.checked = this.selectedFiles.has(path);
                cb.disabled = !isReviewable;
                cb.addEventListener('change', () => {
                    if (cb.checked) this.selectedFiles.add(path);
                    else this.selectedFiles.delete(path);
                    this.updateCount();
                });

                const icon = document.createElement('span');
                icon.className = 'tree-icon';
                icon.textContent = name.endsWith('.cs') ? '💎' : '📄';

                const nameEl = document.createElement('span');
                nameEl.className = 'tree-name';
                nameEl.textContent = name;

                fileEl.append(cb, icon, nameEl);
                fragment.appendChild(fileEl);
            } else if (typeof value === 'object' && !value.__file) {
                const dirPath = prefix ? `${prefix}/${name}` : name;

                const wrapper = document.createElement('div');
                wrapper.className = 'tree-node';

                const dirEl = document.createElement('div');
                dirEl.className = 'tree-dir';
                dirEl.innerHTML = `
                    <span class="tree-chevron">▶</span>
                    <span class="tree-icon">📁</span>
                    <span class="tree-name">${name}</span>
                `;
                dirEl.addEventListener('click', () => {
                    dirEl.classList.toggle('open');
                });

                const children = document.createElement('div');
                children.className = 'tree-children';
                children.appendChild(this._buildNode(value, dirPath));

                wrapper.append(dirEl, children);
                fragment.appendChild(wrapper);
            }
        }

        return fragment;
    },

    updateCount() {
        const label = document.getElementById('file-count-label');
        label.textContent = `${this.selectedFiles.size} of ${this.reviewableFiles.size} reviewable files selected`;
    },

    selectAll() {
        for (const f of this.allFiles) {
            if (this.reviewableFiles.has(f)) this.selectedFiles.add(f);
        }
        this._refreshCheckboxes(true);
    },

    selectNone() {
        this.selectedFiles.clear();
        this._refreshCheckboxes(false);
    },

    selectReviewable() {
        this.selectedFiles.clear();
        for (const f of this.reviewableFiles) {
            this.selectedFiles.add(f);
        }
        this._refreshCheckboxes(null);
    },

    _refreshCheckboxes(forceValue) {
        const checkboxes = document.querySelectorAll('#file-tree .tree-checkbox');
        checkboxes.forEach(cb => {
            if (!cb.disabled) {
                // Find the file path from the checkbox context
                const fileEl = cb.closest('.tree-file');
                if (fileEl) {
                    const name = fileEl.querySelector('.tree-name').textContent;
                    // Rebuild path... simpler to just re-render
                }
            }
        });
        // Simpler: re-render the tree
        const container = document.getElementById('file-tree');
        container.innerHTML = '';
        container.appendChild(this._buildNode(this.tree, ''));
        this.updateCount();
    },

    getSelectedFiles() {
        return Array.from(this.selectedFiles);
    },
};
