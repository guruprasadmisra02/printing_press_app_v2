import os, shutil
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import sqlite3
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = 'supersecretkey'
DB_NAME = os.environ.get('DB_PATH', 'printing_press.db')
# If running on Render with a disk mount, copy seed DB once
disk_path = os.environ.get('DB_PATH')
if disk_path and not os.path.exists(disk_path):
    src = os.path.join(os.path.dirname(__file__), 'printing_press.db')
    # if you committed a starter DB, copy it to disk; otherwise init_db() will create tables
    if os.path.exists(src):
        os.makedirs(os.path.dirname(disk_path), exist_ok=True)
        shutil.copy2(src, disk_path)



# ------------------ DB Helpers ------------------
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


@app.context_processor
def inject_current_year():
    return {'current_year': date.today().year}


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # ---------------- USERS TABLE ----------------
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        username TEXT,
        password TEXT,
        phone TEXT,
        role TEXT CHECK(role IN ('owner', 'worker', 'customer'))
    )''')

    # ---------------- ORDERS TABLE ----------------
    cur.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        customer_name TEXT,
        product_name TEXT,
        size TEXT,
        colour TEXT,
        quantity REAL,
        total_cost REAL,
        amount_paid REAL DEFAULT 0,
        date TEXT,
        status TEXT,
        receive_date TEXT,
        FOREIGN KEY (customer_id) REFERENCES users(id)
    )''')

    # ✅ Ensure `customer_name` column exists (for backward compatibility)
    try:
        cur.execute("ALTER TABLE orders ADD COLUMN customer_name TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists — ignore error safely

    # ---------------- STOCK TABLE ----------------
    cur.execute('''CREATE TABLE IF NOT EXISTS stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT NOT NULL,
        item_no TEXT,
        quantity REAL DEFAULT 0,
        unit_cost REAL DEFAULT 0,
        total_amount REAL DEFAULT 0,
        size TEXT,
        last_updated TEXT
    )''')

    # ---------------- STOCK ADDITIONS (for month-wise tracking) ----------------
    cur.execute('''CREATE TABLE IF NOT EXISTS stock_additions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT,
        item_no TEXT,
        quantity REAL,
        unit_cost REAL,
        total_amount_added REAL,
        date_added TEXT
    )''')

    # ---------------- ORDER ITEMS USED (stock deduction tracking) ----------------
    cur.execute('''CREATE TABLE IF NOT EXISTS order_items_used (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        stock_item_id INTEGER,
        quantity_used REAL,
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (stock_item_id) REFERENCES stock(id)
    )''')

    # ---------------- EXPENSES TABLE ----------------
    cur.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_name TEXT,
        amount REAL,
        description TEXT,
        date TEXT
    )''')

    # ---------------- QUOTES TABLE (for public requests) ----------------
    cur.execute('''CREATE TABLE IF NOT EXISTS quotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        email TEXT,
        product TEXT NOT NULL,
        quantity INTEGER,
        message TEXT,
        date TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()

    # ---------------- DEFAULT USERS (Optional) ----------------
    # Add an owner and worker account if not already present
    cur.execute("SELECT COUNT(*) FROM users WHERE role='owner'")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (name, username, password, role) VALUES (?,?,?,?)",
                    ('Admin', 'owner', 'owner123', 'owner'))
        print("✅ Default Owner created: username='owner', password='owner123'")

    cur.execute("SELECT COUNT(*) FROM users WHERE role='worker'")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (name, username, password, role) VALUES (?,?,?,?)",
                    ('Worker', 'worker', 'worker123', 'worker'))
        print("✅ Default Worker created: username='worker', password='worker123'")

    conn.commit()
    conn.close()

# ------------------ Utility ------------------
def ensure_logged_in(role=None):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if role and session.get('role') != role:
        flash('Unauthorized access', 'danger')
        return redirect(url_for('login'))
    return None


def month_filter():
    m = request.args.get('month')
    if not m:
        m = date.today().strftime('%Y-%m')
    return m


# ------------------ Stock Upsert ------------------
def upsert_stock(conn, item_name, item_no, size, added_qty, addition_total_cost):
    cur = conn.cursor()
    item_no = (item_no or '').strip()
    size = (size or '').strip()
    add_qty = float(added_qty)
    add_cost = float(addition_total_cost)
    today = date.today().isoformat()

    if item_no:
        cur.execute("SELECT * FROM stock WHERE item_no=?", (item_no,))
    else:
        cur.execute("SELECT * FROM stock WHERE item_name=? AND IFNULL(size,'')=?", (item_name, size))
    row = cur.fetchone()

    if row:
        old_qty = float(row['quantity'])
        old_unit = float(row['unit_cost'])
        new_qty = old_qty + add_qty
        new_unit = (old_qty * old_unit + add_cost) / new_qty if new_qty > 0 else 0.0
        new_total = new_qty * new_unit
        cur.execute("""UPDATE stock
                          SET quantity=?, unit_cost=?, total_amount=?, last_updated=?
                        WHERE id=?""",
                    (new_qty, new_unit, new_total, today, row['id']))
    else:
        unit_cost = add_cost / add_qty if add_qty > 0 else 0.0
        total_amount = add_qty * unit_cost
        cur.execute("""INSERT INTO stock (item_name, item_no, quantity, unit_cost, size, total_amount, last_updated)
                       VALUES (?,?,?,?,?,?,?)""",
                    (item_name, item_no, add_qty, unit_cost, size, total_amount, today))

    # Log purchase
    cur.execute("""INSERT INTO stock_additions (item_name, item_no, quantity, unit_cost, total_amount_added, date_added)
                   VALUES (?,?,?,?,?,?)""",
                (item_name, item_no, add_qty,
                 (add_cost / add_qty) if add_qty > 0 else 0.0,
                 add_cost, today))
    conn.commit()


# ------------------ Items Used Helper ------------------
def add_item_usage(conn, order_id, stock_id, qty_used):
    qty_used = float(qty_used)
    if qty_used <= 0:
        return "Quantity must be > 0"
    cur = conn.cursor()
    cur.execute("SELECT id, quantity, unit_cost FROM stock WHERE id=?", (stock_id,))
    s = cur.fetchone()
    if not s:
        return "Stock item not found"
    if float(s['quantity']) < qty_used:
        return "Not enough stock"
    cur.execute("""UPDATE stock
                      SET quantity = quantity - ?,
                          total_amount = (quantity - ?) * unit_cost,
                          last_updated = ?
                    WHERE id=?""",
                (qty_used, qty_used, date.today().isoformat(), stock_id))
    cur.execute("""INSERT INTO order_items_used (order_id, stock_item_id, quantity_used)
                   VALUES (?,?,?)""",
                (order_id, stock_id, qty_used))
    conn.commit()
    return None


# ------------------ Public Pages ------------------
@app.route('/')
def home():
    # Default landing page shows About Us
    return render_template('about.html')

# ------------------ Auth ------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
        user = cur.fetchone()
        conn.close()
        if user:
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['name'] = user['name']
            if user['role'] == 'owner':
                return redirect(url_for('owner_home'))
            elif user['role'] == 'worker':
                return redirect(url_for('worker_home'))
            else:
                flash('Use Customer Login below', 'warning')
        else:
            flash('Invalid credentials', 'danger')
    return render_template('index.html')


@app.route('/customer_login', methods=['POST'])
def customer_login():
    phone = request.form.get('phone', '').strip()
    name = request.form.get('name', '').strip() or phone
    if not phone:
        flash('Phone required', 'danger')
        return redirect(url_for('login'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE phone=? AND role='customer'", (phone,))
    user = cur.fetchone()
    if not user:
        cur.execute("INSERT INTO users (name, phone, role) VALUES (?,?,?)", (name, phone, 'customer'))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE phone=? AND role='customer'", (phone,))
        user = cur.fetchone()
    conn.close()
    session['user_id'] = user['id']
    session['role'] = 'customer'
    session['name'] = user['name']
    return redirect(url_for('customer_home'))


@app.route('/logout')
def logout():
    session.clear
    session.clear()
    return redirect(url_for('login'))


# ------------------ Customer ------------------
@app.route('/customer/home')
def customer_home():
    guard = ensure_logged_in('customer')
    if guard: return guard
    return render_template('customer_home.html')


@app.route('/customer/place_order', methods=['GET', 'POST'])
def customer_place_order():
    guard = ensure_logged_in('customer')
    if guard: return guard
    if request.method == 'POST':
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""INSERT INTO orders (customer_id, product_name, size, colour, quantity, date, status, customer_name)
               VALUES (?,?,?,?,?,?,?,?)""",
            (session['user_id'],
             request.form['product_name'],
             request.form['size'],
             request.form['colour'],
             float(request.form['quantity']),
             date.today().isoformat(),
             'Pending',
             session.get('name', 'Unknown')))

        conn.commit()
        conn.close()
        flash('Order placed!', 'success')
        return redirect(url_for('customer_orders'))
    return render_template('customer_place_order.html', today=date.today().isoformat())


@app.route('/customer/orders')
def customer_orders():
    guard = ensure_logged_in('customer')
    if guard: return guard
    month = month_filter()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT * FROM orders
                   WHERE customer_id=? AND substr(date,1,7)=?
                   ORDER BY id DESC""", (session['user_id'], month))
    rows = cur.fetchall()
    conn.close()
    return render_template('customer_orders.html', orders=rows, month=month)


# Public: Request a Quote (saves to DB)
@app.route('/request_quote', methods=['POST'])
def request_quote():
    name = request.form['name']
    phone = request.form['phone']
    email = request.form['email']
    product = request.form['product']
    quantity = request.form['quantity']
    message = request.form['message']

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO quotes (name, phone, email, product, quantity, message)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (name, phone, email, product, quantity, message))
    conn.commit()
    conn.close()

    flash('Your quote request has been submitted successfully!', 'success')
    return redirect(url_for('home'))


# ------------------ Shared: Add Items Used Inline ------------------
@app.route('/orders/<int:order_id>/items/add', methods=['POST'])
def add_order_item(order_id):
    if 'role' not in session or session['role'] not in ('worker', 'owner'):
        flash('Unauthorized', 'danger')
        return redirect(url_for('login'))

    stock_id = int(request.form['stock_id'])
    qty_used = request.form['qty_used']

    conn = get_db()
    err = add_item_usage(conn, order_id, stock_id, qty_used)
    conn.close()

    if err:
        flash(err, 'danger')
    else:
        flash('Item usage saved & stock updated', 'success')

    if session['role'] == 'worker':
        return redirect(url_for('worker_orders'))
    else:
        return redirect(url_for('owner_orders'))


# ------------------ Worker ------------------
@app.route('/worker/home')
def worker_home():
    guard = ensure_logged_in('worker')
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT o.*, u.name AS customer_name
                     FROM orders o
                     JOIN users u ON u.id=o.customer_id
                    WHERE o.status IN ('Pending','In Progress')
                    ORDER BY o.date ASC""")
    rows = cur.fetchall()
    conn.close()
    return render_template('worker_home.html', orders=rows)

# ------------------ WORKER: Orders ------------------
@app.route('/worker/orders', methods=['GET', 'POST'])
def worker_orders():
    guard = ensure_logged_in('worker')
    if guard: 
        return guard

    conn = get_db()
    cur = conn.cursor()

    # Handle updates
    if request.method == 'POST':
        oid = int(request.form['order_id'])
        act = request.form['action']

        if act == 'update_status':
            s = request.form['status']
            rd = date.today().isoformat() if s == 'Completed' else None
            cur.execute("UPDATE orders SET status=?, receive_date=? WHERE id=?", (s, rd, oid))

        elif act == 'update_paid':
            p = float(request.form['amount_paid'] or 0)
            cur.execute("UPDATE orders SET amount_paid=COALESCE(amount_paid,0)+? WHERE id=?", (p, oid))

        conn.commit()
        conn.close()
        flash('Updated', 'success')
        return redirect(url_for('worker_orders'))

    # GET — list orders for selected month
    month = month_filter()
    cur.execute("""
        SELECT
          o.*,
          COALESCE(o.customer_name, u.name, u.phone, 'Unknown') AS customer_display,
          u.phone AS customer_phone
        FROM orders o
        LEFT JOIN users u ON u.id = o.customer_id
        WHERE substr(o.date, 1, 7) = ?
        ORDER BY o.id DESC
    """, (month,))
    orders = cur.fetchall()

    # Stock list for inline "Items Used" picker
    cur.execute("SELECT id, item_name, item_no, size, quantity FROM stock ORDER BY item_name")
    stock = cur.fetchall()

    conn.close()
    return render_template('worker_orders.html', orders=orders, stock=stock, month=month)


@app.route('/worker/stock', methods=['GET', 'POST'], endpoint='worker_stock')
def worker_stock():
    guard = ensure_logged_in('worker')
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    if request.method == 'POST':
        upsert_stock(conn,
                     request.form['item_name'],
                     request.form.get('item_no', ''),
                     request.form.get('size', ''),
                     float(request.form['added_quantity']),
                     float(request.form['addition_total_cost']))
        flash('Stock updated', 'success')
        return redirect(url_for('worker_stock'))
    cur.execute("SELECT * FROM stock ORDER BY item_name")
    rows = cur.fetchall()
    conn.close()
    return render_template('worker_stock.html', stock=rows)


# Worker: page to apply items used to a specific order
@app.route('/worker/orders/<int:oid>/items', methods=['GET', 'POST'])
def worker_order_items(oid):
    guard = ensure_logged_in('worker')
    if guard: return guard

    conn = get_db()
    cur = conn.cursor()

    if request.method == 'POST':
        # Iterate all stock items and deduct those that have qty_used_X value
        cur.execute("SELECT id FROM stock")
        for s in cur.fetchall():
            qty_used_val = request.form.get(f'qty_used_{s["id"]}')
            if not qty_used_val:
                continue
            qty_used = float(qty_used_val)
            if qty_used <= 0:
                continue
            # Deduct and log usage
            cur.execute(
                """UPDATE stock
                      SET quantity = quantity - ?,
                          total_amount = (quantity - ?) * unit_cost,
                          last_updated = ?
                    WHERE id = ?""",
                (qty_used, qty_used, date.today().isoformat(), s['id'])
            )
            cur2 = conn.cursor()
            cur2.execute(
                """INSERT INTO order_items_used (order_id, stock_item_id, quantity_used)
                   VALUES (?,?,?)""",
                (oid, s['id'], qty_used)
            )
        conn.commit()
        conn.close()
        flash('Items usage saved and stock updated', 'success')
        return redirect(url_for('worker_orders'))

    # GET
    cur.execute("SELECT * FROM stock ORDER BY item_name")
    stock = cur.fetchall()
    cur.execute("SELECT * FROM orders WHERE id=?", (oid,))
    order = cur.fetchone()
    conn.close()
    return render_template('worker_items_used.html', stock=stock, order=order)


# ------------------ Owner Dashboard (month-wise) ------------------
@app.route('/owner/home', methods=['GET', 'POST'])
def owner_home():
    # Auth
    if 'role' not in session or session['role'] != 'owner':
        return redirect(url_for('login'))

    # Month from form (POST) or default to current month
    selected_month = request.form.get('month')
    if not selected_month:
        selected_month = datetime.now().strftime('%Y-%m')

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # --- Totals for widgets ---
    # Orders in selected month
    cur.execute("""
        SELECT COUNT(*) FROM orders
        WHERE strftime('%Y-%m', date) = ?
    """, (selected_month,))
    total_orders = cur.fetchone()[0] or 0

    # Distinct stock items (live count of SKUs)
    cur.execute("SELECT COUNT(*) FROM stock")
    total_stock = cur.fetchone()[0] or 0

    # Other expenses recorded this month
    cur.execute("""
        SELECT IFNULL(SUM(amount), 0)
        FROM expenses
        WHERE strftime('%Y-%m', date) = ?
    """, (selected_month,))
    base_expenses = cur.fetchone()[0] or 0

    # ✅ Stock purchases in the selected month (from stock_additions)
    cur.execute("""
        SELECT IFNULL(SUM(total_amount_added), 0)
        FROM stock_additions
        WHERE strftime('%Y-%m', date_added) = ?
    """, (selected_month,))
    stock_purchases = cur.fetchone()[0] or 0

    # ✅ Total expenses for the month = other expenses + stock purchases this month
    total_expenses = (base_expenses or 0) + (stock_purchases or 0)

    # Income = amount_paid for orders dated in this month and completed
    cur.execute("""
        SELECT IFNULL(SUM(amount_paid), 0)
        FROM orders
        WHERE strftime('%Y-%m', date) = ?
          AND (status = 'Completed' OR status = 'completed')
    """, (selected_month,))
    total_income = cur.fetchone()[0] or 0

    # Profit/Loss
    profit_loss = (total_income or 0) - (total_expenses or 0)

    # (Optional KPI) Current live stock value = SUM(stock.total_amount)
    cur.execute("SELECT IFNULL(SUM(total_amount), 0) FROM stock")
    current_stock_value = cur.fetchone()[0] or 0

    # Quotes count (all-time)
    cur.execute("SELECT COUNT(*) FROM quotes")
    total_quotes = cur.fetchone()[0] or 0

    conn.close()

    return render_template(
        'owner_home.html',
        selected_month=selected_month,
        total_orders=total_orders,
        total_stock=total_stock,
        base_expenses=base_expenses,          # other expenses (this month)
        stock_purchases=stock_purchases,      # ✅ stock purchases (this month)
        total_expenses=total_expenses,        # ✅ used in P/L
        total_income=total_income,
        profit_loss=profit_loss,
        current_stock_value=current_stock_value,  # optional KPI card
        total_quotes=total_quotes
    )


@app.route('/owner/dashboard_data')
def owner_dashboard_data():
    if 'role' not in session or session['role'] != 'owner':
        return redirect(url_for('login'))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Last 6 months income (amount_paid from completed orders)
    cur.execute("""
        SELECT strftime('%Y-%m', date) AS month,
               IFNULL(SUM(amount_paid), 0) AS income
        FROM orders
        WHERE status = 'Completed'
        GROUP BY month
        ORDER BY month DESC
        LIMIT 6
    """)
    income_data = cur.fetchall()

    # Last 6 months expenses
    cur.execute("""
        SELECT strftime('%Y-%m', date) AS month,
               IFNULL(SUM(amount), 0) AS expenses
        FROM expenses
        GROUP BY month
        ORDER BY month DESC
        LIMIT 6
    """)
    expense_data = cur.fetchall()
    conn.close()

    income_dict = dict(income_data)
    expense_dict = dict(expense_data)
    all_months = sorted(set(list(income_dict.keys()) + list(expense_dict.keys())))

    months, incomes, expenses, profits = [], [], [], []
    for m in all_months:
        inc = income_dict.get(m, 0)
        exp = expense_dict.get(m, 0)
        months.append(m)
        incomes.append(inc)
        expenses.append(exp)
        profits.append(inc - exp)

    return jsonify({'months': months, 'incomes': incomes, 'expenses': expenses, 'profits': profits})


@app.route('/owner/quotes')
def owner_quotes():
    if 'role' not in session or session['role'] != 'owner':
        return redirect(url_for('login'))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM quotes ORDER BY date DESC")
    quotes = cur.fetchall()
    conn.close()
    return render_template('owner_quotes.html', quotes=quotes)


# ------------------ Owner: Orders / Stock / Expenses ------------------
# ------------------ OWNER: Orders ------------------
@app.route('/owner/orders', methods=['GET', 'POST'])
def owner_orders():
    guard = ensure_logged_in('owner')
    if guard: 
        return guard

    conn = get_db()
    cur = conn.cursor()

    # Handle updates
    if request.method == 'POST':
        oid = int(request.form['order_id'])
        action = request.form['action']

        if action == 'update_status':
            status = request.form['status']
            rd = date.today().isoformat() if status == 'Completed' else None
            cur.execute("UPDATE orders SET status=?, receive_date=? WHERE id=?", (status, rd, oid))

        elif action == 'update_paid':
            amt = float(request.form['amount_paid'] or 0)
            cur.execute("UPDATE orders SET amount_paid=COALESCE(amount_paid,0)+? WHERE id=?", (amt, oid))

        elif action == 'update_total_cost':
            tc = float(request.form['total_cost'] or 0)
            cur.execute("UPDATE orders SET total_cost=? WHERE id=?", (tc, oid))

        elif action == 'delete_order':
            # clean up items used (optional, keeps history tidy)
            cur.execute("DELETE FROM order_items_used WHERE order_id=?", (oid,))
            cur.execute("DELETE FROM orders WHERE id=?", (oid,))

        conn.commit()
        conn.close()
        flash('Order updated', 'success')
        return redirect(url_for('owner_orders'))

    # GET — list orders for selected month
    m = month_filter()
    cur.execute("""
        SELECT
          o.*,
          COALESCE(o.customer_name, u.name, u.phone, 'Unknown') AS customer_display,
          u.phone AS customer_phone
        FROM orders o
        LEFT JOIN users u ON u.id = o.customer_id
        WHERE substr(o.date, 1, 7) = ?
        ORDER BY o.id DESC
    """, (m,))
    orders = cur.fetchall()

    # Stock list for inline "Items Used" picker
    cur.execute("SELECT id, item_name, item_no, size, quantity FROM stock ORDER BY item_name")
    stock = cur.fetchall()

    conn.close()
    return render_template('owner_orders.html', orders=orders, month=m, stock=stock)


@app.route('/owner/orders/<int:order_id>/edit', methods=['GET', 'POST'])
def owner_edit_order(order_id):
    guard = ensure_logged_in('owner')
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    if request.method == 'POST':
        cur.execute("""UPDATE orders
                          SET product_name=?, size=?, colour=?, quantity=?,
                              total_cost=?, status=?, receive_date=?
                        WHERE id=?""",
                    (request.form['product_name'],
                     request.form['size'],
                     request.form['colour'],
                     float(request.form['quantity']),
                     float(request.form['total_cost'] or 0),
                     request.form['status'],
                     request.form.get('receive_date') or None,
                     order_id))
        conn.commit()
        conn.close()
        flash('Order updated!', 'success')
        return redirect(url_for('owner_orders'))
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    order = cur.fetchone()
    conn.close()
    return render_template('owner_edit_order.html', order=order)


@app.route('/owner/stock', methods=['GET', 'POST'])
def owner_stock():
    guard = ensure_logged_in('owner')
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    if request.method == 'POST':
        upsert_stock(conn,
                     request.form['item_name'],
                     request.form.get('item_no', ''),
                     request.form.get('size', ''),
                     float(request.form['added_quantity']),
                     float(request.form['addition_total_cost']))
        flash('Stock added or updated', 'success')
        return redirect(url_for('owner_stock'))
    cur.execute("SELECT * FROM stock ORDER BY item_name")
    rows = cur.fetchall()
    conn.close()
    return render_template('owner_stock.html', stock=rows)


@app.route('/expenses', methods=['GET', 'POST'])
def expenses():
    guard = ensure_logged_in('owner')
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    if request.method == 'POST':
        cur.execute("""INSERT INTO expenses (expense_name, amount, description, date)
                       VALUES (?,?,?,?)""",
                    (request.form['expense_name'],
                     float(request.form['amount']),
                     request.form.get('description', ''),
                     date.today().isoformat()))
        conn.commit()
        flash('Expense added!', 'success')
        return redirect(url_for('expenses'))
    cur.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC")
    rows = cur.fetchall()
    conn.close()
    return render_template('expenses.html', rows=rows)

@app.route('/bill/<int:order_id>')
def generate_bill(order_id):
    if 'role' not in session or session['role'] not in ('owner', 'worker'):
        flash('Unauthorized access', 'danger')
        return redirect(url_for('login'))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.*, u.name AS user_name
        FROM orders o
        JOIN users u ON u.id = o.customer_id
        WHERE o.id = ?
    """, (order_id,))
    order = cur.fetchone()
    conn.close()

    if not order:
        flash('Order not found', 'danger')
        return redirect(url_for('owner_orders' if session['role'] == 'owner' else 'worker_orders'))

    return render_template('bill.html', order=order)

@app.route('/customer/update_name', methods=['POST'])
def customer_update_name():
    guard = ensure_logged_in('customer')
    if guard: return guard

    new_name = request.form.get('name', '').strip()
    if not new_name:
        flash('Please enter a valid name.', 'danger')
        return redirect(url_for('customer_home'))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET name=? WHERE id=?", (new_name, session['user_id']))
    conn.commit()
    conn.close()

    # Keep session in sync
    session['name'] = new_name
    flash('Name/company updated successfully!', 'success')
    return redirect(url_for('customer_home'))


@app.route('/index')
def index():
    # Login + About combo page
    return render_template('index.html')

# ------------------ Run ------------------
if __name__ == '__main__':
    init_db()
    app.run(debug=True, use_reloader=False)
