const sections = [
    {
        key: 'job_title',
        title: 'Job Title',
        description: 'Alert only when the title matches one of these rules.',
    },
    {
        key: 'blacklist_job_title',
        title: 'Exclude Job Titles',
        description: 'Exclude jobs if the title matches any of these rules.',
    },
    {
        key: 'company_name',
        title: 'Company Name',
        description: 'Alert only when the company name matches one of these rules.',
    },
    {
        key: 'blacklist_company_name',
        title: 'Exclude Companies',
        description: 'Exclude jobs if the company name matches any of these rules.',
    },
    {
        key: 'location',
        title: 'Location',
        description: 'Alert only when the location matches one of these rules.',
    },
    {
        key: 'blacklist_location',
        title: 'Exclude Locations',
        description: 'Exclude jobs if the location matches any of these rules.',
    },
];

const matchLabels = {
    includes: 'Includes',
    starts_with: 'Starts with',
    ends_with: 'Ends with',
};

const RULES_PER_PAGE = 10;

// State management for pagination and search
const sectionState = {};

function initSectionState(sectionKey) {
    if (!sectionState[sectionKey]) {
        sectionState[sectionKey] = {
            currentPage: 1,
            searchQuery: '',
            rules: [],
        };
    }
}

function isRuleDuplicate(value, match, caseSensitive, existingRules, currentIndex) {
    return existingRules.some((rule, idx) => {
        // Don't compare rule with itself
        if (idx === currentIndex) return false;
        return rule.value === value &&
               rule.match === match &&
               rule.case_sensitive === caseSensitive;
    });
}

function createRuleRow(sectionKey, rule, index, sectionData, container) {
    const row = document.createElement('div');
    row.className = 'rule-row';
    row.dataset.section = sectionKey;
    row.dataset.index = index;

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'rule-value';
    nameInput.placeholder = 'Match text...';
    nameInput.value = rule.value || '';

    const matchSelect = document.createElement('select');
    matchSelect.className = 'rule-match';
    Object.entries(matchLabels).forEach(([value, label]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        if (rule.match === value) {
            option.selected = true;
        }
        matchSelect.appendChild(option);
    });

    const caseWrapper = document.createElement('label');
    caseWrapper.className = 'toggle-label';
    const caseToggle = document.createElement('input');
    caseToggle.type = 'checkbox';
    caseToggle.className = 'rule-case';
    caseToggle.checked = Boolean(rule.case_sensitive);
    caseWrapper.appendChild(caseToggle);
    caseWrapper.appendChild(document.createTextNode('Case sensitive'));

    const removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.className = 'btn btn-secondary';
    removeButton.textContent = 'Remove';
    removeButton.addEventListener('click', () => {
        // Remove from both DOM and in-memory array
        const ruleIndex = sectionData.rules.findIndex(r =>
            r.value === rule.value &&
            r.match === rule.match &&
            r.case_sensitive === rule.case_sensitive
        );
        if (ruleIndex > -1) {
            sectionData.rules.splice(ruleIndex, 1);
        }
        row.remove();
        // Re-render to update pagination and counters
        renderRulesPage(sectionKey, sectionData, container);
    });
    
    // Add change listener to detect duplicates
    const updateDuplicateState = () => {
        const value = nameInput.value;
        const match = matchSelect.value;
        const caseSensitive = caseToggle.checked;
        
        if (value && isRuleDuplicate(value, match, caseSensitive, sectionData.rules, index)) {
            nameInput.style.borderColor = '#dc3545';
            nameInput.style.backgroundColor = '#fff5f5';
            nameInput.title = 'This rule already exists';
        } else {
            nameInput.style.borderColor = '';
            nameInput.style.backgroundColor = '';
            nameInput.title = '';
        }
    };
    
    nameInput.addEventListener('change', updateDuplicateState);
    nameInput.addEventListener('input', updateDuplicateState);
    matchSelect.addEventListener('change', updateDuplicateState);
    caseToggle.addEventListener('change', updateDuplicateState);

    row.appendChild(nameInput);
    row.appendChild(matchSelect);
    row.appendChild(caseWrapper);
    row.appendChild(removeButton);
    return row;
}

function filterRules(rules, searchQuery) {
    if (!searchQuery.trim()) {
        return rules;
    }
    
    const query = searchQuery.toLowerCase();
    return rules.filter(rule => 
        (rule.value || '').toLowerCase().includes(query)
    );
}

function renderRulesPage(sectionKey, sectionData, container) {
    initSectionState(sectionKey);
    const state = sectionState[sectionKey];
    state.rules = sectionData.rules || [];

    // Get filtered rules WITH their original indices
    const filteredRulesWithIndices = [];
    state.rules.forEach((rule, actualIdx) => {
        // Check if this rule matches the search query
        if (filterRules([rule], state.searchQuery).length > 0) {
            filteredRulesWithIndices.push({ rule, actualIdx });
        }
    });
    
    const totalRules = filteredRulesWithIndices.length;
    const totalPages = Math.max(1, Math.ceil(totalRules / RULES_PER_PAGE));

    // Clamp current page
    if (state.currentPage > totalPages) {
        state.currentPage = totalPages;
    }

    const startIdx = (state.currentPage - 1) * RULES_PER_PAGE;
    const endIdx = startIdx + RULES_PER_PAGE;
    const paginatedRulesWithIndices = filteredRulesWithIndices.slice(startIdx, endIdx);

    // Clear and rebuild the rules list
    const rulesList = container.querySelector('.rules-list');
    rulesList.innerHTML = '';

    if (paginatedRulesWithIndices.length === 0 && state.rules.length === 0) {
        // No rules at all
        const emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        emptyState.innerHTML = `
            <div class="empty-state-icon">📋</div>
            <p>No rules added yet. Add your first rule below!</p>
        `;
        rulesList.appendChild(emptyState);
    } else if (paginatedRulesWithIndices.length === 0) {
        // No search results
        const emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        emptyState.innerHTML = `
            <div class="empty-state-icon">🔍</div>
            <p>No rules match your search.</p>
        `;
        rulesList.appendChild(emptyState);
    } else {
        // Show paginated rules - use ACTUAL indices, not filtered indices
        paginatedRulesWithIndices.forEach(({ rule, actualIdx }) => {
            rulesList.appendChild(createRuleRow(sectionKey, rule, actualIdx, sectionData, container));
        });
    }

    // Update counter
    const counter = container.querySelector('.rules-counter strong');
    counter.textContent = `${totalRules} ${totalRules === 1 ? 'rule' : 'rules'}`;

    // Update pagination
    updatePaginationControls(sectionKey, state, totalPages, totalRules, container);
}

function updatePaginationControls(sectionKey, state, totalPages, totalRules, container) {
    const paginationInfo = container.querySelector('.pagination-info');
    const paginationButtons = container.querySelector('.pagination-buttons');

    // Update info text
    if (totalRules === 0) {
        paginationInfo.innerHTML = 'No rules';
    } else {
        const start = (state.currentPage - 1) * RULES_PER_PAGE + 1;
        const end = Math.min(state.currentPage * RULES_PER_PAGE, totalRules);
        paginationInfo.innerHTML = `
            Showing <strong>${start}</strong> to <strong>${end}</strong> of <strong>${totalRules}</strong> rules
        `;
    }

    // Update buttons
    const prevBtn = paginationButtons.querySelector('[data-action="prev"]');
    const nextBtn = paginationButtons.querySelector('[data-action="next"]');
    const pageIndicator = paginationButtons.querySelector('.page-indicator');

    prevBtn.disabled = state.currentPage === 1;
    nextBtn.disabled = state.currentPage >= totalPages;
    pageIndicator.textContent = `${state.currentPage} / ${totalPages}`;
}

function renderSection(sectionKey, sectionData) {
    const container = document.createElement('div');
    container.className = 'rules-section disable-select';
    container.dataset.section = sectionKey;

    const section = sections.find((item) => item.key === sectionKey);
    const title = section ? section.title : sectionKey;
    const description = section ? section.description : '';

    // Header with enable toggle
    const header = document.createElement('div');
    header.className = 'section-header';
    
    const headerInfo = document.createElement('div');
    headerInfo.innerHTML = `
        <h2>${title}</h2>
        <p>${description}</p>
    `;

    const toggle = document.createElement('label');
    toggle.className = 'toggle-label section-toggle';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'section-enable';
    checkbox.dataset.section = sectionKey;
    checkbox.checked = Boolean(sectionData.enabled);
    checkbox.addEventListener('change', () => {
        container.classList.toggle('disabled', !checkbox.checked);
    });
    toggle.appendChild(checkbox);
    toggle.appendChild(document.createTextNode(`Enable`));

    header.appendChild(headerInfo);
    header.appendChild(toggle);
    container.appendChild(header);

    // Search and controls
    const controls = document.createElement('div');
    controls.className = 'rules-controls';

    const searchWrapper = document.createElement('div');
    searchWrapper.className = 'search-input-wrapper';
    const searchIcon = document.createElement('span');
    searchIcon.className = 'search-icon';
    searchIcon.textContent = '🔍';
    const searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.className = 'rules-search';
    searchInput.placeholder = 'Search rules...';
    searchInput.dataset.section = sectionKey;
    searchWrapper.appendChild(searchIcon);
    searchWrapper.appendChild(searchInput);

    const counter = document.createElement('div');
    counter.className = 'rules-counter';
    counter.innerHTML = `📊 <strong>0</strong> rules`;

    controls.appendChild(searchWrapper);
    controls.appendChild(counter);
    container.appendChild(controls);

    // Rules list container
    const listContainer = document.createElement('div');
    listContainer.className = 'rules-list-container';
    const rulesList = document.createElement('div');
    rulesList.className = 'rules-list';
    rulesList.id = `${sectionKey}-rules`;
    listContainer.appendChild(rulesList);
    container.appendChild(listContainer);

    // Pagination controls
    const pagination = document.createElement('div');
    pagination.className = 'pagination-controls';
    pagination.innerHTML = `
        <div class="pagination-info">Showing 0 rules</div>
        <div class="pagination-buttons">
            <button type="button" class="btn-pagination" data-action="prev">Previous</button>
            <span class="page-indicator">1 / 1</span>
            <button type="button" class="btn-pagination" data-action="next">Next</button>
        </div>
    `;
    
    // Pagination event listeners
    pagination.querySelector('[data-action="prev"]').addEventListener('click', () => {
        const state = sectionState[sectionKey];
        if (state.currentPage > 1) {
            state.currentPage--;
            renderRulesPage(sectionKey, sectionData, container);
        }
    });

    pagination.querySelector('[data-action="next"]').addEventListener('click', () => {
        const state = sectionState[sectionKey];
        // Calculate total pages based on filtered results
        const filteredCount = state.rules.filter(rule => 
            filterRules([rule], state.searchQuery).length > 0
        ).length;
        const totalPages = Math.max(1, Math.ceil(filteredCount / RULES_PER_PAGE));
        if (state.currentPage < totalPages) {
            state.currentPage++;
            renderRulesPage(sectionKey, sectionData, container);
        }
    });

    container.appendChild(pagination);

    // Add rule button
    const addButtonContainer = document.createElement('div');
    addButtonContainer.className = 'add-rule-button-container';
    const addButton = document.createElement('button');
    addButton.type = 'button';
    addButton.className = 'btn btn-primary btn-add-rule';
    addButton.innerHTML = `<i class="fa-solid fa-plus"></i> Add ${title} Rule`;
    addButton.addEventListener('click', () => {
        // Sync current input values back to sectionData before re-rendering
        const ruleRows = container.querySelectorAll('.rule-row');
        ruleRows.forEach((row) => {
            const index = parseInt(row.dataset.index, 10);
            if (index >= 0 && index < sectionData.rules.length) {
                sectionData.rules[index].value = row.querySelector('.rule-value').value;
                sectionData.rules[index].match = row.querySelector('.rule-match').value;
                sectionData.rules[index].case_sensitive = row.querySelector('.rule-case').checked;
            }
        });
        
        // Now add new empty rule
        sectionData.rules.push({ value: '', match: 'includes', case_sensitive: false });
        
        // Reset to last page to see new rule (accounting for current search filter)
        const state = sectionState[sectionKey];
        const filteredCount = sectionData.rules.filter(rule => 
            filterRules([rule], state.searchQuery || '').length > 0
        ).length;
        const totalPages = Math.ceil(filteredCount / RULES_PER_PAGE);
        state.currentPage = totalPages;
        renderRulesPage(sectionKey, sectionData, container);
    });
    addButtonContainer.appendChild(addButton);
    container.appendChild(addButtonContainer);

    // Search event listener
    searchInput.addEventListener('input', () => {
        sectionState[sectionKey].searchQuery = searchInput.value;
        sectionState[sectionKey].currentPage = 1; // Reset to first page
        renderRulesPage(sectionKey, sectionData, container);
    });

    // Initial render
    renderRulesPage(sectionKey, sectionData, container);

    // Set disabled state
    container.classList.toggle('disabled', !sectionData.enabled);

    return container;
}

function normalizeConfigValue(value) {
    return String(value || '').trim();
}

function gatherConfig() {
    const configEnabled = document.getElementById('config-enabled').checked;
    const config = {
        enabled: configEnabled,
    };

    sections.forEach((section) => {
        const sectionKey = section.key;
        const sectionData = window.notificationConfig[sectionKey] || { enabled: false, rules: [] };
        
        // Gather rules from in-memory state (not from DOM)
        // This ensures we get ALL rules from ALL pages, not just visible ones
        const rules = (sectionData.rules || [])
            .map(rule => ({
                value: normalizeConfigValue(rule.value),
                match: rule.match,
                case_sensitive: rule.case_sensitive
            }))
            .filter(rule => rule.value) // Only include rules with non-empty values
            .filter((rule, idx, arr) => {
                // Remove duplicates: keep only first occurrence
                const firstIdx = arr.findIndex(r =>
                    r.value === rule.value &&
                    r.match === rule.match &&
                    r.case_sensitive === rule.case_sensitive
                );
                return idx === firstIdx;
            });

        // Get enabled state from checkbox
        const sectionContainer = document.querySelector(`.rules-section[data-section="${sectionKey}"]`);
        const enabledCheckbox = sectionContainer.querySelector('.section-enable');

        config[sectionKey] = {
            enabled: enabledCheckbox.checked,
            rules,
        };
    });

    return config;
}

function renderForm() {
    const sectionContainer = document.getElementById('section-container');
    sectionContainer.innerHTML = '';

    sections.forEach((section) => {
        const sectionKey = section.key;
        const sectionData = window.notificationConfig[sectionKey] || { enabled: false, rules: [] };
        sectionContainer.appendChild(renderSection(sectionKey, sectionData));
    });

    document.getElementById('config-enabled').checked = Boolean(window.notificationConfig.enabled);
}

function showToast(message, type = 'info', duration = 3000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 10000;
            pointer-events: none;
        `;
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.style.cssText = `
        padding: 12px 16px;
        margin-bottom: 8px;
        border-radius: 6px;
        font-size: 0.9rem;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
        animation: slideIn 0.3s ease-out;
        pointer-events: auto;
    `;

    if (type === 'success') {
        toast.style.backgroundColor = '#d4edda';
        toast.style.color = '#155724';
        toast.style.borderLeft = '4px solid #28a745';
        toast.innerHTML = `✓ ${message}`;
    } else if (type === 'error') {
        toast.style.backgroundColor = '#f8d7da';
        toast.style.color = '#721c24';
        toast.style.borderLeft = '4px solid #dc3545';
        toast.innerHTML = `✕ ${message}`;
    } else {
        toast.style.backgroundColor = '#d1ecf1';
        toast.style.color = '#0c5460';
        toast.style.borderLeft = '4px solid #17a2b8';
        toast.innerHTML = `ℹ ${message}`;
    }

    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideOut 0.3s ease-out';
        setTimeout(() => {
            toast.remove();
        }, 300);
    }, duration);
}

function setupForm() {
    const form = document.getElementById('notify-form');
    
    // Add CSS animations
    if (!document.getElementById('toast-animations')) {
        const style = document.createElement('style');
        style.id = 'toast-animations';
        style.textContent = `
            @keyframes slideIn {
                from {
                    transform: translateX(400px);
                    opacity: 0;
                }
                to {
                    transform: translateX(0);
                    opacity: 1;
                }
            }
            @keyframes slideOut {
                from {
                    transform: translateX(0);
                    opacity: 1;
                }
                to {
                    transform: translateX(400px);
                    opacity: 0;
                }
            }
        `;
        document.head.appendChild(style);
    }
    
    form.addEventListener('submit', (event) => {
        event.preventDefault();
        
        // Sync all current input values from all sections back to sectionData before gathering config
        document.querySelectorAll('.rules-section').forEach((sectionContainer) => {
            const sectionKey = sectionContainer.dataset.section;
            const sectionData = window.notificationConfig[sectionKey];
            if (sectionData) {
                const ruleRows = sectionContainer.querySelectorAll('.rule-row');
                ruleRows.forEach((row) => {
                    const index = parseInt(row.dataset.index, 10);
                    // Ensure index is valid and rule exists
                    if (index >= 0 && index < sectionData.rules.length) {
                        const value = row.querySelector('.rule-value').value;
                        const match = row.querySelector('.rule-match').value;
                        const caseSensitive = row.querySelector('.rule-case').checked;
                        
                        // Update the rule if it exists
                        if (sectionData.rules[index]) {
                            sectionData.rules[index].value = value;
                            sectionData.rules[index].match = match;
                            sectionData.rules[index].case_sensitive = caseSensitive;
                        }
                    }
                });
                
                // Clean up: remove any undefined rules
                sectionData.rules = sectionData.rules.filter(rule => rule !== undefined && rule !== null);
            }
        });
        
        const config = gatherConfig();
        const payload = JSON.stringify(config);
        
        // Show loading state
        const submitButton = form.querySelector('button[type="submit"]');
        const originalText = submitButton.textContent;
        submitButton.disabled = true;
        submitButton.textContent = 'Saving...';
        
        fetch('/notify', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: payload
        })
        .then(response => {
            if (!response.ok) {
                throw new Error('HTTP ' + response.status + ': ' + response.statusText);
            }
            return response.json();
        })
        .then(data => {
            if (data.status === 'success') {
                showToast('✓ Rules saved successfully!', 'success', 3000);
            } else {
                showToast('✕ Failed to save rules: ' + (data.message || 'Unknown error'), 'error', 5000);
            }
        })
        .catch(error => {
            console.error('Error saving rules:', error);
            showToast('✕ Error saving rules: ' + error.message, 'error', 5000);
        })
        .finally(() => {
            submitButton.disabled = false;
            submitButton.textContent = originalText;
        });
    });
}

window.addEventListener('DOMContentLoaded', () => {
    if (!window.notificationConfig) {
        window.notificationConfig = { 
            enabled: false, 
            job_title: { enabled: false, rules: [] }, 
            blacklist_job_title: { enabled: false, rules: [] },
            company_name: { enabled: false, rules: [] }, 
            blacklist_company_name: { enabled: false, rules: [] },
            location: { enabled: false, rules: [] }, 
            blacklist_location: { enabled: false, rules: [] } 
        };
    }
    renderForm();
    setupForm();
});
