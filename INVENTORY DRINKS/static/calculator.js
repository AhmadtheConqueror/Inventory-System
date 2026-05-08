function _getDisplayElement(id) {
    if (id) return document.getElementById(id) || null;
    return document.getElementById('widget-display') || document.getElementById('calc-display') || null;
}

function appendValue(value, displayId) {
    const el = _getDisplayElement(displayId);
    if (!el) return;
    el.value = (el.value || '') + value;
}

function clearDisplay(displayId) {
    const el = _getDisplayElement(displayId);
    if (!el) return;
    el.value = '';
}

function deleteLast(displayId) {
    const el = _getDisplayElement(displayId);
    if (!el) return;
    el.value = el.value.slice(0, -1);
}

function calculateResult(displayId) {
    const el = _getDisplayElement(displayId);
    if (!el) return;
    try {
        if (!el.value) {
            el.value = '';
            return;
        }
        // replace any unicode operators just in case
        let expr = el.value.replace(/÷/g, '/').replace(/×/g, '*').replace(/−/g, '-');
        // Evaluate expression. Keep simple eval for local calculator; guard undefined/null
        const result = eval(expr);
        el.value = (result === undefined || result === null) ? '' : String(result);
    } catch (e) {
        el.value = 'Error';
    }
}

function toggleCalculator() {
    const floating = document.getElementById('floating-calculator');
    const widget = document.getElementById('calculator-widget');
    if (floating) {
        floating.classList.toggle('hidden');
    }
    if (widget) {
        widget.classList.toggle('hidden');
    }
}

// Wire up buttons for the non-inline version (calculator.html)
document.addEventListener('DOMContentLoaded', function () {
    // Open/close
    const openBtn = document.getElementById('open-calculator') || document.getElementById('calc-toggle-btn');
    if (openBtn) openBtn.addEventListener('click', toggleCalculator);
    const closeBtn = document.getElementById('close-calculator');
    if (closeBtn) closeBtn.addEventListener('click', toggleCalculator);

    // Buttons inside .calc-buttons (calculator.html)
    const btns = document.querySelectorAll('.calc-buttons .btn');
    btns.forEach(btn => {
        btn.addEventListener('click', function () {
            const id = btn.id;
            const txt = btn.textContent.trim();
            if (id === 'calc-clear') {
                clearDisplay();
                return;
            }
            if (id === 'calc-equal') {
                calculateResult();
                return;
            }
            if (btn.classList.contains('op')) {
                // map symbols
                const map = { '÷': '/', '×': '*', '−': '-', '+': '+' };
                appendValue(map[txt] || txt);
                return;
            }
            // default: digit or dot
            appendValue(txt);
        });
    });
});
