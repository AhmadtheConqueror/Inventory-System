document.addEventListener("DOMContentLoaded", () => {

    // --- Card Animation ---
    // Stagger the animation for each card
    const cards = document.querySelectorAll('.card');
    cards.forEach((card, index) => {
        card.style.animationDelay = `${index * 0.1}s`;
    });

    // --- Dark Mode Toggle ---
    const darkModeToggle = document.getElementById("darkModeToggle");
    const body = document.body;

    // Apply saved theme on load
    if (localStorage.getItem("theme") === "dark") {
        body.classList.add("dark-mode");
    }

    if(darkModeToggle) {
        darkModeToggle.addEventListener("click", () => {
            body.classList.toggle("dark-mode");
            // Save theme preference
            if (body.classList.contains("dark-mode")) {
                localStorage.setItem("theme", "dark");
            } else {
                localStorage.setItem("theme", "light");
            }
        });
    }

    // --- Customer Type Toggle for Inventory Page ---
    const typeSelect = document.getElementById('customer_type');
    const customerSelectDiv = document.getElementById('customer_select');

    if (typeSelect && customerSelectDiv) {
        typeSelect.addEventListener('change', function () {
            customerSelectDiv.style.display = (this.value === 'registered') ? 'block' : 'none';
        });
        // Initial check in case the page reloads with a selection
        customerSelectDiv.style.display = (typeSelect.value === 'registered') ? 'block' : 'none';
    }

    // --- Receipt Modal Close Function ---
    // Make closeModal globally accessible for the inline onclick attribute
    window.closeModal = function() {
        const receiptModal = document.getElementById("receiptModal");
        if (receiptModal) {
            receiptModal.style.display = "none";
            // Redirect to clean up URL and session data
        window.location.href = '/inventory';
        }
    }
    
    // --- Toast Message Auto-hide ---
    setTimeout(() => {
        const toastMessages = document.querySelectorAll('.toast');
        toastMessages.forEach(toast => {
            toast.style.transition = 'opacity 0.5s ease';
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 500);
        });
    }, 5000); // Hide after 5 seconds
});
// CALCULATOR LOGIC
document.addEventListener("DOMContentLoaded", function () {
  const calc = document.getElementById("calculator-widget");
  const openBtn = document.getElementById("open-calculator");
  const closeBtn = document.getElementById("close-calculator");
  const display = document.getElementById("calc-display");
  const buttons = document.querySelectorAll(".calc-buttons .btn");
  const clearBtn = document.getElementById("calc-clear");
  const equalBtn = document.getElementById("calc-equal");

  let expression = "";

  openBtn.addEventListener("click", () => {
    calc.classList.toggle("hidden");
  });

  closeBtn.addEventListener("click", () => {
    calc.classList.add("hidden");
  });

  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      expression += btn.textContent;
      display.value = expression;
    });
  });

  clearBtn.addEventListener("click", () => {
    expression = "";
    display.value = "";
  });

  equalBtn.addEventListener("click", () => {
    try {
      expression = eval(expression).toString();
      display.value = expression;
    } catch {
      display.value = "Error";
      expression = "";
    }
  });
});

// ----------------- Client-side form validation for inventory -----------------
document.addEventListener('DOMContentLoaded', function () {
  const addToCartForm = document.getElementById('add-to-cart-form');
  const productSelect = document.getElementById('product_select');

  if (addToCartForm && productSelect) {
    addToCartForm.addEventListener('submit', function (e) {
      const qtyInput = addToCartForm.querySelector('input[name="quantity"]');
      const qty = parseFloat(qtyInput.value);
      const selected = productSelect.options[productSelect.selectedIndex];
      const stock = parseFloat(selected.dataset.stock || '0');
      if (isNaN(qty) || qty <= 0) {
        e.preventDefault();
        alert('Please enter a valid quantity greater than 0.');
        return false;
      }
      if (qty > stock) {
        e.preventDefault();
        alert(`Only ${stock} units available for this product.`);
        return false;
      }
    });
  }

  // Update-quantity forms validation
  const updateForms = document.querySelectorAll('.update-qty-form');
  updateForms.forEach(form => {
    form.addEventListener('submit', function (e) {
      const qtyInput = form.querySelector('.update-qty-input');
      const productId = form.querySelector('input[name="product_id"]').value;
      const qty = parseFloat(qtyInput.value);
      if (isNaN(qty) || qty <= 0) {
        e.preventDefault();
        alert('Please enter a valid quantity greater than 0.');
        return false;
      }
      // try to find stock from main product select options
      const opt = productSelect ? Array.from(productSelect.options).find(o => o.value === productId) : null;
      const stock = opt ? parseFloat(opt.dataset.stock || '0') : null;
      if (stock !== null && qty > stock) {
        e.preventDefault();
        alert(`Only ${stock} units available for this product.`);
        return false;
      }
    });
  });

  // Finalize form validation
  const finalizeForm = document.getElementById('finalize-form');
  const finalizeBtn = document.getElementById('finalize-btn');
  const finalizeError = document.getElementById('finalize-error');
  if (finalizeForm) {
    const cartTotal = parseFloat(finalizeForm.dataset.cartTotal || '0');
    const paymentRadios = finalizeForm.querySelectorAll('input[name="payment_type"]');
    const amountInput = document.getElementById('amount_paid');
    const customerType = document.getElementById('customer_type');
    const customerId = document.getElementById('customer_id');
    const authorized = finalizeForm.querySelector('[name="authorized_by"]');

    function setInvalid(el, set) {
      if (!el) return;
      if (set) el.classList.add('is-invalid'); else el.classList.remove('is-invalid');
    }

    function validateFinalize() {
      let errors = [];
      // clear previous invalid highlights
      setInvalid(customerId, false);
      setInvalid(amountInput, false);
      setInvalid(authorized, false);

      // customer validation
      if (customerType && customerType.value === 'registered') {
        if (!customerId || !customerId.value) {
          errors.push('Select a registered customer when customer type is Registered.');
          setInvalid(customerId, true);
        }
      }
      // payment validation
      const selectedPayment = Array.from(paymentRadios).find(r => r.checked)?.value || 'full';
      if (selectedPayment === 'partial') {
        const amt = parseFloat(amountInput.value);
        if (isNaN(amt)) { errors.push('Enter a valid partial payment amount.'); setInvalid(amountInput, true); }
        else if (amt < 0) { errors.push('Amount paid cannot be negative.'); setInvalid(amountInput, true); }
        else if (amt > cartTotal) { errors.push('Amount paid cannot exceed cart total.'); setInvalid(amountInput, true); }
      }
      // authorized_by
      if (authorized && (!authorized.value || authorized.value.trim() === '')) { errors.push('Select an authorized person.'); setInvalid(authorized, true); }

      if (errors.length) {
        finalizeBtn.disabled = true;
        finalizeError.style.display = 'block';
        // show all errors as list
        finalizeError.innerHTML = '<div class="field-error"><ul>' + errors.map(e => '<li>'+e+'</li>').join('') + '</ul></div>';
        return false;
      } else {
        finalizeBtn.disabled = false;
        finalizeError.style.display = 'none';
        finalizeError.innerHTML = '';
        return true;
      }
    }

    // Attach listeners
    finalizeForm.addEventListener('change', validateFinalize);
    finalizeForm.addEventListener('input', validateFinalize);
    // run initial validation
    validateFinalize();

    // final check on submit
    finalizeForm.addEventListener('submit', function (e) {
      if (!validateFinalize()) {
        e.preventDefault();
        alert('Finalize form invalid. Please fix the highlighted errors.');
        return false;
      }
    });
  }

});
