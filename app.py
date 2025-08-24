import os
import json
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ===== Config =====
APP_SECRET = os.environ.get("APP_SECRET", "dev-secret-change-me")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(os.getcwd(), "uploads"))
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "png", "jpg", "jpeg"}
DB_PATH = os.path.join(os.getcwd(), "database.db")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")  # optional for Telegram webhook

app = Flask(__name__)
app.secret_key = APP_SECRET
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ===== Helpers =====

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,              -- 'admin', 'student', 'vendor'
            vendor_type TEXT DEFAULT NULL,   -- 'canteen' or 'print' for vendor
            full_name TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS canteens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_open INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canteen_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            available INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (canteen_id) REFERENCES canteens(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            canteen_id INTEGER NOT NULL,
            items_json TEXT NOT NULL, -- list of {item_id, name, price, qty}
            total REAL NOT NULL,
            status TEXT NOT NULL, -- 'Accepted', 'In Progress', 'Ready', 'Rejected', 'Created'
            paid INTEGER NOT NULL DEFAULT 0,
            otp_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES users(id),
            FOREIGN KEY (canteen_id) REFERENCES canteens(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            vendor_id INTEGER NOT NULL, -- a 'print' vendor (user.id)
            filename TEXT NOT NULL,
            copies INTEGER NOT NULL,
            color TEXT NOT NULL, -- 'color' or 'bw'
            binding TEXT NOT NULL, -- 'none', 'spiral', 'staple'
            price REAL NOT NULL,
            status TEXT NOT NULL, -- 'Accepted', 'In Progress', 'Ready', 'Rejected', 'Created'
            paid INTEGER NOT NULL DEFAULT 0,
            otp_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES users(id),
            FOREIGN KEY (vendor_id) REFERENCES users(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )

    # Seed admin and sample student
    def ensure_user(username, pwd, role, vendor_type=None):
        cur.execute("SELECT id FROM users WHERE username=?", (username,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (username, password_hash, role, vendor_type, full_name, created_at) VALUES (?,?,?,?,?,?)",
                (
                    username,
                    generate_password_hash(pwd),
                    role,
                    vendor_type,
                    username.title(),
                    datetime.utcnow().isoformat(),
                ),
            )

    ensure_user("admin", "admin", "admin")
    ensure_user("sec1", "sec1", "student")

    # Seed one canteen and a few items for demo
    cur.execute("SELECT id FROM canteens")
    if not cur.fetchone():
        cur.execute("INSERT INTO canteens (name, is_open) VALUES (?,1)", ("Main Canteen",))
        canteen_id = cur.lastrowid
        sample_items = [(canteen_id, "Idli", 10.0, 1), (canteen_id, "Vada", 12.0, 1), (canteen_id, "Tea", 8.0, 1)]
        cur.executemany(
            "INSERT INTO menu_items (canteen_id, name, price, available) VALUES (?,?,?,?)",
            sample_items,
        )

    conn.commit()
    conn.close()


init_db()

# ===== Auth Decorators =====

def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ===== Utils =====

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def notify(user_id: int, message: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO notifications (user_id, message, created_at) VALUES (?,?,?)",
        (user_id, message, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def generate_otp():
    from random import randint
    return f"{randint(100000, 999999)}"


# ===== Routes: Auth =====
@app.route("/")
def index():
    if "user_id" in session:
        role = session.get("role")
        if role == "admin":
            return redirect(url_for("admin_dashboard"))
        if role == "vendor":
            return redirect(url_for("vendor_dashboard"))
        return redirect(url_for("student_dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.execute("SELECT * FROM users WHERE username=?", (username,))
        user = cur.fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["vendor_type"] = user["vendor_type"]
            flash("Logged in.", "success")
            return redirect(url_for("index"))
        flash("Invalid credentials.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


# ===== Admin =====
@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    conn = get_db()
    stats = {}
    stats["users"] = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    stats["canteens"] = conn.execute("SELECT COUNT(*) c FROM canteens").fetchone()["c"]
    stats["orders"] = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    stats["prints"] = conn.execute("SELECT COUNT(*) c FROM print_jobs").fetchone()["c"]
    conn.close()
    return render_template("admin_dashboard.html", stats=stats)


@app.route("/admin/users", methods=["GET", "POST"])
@login_required("admin")
def admin_users():
    conn = get_db()
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password")
        role = request.form.get("role")
        vendor_type = request.form.get("vendor_type") if role == "vendor" else None
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, vendor_type, full_name, created_at) VALUES (?,?,?,?,?,?)",
                (
                    username,
                    generate_password_hash(password),
                    role,
                    vendor_type,
                    username.title(),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
            flash("User created.", "success")
        except sqlite3.IntegrityError:
            flash("Username already exists.", "danger")

    users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_users.html", users=users)


# ===== Student Side =====
@app.route("/student")
@login_required("student")
def student_dashboard():
    conn = get_db()
    canteens = conn.execute("SELECT * FROM canteens").fetchall()
    # For print vendors
    printers = conn.execute("SELECT * FROM users WHERE role='vendor' AND vendor_type='print'").fetchall()
    conn.close()
    return render_template("student_dashboard.html", canteens=canteens, printers=printers)


@app.route("/canteen/<int:canteen_id>")
@login_required("student")
def canteen_menu(canteen_id):
    conn = get_db()
    canteen = conn.execute("SELECT * FROM canteens WHERE id=?", (canteen_id,)).fetchone()
    if not canteen:
        abort(404)
    items = conn.execute("SELECT * FROM menu_items WHERE canteen_id=?", (canteen_id,)).fetchall()
    # naive queue time: count of orders not Ready * 2 minutes
    qcount = conn.execute("SELECT COUNT(*) c FROM orders WHERE canteen_id=? AND status IN ('Accepted','In Progress')", (canteen_id,)).fetchone()["c"]
    queue_time = qcount * 2
    conn.close()
    return render_template("canteen_menu.html", canteen=canteen, items=items, queue_time=queue_time)


@app.route("/cart/add", methods=["POST"])
@login_required("student")
def cart_add():
    item_id = int(request.form["item_id"]) 
    qty = int(request.form.get("qty", 1))
    conn = get_db()
    item = conn.execute("SELECT * FROM menu_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not item:
        abort(404)
    cart = session.get("cart", [])
    # append
    cart.append({
        "item_id": item["id"],
        "name": item["name"],
        "price": float(item["price"]),
        "qty": qty
    })
    session["cart"] = cart
    flash("Added to cart.", "success")
    return redirect(url_for("canteen_menu", canteen_id=item["canteen_id"]))


@app.route("/cart")
@login_required("student")
def cart_view():
    cart = session.get("cart", [])
    total = sum(i["price"] * i["qty"] for i in cart)
    return render_template("order_cart.html", cart=cart, total=total)


@app.route("/order/checkout", methods=["POST"]) 
@login_required("student")
def order_checkout():
    cart = session.get("cart", [])
    canteen_id = int(request.form.get("canteen_id"))
    if not cart:
        flash("Cart empty.", "warning")
        return redirect(url_for("canteen_menu", canteen_id=canteen_id))
    total = sum(i["price"] * i["qty"] for i in cart)
    otp = generate_otp()
    conn = get_db()
    conn.execute(
        "INSERT INTO orders (student_id, canteen_id, items_json, total, status, paid, otp_code, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            session["user_id"],
            canteen_id,
            json.dumps(cart),
            total,
            "Created",
            0,
            otp,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    order_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    session.pop("cart", None)
    flash(f"Order #{order_id} created. Show OTP {otp} at pickup.", "success")
    # Notify vendor(s): all canteen vendors
    conn = get_db()
    vends = conn.execute("SELECT id FROM users WHERE role='vendor' AND vendor_type='canteen'").fetchall()
    for v in vends:
        notify(v["id"], f"New canteen order #{order_id} placed.")
    conn.close()
    return redirect(url_for("order_track", order_id=order_id))


@app.route("/order/<int:order_id>")
@login_required("student")
def order_track(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=? AND student_id=?", (order_id, session["user_id"]))
    order = order.fetchone()
    conn.close()
    if not order:
        abort(404)
    return render_template("order_track.html", order=order, items=json.loads(order["items_json"]))


@app.route("/print/upload", methods=["GET", "POST"]) 
@login_required("student")
def print_upload():
    conn = get_db()
    printers = conn.execute("SELECT * FROM users WHERE role='vendor' AND vendor_type='print'").fetchall()
    if request.method == "POST":
        vendor_id = int(request.form.get("vendor_id"))
        copies = int(request.form.get("copies", 1))
        color = request.form.get("color", "bw")
        binding = request.form.get("binding", "none")
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Select a file.", "danger")
            return redirect(request.url)
        if not allowed_file(file.filename):
            flash("File type not allowed.", "danger")
            return redirect(request.url)
        filename = secure_filename(f"{datetime.utcnow().timestamp()}_{file.filename}")
        file.save(os.path.join(UPLOAD_FOLDER, filename))
        # Simple pricing: base 2 per page simulated (unknown pages) -> flat 5 + copies*(color?3:1.5)
        price = 5 + copies * (3 if color == "color" else 1.5)
        otp = generate_otp()
        conn.execute(
            "INSERT INTO print_jobs (student_id, vendor_id, filename, copies, color, binding, price, status, paid, otp_code, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                session["user_id"], vendor_id, filename, copies, color, binding, price, "Created", 0, otp, datetime.utcnow().isoformat()
            ),
        )
        conn.commit()
        job_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.close()
        notify(vendor_id, f"New print job #{job_id} received.")
        flash(f"Print job #{job_id} created. OTP {otp}.", "success")
        return redirect(url_for("order_track_print", job_id=job_id))
    conn.close()
    return render_template("print_upload.html", printers=printers)


@app.route("/print/job/<int:job_id>")
@login_required("student")
def order_track_print(job_id):
    conn = get_db()
    job = conn.execute("SELECT * FROM print_jobs WHERE id=? AND student_id=?", (job_id, session["user_id"]))
    job = job.fetchone()
    conn.close()
    if not job:
        abort(404)
    return render_template("order_track.html", order=job, items=[], is_print=True)


@app.route("/notifications")
@login_required()
def notifications():
    conn = get_db()
    notes = conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (session["user_id"],)).fetchall()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (session["user_id"],))
    conn.commit()
    conn.close()
    return render_template("notifications.html", notes=notes)


@app.route("/profile")
@login_required("student")
def profile():
    conn = get_db()
    orders = conn.execute("SELECT * FROM orders WHERE student_id=? ORDER BY id DESC", (session["user_id"],)).fetchall()
    prints = conn.execute("SELECT * FROM print_jobs WHERE student_id=? ORDER BY id DESC", (session["user_id"],)).fetchall()
    conn.close()
    return render_template("profile.html", orders=orders, prints=prints)


# ===== Vendor Side =====
@app.route("/vendor")
@login_required("vendor")
def vendor_dashboard():
    conn = get_db()
    if session.get("vendor_type") == "canteen":
        # all open orders for any canteen (simplified)
        orders = conn.execute("SELECT * FROM orders WHERE status IN ('Created','Accepted','In Progress') ORDER BY id DESC").fetchall()
        my_type = "canteen"
        prints = []
    else:
        prints = conn.execute("SELECT * FROM print_jobs WHERE vendor_id=? AND status IN ('Created','Accepted','In Progress') ORDER BY id DESC", (session["user_id"],)).fetchall()
        orders = []
        my_type = "print"
    conn.close()
    return render_template("vendor_dashboard.html", orders=orders, prints=prints, my_type=my_type)


@app.route("/vendor/order/<string:kind>/<int:oid>/<string:action>")
@login_required("vendor")
def vendor_order_action(kind, oid, action):
    allowed_actions = {"accept": "Accepted", "progress": "In Progress", "ready": "Ready", "reject": "Rejected", "paid": None}
    if action not in allowed_actions:
        abort(400)
    status = allowed_actions[action]
    conn = get_db()
    if kind == "canteen" and session.get("vendor_type") == "canteen":
        if action == "paid":
            conn.execute("UPDATE orders SET paid=1 WHERE id=?", (oid,))
        else:
            conn.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        # notify student
        row = conn.execute("SELECT student_id FROM orders WHERE id=?", (oid,)).fetchone()
        if row:
            notify(row["student_id"], f"Canteen order #{oid} -> {action.title()}")
    elif kind == "print" and session.get("vendor_type") == "print":
        if action == "paid":
            conn.execute("UPDATE print_jobs SET paid=1 WHERE id=?", (oid,))
        else:
            conn.execute("UPDATE print_jobs SET status=? WHERE id=?", (status, oid))
        row = conn.execute("SELECT student_id FROM print_jobs WHERE id=?", (oid,)).fetchone()
        if row:
            notify(row["student_id"], f"Print job #{oid} -> {action.title()}")
    else:
        conn.close()
        abort(403)
    conn.commit()
    conn.close()
    flash("Updated.", "success")
    return redirect(url_for("vendor_dashboard"))


@app.route("/vendor/menu", methods=["GET", "POST"]) 
@login_required("vendor")
def vendor_menu_update():
    if session.get("vendor_type") != "canteen":
        abort(403)
    conn = get_db()
    canteen = conn.execute("SELECT * FROM canteens LIMIT 1").fetchone()  # simplified: one canteen
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        price = float(request.form.get("price", 0))
        if name and price > 0:
            conn.execute("INSERT INTO menu_items (canteen_id, name, price, available) VALUES (?,?,?,1)", (canteen["id"], name, price))
            conn.commit()
            flash("Item added.", "success")
        for key, val in request.form.items():
            if key.startswith("avail_"):
                mid = int(key.split("_",1)[1])
                conn.execute("UPDATE menu_items SET available=? WHERE id=?", (1 if val == "on" else 0, mid))
        conn.commit()
    items = conn.execute("SELECT * FROM menu_items WHERE canteen_id=?", (canteen["id"],)).fetchall()
    conn.close()
    return render_template("vendor_menu_update.html", canteen=canteen, items=items)


# ===== Payments (Simulated UPI QR / Confirmation) =====
@app.route("/pay/upi/<string:kind>/<int:oid>")
@login_required()
def pay_upi(kind, oid):
    # This returns a simple UPI intent string; in real app, integrate a gateway or show QR
    upi_id = "upi@sairam"
    amount = 0.0
    label = f"Sairam {kind.capitalize()} #{oid}"
    conn = get_db()
    if kind == "canteen":
        row = conn.execute("SELECT total FROM orders WHERE id=?", (oid,)).fetchone()
        amount = float(row["total"]) if row else 0.0
    else:
        row = conn.execute("SELECT price FROM print_jobs WHERE id=?", (oid,)).fetchone()
        amount = float(row["price"]) if row else 0.0
    conn.close()
    upi_uri = f"upi://pay?pa={upi_id}&pn=Sairam&am={amount:.2f}&cu=INR&tn={label}"
    return jsonify({"upi": upi_uri, "note": "Use any UPI app to pay. After payment, ask vendor to mark as paid."})


# ===== Telegram Webhook (Optional) =====
@app.route("/telegram/webhook", methods=["POST"]) 
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    # Basic parser for commands
    message = data.get("message") or data.get("edited_message") or {}
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    def reply(msg):
        # passive webhook response (no outgoing request). Telegram expects 200 OK; to actively send, set TELEGRAM_BOT_TOKEN and call sendMessage.
        return jsonify({"method": "sendMessage", "chat_id": chat_id, "text": msg})

    if not text:
        return ("", 200)

    if text.startswith("/start"):
        return reply("Welcome to Sairam Campus Bot. Use /menu, /order <item_id>x<qty>, /uploadfile (not supported in Telegram), /status <id>.")

    if text.startswith("/menu"):
        conn = get_db()
        items = conn.execute("SELECT id,name,price,available FROM menu_items WHERE available=1 LIMIT 25").fetchall()
        conn.close()
        lines = [f"{row['id']}. {row['name']} - ₹{row['price']:.2f}" for row in items]
        return reply("Live Menu:\n" + ("\n".join(lines) or "No items"))

    if text.startswith("/status"):
        parts = text.split()
        if len(parts) < 2:
            return reply("Usage: /status <order_id>")
        oid = int(parts[1])
        conn = get_db()
        row = conn.execute("SELECT status, paid FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            row = conn.execute("SELECT status, paid FROM print_jobs WHERE id=?", (oid,)).fetchone()
            kind = "Print"
        else:
            kind = "Order"
        conn.close()
        if not row:
            return reply("Not found.")
        return reply(f"{kind} #{oid}: {row['status']} | Paid: {'Yes' if row['paid'] else 'No'}")

    if text.startswith("/order"):
        # /order <item_id>x<qty> (simple)
        try:
            payload = text.split(maxsplit=1)[1]
            item_part, qty_part = payload.split("x")
            item_id = int(item_part)
            qty = int(qty_part)
        except Exception:
            return reply("Usage: /order <item_id>x<qty>")
        # Create order under anonymous student (sec1) for demo
        conn = get_db()
        stu = conn.execute("SELECT id FROM users WHERE username='sec1'").fetchone()
        item = conn.execute("SELECT * FROM menu_items WHERE id=?", (item_id,)).fetchone()
        if not (stu and item):
            conn.close()
            return reply("Invalid item.")
        otp = generate_otp()
        items_json = json.dumps([{ "item_id": item["id"], "name": item["name"], "price": float(item["price"]), "qty": qty }])
        total = float(item["price"]) * qty
        conn.execute(
            "INSERT INTO orders (student_id, canteen_id, items_json, total, status, paid, otp_code, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (stu["id"], 1, items_json, total, "Created", 0, otp, datetime.utcnow().isoformat()),
        )
        conn.commit()
        oid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.close()
        return reply(f"Order #{oid} created. Amount ₹{total:.2f}. OTP {otp}.")

    return reply("Unknown command. Use /menu, /order, /status.")


# ===== Static (PWA) =====
@app.route('/static/<path:filename>')
def custom_static(filename):
    return send_from_directory('static', filename)


if __name__ == "__main__":
    app.run(debug=True)