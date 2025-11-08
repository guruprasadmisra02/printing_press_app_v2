import os
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file
from fpdf import FPDF
import psycopg2
import psycopg2.extras

# ------------------ App & DB ------------------
app = Flask(__name__)
app.secret_key = "supersecretkey"

# Read from Render env var: Settings ➜ Environment ➜ DATABASE_URL
DB_URL = os.environ.get("DATABASE_URL", "postgresql://user:password@host:port/dbname")

def get_db():
    # RealDictCursor -> rows behave like dicts (row['field'])
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

@app.context_processor
def inject_current_year():
    return {"current_year": date.today().year}

def init_db_postgres():
    conn = get_db()
    cur = conn.cursor()

    # USERS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT,
            username TEXT,
            password TEXT,
            phone TEXT,
            role TEXT CHECK (role IN ('owner','worker','customer'))
        )
    """)

    # ORDERS (keep dates as text 'YYYY-MM-DD' for simpler SUBSTRING filters)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER REFERENCES users(id),
            customer_name TEXT,
            product_name TEXT,
            size TEXT,
            colour TEXT,
            quantity REAL,
            total_cost REAL,
            amount_paid REAL DEFAULT 0,
            date TEXT,
            status TEXT,
            receive_date TEXT
        )
    """)

    # STOCK
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id SERIAL PRIMARY KEY,
            item_name TEXT NOT NULL,
            item_no TEXT,
            quantity REAL DEFAULT 0,
            unit_cost REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            size TEXT,
            last_updated TEXT
        )
    """)

    # STOCK ADDITIONS (to sum monthly stock purchases)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_additions (
            id SERIAL PRIMARY KEY,
            item_name TEXT,
            item_no TEXT,
            quantity REAL,
            unit_cost REAL,
            total_amount_added REAL,
            date_added TEXT
        )
    """)

    # ORDER ITEMS USED
    cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items_used (
            id SERIAL PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id),
            stock_item_id INTEGER REFERENCES stock(id),
            quantity_used REAL
        )
    """)

    # EXPENSES
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            expense_name TEXT,
            amount REAL,
            description TEXT,
            date TEXT
        )
    """)

    # QUOTES
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            product TEXT NOT NULL,
            quantity INTEGER,
            message TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Seed default owner/worker if missing
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE role='owner'")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO users (name, username, password, role) VALUES (%s,%s,%s,%s)",
            ("Admin", "owner", "owner123", "owner")
        )

    cur.execute("SELECT COUNT(*) AS c FROM users WHERE role='worker'")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO users (name, username, password, role) VALUES (%s,%s,%s,%s)",
            ("Worker", "worker", "worker123", "worker")
        )

    conn.commit()
    conn.close()

# ------------------ Helpers ------------------
def ensure_logged_in(role=None):
    if "user_id" not in session:
        return redirect(url_for("login"))
    if role and session.get("role") != role:
        flash("Unauthorized access", "danger")
        return redirect(url_for("login"))
    return None

def month_filter():
    m = request.args.get("month")
    return m if m else date.today().strftime("%Y-%m")

# Stock upsert + purchase log
def upsert_stock(conn, item_name, item_no, size, added_qty, addition_total_cost):
    cur = conn.cursor()
    item_no = (item_no or "").strip()
    size = (size or "").strip()
    add_qty = float(added_qty)
    add_cost = float(addition_total_cost)
    today = date.today().isoformat()

    if item_no:
        cur.execute("SELECT * FROM stock WHERE item_no=%s", (item_no,))
    else:
        cur.execute("SELECT * FROM stock WHERE item_name=%s AND COALESCE(size,'')=%s", (item_name, size))
    row = cur.fetchone()

    if row:
        old_qty = float(row["quantity"] or 0)
        old_unit = float(row["unit_cost"] or 0)
        new_qty  = old_qty + add_qty
        new_unit = (old_qty * old_unit + add_cost) / new_qty if new_qty > 0 else 0.0
        new_total = new_qty * new_unit
        cur.execute(
            """UPDATE stock SET quantity=%s, unit_cost=%s, total_amount=%s, last_updated=%s WHERE id=%s""",
            (new_qty, new_unit, new_total, today, row["id"])
        )
    else:
        unit_cost = add_cost / add_qty if add_qty > 0 else 0.0
        total_amount = add_qty * unit_cost
        cur.execute(
            """INSERT INTO stock (item_name, item_no, quantity, unit_cost, size, total_amount, last_updated)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (item_name, item_no, add_qty, unit_cost, size, total_amount, today)
        )

    # log purchase
    cur.execute(
        """INSERT INTO stock_additions (item_name, item_no, quantity, unit_cost, total_amount_added, date_added)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (item_name, item_no, add_qty, (add_cost / add_qty) if add_qty > 0 else 0.0, add_cost, today)
    )
    conn.commit()

def add_item_usage(conn, order_id, stock_id, qty_used):
    qty_used = float(qty_used)
    if qty_used <= 0:
        return "Quantity must be > 0"
    cur = conn.cursor()
    cur.execute("SELECT id, quantity, unit_cost FROM stock WHERE id=%s", (stock_id,))
    s = cur.fetchone()
    if not s:
        return "Stock item not found"
    if float(s["quantity"] or 0) < qty_used:
        return "Not enough stock"
    cur.execute(
        """UPDATE stock
             SET quantity = quantity - %s,
                 total_amount = (quantity - %s) * unit_cost,
                 last_updated = %s
           WHERE id=%s""",
        (qty_used, qty_used, date.today().isoformat(), stock_id)
    )
    cur.execute(
        "INSERT INTO order_items_used (order_id, stock_item_id, quantity_used) VALUES (%s,%s,%s)",
        (order_id, stock_id, qty_used)
    )
    conn.commit()
    return None

# ------------------ Public / Auth ------------------
@app.route("/")
def home():
    return render_template("about.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=%s AND password=%s", (username, password))
        user = cur.fetchone()
        conn.close()
        if user:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["name"] = user["name"]
            if user["role"] == "owner":
                return redirect(url_for("owner_home"))
            if user["role"] == "worker":
                return redirect(url_for("worker_home"))
            flash("Use Customer Login below", "warning")
        else:
            flash("Invalid credentials", "danger")
    return render_template("index.html")

@app.route("/customer_login", methods=["POST"])
def customer_login():
    phone = request.form.get("phone","").strip()
    name  = request.form.get("name","").strip() or phone
    if not phone:
        flash("Phone required", "danger")
        return redirect(url_for("login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE phone=%s AND role='customer'", (phone,))
    user = cur.fetchone()
    if not user:
        cur.execute("INSERT INTO users (name, phone, role) VALUES (%s,%s,%s)", (name, phone, "customer"))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE phone=%s AND role='customer'", (phone,))
        user = cur.fetchone()
    conn.close()
    session["user_id"] = user["id"]
    session["role"] = "customer"
    session["name"] = user["name"]
    return redirect(url_for("customer_home"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------ Customer ------------------
@app.route("/customer/home")
def customer_home():
    guard = ensure_logged_in("customer")
    if guard: return guard
    return render_template("customer_home.html")

@app.route("/customer/place_order", methods=["GET","POST"])
def customer_place_order():
    guard = ensure_logged_in("customer")
    if guard: return guard
    if request.method == "POST":
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO orders (customer_id, product_name, size, colour, quantity, date, status, customer_name)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (session["user_id"],
             request.form["product_name"],
             request.form["size"],
             request.form["colour"],
             float(request.form["quantity"]),
             date.today().isoformat(),
             "Pending",
             session.get("name","Unknown"))
        )
        conn.commit()
        conn.close()
        flash("Order placed!", "success")
        return redirect(url_for("customer_orders"))
    return render_template("customer_place_order.html", today=date.today().isoformat())

@app.route("/customer/orders")
def customer_orders():
    guard = ensure_logged_in("customer")
    if guard: return guard
    m = month_filter()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT * FROM orders
           WHERE customer_id=%s AND SUBSTRING(date,1,7)=%s
           ORDER BY id DESC""",
        (session["user_id"], m)
    )
    rows = cur.fetchall()
    conn.close()
    return render_template("customer_orders.html", orders=rows, month=m)

@app.route("/request_quote", methods=["POST"])
def request_quote():
    name = request.form["name"]
    phone = request.form["phone"]
    email = request.form["email"]
    product = request.form["product"]
    quantity = request.form["quantity"]
    message = request.form["message"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO quotes (name, phone, email, product, quantity, message)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (name, phone, email, product, quantity, message)
    )
    conn.commit()
    conn.close()
    flash("Your quote request has been submitted successfully!", "success")
    return redirect(url_for("home"))

# ------------------ Shared: Items Used Inline ------------------
@app.route("/orders/<int:order_id>/items/add", methods=["POST"])
def add_order_item(order_id):
    if "role" not in session or session["role"] not in ("worker","owner"):
        flash("Unauthorized", "danger")
        return redirect(url_for("login"))
    stock_id = int(request.form["stock_id"])
    qty_used = request.form["qty_used"]
    conn = get_db()
    err = add_item_usage(conn, order_id, stock_id, qty_used)
    conn.close()
    flash("Item usage saved & stock updated" if not err else err, "success" if not err else "danger")
    return redirect(url_for("worker_orders" if session["role"]=="worker" else "owner_orders"))

# ------------------ Worker ------------------
@app.route("/worker/home")
def worker_home():
    guard = ensure_logged_in("worker")
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT o.*, u.name AS customer_name
             FROM orders o
             JOIN users u ON u.id=o.customer_id
            WHERE o.status IN ('Pending','In Progress')
            ORDER BY o.date ASC"""
    )
    rows = cur.fetchall()
    conn.close()
    return render_template("worker_home.html", orders=rows)

@app.route("/worker/orders", methods=["GET","POST"])
def worker_orders():
    guard = ensure_logged_in("worker")
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        oid = int(request.form["order_id"])
        act = request.form["action"]
        if act == "update_status":
            s = request.form["status"]
            rd = date.today().isoformat() if s == "Completed" else None
            cur.execute("UPDATE orders SET status=%s, receive_date=%s WHERE id=%s", (s, rd, oid))
        elif act == "update_paid":
            p = float(request.form["amount_paid"] or 0)
            cur.execute("UPDATE orders SET amount_paid=COALESCE(amount_paid,0)+%s WHERE id=%s", (p, oid))
        conn.commit()
        conn.close()
        flash("Updated", "success")
        return redirect(url_for("worker_orders"))

    m = month_filter()
    cur.execute(
        """SELECT o.*, COALESCE(o.customer_name,u.name,u.phone,'Unknown') AS customer_display, u.phone AS customer_phone
             FROM orders o
        LEFT JOIN users u ON u.id=o.customer_id
            WHERE SUBSTRING(o.date,1,7)=%s
            ORDER BY o.id DESC""",
        (m,)
    )
    orders = cur.fetchall()
    cur.execute("SELECT id,item_name,item_no,size,quantity FROM stock ORDER BY item_name")
    stock = cur.fetchall()
    conn.close()
    return render_template("worker_orders.html", orders=orders, stock=stock, month=m)

@app.route("/worker/stock", methods=["GET","POST"], endpoint="worker_stock")
def worker_stock():
    guard = ensure_logged_in("worker")
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    if request.method == "POST":
        upsert_stock(
            conn,
            request.form["item_name"],
            request.form.get("item_no",""),
            request.form.get("size",""),
            float(request.form["added_quantity"]),
            float(request.form["addition_total_cost"])
        )
        flash("Stock updated", "success")
        return redirect(url_for("worker_stock"))
    cur.execute("SELECT * FROM stock ORDER BY item_name")
    rows = cur.fetchall()
    conn.close()
    return render_template("worker_stock.html", stock=rows)

@app.route("/worker/orders/<int:oid>/items", methods=["GET","POST"])
def worker_order_items(oid):
    guard = ensure_logged_in("worker")
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        cur.execute("SELECT id FROM stock")
        for s in cur.fetchall():
            sid = s["id"]
            qty_used_val = request.form.get(f"qty_used_{sid}")
            if not qty_used_val:
                continue
            qty_used = float(qty_used_val)
            if qty_used <= 0:
                continue
            cur.execute(
                """UPDATE stock
                     SET quantity = quantity - %s,
                         total_amount = (quantity - %s) * unit_cost,
                         last_updated = %s
                   WHERE id = %s""",
                (qty_used, qty_used, date.today().isoformat(), sid)
            )
            cur.execute(
                "INSERT INTO order_items_used (order_id, stock_item_id, quantity_used) VALUES (%s,%s,%s)",
                (oid, sid, qty_used)
            )
        conn.commit()
        conn.close()
        flash("Items usage saved and stock updated", "success")
        return redirect(url_for("worker_orders"))

    cur.execute("SELECT * FROM stock ORDER BY item_name")
    stock = cur.fetchall()
    cur.execute("SELECT * FROM orders WHERE id=%s", (oid,))
    order = cur.fetchone()
    conn.close()
    return render_template("worker_items_used.html", stock=stock, order=order)

# ------------------ Owner Dashboard ------------------
@app.route("/owner/home", methods=["GET","POST"])
def owner_home():
    if "role" not in session or session["role"] != "owner":
        return redirect(url_for("login"))

    selected_month = request.form.get("month") or datetime.now().strftime("%Y-%m")

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COALESCE(COUNT(*),0) AS c FROM orders WHERE SUBSTRING(date,1,7)=%s", (selected_month,))
    total_orders = cur.fetchone()["c"]

    cur.execute("SELECT COALESCE(COUNT(*),0) AS c FROM stock")
    total_stock = cur.fetchone()["c"]

    cur.execute("SELECT COALESCE(SUM(amount),0) AS s FROM expenses WHERE SUBSTRING(date,1,7)=%s", (selected_month,))
    base_expenses = float(cur.fetchone()["s"] or 0)

    cur.execute("SELECT COALESCE(SUM(total_amount_added),0) AS s FROM stock_additions WHERE SUBSTRING(date_added,1,7)=%s", (selected_month,))
    stock_purchases = float(cur.fetchone()["s"] or 0)

    total_expenses = base_expenses + stock_purchases

    cur.execute(
        """SELECT COALESCE(SUM(amount_paid),0) AS s
             FROM orders
            WHERE SUBSTRING(date,1,7)=%s
              AND (status='Completed' OR status='completed')""",
        (selected_month,)
    )
    total_income = float(cur.fetchone()["s"] or 0)

    profit_loss = total_income - total_expenses

    cur.execute("SELECT COALESCE(SUM(total_amount),0) AS s FROM stock")
    current_stock_value = float(cur.fetchone()["s"] or 0)

    cur.execute("SELECT COALESCE(COUNT(*),0) AS c FROM quotes")
    total_quotes = cur.fetchone()["c"]

    conn.close()

    return render_template(
        "owner_home.html",
        selected_month=selected_month,
        total_orders=total_orders,
        total_stock=total_stock,
        base_expenses=base_expenses,
        stock_purchases=stock_purchases,
        total_expenses=total_expenses,
        total_income=total_income,
        profit_loss=profit_loss,
        current_stock_value=current_stock_value,
        total_quotes=total_quotes
    )

@app.route("/owner/dashboard_data")
def owner_dashboard_data():
    if "role" not in session or session["role"] != "owner":
        return redirect(url_for("login"))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT SUBSTRING(date,1,7) AS month, COALESCE(SUM(amount_paid),0) AS income
          FROM orders
         WHERE status='Completed'
         GROUP BY month
         ORDER BY month DESC
         LIMIT 6
    """)
    income_data = cur.fetchall()

    cur.execute("""
        SELECT SUBSTRING(date,1,7) AS month, COALESCE(SUM(amount),0) AS expenses
          FROM expenses
         GROUP BY month
         ORDER BY month DESC
         LIMIT 6
    """)
    expense_data = cur.fetchall()
    conn.close()

    income_dict  = {r["month"]: float(r["income"]) for r in income_data}
    expense_dict = {r["month"]: float(r["expenses"]) for r in expense_data}
    all_months = sorted(set(income_dict.keys()) | set(expense_dict.keys()))

    months, incomes, expenses, profits = [], [], [], []
    for m in all_months:
        inc = income_dict.get(m, 0.0)
        exp = expense_dict.get(m, 0.0)
        months.append(m)
        incomes.append(inc)
        expenses.append(exp)
        profits.append(inc - exp)

    return jsonify({"months": months, "incomes": incomes, "expenses": expenses, "profits": profits})

# ------------------ Owner: Orders / Stock / Expenses ------------------
@app.route("/owner/orders", methods=["GET","POST"])
def owner_orders():
    guard = ensure_logged_in("owner")
    if guard: return guard

    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        oid = int(request.form["order_id"])
        action = request.form["action"]

        if action == "update_status":
            status = request.form["status"]
            rd = date.today().isoformat() if status == "Completed" else None
            cur.execute("UPDATE orders SET status=%s, receive_date=%s WHERE id=%s", (status, rd, oid))

        elif action == "update_paid":
            amt = float(request.form["amount_paid"] or 0)
            cur.execute("UPDATE orders SET amount_paid=COALESCE(amount_paid,0)+%s WHERE id=%s", (amt, oid))

        elif action == "update_total_cost":
            tc = float(request.form["total_cost"] or 0)
            cur.execute("UPDATE orders SET total_cost=%s WHERE id=%s", (tc, oid))

        elif action == "delete_order":
            cur.execute("DELETE FROM order_items_used WHERE order_id=%s", (oid,))
            cur.execute("DELETE FROM orders WHERE id=%s", (oid,))

        conn.commit()
        conn.close()
        flash("✅ Order updated successfully!", "success")
        return redirect(url_for("owner_orders"))

    m = month_filter()
    cur.execute("""
        SELECT o.*,
               COALESCE(o.customer_name, u.name, u.phone, 'Unknown') AS customer_display,
               u.phone AS customer_phone
          FROM orders o
     LEFT JOIN users u ON u.id = o.customer_id
         WHERE SUBSTRING(o.date,1,7)=%s
         ORDER BY o.id DESC
    """, (m,))
    orders = cur.fetchall()

    cur.execute("SELECT id, item_name, item_no, size, quantity FROM stock ORDER BY item_name")
    stock = cur.fetchall()

    cur.execute("""
        SELECT oiu.order_id, s.item_name, s.size, oiu.quantity_used
          FROM order_items_used oiu
          JOIN stock s ON s.id = oiu.stock_item_id
    """)
    used_items_raw = cur.fetchall()

    used = {}
    for r in used_items_raw:
        used.setdefault(r["order_id"], []).append(dict(r))

    conn.close()
    return render_template("owner_orders.html", orders=orders, month=m, stock=stock, used=used)

@app.route("/owner/orders/<int:order_id>/edit", methods=["GET","POST"])
def owner_edit_order(order_id):
    guard = ensure_logged_in("owner")
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    if request.method == "POST":
        cur.execute(
            """UPDATE orders
                  SET product_name=%s, size=%s, colour=%s, quantity=%s,
                      total_cost=%s, status=%s, receive_date=%s
                WHERE id=%s""",
            (request.form["product_name"],
             request.form["size"],
             request.form["colour"],
             float(request.form["quantity"]),
             float(request.form["total_cost"] or 0),
             request.form["status"],
             request.form.get("receive_date") or None,
             order_id)
        )
        conn.commit()
        conn.close()
        flash("Order updated!", "success")
        return redirect(url_for("owner_orders"))
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    conn.close()
    return render_template("owner_edit_order.html", order=order)

@app.route("/owner/stock", methods=["GET","POST"])
def owner_stock():
    guard = ensure_logged_in("owner")
    if guard: return guard

    conn = get_db()
    cur = conn.cursor()
    action = request.form.get("action","")

    if request.method == "POST" and action == "add_stock":
        upsert_stock(
            conn,
            request.form["item_name"],
            request.form.get("item_no",""),
            request.form.get("size",""),
            float(request.form["added_quantity"]),
            float(request.form["addition_total_cost"])
        )
        flash("✅ Stock added or updated successfully", "success")
        return redirect(url_for("owner_stock"))

    cur.execute("SELECT * FROM stock ORDER BY item_name")
    rows = cur.fetchall()
    stock_list = []
    for r in rows:
        cur.execute("SELECT COUNT(*) AS c FROM order_items_used WHERE stock_item_id=%s", (r["id"],))
        used_flag = cur.fetchone()["c"] > 0
        stock_list.append({**dict(r), "used": used_flag})

    conn.close()
    return render_template("owner_stock.html", stock=stock_list)

@app.route("/owner/stock/<int:stock_id>/delete", methods=["POST"])
def delete_stock(stock_id):
    guard = ensure_logged_in("owner")
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM order_items_used WHERE stock_item_id=%s", (stock_id,))
    used_count = cur.fetchone()["c"]
    if used_count > 0:
        flash("⚠️ This stock item has been used in previous orders and cannot be deleted.", "warning")
    else:
        cur.execute("DELETE FROM stock WHERE id=%s", (stock_id,))
        conn.commit()
        flash("✅ Stock item deleted successfully!", "success")
    conn.close()
    return redirect(url_for("owner_stock"))

@app.route("/expenses", methods=["GET","POST"])
def expenses():
    guard = ensure_logged_in("owner")
    if guard: return guard
    conn = get_db()
    cur = conn.cursor()
    if request.method == "POST":
        cur.execute(
            "INSERT INTO expenses (expense_name, amount, description, date) VALUES (%s,%s,%s,%s)",
            (request.form["expense_name"],
             float(request.form["amount"]),
             request.form.get("description",""),
             date.today().isoformat())
        )
        conn.commit()
        flash("Expense added!", "success")
        return redirect(url_for("expenses"))
    cur.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC")
    rows = cur.fetchall()
    conn.close()
    return render_template("expenses.html", rows=rows)

# ------------------ Bill (single or multiple order IDs) ------------------
@app.route("/bill/<order_ids>")
def bill(order_ids):
    if "role" not in session:
        flash("Please login first", "danger")
        return redirect(url_for("login"))

    ids = [int(i) for i in order_ids.split(",") if i.strip().isdigit()]
    if not ids:
        flash("No valid order selected", "danger")
        return redirect(url_for("owner_orders"))

    conn = get_db()
    cur = conn.cursor()
    qmarks = ",".join(["%s"]*len(ids))
    cur.execute(
        f"""SELECT o.*, u.name AS customer_name, u.phone
               FROM orders o
          LEFT JOIN users u ON u.id=o.customer_id
              WHERE o.id IN ({qmarks})""",
        ids
    )
    orders = cur.fetchall()
    conn.close()

    if not orders:
        flash("No orders found", "warning")
        return redirect(url_for("owner_orders"))

    customer_name = orders[0]["customer_name"] or orders[0]["phone"] or "Customer"
    bill_no = str(orders[0]["id"])
    bill_date = date.today().strftime("%d-%b-%Y")

    return render_template(
        "bill.html",
        orders=orders,
        customer_name=customer_name,
        today=bill_date,
        bill_no=bill_no,
        gstin="",
        order_ids=order_ids
    )

@app.route("/download_bill_pdf/<order_ids>")
def download_bill_pdf(order_ids):
    if "role" not in session:
        flash("Please login first", "danger")
        return redirect(url_for("login"))

    ids = [int(i) for i in order_ids.split(",") if i.strip().isdigit()]
    if not ids:
        flash("No valid orders selected", "warning")
        return redirect(url_for("owner_orders"))

    conn = get_db()
    cur = conn.cursor()
    qmarks = ",".join(["%s"]*len(ids))
    cur.execute(
        f"""SELECT o.*, u.name AS customer_name, u.phone
               FROM orders o
               JOIN users u ON u.id=o.customer_id
              WHERE o.id IN ({qmarks})""",
        ids
    )
    orders = cur.fetchall()
    conn.close()

    if not orders:
        flash("No orders found", "warning")
        return redirect(url_for("owner_orders"))

    customer_name = orders[0]["customer_name"] or orders[0]["phone"] or "Unknown"
    bill_no = str(orders[0]["id"])
    bill_date = date.today().strftime("%d-%b-%Y")

    pdf = FPDF()
    pdf.add_page()
    # NOTE: ensure these font files exist under /fonts in your repo
    pdf.add_font("NotoSans", "", "fonts/NotoSans-Regular.ttf", uni=True)
    pdf.add_font("NotoSans", "B", "fonts/NotoSans-Bold.ttf", uni=True)
    pdf.add_font("NotoSans", "I", "fonts/NotoSans-Italic.ttf", uni=True)

    # Header
    pdf.set_font("NotoSans", "B", 14)
    pdf.cell(200, 10, txt="ANANTA BALIA PRINTERS & PUBLISHERS", ln=True, align="C")
    pdf.set_font("NotoSans", size=10)
    pdf.cell(200, 6, txt="Plot No. 523, Mahanadi Vihar, Cuttack - 753004, Nayabazar", ln=True, align="C")
    pdf.cell(200, 6, txt="Phone: 9937043648 | Email: pkmisctc17@gmail.com", ln=True, align="C")
    pdf.ln(8)

    # Bill Info
    pdf.set_font("NotoSans", "B", 12)
    pdf.cell(100, 8, txt=f"BILL NO: {bill_no}", ln=0, align="L")
    pdf.cell(90, 8, txt=f"Date: {bill_date}", ln=1, align="R")
    pdf.set_font("NotoSans", size=11)
    pdf.cell(200, 8, txt=f"Customer: {customer_name}", ln=True, align="L")
    pdf.ln(5)

    # Table Header
    pdf.set_font("NotoSans", "B", 10)
    pdf.cell(10, 8, txt="#", border=1)
    pdf.cell(70, 8, txt="Product", border=1)
    pdf.cell(30, 8, txt="Qty", border=1)
    pdf.cell(35, 8, txt="Unit Cost (₹)", border=1)
    pdf.cell(45, 8, txt="Total (₹)", border=1, ln=True)

    # Rows
    total = 0.0
    pdf.set_font("NotoSans", size=10)
    for i, o in enumerate(orders, start=1):
        qty  = float(o["quantity"] or 0)
        cost = float(o["total_cost"] or 0)
        unit = (cost / qty) if qty else 0.0
        total += cost
        pdf.cell(10, 8, txt=str(i), border=1)
        pdf.cell(70, 8, txt=o["product_name"], border=1)
        pdf.cell(30, 8, txt=f"{qty:.0f}", border=1, align="R")
        pdf.cell(35, 8, txt=f"{unit:.2f}", border=1, align="R")
        pdf.cell(45, 8, txt=f"₹ {cost:.2f}", border=1, align="R", ln=True)

    # Total
    pdf.set_font("NotoSans", "B", 11)
    pdf.cell(145, 8, txt="TOTAL AMOUNT", border=1, align="R")
    pdf.cell(45, 8, txt=f"₹ {total:.2f}", border=1, align="R", ln=True)

    # Footer
    pdf.ln(10)
    pdf.set_font("NotoSans", "I", 10)
    pdf.multi_cell(0, 6, txt="Thank you for choosing Ananta Balia Printers & Publishers!\nWe appreciate your business and hope to serve you again.", align="C")

    filepath = f"bill_{'_'.join(map(str, ids))}.pdf"
    pdf.output(filepath)
    return send_file(filepath, as_attachment=True)

# Combined Login/About page (if you use it)
@app.route("/index")
def index():
    return render_template("index.html")

# ------------------ Main ------------------
if __name__ == "__main__":
    init_db_postgres()
    app.run(debug=True, use_reloader=False)
