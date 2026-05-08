from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask_login import UserMixin

db = SQLAlchemy()

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=True)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    # Nullable to support Clerk-only accounts (gradual migration)
    password = db.Column(db.String(150), nullable=True)
    full_name = db.Column(db.String(100), nullable=True)
    profile_image = db.Column(db.String(20), nullable=False, default='default.jpg')
    role = db.Column(db.String(20), nullable=False, default='user')
    # Clerk integration fields
    clerk_user_id = db.Column(db.String(255), unique=True, nullable=True, index=True)
    clerk_email = db.Column(db.String(255), nullable=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    # FIX: Changed quantity to Float to allow for decimals
    quantity = db.Column(db.Float, nullable=False)

    selling_price = db.Column(db.Float, nullable=False)
    cost_price = db.Column(db.Float, nullable=False)
    expiry_date = db.Column(db.Date)

    # ✅ NEW FIELD (used in historical monthly stock calculations)
    initial_quantity = db.Column(db.Float, nullable=False, default=0)

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(150), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, nullable=False)
    edited_date = db.Column(db.DateTime, nullable=True)

class Debt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    original_amount = db.Column(db.Float, nullable=False)
    amount_paid = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), default='pending')
    date = db.Column(db.DateTime, nullable=False)

    # Add created_at for historical queries
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Optional link to the originating Sale (nullable)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=True)

    @property
    def balance(self):
        return self.original_amount - self.amount_paid

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20))
    sales = db.relationship('Sale', backref='customer', lazy=True)

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total_amount = db.Column(db.Float)
    amount_paid = db.Column(db.Float, nullable=False, default=0.0)
    authorized_by = db.Column(db.String(50), nullable=True)
    source_type = db.Column(db.String(50), nullable=True, default='inventory')

    items = db.relationship('SaleItem', backref='sale', lazy=True, cascade="all, delete-orphan")

    @property
    def balance_due(self):
        return self.total_amount - self.amount_paid

class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)

    # FIX: Quantity is float
    quantity = db.Column(db.Float, nullable=False)

    unit_price = db.Column(db.Float, nullable=False)
    cost_price = db.Column(db.Float, nullable=False, default=0.0)  # Store cost price at time of sale
    product = db.relationship('Product')

# ✅ NEW TABLE (for updated cash tracking system)
class CashHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)

# ✅ Manual override for total business assets per month (e.g. "Nov 2025")
class MonthlyAssetOverride(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    month_key = db.Column(db.String(20), unique=True, nullable=False)  # e.g. "Nov 2025"
    overridden_assets = db.Column(db.Float, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
