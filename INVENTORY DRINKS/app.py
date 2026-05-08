from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify
import csv
from datetime import datetime, date, timedelta
from openpyxl import Workbook
from sqlalchemy import func, text
from xhtml2pdf import pisa
import io, secrets, os, json
from io import BytesIO
from werkzeug.utils import secure_filename
from functools import wraps
import glob
import sqlite3

# Authentication and Security Imports
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import OperationalError as SAOperationalError

# --- Clerk Authentication: JWT verification ---
import jwt as pyjwt
import requests as http_requests
from dotenv import load_dotenv

# Load .env file (Clerk keys live here)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# Make sure the User model is imported from your models file
from models import db, User, Product, Expense, Debt, Customer, Sale, SaleItem, Setting, CashHistory, MonthlyAssetOverride



# --- Always use root folder for all paths ---
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(ROOT_DIR, 'instance')
BACKUPS_DIR = os.path.join(ROOT_DIR, 'backups')
UPLOAD_DIR = os.path.join(ROOT_DIR, 'static', 'profile_pics')
for folder in [INSTANCE_DIR, BACKUPS_DIR, UPLOAD_DIR]:
    if not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

app = Flask(__name__, instance_path=INSTANCE_DIR)
db_file = os.path.join(INSTANCE_DIR, 'database.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_file}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(16))
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
app.config['BACKUP_FOLDER'] = BACKUPS_DIR

# --- Clerk Configuration (loaded from .env) ---
CLERK_PUBLISHABLE_KEY = os.getenv('CLERK_PUBLISHABLE_KEY', '')
CLERK_SECRET_KEY = os.getenv('CLERK_SECRET_KEY', '')
CLERK_FRONTEND_API = os.getenv('CLERK_FRONTEND_API', '')

# --- Gemini Configuration for report insights ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')

# Derive the ClerkJS CDN URL from the frontend API domain
_CLERK_JS_URL = f'{CLERK_FRONTEND_API}/npm/@clerk/clerk-js@latest/dist/clerk.browser.js' if CLERK_FRONTEND_API else ''

# Cache for Clerk's JWKS public keys (avoids repeated network calls)
_clerk_jwks_cache = {}

def verify_clerk_jwt(token: str):
    """Verify a Clerk session JWT and return the decoded payload.
    Returns None if the token is invalid or Clerk is not configured.
    Strategy: fetch JWKS from Clerk and verify signature.
    """
    if not CLERK_SECRET_KEY or not CLERK_FRONTEND_API:
        return None
    try:
        # Decode header to get kid (key id)
        header = pyjwt.get_unverified_header(token)
        kid = header.get('kid')

        # Fetch JWKS if not cached or key not found
        if kid not in _clerk_jwks_cache:
            jwks_url = f'{CLERK_FRONTEND_API}/.well-known/jwks.json'
            resp = http_requests.get(jwks_url, timeout=5)
            resp.raise_for_status()
            jwks = resp.json()
            for key_data in jwks.get('keys', []):
                from jwt.algorithms import RSAAlgorithm
                pub_key = RSAAlgorithm.from_jwk(key_data)
                _clerk_jwks_cache[key_data['kid']] = pub_key

        public_key = _clerk_jwks_cache.get(kid)
        if not public_key:
            return None

        payload = pyjwt.decode(
            token,
            public_key,
            algorithms=['RS256'],
            options={'verify_exp': True}
        )
        return payload
    except Exception:
        return None

db.init_app(app)

# Configure Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'
login_manager.login_message = "Please log in to access this page."

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def get_product_columns():
    rows = db.session.execute(text("PRAGMA table_info('product')")).fetchall()
    return [row[1] for row in rows]


def add_initial_quantity_column():
    try:
        db.session.execute(text("ALTER TABLE product ADD COLUMN initial_quantity FLOAT DEFAULT 0.0"))
        db.session.execute(text("UPDATE product SET initial_quantity = quantity WHERE initial_quantity IS NULL"))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

with app.app_context():
    # Create all tables first
    db.create_all()

    # Ensure upload and backup folders exist


    # Initialize default cash_on_hand if it doesn't exist
    if not Setting.query.filter_by(key='cash_on_hand').first():
        initial_cash = Setting(key='cash_on_hand', value='0.0')
        db.session.add(initial_cash)
        db.session.commit()

    # --- BACKWARD-COMPAT: ensure `initial_quantity` column exists on older DBs
    try:
        if 'initial_quantity' not in get_product_columns():
            add_initial_quantity_column()
    except Exception:
        # If PRAGMA fails (e.g., no product table yet), ignore — create_all above should handle schema creation
        pass


def ensure_initial_quantity_exists_once():
    """Ensure product.initial_quantity exists. Runs once per process and is safe to call multiple times."""
    if app.config.get('_initial_quantity_checked'):
        return
    try:
        if 'initial_quantity' not in get_product_columns():
            add_initial_quantity_column()
    except Exception:
        # If PRAGMA fails, ignore — will try again on next request
        pass
    app.config['_initial_quantity_checked'] = True


@app.before_request
def _ensure_db_compat():
    # Run the check before handling any request so routes won't hit missing-column errors
    try:
        ensure_initial_quantity_exists_once()
    except SAOperationalError:
        # If the DB is in a bad state, ignore here — the route handlers will show errors
        pass


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('inventory'))
        return f(*args, **kwargs)
    return decorated_function

# ============================ AUTHENTICATION & PROFILE ROUTES ============================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('inventory'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        # Guard: Clerk-only accounts have no local password — direct to Clerk login
        if user and user.password is None:
            flash('This account uses Clerk Sign-In. Please use the "Sign in with Clerk" button below.', 'info')
        elif user and check_password_hash(user.password, request.form.get('password')):
            login_user(user)
            flash('Logged in successfully!', 'success')
            return redirect(url_for('inventory'))
        else:
            flash('Login Unsuccessful. Please check username and password', 'danger')
    return render_template(
        'login.html',
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_js_url=_CLERK_JS_URL,
        clerk_enabled=bool(CLERK_PUBLISHABLE_KEY)
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('inventory'))
    if request.method == 'POST':
        username = request.form.get('username')
        full_name = request.form.get('full_name')
        password = request.form.get('password')

        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'warning')
            return redirect(url_for('register'))
        
        role = 'admin' if not User.query.first() else 'user'
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(username=username, full_name=full_name, password=hashed_password, role=role)
        db.session.add(new_user)
        db.session.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template(
        'register.html',
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_js_url=_CLERK_JS_URL,
        clerk_enabled=bool(CLERK_PUBLISHABLE_KEY)
    )

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ============================ CLERK AUTHENTICATION ROUTES ============================

@app.route('/clerk-callback', methods=['POST'])
def clerk_callback():
    """Called by the frontend after Clerk successfully signs in a user.
    Receives the Clerk session token, verifies it, then creates or finds a
    matching User in the local DB and establishes a Flask-Login session.
    This keeps @login_required working across the app unchanged.
    """
    data = request.get_json(silent=True) or {}
    token = data.get('token', '').strip()

    if not token:
        return jsonify({'status': 'error', 'message': 'No token provided'}), 400

    if not CLERK_SECRET_KEY:
        return jsonify({'status': 'error', 'message': 'Clerk is not configured on the server. Please set CLERK_SECRET_KEY in .env'}), 503

    payload = verify_clerk_jwt(token)
    if not payload:
        return jsonify({'status': 'error', 'message': 'Invalid or expired Clerk session token'}), 401

    # Extract identity fields from the JWT payload
    clerk_user_id = payload.get('sub')  # Clerk's unique user ID
    # Email may be directly in payload or in a nested structure
    email = payload.get('email') or ''
    # Clerk puts the primary email in email_addresses or as a top-level claim
    if not email:
        email_addrs = payload.get('email_addresses', [])
        if email_addrs:
            email = email_addrs[0].get('email_address', '')

    first_name = payload.get('first_name', '') or ''
    last_name = payload.get('last_name', '') or ''
    full_name = f'{first_name} {last_name}'.strip() or email.split('@')[0]

    # --- Find or create local User record ---
    user = User.query.filter_by(clerk_user_id=clerk_user_id).first()

    if not user:
        # Check if a legacy user with the same email/username already exists
        if email:
            user = User.query.filter_by(clerk_email=email).first()
        if not user and email:
            # Try matching by username == email prefix (common convention)
            user = User.query.filter_by(username=email.split('@')[0]).first()

    if user:
        # Link the Clerk ID to the existing account (migration step)
        if not user.clerk_user_id:
            user.clerk_user_id = clerk_user_id
        if not user.clerk_email and email:
            user.clerk_email = email
        if not user.full_name and full_name:
            user.full_name = full_name
        db.session.commit()
    else:
        # Brand-new Clerk user not in DB — create a local record
        # First user in the system becomes admin, rest are regular users
        role = 'admin' if not User.query.first() else 'user'
        username = email.split('@')[0] if email else clerk_user_id[:20]
        # Ensure username uniqueness
        base_username = username
        counter = 1
        while User.query.filter_by(username=username).first():
            username = f'{base_username}{counter}'
            counter += 1
        user = User(
            username=username,
            password=None,  # Clerk-only account — no local password
            full_name=full_name,
            clerk_user_id=clerk_user_id,
            clerk_email=email,
            role=role
        )
        db.session.add(user)
        db.session.commit()

    # Create Flask-Login session — all @login_required routes now work normally
    login_user(user)
    return jsonify({
        'status': 'success',
        'redirect': url_for('inventory'),
        'username': user.username,
        'role': user.role
    })


@app.route('/clerk-logout', methods=['POST'])
def clerk_logout():
    """Sign out the Flask session when Clerk signs the user out on the frontend."""
    logout_user()
    return jsonify({'status': 'success', 'redirect': url_for('login')})

def save_picture(form_picture):
    random_hex = secrets.token_hex(8)
    _, f_ext = os.path.splitext(form_picture.filename)
    picture_fn = random_hex + f_ext
    picture_path = os.path.join(app.root_path, app.config['UPLOAD_FOLDER'], picture_fn)
    form_picture.save(picture_path)
    return picture_fn

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        form_name = request.form.get('form_name')
        if form_name == 'update_profile':
            current_user.full_name = request.form.get('full_name')
            if 'profile_image' in request.files:
                picture_file = request.files['profile_image']
                if picture_file.filename != '':
                    if current_user.profile_image != 'default.jpg':
                        old_pic_path = os.path.join(app.root_path, app.config['UPLOAD_FOLDER'], current_user.profile_image)
                        if os.path.exists(old_pic_path):
                            os.remove(old_pic_path)
                    picture_filename = save_picture(picture_file)
                    current_user.profile_image = picture_filename
            db.session.commit()
            flash('Your profile has been updated!', 'success')
            return redirect(url_for('profile'))
        elif form_name == 'change_password':
            old_password = request.form.get('old_password')
            new_password = request.form.get('new_password')
            confirm_new_password = request.form.get('confirm_new_password')
            if current_user.password is None:
                flash('This account uses Clerk Sign-In. Please change your password through Clerk.', 'info')
            elif not check_password_hash(current_user.password, old_password):
                flash('Current password incorrect.', 'danger')
            elif new_password != confirm_new_password:
                flash('New passwords do not match.', 'danger')
            else:
                current_user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
                db.session.commit()
                flash('Your password has been updated!', 'success')
            return redirect(url_for('profile'))
    return render_template('profile.html')

# ============================ MAIN APP ROUTES ============================
@app.route('/')
@login_required
def home():
    return redirect(url_for('inventory'))

@app.route('/inventory', methods=['GET'])
@login_required
def inventory():
    products = Product.query.all()
    customers = Customer.query.all()
    cart = session.get('cart', [])
    cart_total = sum(item['total_price'] for item in cart)
    show_receipt = session.pop('show_receipt', False)
    receipt_data = {
        "items": session.pop('receipt_items', []),
        "total": session.pop('receipt_total', 0),
        "date": session.pop('receipt_date', ""),
        "customer": session.pop('receipt_customer', "Walk-in Customer"),
        "amount_paid": session.pop('receipt_amount_paid', 0),
        "balance_due": session.pop('receipt_balance_due', 0),
        "authorized_by": session.pop('receipt_authorized_by', '')
    } if show_receipt else None
    return render_template('inventory.html', products=products, customers=customers, cart=cart, cart_total=cart_total, receipt=receipt_data)

@app.route('/add_to_cart', methods=['POST'])
@login_required
def add_to_cart():
    product_id = int(request.form['product_id'])
    quantity = float(request.form['quantity'])
    # Debug: log incoming form data to help trace unexpected flashes
    try:
        print(f"[DEBUG add_to_cart] form data: {request.form.to_dict()}")
    except Exception:
        pass
    product = Product.query.get_or_404(product_id)
    if quantity > product.quantity:
        flash(f"Only {product.quantity} units of {product.name} available!", "warning")
        return redirect(url_for('inventory'))
    cart = session.get('cart', [])

    # Check if product already in cart; if so, increment quantity
    existing = None
    for it in cart:
        if it.get('product_id') == product.id:
            existing = it
            break

    if existing:
        new_qty = existing['quantity'] + quantity
        if new_qty > product.quantity:
            flash(f"Only {product.quantity} units of {product.name} available (including current cart).", "warning")
            return redirect(url_for('inventory'))
        existing['quantity'] = new_qty
        existing['total_price'] = round(existing['quantity'] * existing['unit_price'], 2)
    else:
        cart_item = {
            'product_id': product.id,
            'product_name': product.name,
            'quantity': quantity,
            'unit_price': float(product.selling_price),
            'total_price': round(quantity * float(product.selling_price), 2)
        }
        cart.append(cart_item)

    session['cart'] = cart
    flash(f"Added {quantity} of {product.name} to cart.", "success")
    return redirect(url_for('inventory'))


@app.route('/update_cart_quantity', methods=['POST'])
@login_required
def update_cart_quantity():
    try:
        product_id = int(request.form.get('product_id'))
        quantity = float(request.form.get('quantity'))
    except (TypeError, ValueError):
        flash('Invalid quantity submitted.', 'danger')
        return redirect(url_for('inventory'))

    cart = session.get('cart', [])
    for it in cart:
        if it.get('product_id') == product_id:
            if quantity <= 0:
                flash('Quantity must be positive.', 'warning')
                return redirect(url_for('inventory'))
            # Check available stock
            prod = Product.query.get(product_id)
            if quantity > prod.quantity:
                flash(f'Only {prod.quantity} units of {prod.name} available.', 'warning')
                return redirect(url_for('inventory'))
            it['quantity'] = quantity
            it['total_price'] = round(quantity * it.get('unit_price', 0), 2)
            session['cart'] = cart
            flash('Cart updated.', 'success')
            return redirect(url_for('inventory'))

    flash('Product not found in cart.', 'warning')
    return redirect(url_for('inventory'))


@app.route('/remove_from_cart')
@login_required
def remove_from_cart():
    # support both query param and path-style calls
    index = request.args.get('index')
    try:
        idx = int(index)
    except Exception:
        flash('Invalid cart item index.', 'danger')
        return redirect(url_for('inventory'))

    cart = session.get('cart', [])
    if 0 <= idx < len(cart):
        removed = cart.pop(idx)
        session['cart'] = cart
        flash(f"Removed {removed.get('product_name')} from cart.", 'info')
    else:
        flash('Cart item not found.', 'warning')
    return redirect(url_for('inventory'))


@app.route('/finalize_purchase', methods=['POST'])
@login_required
def finalize_purchase():
    # Robust finalize handler: uses session cart and posted form values
    cart = session.get('cart', [])
    if not cart:
        flash('Cart is empty. Add items before finalizing purchase.', 'warning')
        return redirect(url_for('inventory'))

    customer_type = request.form.get('customer_type')
    customer_id = request.form.get('customer_id')
    payment_type = request.form.get('payment_type')
    amount_paid_str = request.form.get('amount_paid')
    authorized_by = request.form.get('authorized_by')
    source_type = 'inventory'

    total_amount = sum(item['total_price'] for item in cart)
    amount_paid = 0.0

    if payment_type == 'full' or not payment_type:
        amount_paid = total_amount
    else:
        try:
            amount_paid = float(amount_paid_str)
        except (ValueError, TypeError):
            flash('Invalid amount entered for partial payment.', 'danger')
            return redirect(url_for('inventory'))

    if amount_paid > total_amount or amount_paid < 0:
        flash('Amount paid cannot be negative or more than the total.', 'danger')
        return redirect(url_for('inventory'))

    customer_name = 'Walk-in Customer'
    customer_obj = None
    if customer_type == 'registered' and customer_id:
        try:
            customer_obj = Customer.query.get(int(customer_id))
        except Exception:
            customer_obj = None
        if customer_obj:
            customer_name = customer_obj.name

    balance_due = total_amount - amount_paid

    if balance_due > 0 and (customer_type != 'registered' or not customer_obj):
        flash('Partial payments are only allowed for registered customers.', 'danger')
        return redirect(url_for('inventory'))

    try:
        sale = Sale(
            customer_id=customer_obj.id if customer_obj else None,
            total_amount=total_amount,
            amount_paid=amount_paid,
            authorized_by=authorized_by,
            source_type=source_type,
            date=datetime.now()
        )
        db.session.add(sale)
        db.session.commit()

        for item in cart:
            product = Product.query.get(item['product_id'])
            sale_item = SaleItem(
                sale_id=sale.id,
                product_id=item['product_id'],
                quantity=item['quantity'],
                unit_price=item['unit_price'],
                cost_price=product.cost_price  # Store cost price at time of sale
            )
            db.session.add(sale_item)
            product.quantity -= item['quantity']

        if balance_due > 0:
            new_debt = Debt(
                customer_name=customer_name,
                original_amount=balance_due,
                amount_paid=0,
                status='pending',
                date=datetime.now()
            )
            db.session.add(new_debt)

        db.session.commit()

        session['receipt_items'] = cart
        session['receipt_total'] = total_amount
        session['receipt_date'] = sale.date.strftime('%Y-%m-%d %H:%M:%S')
        session['receipt_customer'] = customer_name
        session['receipt_amount_paid'] = amount_paid
        session['receipt_balance_due'] = balance_due
        session['receipt_authorized_by'] = authorized_by
        session['show_receipt'] = True

        session.pop('cart', None)
        flash('Purchase completed successfully!', 'success')

        # Update cash history
        try:
            # Calculate cost-of-goods-sold for this sale (stock value reduced)
            try:
                cogs_total = 0.0
                for item in cart:
                    prod = Product.query.get(item['product_id'])
                    cogs_total += float(item['quantity']) * float(prod.cost_price if prod else 0.0)
            except Exception:
                cogs_total = 0.0

            # Use the sale date as the entry date and compute last cash amount as of that date
            entry_date = sale.date if getattr(sale, 'date', None) else datetime.now()
            last_cash = CashHistory.query.filter(CashHistory.date <= entry_date).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
            last_amount = float(last_cash.amount) if last_cash else 0.0

            # Update cash history: only actual payment received increases cash on hand.
            # Do NOT add COGS here — COGS reduces stock value, cash increases by amount actually paid.
            paid = float(amount_paid or 0.0)
            if paid > 0.0:
                new_amount = last_amount + paid
                # Use the sale date for the cash entry so filtered reports include this payment
                entry_date = sale.date if getattr(sale, 'date', None) else datetime.now()
                new_cash = CashHistory(amount=new_amount, date=entry_date)
                db.session.add(new_cash)
                db.session.commit()
        except Exception:
            db.session.rollback()

    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred during finalization: {e}', 'danger')

    return redirect(url_for('inventory'))

# ============================ PRODUCTS PAGE ============================
@app.route('/products', methods=['GET', 'POST'])
@login_required
@admin_required
def products():
    if request.method == 'POST':
        name = request.form['name']
        quantity = float(request.form['quantity'])
        selling_price = float(request.form['selling_price'])
        cost_price = float(request.form['cost_price'])
        expiry_date_str = request.form.get('expiry_date')
        expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d').date() if expiry_date_str else None

        new_product = Product(
            name=name,
            quantity=quantity,
            selling_price=selling_price,
            cost_price=cost_price,
            expiry_date=expiry_date
        )
        db.session.add(new_product)
        db.session.commit()
        return redirect(url_for('products'))

    all_products = Product.query.order_by(Product.name).all()
    return render_template('products.html', products=all_products)

@app.route('/add_stock/<int:product_id>', methods=['POST'])
@login_required
@admin_required
def add_stock(product_id):
    product = Product.query.get_or_404(product_id)
    try:
        quantity_to_add = float(request.form.get('quantity'))
        if quantity_to_add > 0:
            product.quantity += quantity_to_add
            db.session.commit()
            flash(f"Added {quantity_to_add} units to {product.name}. New stock: {product.quantity}", "success")
        else:
            flash("Please enter a positive number to add to stock.", "warning")
    except (ValueError, TypeError):
        flash("Invalid quantity entered.", "danger")
    return redirect(url_for('products'))

@app.route('/edit_product/<int:product_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    if request.method == 'POST':
        product.name = request.form['name']
        product.quantity = float(request.form['quantity'])
        product.selling_price = float(request.form['selling_price'])
        product.cost_price = float(request.form['cost_price'])
        expiry_date_str = request.form.get('expiry_date')
        product.expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d').date() if expiry_date_str else None

        db.session.commit()
        flash(f"Product '{product.name}' updated successfully.", "success")
        return redirect(url_for('products'))
    return render_template('edit_product.html', product=product)

@app.route('/delete_product/<int:product_id>', methods=['POST'])
@login_required
@admin_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    if SaleItem.query.filter_by(product_id=product.id).first():
        flash(f"Cannot delete '{product.name}' as it has been sold before.", "danger")
        return redirect(url_for('products'))
    db.session.delete(product)
    db.session.commit()
    flash(f"Product '{product.name}' has been deleted.", "success")
    return redirect(url_for('products'))

# ============================ CUSTOMERS ============================
@app.route('/customers', methods=['GET', 'POST'])
@login_required
def customers():
    if request.method == 'POST':
        name = request.form['name']
        phone = request.form.get('phone')
        new_customer = Customer(name=name, phone=phone)
        db.session.add(new_customer)
        db.session.commit()
        flash("Customer added successfully!", "success")
        return redirect(url_for('customers'))
    all_customers = Customer.query.all()
    return render_template('customers.html', customers=all_customers)

@app.route('/edit_customer/<int:customer_id>', methods=['GET', 'POST'])
@login_required
def edit_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    if request.method == 'POST':
        customer.name = request.form['name']
        customer.phone = request.form['phone']
        db.session.commit()
        flash("Customer updated successfully!", "success")
        return redirect(url_for('customers'))
    return render_template('edit_customer.html', customer=customer)

@app.route('/delete_customer/<int:customer_id>', methods=['POST'])
@login_required
def delete_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    if customer.sales:
        flash("Cannot delete customer with existing sales records.", "danger")
        return redirect(url_for('customers'))
    db.session.delete(customer)
    db.session.commit()
    flash("Customer deleted.", "success")
    return redirect(url_for('customers'))

# ============================ DEBTS ============================
@app.route('/debts', methods=['GET', 'POST'])
@login_required
def debts():
    if request.method == 'POST':
        customer_name = request.form['customer_name']
        amount = float(request.form['amount'])
        new_debt = Debt(
            customer_name=customer_name, 
            original_amount=amount, 
            status='pending', 
            date=datetime.now()
        )
        db.session.add(new_debt)
        db.session.commit()
        flash("New debt added successfully!", "success")
        return redirect(url_for('debts'))

    search_query = request.args.get('q', '')
    if search_query:
        debts_query = Debt.query.filter(Debt.customer_name.ilike(f'%{search_query}%'))
    else:
        debts_query = Debt.query

    all_debts = debts_query.order_by(Debt.status, Debt.date.desc()).all()
    return render_template('debts.html', debts=all_debts, search_query=search_query)


# ============================ ADMIN: Link Debts to Sales ============================
@app.route('/admin/link_debts', methods=['GET'])
@login_required
@admin_required
def link_debts():
    # Show debts that do not have an associated sale_id so admin can manually link them
    debts_to_link = Debt.query.filter(Debt.sale_id == None).order_by(Debt.date.desc()).limit(200).all()

    # For each debt, try to find candidate sales by exact customer match
    candidates = {}
    for d in debts_to_link:
        cname = (d.customer_name or '').strip().lower()
        cust = None
        cand_sales = []
        if cname:
            cust = Customer.query.filter(func.lower(Customer.name) == cname).first()
        if cust:
            cand_sales = Sale.query.filter(Sale.customer_id == cust.id).order_by(Sale.date.desc()).limit(50).all()
        candidates[d.id] = cand_sales

    return render_template('link_debts.html', debts=debts_to_link, candidates=candidates)


@app.route('/admin/link_debt/<int:debt_id>', methods=['POST'])
@login_required
@admin_required
def link_debt(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    sale_id = request.form.get('sale_id')
    if not sale_id:
        flash('No sale selected for linking.', 'warning')
        return redirect(url_for('link_debts'))
    try:
        sale_obj = Sale.query.get(int(sale_id))
        if not sale_obj:
            flash('Selected sale not found.', 'danger')
            return redirect(url_for('link_debts'))
        debt.sale_id = sale_obj.id
        db.session.commit()
        flash(f'Debt {debt.id} linked to Sale {sale_obj.id}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error linking debt: {e}', 'danger')
    return redirect(url_for('link_debts'))

@app.route('/pay_debt/<int:debt_id>', methods=['POST'])
@login_required
def pay_debt(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    payment_amount = float(request.form['payment_amount'])
    if payment_amount <= 0:
        flash("Payment amount must be positive.", "danger")
        return redirect(url_for('debts'))
    if payment_amount > debt.balance:
        flash(f"Payment cannot be more than the remaining balance of ₦{debt.balance:.2f}.", "danger")
        return redirect(url_for('debts'))
    debt.amount_paid += payment_amount
    if debt.balance <= 0:
        debt.status = 'paid'
        debt.amount_paid = debt.original_amount
        flash("Debt has been fully paid!", "success")
    else:
        flash(f"Payment of ₦{payment_amount:.2f} recorded successfully.", "success")
    db.session.commit()
    # --- AUTO UPDATE CASH HISTORY: customer payment increases cash on hand
    try:
        if payment_amount and payment_amount > 0:
            entry_date = datetime.now()
            last_cash = CashHistory.query.filter(CashHistory.date <= entry_date).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
            last_amount = float(last_cash.amount) if last_cash else 0.0
            new_cash = CashHistory(amount=last_amount + payment_amount, date=entry_date)
            db.session.add(new_cash)
            db.session.commit()
    except Exception:
        db.session.rollback()
        # non-fatal
    return redirect(url_for('debts'))

@app.route('/edit_debt/<int:debt_id>', methods=['GET', 'POST'])
@login_required
def edit_debt(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    if request.method == 'POST':
        debt.customer_name = request.form['customer_name']
        debt.original_amount = float(request.form['original_amount'])
        debt.amount_paid = float(request.form['amount_paid'])
        debt.date = datetime.strptime(request.form['date'], '%Y-%m-%d')
        if debt.balance <= 0:
            debt.status = 'paid'
        else:
            debt.status = 'pending'
        db.session.commit()
        flash("Debt updated successfully.", "success")
        return redirect(url_for('debts'))
    return render_template('edit_debt.html', debt=debt)

@app.route('/delete_debt/<int:debt_id>', methods=['POST'])
@login_required
def delete_debt(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    db.session.delete(debt)
    db.session.commit()
    flash("Debt record deleted.", "info")
    return redirect(url_for('debts'))

# ============================ EXPENSES ============================
@app.route('/expenses', methods=['GET', 'POST'])
@login_required
def expenses():
    if request.method == 'POST':
        description = request.form['description']
        amount = float(request.form['amount'])
        category = request.form['category']
        new_expense = Expense(description=description, amount=amount, category=category, date=datetime.now())
        db.session.add(new_expense)
        db.session.commit()

        # --- AUTO UPDATE CASH HISTORY: decrement cash by expense amount
        try:
            entry_date = datetime.now()
            last_cash = CashHistory.query.filter(CashHistory.date <= entry_date).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
            last_amount = float(last_cash.amount) if last_cash else 0.0
            new_cash = CashHistory(amount=last_amount - amount, date=entry_date)
            db.session.add(new_cash)
            db.session.commit()
        except Exception:
            db.session.rollback()
            # non-fatal

        return redirect(url_for('expenses'))
    all_expenses = Expense.query.order_by(Expense.date.desc()).all()
    return render_template('expenses.html', expenses=all_expenses)

@app.route('/edit_expense/<int:expense_id>', methods=['GET', 'POST'])
@login_required
def edit_expense(expense_id):
    expense = Expense.query.get_or_404(expense_id)
    if request.method == 'POST':
        expense.description = request.form['description']
        expense.amount = float(request.form['amount'])
        expense.category = request.form['category']
        # Update the actual expense date
        date_str = request.form.get('date', '').strip()
        if date_str:
            try:
                expense.date = datetime.strptime(date_str, '%Y-%m-%d')
            except Exception:
                pass
        try:
            db.session.commit()
            flash('Expense updated successfully.', 'success')
            return redirect(url_for('expenses'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating expense: {e}', 'danger')
            return redirect(url_for('edit_expense', expense_id=expense_id))
    return render_template('edit_expense.html', expense=expense)

@app.route('/delete_expense/<int:expense_id>', methods=['POST'])
@login_required
def delete_expense(expense_id):
    expense = Expense.query.get_or_404(expense_id)
    db.session.delete(expense)
    db.session.commit()
    return redirect(url_for('expenses'))

# ============================ REPORTS ============================
_backup_cache = {}  # Global cache for backup data {month_end_str: backup_data}

def get_backup_asset_value(month_end):
    """
    Fetch backup asset value for a given month end date.
    Uses caching to avoid redundant backup searches.
    """
    import os
    import sqlite3
    import re
    
    # Cache key: use month_end as string
    cache_key = month_end.strftime('%Y-%m-%d')
    if cache_key in _backup_cache:
        print(f"[DEBUG] Cache HIT for {cache_key}")
        return _backup_cache[cache_key]
    
    backups_dir = os.path.join(os.path.dirname(__file__), 'backups')
    print(f"[DEBUG] Searching for backups in {backups_dir} for month ending {month_end}")
    
    all_files = [f for f in os.listdir(backups_dir) if f.startswith('backup_') and f.endswith('.db')]
    if not all_files:
        print(f"[DEBUG] No backups found in backups directory: {backups_dir}")
        _backup_cache[cache_key] = None
        return None

    # Extract date from filename like backup_YYYY-MM-DD[_HH-MM-SS].db
    date_map = {}
    for fn in all_files:
        m = re.match(r'backup_(\d{4}-\d{2}-\d{2})', fn)
        if m:
            try:
                d = datetime.strptime(m.group(1), '%Y-%m-%d').date()
                date_map[fn] = d
            except Exception:
                continue

    # First try: backups within the same year-month
    month_prefix = month_end.strftime('%Y-%m')
    candidates = [fn for fn, d in date_map.items() if d.strftime('%Y-%m') == month_prefix]
    
    if candidates:
        # pick latest by parsed date (and filename for ties)
        candidates.sort(key=lambda f: (date_map.get(f), f))
        backup_path = os.path.join(backups_dir, candidates[-1])
        print(f"[DEBUG] Found backup in same month: {backup_path} for {month_end.strftime('%b %Y')}")
    else:
        # fallback: pick the latest backup with date <= month_end
        earlier = [fn for fn, d in date_map.items() if d <= month_end]
        if not earlier:
            print(f"[DEBUG] No backup files found for {month_end.strftime('%b %Y')} or earlier")
            _backup_cache[cache_key] = None
            return None
        earlier.sort(key=lambda f: (date_map.get(f), f))
        backup_path = os.path.join(backups_dir, earlier[-1])
        print(f"[DEBUG] Using latest earlier backup: {backup_path} for {month_end.strftime('%b %Y')}")
    
    # Read from backup
    conn = sqlite3.connect(backup_path)
    cursor = conn.cursor()
    
    def safe_query(sql):
        try:
            cursor.execute(sql)
            return cursor.fetchall()
        except sqlite3.OperationalError:
            print(f"[DEBUG] SQL error for query: {sql}")
            return []

    # Determine cash table name (some backups use 'cash_history', others 'cashhistory')
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {r[0].lower() for r in cursor.fetchall()}
    cash_table = None
    if 'cash_history' in existing_tables:
        cash_table = 'cash_history'
    elif 'cashhistory' in existing_tables:
        cash_table = 'cashhistory'

    if not cash_table:
        # no cash table in backup
        print(f"[DEBUG] No cash table found in backup {backup_path}; treating cash as 0")
        cash = 0.0
    else:
        cash_rows = safe_query(f'SELECT amount FROM {cash_table} ORDER BY date DESC LIMIT 1')
        cash = float(cash_rows[0][0]) if cash_rows else 0.0
    
    prod_rows = safe_query('SELECT quantity, cost_price FROM product')
    stock_value = sum(q * c for q, c in prod_rows)
    
    debt_rows = safe_query("SELECT original_amount, amount_paid FROM debt WHERE status='pending'")
    pending_debt = sum(o - p for o, p in debt_rows)
    
    rev_rows = safe_query('SELECT SUM(total_amount) FROM sale')
    revenue = rev_rows[0][0] if rev_rows and rev_rows[0][0] is not None else 0.0
    
    cogs_rows = safe_query('SELECT SUM(quantity * cost_price) FROM sale_item')
    cogs = cogs_rows[0][0] if cogs_rows and cogs_rows[0][0] is not None else 0.0
    
    exp_rows = safe_query('SELECT SUM(amount) FROM expense')
    expenses = exp_rows[0][0] if exp_rows and exp_rows[0][0] is not None else 0.0
    
    profit = revenue - cogs - expenses
    assets = cash + stock_value + pending_debt
    
    print(f"[DEBUG] Asset value for {month_end.strftime('%b %Y')} from backup: {assets}")
    conn.close()
    
    result = {
        'assets': assets,
        'cash': cash,
        'stock_value': stock_value,
        'pending_debt': pending_debt,
        'month_revenue': revenue,
        'month_cogs': cogs,
        'month_expenses': expenses,
        'month_profit': profit,
        'backup_path': backup_path
    }
    
    # Store in cache
    _backup_cache[cache_key] = result
    return result

def compute_monthly_assets(first_sale=None, current_month_total_assets=None, current_month=None):
    """
    Compute monthly asset snapshots from backups.
    Uses caching to avoid redundant backup searches.
    """
    if first_sale is None:
        first_sale = Sale.query.order_by(Sale.date.asc()).first()

    if first_sale:
        raw_date = first_sale.date
        if isinstance(raw_date, datetime):
            raw_date = raw_date.date()
        first_month = raw_date.replace(day=1)
    else:
        first_month = date.today().replace(day=1)

    if current_month is None:
        today = date.today()
        current_month = today.replace(day=1)
    else:
        # ensure current_month is a date representing first of month
        if isinstance(current_month, datetime):
            current_month = current_month.date().replace(day=1)
        else:
            current_month = current_month.replace(day=1)

    months = []
    cursor = first_month
    while cursor <= current_month:
        months.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    monthly_assets = []
    cumulative_retained = 0.0

    for month_start in months:
        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        label = month_start.strftime("%b %Y")
        backup_source = None
        
        if label.startswith('Oct'):
            assets = 2672750.00
            backup_source = 'hardcoded'
            print(f"[DEBUG] Hardcoded asset value for {label}: {assets}")
            month_cash = 0.0
            month_stock_value = 0.0
            month_pending_debt = 0.0
            month_revenue = 0.0
            month_cogs = 0.0
            month_expenses = 0.0
            month_profit = 0.0
            retained = cumulative_retained
        else:
            # Get from cache (calls get_backup_asset_value with caching)
            backup_asset = get_backup_asset_value(month_end)
            
            if backup_asset is not None:
                assets = backup_asset.get('assets', 0.0)
                month_cash = float(backup_asset.get('cash', 0.0))
                month_stock_value = float(backup_asset.get('stock_value', 0.0))
                month_pending_debt = float(backup_asset.get('pending_debt', 0.0))
                month_revenue = float(backup_asset.get('month_revenue', 0.0))
                month_cogs = float(backup_asset.get('month_cogs', 0.0))
                month_expenses = float(backup_asset.get('month_expenses', 0.0))
                month_profit = float(backup_asset.get('month_profit', 0.0))
                retained = cumulative_retained
                backup_source = 'backup'
            else:
                print(f"[DEBUG] No backup found for {label}, marking as no snapshot.")
                assets = None
                month_cash = None
                month_stock_value = None
                month_pending_debt = None
                month_revenue = None
                month_cogs = None
                month_expenses = None
                month_profit = None
                retained = None
                backup_source = None
        
        # --- CHECK FOR MANUAL OVERRIDE ---
        try:
            override = MonthlyAssetOverride.query.filter_by(month_key=label).first()
            if override is not None:
                assets = float(override.overridden_assets)
                backup_source = 'manual_override'
                print(f"[DEBUG] Manual override for {label}: {assets}")
        except Exception:
            pass

        monthly_assets.append({
            "label": label,
            "assets": assets,
            "cash": month_cash,
            "stock_value": month_stock_value,
            "pending_debt": month_pending_debt,
            "month_revenue": month_revenue,
            "month_cogs": month_cogs,
            "month_expenses": month_expenses,
            "month_profit": month_profit,
            "retained": retained,
            "backup_source": backup_source
        })

    # CHANGE COMPARISON (skip months with no snapshot)
    for i in range(len(monthly_assets)):
        if i == 0:
            monthly_assets[i]["change"] = None
            monthly_assets[i]["pct_change"] = None
        else:
            prev = monthly_assets[i - 1]["assets"]
            curr = monthly_assets[i]["assets"]
            
            # Special case: if this is the last month and we have current_total_assets
            is_last_month = (i == len(monthly_assets) - 1)
            if is_last_month and current_month_total_assets is not None:
                curr = current_month_total_assets
                print(f"[DEBUG] Using current_total_assets ({current_month_total_assets}) for last month change calculation")
            
            # Only compute change if prev is not None
            if prev is not None:
                change = curr - prev if curr is not None else None
                if change is not None:
                    monthly_assets[i]["change"] = change
                    try:
                        monthly_assets[i]["pct_change"] = (change / prev) * 100 if prev not in (0, None) else None
                    except Exception:
                        monthly_assets[i]["pct_change"] = None
                else:
                    monthly_assets[i]["change"] = None
                    monthly_assets[i]["pct_change"] = None
            else:
                monthly_assets[i]["change"] = None
                monthly_assets[i]["pct_change"] = None

    return monthly_assets


@app.route('/admin/clear_backup_cache', methods=['POST'])
@login_required
@admin_required
def clear_backup_cache():
    global _backup_cache
    _backup_cache = {}
    return ('', 204)


@app.route('/admin/set_monthly_asset_override', methods=['POST'])
@login_required
@admin_required
def set_monthly_asset_override():
    month_label = request.form.get('month_label', '').strip()
    assets_value = request.form.get('assets_value', '').strip()
    keep_start = request.form.get('keep_start_date', '')
    keep_end = request.form.get('keep_end_date', '')

    if not month_label or not assets_value:
        flash('Month label and asset value are required.', 'danger')
        return redirect(url_for('reports', start_date=keep_start, end_date=keep_end))

    try:
        value = float(assets_value)
    except ValueError:
        flash('Invalid asset value entered. Please enter a number.', 'danger')
        return redirect(url_for('reports', start_date=keep_start, end_date=keep_end))

    try:
        existing = MonthlyAssetOverride.query.filter_by(month_key=month_label).first()
        if existing:
            existing.overridden_assets = value
            existing.updated_at = datetime.now()
        else:
            new_override = MonthlyAssetOverride(
                month_key=month_label,
                overridden_assets=value,
                updated_at=datetime.now()
            )
            db.session.add(new_override)
        db.session.commit()
        # Clear backup cache so the override reflects immediately in the chart
        global _backup_cache
        _backup_cache = {}
        flash(f'Total Business Assets for {month_label} set to ₦{value:,.2f}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to save override: {e}', 'danger')

    return redirect(url_for('reports', start_date=keep_start, end_date=keep_end))


@app.route('/admin/add_cash_snapshot', methods=['POST'])
@login_required
@admin_required
def add_cash_snapshot():
    from datetime import datetime
    try:
        amount = float(request.form.get('amount'))
        day_date = request.form.get('day_date')  # expected YYYY-MM-DD
        keep_start = request.form.get('keep_start_date')
        keep_end = request.form.get('keep_end_date')
        if not day_date:
            flash('Missing date for snapshot', 'danger')
            return redirect(url_for('reports', start_date=keep_start, end_date=keep_end))

        # create CashHistory record in primary DB
        dt = datetime.strptime(day_date, '%Y-%m-%d')
        # set time to end of day
        dt = dt.replace(hour=23, minute=59, second=59)
        ch = CashHistory(amount=amount, date=dt)
        db.session.add(ch)
        db.session.commit()

        # invalidate cache for the month
        cache_key = dt.date().strftime('%Y-%m-%d')
        # also clear whole cache to be safe
        global _backup_cache
        _backup_cache = {}

        flash('Cash snapshot added.', 'success')
        return redirect(url_for('reports', start_date=keep_start, end_date=keep_end))
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to add snapshot: {e}', 'danger')
        return redirect(url_for('reports'))
@app.route('/update_cash', methods=['POST'])
@login_required
@admin_required
def update_cash():
    try:
        amount = float(request.form.get('amount'))
        
        # Create a new CashHistory record
        # (Assuming your model is named CashHistory and has 'amount' and 'date' columns)
        # If caller provided keep_end_date, create the cash entry at the end of that day so reports picking that end_date sees it
        keep_end = request.form.get('keep_end_date')
        if keep_end:
            try:
                dt = datetime.strptime(keep_end, '%Y-%m-%d')
                dt = dt.replace(hour=23, minute=59, second=59)
            except Exception:
                dt = datetime.now()
        else:
            dt = datetime.now()

        new_cash_entry = CashHistory(
            amount=amount,
            date=dt
        )
        
        db.session.add(new_cash_entry)
        db.session.commit()

        # clear backup cache so snapshots are not stale
        global _backup_cache
        _backup_cache = {}

        flash(f"Cash on hand updated to ₦{amount:,.2f}", "success")
        # redirect back to reports if caller passed keep_start_date/keep_end_date
        keep_start = request.form.get('keep_start_date')
        keep_end = request.form.get('keep_end_date')
        if keep_start and keep_end:
            return redirect(url_for('reports', start_date=keep_start, end_date=keep_end))
        else:
            return redirect(url_for('reports'))
    except ValueError:
        flash("Invalid amount entered.", "danger")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating cash: {str(e)}", "danger")

    return redirect(url_for('reports'))


@app.route('/monthly_assets_json')
@login_required
@admin_required
def monthly_assets_json():
    # duplicate the monthly asset computation used in reports() to provide
    # chart data for the reporting page
    monthly_assets = compute_monthly_assets()
    return jsonify(monthly_assets)


@app.route('/export_monthly_assets')
@login_required
@admin_required
def export_monthly_assets():
    monthly_assets = compute_monthly_assets()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Month", "Assets", "Revenue", "COGS", "Expenses", "Profit", "Cash", "Stock", "PendingDebt", "Retained", "Change"])
    for m in monthly_assets:
        writer.writerow([
            m.get('label'),
            f"{m.get('assets',0):.2f}",
            f"{m.get('month_revenue',0):.2f}",
            f"{m.get('month_cogs',0):.2f}",
            f"{m.get('month_expenses',0):.2f}",
            f"{m.get('month_profit',0):.2f}",
            f"{m.get('cash',0):.2f}",
            f"{m.get('stock_value',0):.2f}",
            f"{m.get('pending_debt',0):.2f}",
            f"{m.get('retained',0):.2f}",
            f"{(m.get('change') if m.get('change') is not None else '')}"
        ])
    output.seek(0)
    data = output.getvalue().encode('utf-8')
    return send_file(BytesIO(data), as_attachment=True, download_name='monthly_assets.csv', mimetype='text/csv')

# ============================ EXPORT SALES ============================
@app.route('/export_sales')
@login_required
def export_sales():
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    sales_query = Sale.query
    if start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            end_date = datetime.combine(datetime.strptime(end_date_str, '%Y-%m-%d').date(), datetime.max.time())
            sales_query = sales_query.filter(Sale.date.between(start_date, end_date))
        except ValueError:
            flash("Invalid date format for export. Exporting all sales.", "warning")
    sales = sales_query.order_by(Sale.date.desc()).all()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales Report"
    sheet.append(["Date", "Customer", "Total Amount"])
    for sale in sales:
        customer_name = sale.customer.name if sale.customer else "Walk-in Customer"
        sheet.append([
            sale.date.strftime("%Y-%m-%d %H:%M:%S"),
            customer_name,
            f"{sale.total_amount:.2f}"
        ])
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = f"sales_report_{start_date_str}to{end_date_str}.xlsx" if start_date_str and end_date_str else "sales_report_all.xlsx"
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )


@app.route('/update_snapshot', methods=['POST'])
@login_required
@admin_required
def update_snapshot():
    import sqlite3 as _sqlite
    from datetime import datetime as _dt
    backup_filename = request.form.get('backup_filename')
    if not backup_filename:
        flash('No backup specified.', 'danger')
        return redirect(url_for('reports'))

    # Only allow files within backups folder
    backup_path = os.path.join(app.config['BACKUP_FOLDER'], backup_filename)
    if not os.path.exists(backup_path):
        flash('Backup file not found.', 'danger')
        return redirect(url_for('reports'))

    try:
        conn = _sqlite.connect(backup_path)
        cur = conn.cursor()

        # Update cash: insert a new cash_history record to represent new cash on hand
        cash_val = request.form.get('cash_on_hand')
        if cash_val is not None and cash_val != '':
            try:
                amt = float(cash_val)
                cur.execute("INSERT INTO cash_history (amount, date) VALUES (?, ?)", (amt, _dt.now().strftime('%Y-%m-%d %H:%M:%S')))
            except Exception:
                pass

        # Update products
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        cost_prices = request.form.getlist('cost_price[]')
        for i, pid in enumerate(product_ids):
            try:
                q = float(quantities[i])
                cp = float(cost_prices[i])
                cur.execute('UPDATE product SET quantity = ?, cost_price = ? WHERE id = ?', (q, cp, int(pid)))
            except Exception:
                continue

        # Update debts
        debt_ids = request.form.getlist('debt_id[]')
        origs = request.form.getlist('original_amount[]')
        paids = request.form.getlist('amount_paid[]')
        for i, did in enumerate(debt_ids):
            try:
                o = float(origs[i])
                p = float(paids[i])
                cur.execute('UPDATE debt SET original_amount = ?, amount_paid = ? WHERE id = ?', (o, p, int(did)))
            except Exception:
                continue

        conn.commit()
        conn.close()

        # Invalidate cache for the month that was edited so that subsequent reads reflect the change
        edit_month = request.form.get('edit_month')
        if edit_month:
            try:
                _backup_key = _dt.strptime(edit_month, '%Y-%m-%d').date().strftime('%Y-%m-%d')
                if _backup_key in _backup_cache:
                    _backup_cache.pop(_backup_key, None)
            except Exception:
                pass

        flash('Snapshot updated successfully.', 'success')
    except Exception as e:
        flash(f'Failed to update backup: {e}', 'danger')

    # Redirect back to reports with same filter if provided
    start_date = request.form.get('keep_start_date')
    end_date = request.form.get('keep_end_date')
    if start_date and end_date:
        return redirect(url_for('reports', start_date=start_date, end_date=end_date))
    return redirect(url_for('reports'))

# ============================ INVOICES ============================
@app.route('/invoices')
@login_required
def invoices():
    customer_filter = request.args.get('customer', '').strip().lower()
    date_filter = request.args.get('date')
    query = Sale.query.order_by(Sale.date.desc())
    if customer_filter:
        query = query.join(Customer).filter(func.lower(Customer.name).contains(customer_filter))
    if date_filter:
        try:
            parsed_date = datetime.strptime(date_filter, '%Y-%m-%d').date()
            query = query.filter(func.date(Sale.date) == parsed_date)
        except ValueError:
            flash("Invalid date format", "danger")
    sales = query.all()
    return render_template('invoices.html', sales=sales)

@app.route('/download_receipt/<int:sale_id>')
@login_required
def download_receipt(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    return render_template('receipt_download.html', sale=sale)

@app.route('/download_receipt_pdf/<int:sale_id>')
@login_required
def download_receipt_pdf(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    html = render_template('receipt_download.html', sale=sale)
    pdf = BytesIO()
    pisa_status = pisa.CreatePDF(html, dest=pdf)
    if pisa_status.err:
        return "PDF generation failed"
    pdf.seek(0)
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=f"receipt_{sale_id}.pdf")


# ============================ SALE ITEM EDIT/DELETE ============================
@app.route('/edit_sale_item/<int:item_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_sale_item(item_id):
    item = SaleItem.query.get_or_404(item_id)
    sale = item.sale
    product = Product.query.get(item.product_id)
    if request.method == 'POST':
        try:
            new_qty = float(request.form.get('quantity'))
            new_price = float(request.form.get('unit_price'))
            sale_date_str = request.form.get('sale_date')
        except (TypeError, ValueError):
            flash('Invalid quantity or price provided.', 'danger')
            return redirect(url_for('edit_sale_item', item_id=item_id))

        # Validate stock availability when increasing sold quantity
        delta = new_qty - item.quantity
        if delta > 0 and product.quantity < delta:
            flash(f'Not enough stock to increase sold quantity. Available: {product.quantity}', 'danger')
            return redirect(url_for('edit_sale_item', item_id=item_id))

        try:
            # Adjust product stock (return or consume)
            product.quantity -= delta
            # Update item and sale totals
            item.quantity = new_qty
            item.unit_price = new_price
            # If a sale date/time was provided, update the parent sale
            if sale_date_str:
                try:
                    # Expecting input like 'YYYY-MM-DDTHH:MM'
                    parsed_dt = None
                    try:
                        parsed_dt = datetime.fromisoformat(sale_date_str)
                    except Exception:
                        parsed_dt = datetime.strptime(sale_date_str, '%Y-%m-%dT%H:%M')
                    if parsed_dt:
                        sale.date = parsed_dt
                except Exception:
                    # ignore parse errors; keep original sale.date
                    pass

            sale.total_amount = sum(si.quantity * si.unit_price for si in sale.items)

            # If sale has no items after edit (shouldn't happen here), handle gracefully
            db.session.commit()
            # Inform admin of updated sale date/time in confirmation
            try:
                formatted_dt = sale.date.strftime('%Y-%m-%d %H:%M') if sale.date else 'N/A'
                flash(f'Sale item updated successfully. Sale date set to {formatted_dt}. Please review payments/debts if necessary.', 'success')
            except Exception:
                flash('Sale item updated successfully. Please review payments/debts if necessary.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating sale item: {e}', 'danger')
        return redirect(url_for('invoices'))

    return render_template('edit_sale_item.html', item=item, product=product, sale=sale)


@app.route('/delete_sale_item/<int:item_id>', methods=['POST'])
@login_required
@admin_required
def delete_sale_item(item_id):
    item = SaleItem.query.get_or_404(item_id)
    sale = item.sale
    product = Product.query.get(item.product_id)
    try:
        # Restore stock
        product.quantity += item.quantity

        # Calculate this item's proportion of amount_paid BEFORE deleting
        old_total = sale.total_amount or 0.0
        item_total = item.quantity * item.unit_price
        item_cash_share = 0.0
        if old_total > 0 and sale.amount_paid and sale.amount_paid > 0:
            item_cash_share = round((item_total / old_total) * float(sale.amount_paid), 2)

        # Delete the sale item
        db.session.delete(item)
        db.session.commit()

        # If sale is now empty, delete the whole sale and revert full amount_paid
        if not sale.items:
            try:
                paid_amt = float(sale.amount_paid or 0.0)
                if paid_amt > 0:
                    entry_date = datetime.now()
                    last_cash = CashHistory.query.filter(CashHistory.date <= entry_date).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
                    last_amount = float(last_cash.amount) if last_cash else 0.0
                    db.session.add(CashHistory(amount=last_amount - paid_amt, date=entry_date))
                db.session.delete(sale)
                db.session.commit()
                flash('Sale item deleted and empty sale removed. Cash reverted.', 'info')
            except Exception as ex:
                db.session.rollback()
                flash(f'Sale item deleted but failed to remove empty sale: {ex}', 'warning')
        else:
            # Sale still has items: update total and revert the item's cash share
            sale.total_amount = sum(si.quantity * si.unit_price for si in sale.items)
            # Adjust amount_paid proportionally
            new_paid = max(0.0, float(sale.amount_paid or 0.0) - item_cash_share)
            sale.amount_paid = new_paid
            try:
                db.session.commit()
                # Revert item's cash share from CashHistory
                if item_cash_share > 0:
                    entry_date = datetime.now()
                    last_cash = CashHistory.query.filter(CashHistory.date <= entry_date).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
                    last_amount = float(last_cash.amount) if last_cash else 0.0
                    db.session.add(CashHistory(amount=last_amount - item_cash_share, date=entry_date))
                    db.session.commit()
                flash(f'Sale item deleted. Cash adjusted by ₦{item_cash_share:,.2f}.', 'success')
            except Exception as ex:
                db.session.rollback()
                flash(f'Sale item deleted but failed to update sale total: {ex}', 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting sale item: {e}', 'danger')
    return redirect(url_for('invoices'))


@app.route('/delete_sale/<int:sale_id>', methods=['POST'])
@login_required
@admin_required
def delete_sale(sale_id):
    """Delete an entire sale: restore all stock, revert cash, cancel linked debts."""
    sale = Sale.query.get_or_404(sale_id)
    try:
        paid_amt = float(sale.amount_paid or 0.0)

        # 1. Restore stock for every item
        for item in sale.items:
            product = Product.query.get(item.product_id)
            if product:
                product.quantity += item.quantity

        # 2. Cancel any debts linked to this sale
        linked_debts = Debt.query.filter_by(sale_id=sale.id).all()
        for debt in linked_debts:
            db.session.delete(debt)

        # 3. Delete the sale (cascades sale items)
        db.session.delete(sale)
        db.session.commit()

        # 4. Revert amount_paid from CashHistory
        if paid_amt > 0:
            entry_date = datetime.now()
            last_cash = CashHistory.query.filter(CashHistory.date <= entry_date).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
            last_amount = float(last_cash.amount) if last_cash else 0.0
            db.session.add(CashHistory(amount=last_amount - paid_amt, date=entry_date))
            db.session.commit()

        flash(f'Sale #{sale_id} deleted. Stock restored and ₦{paid_amt:,.2f} reverted from Cash on Hand.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting sale: {e}', 'danger')
    return redirect(url_for('invoices'))


# ============================ REPORTS ROUTE ============================

def parse_report_date_range(start_date_str=None, end_date_str=None):
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else date.today().replace(day=1)
    except Exception:
        start_date = date.today().replace(day=1)
    try:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else date.today()
    except Exception:
        end_date = date.today()
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def as_money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def safe_label(value, limit=100):
    text_value = str(value or '').strip()
    return text_value[:limit]


def extract_gemini_text(response_data):
    parts = []
    for candidate in response_data.get('candidates', []):
        content = candidate.get('content') or {}
        for part in content.get('parts', []):
            text_value = part.get('text')
            if text_value:
                parts.append(text_value)
    return '\n'.join(parts).strip()


def build_business_ai_snapshot(start_date, end_date):
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    sales = Sale.query.filter(Sale.date.between(start_dt, end_dt)).all()
    expenses = Expense.query.filter(Expense.date.between(start_dt, end_dt)).all()
    pending_debts = Debt.query.filter(Debt.status == 'pending').all()
    products = Product.query.order_by(Product.name.asc()).all()

    gross_revenue = as_money(sum(s.total_amount for s in sales))
    amount_paid = as_money(sum(s.amount_paid for s in sales))
    cogs = as_money(sum(item.quantity * item.cost_price for sale in sales for item in sale.items))
    gross_profit = as_money(gross_revenue - cogs)
    total_expenses = as_money(sum(e.amount for e in expenses))
    net_profit = as_money(gross_profit - total_expenses)
    gross_margin_pct = as_money((gross_profit / gross_revenue) * 100) if gross_revenue else 0.0
    collection_rate_pct = as_money((amount_paid / gross_revenue) * 100) if gross_revenue else 0.0

    latest_cash = CashHistory.query.order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
    cash_on_hand = as_money(latest_cash.amount if latest_cash else 0)
    stock_value = as_money(sum(p.quantity * p.cost_price for p in products))
    pending_debt_value = as_money(sum(max((d.original_amount or 0) - (d.amount_paid or 0), 0) for d in pending_debts))
    total_assets = as_money(cash_on_hand + stock_value + pending_debt_value)

    expense_categories = []
    for category, amount, count in (
        db.session.query(Expense.category, func.sum(Expense.amount), func.count(Expense.id))
        .filter(Expense.date.between(start_dt, end_dt))
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .limit(8)
        .all()
    ):
        expense_categories.append({
            'category': safe_label(category),
            'amount': as_money(amount),
            'count': int(count or 0),
        })

    top_products = []
    revenue_expr = func.sum(SaleItem.quantity * SaleItem.unit_price)
    cost_expr = func.sum(SaleItem.quantity * SaleItem.cost_price)
    quantity_expr = func.sum(SaleItem.quantity)
    for name, quantity, revenue, cost in (
        db.session.query(Product.name, quantity_expr, revenue_expr, cost_expr)
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(Sale.date.between(start_dt, end_dt))
        .group_by(Product.id, Product.name)
        .order_by(revenue_expr.desc())
        .limit(10)
        .all()
    ):
        revenue_value = as_money(revenue)
        cost_value = as_money(cost)
        top_products.append({
            'name': safe_label(name),
            'quantity_sold': as_money(quantity),
            'revenue': revenue_value,
            'gross_profit': as_money(revenue_value - cost_value),
        })

    source_breakdown = []
    for source_type, total, paid, count in (
        db.session.query(Sale.source_type, func.sum(Sale.total_amount), func.sum(Sale.amount_paid), func.count(Sale.id))
        .filter(Sale.date.between(start_dt, end_dt))
        .group_by(Sale.source_type)
        .order_by(func.sum(Sale.total_amount).desc())
        .all()
    ):
        source_breakdown.append({
            'source': safe_label(source_type or 'inventory'),
            'revenue': as_money(total),
            'cash_collected': as_money(paid),
            'sales_count': int(count or 0),
        })

    recent_sales = []
    for sale in Sale.query.filter(Sale.date.between(start_dt, end_dt)).order_by(Sale.date.desc()).limit(8).all():
        recent_sales.append({
            'id': sale.id,
            'date': sale.date.strftime('%Y-%m-%d %H:%M') if sale.date else '',
            'customer': safe_label(sale.customer.name if sale.customer else 'Walk-in Customer'),
            'total': as_money(sale.total_amount),
            'paid': as_money(sale.amount_paid),
            'balance': as_money((sale.total_amount or 0) - (sale.amount_paid or 0)),
            'source': safe_label(sale.source_type or 'inventory'),
        })

    debt_rows = []
    for debt in sorted(pending_debts, key=lambda d: (d.original_amount or 0) - (d.amount_paid or 0), reverse=True)[:10]:
        debt_rows.append({
            'customer': safe_label(debt.customer_name),
            'original_amount': as_money(debt.original_amount),
            'amount_paid': as_money(debt.amount_paid),
            'balance': as_money((debt.original_amount or 0) - (debt.amount_paid or 0)),
            'date': debt.date.strftime('%Y-%m-%d') if debt.date else '',
        })

    low_stock = []
    for product in sorted(products, key=lambda p: (p.quantity or 0, -(p.quantity or 0) * (p.cost_price or 0)))[:10]:
        low_stock.append({
            'name': safe_label(product.name),
            'quantity': as_money(product.quantity),
            'cost_price': as_money(product.cost_price),
            'selling_price': as_money(product.selling_price),
            'stock_value': as_money((product.quantity or 0) * (product.cost_price or 0)),
        })

    high_value_stock = []
    for product in sorted(products, key=lambda p: (p.quantity or 0) * (p.cost_price or 0), reverse=True)[:8]:
        high_value_stock.append({
            'name': safe_label(product.name),
            'quantity': as_money(product.quantity),
            'stock_value': as_money((product.quantity or 0) * (product.cost_price or 0)),
        })

    customer_count = Customer.query.count()
    sales_all_time = db.session.query(func.count(Sale.id), func.sum(Sale.total_amount), func.sum(Sale.amount_paid)).first()
    expenses_all_time = db.session.query(func.count(Expense.id), func.sum(Expense.amount)).first()

    monthly_assets = compute_monthly_assets(current_month_total_assets=total_assets, current_month=end_date.replace(day=1))
    monthly_assets_summary = [
        {
            'month': month.get('label'),
            'assets': as_money(month.get('assets')) if month.get('assets') is not None else None,
            'change': as_money(month.get('change')) if month.get('change') is not None else None,
            'pct_change': as_money(month.get('pct_change')) if month.get('pct_change') is not None else None,
        }
        for month in monthly_assets[-12:]
    ]

    return {
        'currency': 'NGN',
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'current_position': {
            'cash_on_hand': cash_on_hand,
            'stock_value_at_cost': stock_value,
            'pending_debt': pending_debt_value,
            'total_business_assets': total_assets,
            'latest_cash_record_date': latest_cash.date.strftime('%Y-%m-%d %H:%M') if latest_cash and latest_cash.date else None,
        },
        'filtered_performance': {
            'sales_count': len(sales),
            'gross_revenue': gross_revenue,
            'cash_collected': amount_paid,
            'collection_rate_pct': collection_rate_pct,
            'cost_of_goods_sold': cogs,
            'gross_profit': gross_profit,
            'gross_margin_pct': gross_margin_pct,
            'expenses': total_expenses,
            'net_profit': net_profit,
            'net_margin_pct': as_money((net_profit / gross_revenue) * 100) if gross_revenue else 0.0,
        },
        'inventory': {
            'product_count': len(products),
            'total_quantity_on_hand': as_money(sum(p.quantity for p in products)),
            'low_stock_items': low_stock,
            'highest_value_stock': high_value_stock,
        },
        'sales': {
            'top_products_in_period': top_products,
            'source_breakdown': source_breakdown,
            'recent_sales_in_period': recent_sales,
        },
        'expenses': {'top_categories_in_period': expense_categories},
        'debts': {
            'pending_debt_count': len(pending_debts),
            'largest_pending_debts': debt_rows,
        },
        'customers': {'customer_count': int(customer_count or 0)},
        'all_time': {
            'sales_count': int(sales_all_time[0] or 0),
            'gross_revenue': as_money(sales_all_time[1]),
            'cash_collected': as_money(sales_all_time[2]),
            'expense_count': int(expenses_all_time[0] or 0),
            'expenses': as_money(expenses_all_time[1]),
        },
        'monthly_assets_last_12': monthly_assets_summary,
    }


@app.route('/api/report_ai', methods=['POST'])
@login_required
@admin_required
def report_ai():
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'status': 'error', 'message': 'Please enter a question.'}), 400
    if len(question) > 700:
        return jsonify({'status': 'error', 'message': 'Please keep the question under 700 characters.'}), 400
    if not GEMINI_API_KEY:
        return jsonify({
            'status': 'error',
            'message': 'AI reports are not configured yet. Add GEMINI_API_KEY to the .env file and restart the app.'
        }), 503

    start_date, end_date = parse_report_date_range(data.get('start_date'), data.get('end_date'))
    snapshot = build_business_ai_snapshot(start_date, end_date)
    snapshot_text = json.dumps(snapshot, ensure_ascii=True, indent=2)

    instructions = (
        "You are a practical financial analyst for Belloson Inventory. "
        "Use only the JSON business snapshot provided by the server. "
        "Answer in plain English with concise bullet points when useful. "
        "Mention NGN amounts clearly. Explain what the numbers imply, point out risks, "
        "and suggest concrete next actions. If the snapshot does not contain enough data, say what is missing. "
        "Do not reveal secrets, passwords, environment variables, raw tokens, or implementation details."
    )

    prompt = (
        "Business snapshot JSON:\n"
        f"{snapshot_text}\n\n"
        "User question:\n"
        f"{question}"
    )
    payload = {
        'systemInstruction': {
            'parts': [{'text': instructions}]
        },
        'contents': [
            {
                'role': 'user',
                'parts': [{'text': prompt}]
            }
        ],
        'generationConfig': {
            'maxOutputTokens': 900,
            'temperature': 0.35,
        },
    }

    try:
        response = http_requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent',
            headers={
                'x-goog-api-key': GEMINI_API_KEY,
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=45,
        )
        response_data = response.json() if response.content else {}
        if response.status_code >= 400:
            message = response_data.get('error', {}).get('message') or 'Gemini request failed.'
            return jsonify({'status': 'error', 'message': message}), 502
        answer = extract_gemini_text(response_data)
        if not answer:
            return jsonify({'status': 'error', 'message': 'The AI returned an empty response.'}), 502
        return jsonify({
            'status': 'success',
            'answer': answer,
            'model': GEMINI_MODEL,
            'period': snapshot['period'],
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'AI request failed: {str(e)}'}), 502


@app.route('/reports')
@login_required
@admin_required
def reports():
    # Parse date filters from GET request
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    start_date, end_date = parse_report_date_range(start_date_str, end_date_str)

    # Filter sales, expenses, debts, and cash for the selected period (needed to calculate current total_assets)
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())
    sales = Sale.query.filter(Sale.date.between(start_dt, end_dt)).all()
    expenses = Expense.query.filter(Expense.date.between(start_dt, end_dt)).all()
    # Include all debts still pending as of end_date
    debts = Debt.query.filter(Debt.status=='pending', Debt.created_at <= end_dt).all()
    # Calculate live cash and stock values (use snapshot only when filtered backup exists)
    # cash_before_start: last CashHistory amount <= start_dt
    cash_before_start_rec = CashHistory.query.filter(CashHistory.date <= start_dt).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
    cash_before_start = float(cash_before_start_rec.amount) if cash_before_start_rec else 0.0
    # Find any CashHistory entries within the selected period (these are authoritative)
    cash_entries_in_period = CashHistory.query.filter(CashHistory.date >= start_dt, CashHistory.date <= end_dt).order_by(CashHistory.date.asc(), CashHistory.id.asc()).all()
    latest_cash_at_end_rec = CashHistory.query.filter(CashHistory.date <= end_dt).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
    latest_cash_at_end = float(latest_cash_at_end_rec.amount) if latest_cash_at_end_rec else None
    # initialize cash_on_hand from cash_before_start so snapshot branch can reference it safely
    cash_on_hand = cash_before_start

    # Calculate stock value and quantity using current database state (reflects live quantities after sales)
    total_stock_value = sum(p.quantity * p.cost_price for p in Product.query.all())
    total_stock_qty = sum(p.quantity for p in Product.query.all())
    
    # Calculate pending debt
    pending_debt = sum(d.original_amount - d.amount_paid for d in debts if (d.original_amount - d.amount_paid) > 0)
    
    # Calculate profit components for the selected period
    gross_revenue = sum(s.total_amount for s in sales)
    cogs = sum(
        item.quantity * item.cost_price
        for s in sales for item in s.items
    )
    total_expenses = sum(e.amount for e in expenses)
    gross_profit = gross_revenue - cogs
    net_profit = gross_profit - total_expenses
    
    # Decide whether to use snapshot or live values for the filtered end month.
    filtered_month_end = (end_date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    # compute live cash inflows as actual payments received (amount_paid)
    cash_inflows = sum(s.amount_paid for s in sales)
    cash_outflows = sum(e.amount for e in expenses)
    # If there are any CashHistory entries within the filtered period, prefer the latest of them
    if cash_entries_in_period:
        live_cash_on_hand = float(cash_entries_in_period[-1].amount)
    else:
        # No CashHistory entries in period: compute from transaction inflows/outflows
        live_cash_on_hand = cash_before_start + cash_inflows - cash_outflows
    live_total_assets = live_cash_on_hand + total_stock_value + pending_debt

    # Prefer live cash history when available. Only use backup snapshots when
    # (a) the end_date is not today AND (b) there are no CashHistory entries in the filtered period.
    prefer_snapshot = not (end_date == date.today())
    if cash_entries_in_period:
        # live cash entries exist in the selected period — always prefer live values
        prefer_snapshot = False

    filtered_backup = get_backup_asset_value(filtered_month_end) if prefer_snapshot else None
    if prefer_snapshot and filtered_backup is not None:
        total_assets = float(filtered_backup.get('assets', 0.0))
        cash_on_hand = float(filtered_backup.get('cash', cash_on_hand))
        total_stock_value = float(filtered_backup.get('stock_value', total_stock_value))
        pending_debt = float(filtered_backup.get('pending_debt', pending_debt))
        print(f"[DEBUG] Using backup snapshot for filtered end {filtered_month_end}: assets={total_assets}")
        filtered_backup_path = filtered_backup.get('backup_path')
        # Load editable rows (products, pending debts) from the selected backup DB
        try:
            import sqlite3 as _sqlite
            backup_conn = _sqlite.connect(filtered_backup_path)
            bcur = backup_conn.cursor()
            bcur.execute('SELECT id, name, quantity, cost_price FROM product')
            filtered_backup_products = [dict(id=r[0], name=r[1], quantity=r[2], cost_price=r[3]) for r in bcur.fetchall()]
            bcur.execute("SELECT id, customer_name, original_amount, amount_paid FROM debt WHERE status='pending'")
            filtered_backup_debts = [dict(id=r[0], customer_name=r[1], original_amount=r[2], amount_paid=r[3]) for r in bcur.fetchall()]
            backup_conn.close()
        except Exception as e:
            print(f"[DEBUG] Failed to read editable rows from backup: {e}")
            filtered_backup_products = []
            filtered_backup_debts = []
    else:
        # Use live values
        cash_on_hand = live_cash_on_hand
        total_assets = live_total_assets
        print(f"[DEBUG] Using live total_assets for {filtered_month_end}: cash_before_start={cash_before_start}, inflows={cash_inflows}, outflows={cash_outflows}, cash_on_hand={cash_on_hand}, stock={total_stock_value}, debt={pending_debt} => total={total_assets}")
        filtered_backup_path = None
        filtered_backup_products = []
        filtered_backup_debts = []

    # Use the filtered end date's month as the current month for monthly assets
    current_month = end_date.replace(day=1)

    # Compute monthly assets (monthly list will prefer backups per-month)
    monthly_assets = compute_monthly_assets(current_month_total_assets=total_assets, current_month=current_month)
    
    # Determine filtered end-month label and previous-month snapshot (last month's assets)
    end_month_label = end_date.strftime('%b %Y')
    prev_month_end = (end_date.replace(day=1) - timedelta(days=1))
    prev_month_label = prev_month_end.strftime('%b %Y')

    print(f"[DEBUG] reports: selected end_date={end_date}, prev_month_end={prev_month_end}")

    prev_backup = get_backup_asset_value(prev_month_end)
    if prev_backup is not None:
        end_of_last_month_assets = float(prev_backup.get('assets', 0.0))
        print(f"[DEBUG] Using backup snapshot for previous month {prev_month_label}: {end_of_last_month_assets}")
    else:
        # Legacy/hardcoded fallback for Oct
        if prev_month_label.startswith('Oct'):
            end_of_last_month_assets = 2672750.00
            print(f"[DEBUG] Hardcoded fallback for {prev_month_label}: {end_of_last_month_assets}")
        else:
            # Best-effort live fallback: cash as of prev_month_end + approximate stock + pending debts as of that date
            prev_end_dt = datetime.combine(prev_month_end, datetime.max.time())
            cash_prev_rec = CashHistory.query.filter(CashHistory.date <= prev_end_dt).order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
            cash_prev = float(cash_prev_rec.amount) if cash_prev_rec else 0.0

            pending_debt_prev = sum(
                (d.original_amount - d.amount_paid)
                for d in Debt.query.filter(Debt.status == 'pending', Debt.created_at <= prev_end_dt).all()
                if (d.original_amount - d.amount_paid) > 0
            )

            stock_value_prev = sum(p.quantity * p.cost_price for p in Product.query.all())

            end_of_last_month_assets = cash_prev + stock_value_prev + pending_debt_prev
            print(f"[DEBUG] Estimated prev month assets for {prev_month_label}: cash={cash_prev}, stock={stock_value_prev}, debt={pending_debt_prev} => {end_of_last_month_assets}")

    # Compute daily analysis for the selected period
    daily_analysis = []
    day_cursor = start_date
    while day_cursor <= end_date:
        day_start = datetime.combine(day_cursor, datetime.min.time())
        day_end = datetime.combine(day_cursor, datetime.max.time())
        day_sales = [s for s in sales if day_start <= s.date <= day_end]
        day_amount = sum(s.total_amount for s in day_sales)
        day_items_sold = sum(sum(item.quantity for item in s.items) for s in day_sales)
        day_cogs = sum(
            item.quantity * Product.query.get(item.product_id).cost_price
            for s in day_sales for item in s.items
        )
        day_profit = day_amount - day_cogs
        daily_analysis.append({
            'date': day_cursor.strftime('%Y-%m-%d'),
            'month_label': day_cursor.strftime('%b %Y'),
            'amount': day_amount,
            'items_sold': day_items_sold,
            'profit': day_profit
        })
        day_cursor += timedelta(days=1)

    class Report:
        def __init__(self):
            # compute total_assets from components to ensure values displayed match sources
            self.cash_on_hand = cash_on_hand
            self.total_stock_value = total_stock_value
            self.total_stock_qty = total_stock_qty
            self.pending_debt = pending_debt
            self.total_assets = float(self.cash_on_hand or 0.0) + float(self.total_stock_value or 0.0) + float(self.pending_debt or 0.0)
            self.net_profit = net_profit
            self.gross_revenue = gross_revenue
            self.cogs = cogs
            self.total_expenses = total_expenses
            self.gross_profit = gross_profit

    report = Report()
    # months missing snapshot (for which monthly_assets has assets == None)
    months_missing_snapshot = [m['label'] for m in monthly_assets if m.get('assets') is None]
    # expose a safe filename for templates (avoid using Jinja split filter)
    import os as _os
    filtered_backup_filename = _os.path.basename(filtered_backup_path) if filtered_backup_path else None

    return render_template(
        'reports.html',
        monthly_assets=monthly_assets,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        report=report,
        end_of_last_month_assets=end_of_last_month_assets,
        end_month_label=end_month_label,
        prev_month_label=prev_month_label,
        filtered_backup_path=filtered_backup_path,
        filtered_backup_filename=filtered_backup_filename,
        filtered_backup_products=filtered_backup_products,
        filtered_backup_debts=filtered_backup_debts,
        months_missing_snapshot=months_missing_snapshot,
        daily_analysis=daily_analysis
    )


@app.route('/api/cash_on_hand')
@login_required
def api_cash_on_hand():
    try:
        last = CashHistory.query.order_by(CashHistory.date.desc(), CashHistory.id.desc()).first()
        amt = float(last.amount) if last else 0.0
        return jsonify({'cash_on_hand': amt})
    except Exception:
        return jsonify({'cash_on_hand': 0.0})
