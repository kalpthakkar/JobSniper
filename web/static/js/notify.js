const sections = [
    {
        key: 'job_title',
        title: 'Job Title',
        description: 'Alert only when the title matches one of these rules.',
    },
    {
        key: 'company_name',
        title: 'Company Name',
        description: 'Alert only when the company name matches one of these rules.',
    },
    {
        key: 'location',
        title: 'Location',
        description: 'Alert only when the location matches one of these rules.',
    },
    {
        key: 'blacklist',
        title: 'Blacklist',
        description: 'Exclude matching jobs from notifications even when they pass other filters.',
    },
];

const matchLabels = {
    includes: 'Includes',
    starts_with: 'Starts with',
    ends_with: 'Ends with',
};

function createRuleRow(sectionKey, rule, index) {
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
        row.remove();
    });

    row.appendChild(nameInput);
    row.appendChild(matchSelect);
    row.appendChild(caseWrapper);
    row.appendChild(removeButton);
    return row;
}

function renderSection(sectionKey, sectionData) {
    const container = document.createElement('div');
    container.className = 'section-card';

    const section = sections.find((item) => item.key === sectionKey);
    const title = section ? section.title : sectionKey;
    const description = section ? section.description : '';

    const header = document.createElement('div');
    header.className = 'section-header';
    header.innerHTML = `
        <div>
            <h2>${title}</h2>
            <p>${description}</p>
        </div>
    `;

    const toggle = document.createElement('label');
    toggle.className = 'toggle-label';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'section-enable';
    checkbox.dataset.section = sectionKey;
    checkbox.checked = Boolean(sectionData.enabled);
    checkbox.addEventListener('change', () => {
        container.classList.toggle('disabled', !checkbox.checked);
    });
    toggle.appendChild(checkbox);
    toggle.appendChild(document.createTextNode(`Enable ${title}`));
    header.appendChild(toggle);
    container.appendChild(header);

    const rulesList = document.createElement('div');
    rulesList.className = 'rules-list';
    rulesList.id = `${sectionKey}-rules`;
    sectionData.rules.forEach((rule, index) => {
        rulesList.appendChild(createRuleRow(sectionKey, rule, index));
    });
    if (sectionData.rules.length === 0) {
        rulesList.appendChild(createRuleRow(sectionKey, { value: '', match: 'includes', case_sensitive: false }, 0));
    }
    container.appendChild(rulesList);

    const footer = document.createElement('div');
    footer.className = 'section-footer';
    const addButton = document.createElement('button');
    addButton.type = 'button';
    addButton.className = 'btn btn-secondary';
    addButton.textContent = `Add ${title} Rule`;
    addButton.addEventListener('click', () => {
        const nextIndex = rulesList.children.length;
        rulesList.appendChild(createRuleRow(sectionKey, { value: '', match: 'includes', case_sensitive: false }, nextIndex));
    });
    footer.appendChild(addButton);
    container.appendChild(footer);

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
        const sectionCard = document.querySelector(`.section-card:nth-of-type(${sections.indexOf(section) + 1})`);
        const enabledCheckbox = sectionCard.querySelector('.section-enable');
        const ruleRows = sectionCard.querySelectorAll('.rule-row');
        const rules = [];

        ruleRows.forEach((row) => {
            const value = normalizeConfigValue(row.querySelector('.rule-value').value);
            const match = row.querySelector('.rule-match').value;
            const caseSensitive = row.querySelector('.rule-case').checked;
            if (value) {
                rules.push({ value, match, case_sensitive: caseSensitive });
            }
        });

        config[section.key] = {
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

function setupForm() {
    const form = document.getElementById('notify-form');
    form.addEventListener('submit', (event) => {
        const payload = JSON.stringify(gatherConfig());
        document.getElementById('notification-config').value = payload;
    });
}

window.addEventListener('DOMContentLoaded', () => {
    if (!window.notificationConfig) {
        window.notificationConfig = { enabled: false, job_title: { enabled: false, rules: [] }, company_name: { enabled: false, rules: [] }, location: { enabled: false, rules: [] }, blacklist: { enabled: false, rules: [] } };
    }
    renderForm();
    setupForm();
});
