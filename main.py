import base64
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections import Counter
from html import escape

import boto3
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = tempfile.gettempdir()
DB_PATH = os.path.join(TMP_DIR, "database.db")
UPLOADS_DIR = os.path.join(TMP_DIR, "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(UPLOADS_DIR, exist_ok=True)

app = FastAPI(title="Vape Hub")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
# Здесь лежит logo.png (static/logo.png)
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Хранилище S3 (необязательно): без ключей всё работает во временной папке
S3_BUCKET = os.getenv("S3_BUCKET", "vape-hub-storage")
S3_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
S3_SECRET = os.getenv("AWS_SECRET_ACCESS_KEY", "")
S3_ENDPOINT = "https://storage.yandexcloud.net"

# Пароль админки берётся из переменной окружения ADMIN_PWD
ADMIN_PWD = os.getenv("ADMIN_PWD") or "unset-" + uuid.uuid4().hex

# Баннер по умолчанию: встроенный SVG, не зависит от внешних сайтов (Unsplash и т.п.)
DEFAULT_BANNER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 320" preserveAspectRatio="xMidYMid slice">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1a0a14"/><stop offset="1" stop-color="#050505"/></linearGradient>
<radialGradient id="g1" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#f20b78" stop-opacity=".75"/><stop offset="1" stop-color="#f20b78" stop-opacity="0"/></radialGradient>
<radialGradient id="g2" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#ff5b9f" stop-opacity=".5"/><stop offset="1" stop-color="#ff5b9f" stop-opacity="0"/></radialGradient>
</defs>
<rect width="800" height="320" fill="url(#bg)"/>
<circle cx="620" cy="90" r="190" fill="url(#g1)"/>
<circle cx="180" cy="230" r="150" fill="url(#g2)"/>
<circle cx="420" cy="40" r="90" fill="url(#g2)"/>
</svg>"""
DEFAULT_BANNER_URL = "data:image/svg+xml;base64," + base64.b64encode(DEFAULT_BANNER_SVG.encode("utf-8")).decode("ascii")
DEAD_BANNER_MARK = "images.unsplash.com/photo-1560913210"


def read_template(name):
    for folder in (os.path.join(BASE_DIR, "templates"), BASE_DIR):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    raise HTTPException(status_code=500, detail="Шаблон не найден: " + name)


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name="ru-central1",
        aws_access_key_id=S3_KEY_ID,
        aws_secret_access_key=S3_SECRET,
    )


def download_db():
    if not os.path.exists(DB_PATH) and S3_KEY_ID:
        try:
            get_s3_client().download_file(S3_BUCKET, "database.db", DB_PATH)
        except Exception:
            init_local_db()
    elif not os.path.exists(DB_PATH):
        init_local_db()


def upload_db():
    if S3_KEY_ID:
        try:
            get_s3_client().upload_file(DB_PATH, S3_BUCKET, "database.db")
        except Exception as e:
            print(f"S3 DB upload error: {e}")


def init_local_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rating INTEGER NOT NULL,
            comment TEXT,
            phone TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            image_url TEXT NOT NULL,
            target_url TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    cursor.execute("SELECT COUNT(*) FROM banners")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO banners (title, image_url, target_url) VALUES (?, ?, ?)",
            ("Новинки Waka и HQD", DEFAULT_BANNER_URL, "https://t.me/Dimniy_Viborr"),
        )
    conn.commit()
    conn.close()
    upload_db()


SHOP_CONFIG = {
    "name": "SMOKE LAB MOSCOW",
    "subtitle": "Информационный портал для совершеннолетних (18+)",
    "logo": "https://cdn-icons-png.flaticon.com/512/3076/3076045.png",
    "yandex_review_url": "https://yandex.ru/maps",
    "twogis_review_url": "https://2gis.ru/moscow",
    # Основные кнопки под баннером
    "links": {
        "telegram_catalog": {"title": "💨 Наличие и вкусы (Telegram)", "url": "https://t.me/Dimniy_Viborr", "icon": "fa-brands fa-telegram"},
    },
    # Блок «Связаться с нами» — единственное место, где живут контакты
    "contacts": {
        "whatsapp": {"title": "WhatsApp", "url": "https://wa.me/79096695445", "icon": "fa-brands fa-whatsapp"},
        "telegram": {"title": "Telegram", "url": "https://t.me/+79096695445", "icon": "fa-brands fa-telegram"},
        "phone": {"title": "+7 (909) 669-54-45", "url": "tel:+79096695445", "icon": "fa-solid fa-phone"},
    },
}
# Все ссылки, по которым считаются клики через /go/<key>
ALL_LINKS = {**SHOP_CONFIG["links"], **SHOP_CONFIG["contacts"]}


@app.get("/", response_class=HTMLResponse)
async def landing_page(src: str = "direct"):
    download_db()
    source_clean = src.lower() if src.lower() in ["nfc", "qr"] else "direct"

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO analytics (event_type, source) VALUES (?, ?)", ("visit", source_clean))
    # Чиним старую базу: заменяем удалённую картинку Unsplash на встроенный баннер
    cursor.execute(
        "UPDATE banners SET image_url = ? WHERE image_url LIKE ?",
        (DEFAULT_BANNER_URL, f"%{DEAD_BANNER_MARK}%"),
    )
    conn.commit()
    cursor.execute("SELECT id, title, image_url, target_url FROM banners WHERE is_active = 1")
    rows = cursor.fetchall()
    conn.close()
    upload_db()

    banners_html = ""
    for r in rows:
        banners_html += f'''
        <div class="swiper-slide relative">
            <a href="{escape(str(r[3]), quote=True)}" target="_blank">
                <img src="{escape(str(r[2]), quote=True)}" class="w-full h-40 object-cover">
                <div class="absolute inset-0 bg-gradient-to-t from-black/80 via-black/20 to-transparent flex items-end p-4">
                    <span class="text-sm font-bold text-white">{escape(str(r[1]))}</span>
                </div>
            </a>
        </div>
        '''

    buttons_html = ""
    for key, item in SHOP_CONFIG["links"].items():
        buttons_html += f'''
        <a href="/go/{key}" class="glass link-card flex items-center justify-between p-4 rounded-2xl">
            <span class="font-medium text-sm">{item["title"]}</span>
            <i class="{item["icon"]} text-lg text-pink-400"></i>
        </a>
        '''

    contacts_html = ""
    for key, item in SHOP_CONFIG["contacts"].items():
        contacts_html += f'''
        <a class="contact-link" href="/go/{key}">
            <i class="{item["icon"]} contact-icon"></i> {item["title"]}
        </a>
        '''

    page = read_template("index.html")
    page = page.replace("{{ shop.name }}", SHOP_CONFIG["name"])
    page = page.replace("{{ shop.subtitle }}", SHOP_CONFIG["subtitle"])
    page = page.replace("{{ shop.logo }}", SHOP_CONFIG["logo"])
    page = page.replace("<!-- BANNERS_PLACEHOLDER -->", banners_html)
    page = page.replace("<!-- BUTTONS_PLACEHOLDER -->", buttons_html)
    page = page.replace("<!-- CONTACTS_PLACEHOLDER -->", contacts_html)
    return HTMLResponse(content=page)


@app.get("/go/{btn_key}")
async def track_click(btn_key: str):
    if btn_key not in ALL_LINKS:
        raise HTTPException(status_code=404, detail="Не найдено")
    download_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO analytics (event_type, source) VALUES (?, ?)", ("click", btn_key))
    conn.commit()
    conn.close()
    upload_db()
    return RedirectResponse(url=ALL_LINKS[btn_key]["url"], status_code=303)


@app.post("/api/review")
async def handle_review(rating: int = Form(...), comment: str = Form(""), phone: str = Form("")):
    download_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if rating >= 4:
        cursor.execute("INSERT INTO analytics (event_type, source) VALUES (?, ?)", ("click", "positive_review_5star"))
        conn.commit()
        conn.close()
        upload_db()
        return JSONResponse({
            "action": "choose_map",
            "yandex_url": SHOP_CONFIG["yandex_review_url"],
            "twogis_url": SHOP_CONFIG["twogis_review_url"],
        })
    cursor.execute("INSERT INTO feedback (rating, comment, phone) VALUES (?, ?, ?)", (rating, comment, phone))
    conn.commit()
    conn.close()
    upload_db()
    return JSONResponse({"action": "thank_you", "message": "Спасибо! Ваше обращение передано руководству."})


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(pwd: str = ""):
    if pwd != ADMIN_PWD:
        return HTMLResponse('''
        <body style="background:#090d16;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;">
            <form method="get" style="background:#1e293b;padding:30px;border-radius:16px;text-align:center;">
                <h3 style="margin-bottom:12px;">🔒 Вход в панель</h3>
                <input type="password" name="pwd" placeholder="Пароль" style="padding:8px;border-radius:6px;border:none;margin-bottom:12px;outline:none;"><br>
                <button type="submit" style="padding:8px 20px;background:#10b981;color:#fff;border:none;border-radius:6px;font-weight:bold;cursor:pointer;">Войти</button>
            </form>
        </body>
        ''')

    download_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT source, event_type FROM analytics")
    analytics_rows = cursor.fetchall()
    cursor.execute("SELECT id, rating, comment, phone, created_at FROM feedback ORDER BY id DESC")
    feedback_rows = cursor.fetchall()
    cursor.execute("SELECT id, title, image_url, target_url FROM banners")
    banner_rows = cursor.fetchall()
    conn.close()

    visits = [r[0] for r in analytics_rows if r[1] == "visit"]
    visit_counts = Counter(visits)
    qr_vs_nfc = {
        "nfc": visit_counts.get("nfc", 0),
        "qr": visit_counts.get("qr", 0),
        "direct": visit_counts.get("direct", 0),
    }
    clicks = [r[0] for r in analytics_rows if r[1] == "click"]
    click_counts = dict(Counter(clicks))

    feedbacks_html = ""
    for f in feedback_rows:
        phone_safe = escape(str(f[3] or ""), quote=True)
        feedbacks_html += f'''
        <tr>
            <td class="py-2.5 px-3 text-xs text-slate-400">{escape(str(f[4])[:16].replace('T', ' '))}</td>
            <td class="py-2.5 px-3 text-amber-400 font-bold">{f[1]} ★</td>
            <td class="py-2.5 px-3 text-xs">{escape(str(f[2] or ""))}</td>
            <td class="py-2.5 px-3 text-xs"><a href="tel:{phone_safe}" class="text-emerald-400 underline">{phone_safe}</a></td>
        </tr>
        '''
    if not feedback_rows:
        feedbacks_html = '<tr><td colspan="4" class="py-4 text-center text-xs text-slate-500">Жалоб пока нет 🎉</td></tr>'

    banners_admin_html = ""
    for b in banner_rows:
        banners_admin_html += f'''
        <div class="bg-slate-950 rounded-xl overflow-hidden border border-slate-800">
            <img src="{escape(str(b[2]), quote=True)}" class="h-24 w-full object-cover">
            <div class="p-2.5 flex justify-between items-center">
                <span class="text-xs truncate font-medium">{escape(str(b[1]))}</span>
                <form action="/admin/banner/delete/{b[0]}" method="post">
                    <input type="hidden" name="pwd" value="{escape(pwd, quote=True)}">
                    <button class="text-red-400 text-xs hover:underline">Удалить</button>
                </form>
            </div>
        </div>
        '''

    page = read_template("dashboard.html")
    page = page.replace("{{ pwd }}", escape(pwd, quote=True))
    page = page.replace("{{ qr_vs_nfc | tojson }}", json.dumps(qr_vs_nfc))
    page = page.replace("{{ click_counts | tojson }}", json.dumps(click_counts))
    page = page.replace("<!-- FEEDBACKS_PLACEHOLDER -->", feedbacks_html)
    page = page.replace("<!-- BANNERS_ADMIN_PLACEHOLDER -->", banners_admin_html)
    return HTMLResponse(content=page)


@app.post("/admin/banner/add")
async def add_banner(
    pwd: str = Form(...),
    title: str = Form(...),
    target_url: str = Form(...),
    image_file: UploadFile = File(None),
    image_url: str = Form(""),
):
    if pwd == ADMIN_PWD:
        final_image_url = image_url.strip() if image_url else ""

        if image_file and image_file.filename:
            ext = os.path.splitext(image_file.filename)[1]
            new_filename = f"{uuid.uuid4().hex}{ext}"

            if S3_KEY_ID:
                try:
                    get_s3_client().upload_fileobj(
                        image_file.file,
                        S3_BUCKET,
                        f"banners/{new_filename}",
                        ExtraArgs={"ACL": "public-read", "ContentType": image_file.content_type or "image/jpeg"},
                    )
                    final_image_url = f"{S3_ENDPOINT}/{S3_BUCKET}/banners/{new_filename}"
                except Exception as e:
                    print(f"S3 Error: {e}")

            if not final_image_url:
                local_path = os.path.join(UPLOADS_DIR, new_filename)
                with open(local_path, "wb+") as f_out:
                    shutil.copyfileobj(image_file.file, f_out)
                final_image_url = f"/uploads/{new_filename}"

        if final_image_url:
            download_db()
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO banners (title, image_url, target_url) VALUES (?, ?, ?)",
                (title, final_image_url, target_url),
            )
            conn.commit()
            conn.close()
            upload_db()
    return RedirectResponse(url=f"/admin?pwd={pwd}", status_code=303)


@app.post("/admin/banner/delete/{banner_id}")
async def delete_banner(banner_id: int, pwd: str = Form(...)):
    if pwd == ADMIN_PWD:
        download_db()
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banners WHERE id = ?", (banner_id,))
        conn.commit()
        conn.close()
        upload_db()
    return RedirectResponse(url=f"/admin?pwd={pwd}", status_code=303)
