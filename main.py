import html as htmllib
import json
import os
import secrets
from collections import Counter

import psycopg
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# ---------- Настройки (берутся из переменных окружения Vercel) ----------
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_PWD = os.getenv("ADMIN_PWD", "")
MAX_IMAGE_BYTES = 1_000_000  # 1 МБ на картинку баннера

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Vape Hub Serverless")


def esc(value) -> str:
    """Экранирование текста перед вставкой в HTML (защита от XSS)."""
    return htmllib.escape(str(value if value is not None else ""), quote=True)


def read_template(name: str) -> str:
    # ищем и в папке templates/, и в корне проекта
    for folder in (os.path.join(BASE_DIR, "templates"), BASE_DIR):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    raise FileNotFoundError(name)


def check_pwd(pwd: str) -> bool:
    return bool(ADMIN_PWD) and secrets.compare_digest(pwd.encode(), ADMIN_PWD.encode())


# ---------- База данных (Neon Postgres) ----------
_db_ready = False


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytics (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            source TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            rating INTEGER NOT NULL,
            comment TEXT,
            phone TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS banners (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            image_url TEXT NOT NULL,
            target_url TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            image_data BYTEA,
            image_mime TEXT
        )""")
    count = conn.execute("SELECT COUNT(*) FROM banners").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO banners (title, image_url, target_url) VALUES (%s, %s, %s)",
            (
                "Новинки Waka и HQD",
                "https://images.unsplash.com/photo-1560913210-91e811632701?auto=format&fit=crop&w=800&q=80",
                "https://t.me",
            ),
        )
    conn.commit()


def get_conn():
    """Новое соединение на каждый запрос (так правильно для serverless)."""
    global _db_ready
    conn = psycopg.connect(DATABASE_URL)
    if not _db_ready:
        init_db(conn)
        _db_ready = True
    return conn


SHOP_CONFIG = {
    "name": "SMOKE LAB MOSCOW",
    "subtitle": "Информационный портал для совершеннолетних (18+)",
    "logo": "https://cdn-icons-png.flaticon.com/512/3076/3076045.png",
    "yandex_review_url": "https://yandex.ru/maps",
    "twogis_review_url": "https://2gis.ru/moscow",
    "links": {
        "telegram_catalog": {"title": "💨 Наличие и вкусы (Telegram)", "url": "https://t.me", "icon": "fa-brands fa-telegram"},
        "loyalty": {"title": "💳 Карта постоянного гостя", "url": "https://t.me", "icon": "fa-solid fa-id-card"},
        "whatsapp": {"title": "💬 Написать в WhatsApp", "url": "https://wa.me/79000000000", "icon": "fa-brands fa-whatsapp"},
        "vk": {"title": "📘 Сообщество ВКонтакте", "url": "https://vk.com", "icon": "fa-brands fa-vk"},
        "phone": {"title": "📞 Позвонить в магазин", "url": "tel:+79000000000", "icon": "fa-solid fa-phone"},
    },
}


# ---------- Страницы ----------
@app.get("/", response_class=HTMLResponse)
async def landing_page(src: str = "direct"):
    source_clean = src.lower() if src.lower() in ["nfc", "qr"] else "direct"

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO analytics (event_type, source) VALUES (%s, %s)",
            ("visit", source_clean),
        )
        rows = conn.execute(
            "SELECT id, title, image_url, target_url FROM banners WHERE is_active = TRUE ORDER BY id"
        ).fetchall()

    banners_html = ""
    for r in rows:
        banners_html += f'''
        <div class="swiper-slide relative">
            <a href="{esc(r[3])}" target="_blank">
                <img src="{esc(r[2])}" class="w-full h-40 object-cover">
                <div class="absolute inset-0 bg-gradient-to-t from-black/80 via-black/20 to-transparent flex items-end p-4">
                    <span class="text-sm font-bold text-white">{esc(r[1])}</span>
                </div>
            </a>
        </div>
        '''

    buttons_html = ""
    for key, item in SHOP_CONFIG["links"].items():
        buttons_html += f'''
        <a href="/go/{key}" class="glass flex items-center justify-between p-4 rounded-2xl hover:bg-slate-800/80 active:scale-98 transition">
            <span class="font-medium text-sm">{item["title"]}</span>
            <i class="{item["icon"]} text-lg text-emerald-400"></i>
        </a>
        '''

    page = read_template("index.html")
    page = page.replace("{{ shop.name }}", SHOP_CONFIG["name"])
    page = page.replace("{{ shop.subtitle }}", SHOP_CONFIG["subtitle"])
    page = page.replace("{{ shop.logo }}", SHOP_CONFIG["logo"])
    page = page.replace("<!-- BANNERS_PLACEHOLDER -->", banners_html)
    page = page.replace("<!-- BUTTONS_PLACEHOLDER -->", buttons_html)
    return HTMLResponse(content=page)


@app.get("/banner-img/{banner_id}")
async def banner_image(banner_id: int):
    """Отдаёт картинку баннера, загруженную через админку (хранится в базе)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT image_data, image_mime FROM banners WHERE id = %s", (banner_id,)
        ).fetchone()
    if not row or row[0] is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    return Response(
        content=bytes(row[0]),
        media_type=row[1] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/go/{btn_key}")
async def track_click(btn_key: str):
    if btn_key not in SHOP_CONFIG["links"]:
        raise HTTPException(status_code=404, detail="Не найдено")

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO analytics (event_type, source) VALUES (%s, %s)",
            ("click", btn_key),
        )
    return RedirectResponse(url=SHOP_CONFIG["links"][btn_key]["url"], status_code=303)


@app.post("/api/review")
async def handle_review(rating: int = Form(...), comment: str = Form(""), phone: str = Form("")):
    with get_conn() as conn:
        if rating >= 4:
            conn.execute(
                "INSERT INTO analytics (event_type, source) VALUES (%s, %s)",
                ("click", "positive_review_5star"),
            )
            return JSONResponse({
                "action": "choose_map",
                "yandex_url": SHOP_CONFIG["yandex_review_url"],
                "twogis_url": SHOP_CONFIG["twogis_review_url"],
            })

        conn.execute(
            "INSERT INTO feedback (rating, comment, phone) VALUES (%s, %s, %s)",
            (rating, comment, phone),
        )
    return JSONResponse({"action": "thank_you", "message": "Спасибо! Ваше обращение передано руководству."})


# ---------- Админка ----------
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(pwd: str = ""):
    if not check_pwd(pwd):
        return HTMLResponse('''
        <body style="background:#090d16;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;">
            <form method="get" style="background:#1e293b;padding:30px;border-radius:16px;text-align:center;">
                <h3 style="margin-bottom:12px;">🔒 Вход в панель</h3>
                <input type="password" name="pwd" placeholder="Пароль" style="padding:8px;border-radius:6px;border:none;margin-bottom:12px;outline:none;"><br>
                <button type="submit" style="padding:8px 20px;background:#10b981;color:#fff;border:none;border-radius:6px;font-weight:bold;cursor:pointer;">Войти</button>
            </form>
        </body>
        ''')

    with get_conn() as conn:
        analytics_rows = conn.execute("SELECT source, event_type FROM analytics").fetchall()
        feedback_rows = conn.execute(
            "SELECT id, rating, comment, phone, created_at FROM feedback ORDER BY id DESC"
        ).fetchall()
        banner_rows = conn.execute(
            "SELECT id, title, image_url, target_url FROM banners ORDER BY id"
        ).fetchall()

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
        feedbacks_html += f'''
        <tr>
            <td class="py-2.5 px-3 text-xs text-slate-400">{esc(str(f[4])[:16])}</td>
            <td class="py-2.5 px-3 text-amber-400 font-bold">{esc(f[1])} ★</td>
            <td class="py-2.5 px-3 text-xs">{esc(f[2])}</td>
            <td class="py-2.5 px-3 text-xs"><a href="tel:{esc(f[3])}" class="text-emerald-400 underline">{esc(f[3])}</a></td>
        </tr>
        '''
    if not feedback_rows:
        feedbacks_html = '<tr><td colspan="4" class="py-4 text-center text-xs text-slate-500">Жалоб пока нет 🎉</td></tr>'

    banners_admin_html = ""
    for b in banner_rows:
        banners_admin_html += f'''
        <div class="bg-slate-950 rounded-xl overflow-hidden border border-slate-800">
            <img src="{esc(b[2])}" class="h-24 w-full object-cover">
            <div class="p-2.5 flex justify-between items-center">
                <span class="text-xs truncate font-medium">{esc(b[1])}</span>
                <form action="/admin/banner/delete/{b[0]}" method="post">
                    <input type="hidden" name="pwd" value="{esc(pwd)}">
                    <button class="text-red-400 text-xs hover:underline">Удалить</button>
                </form>
            </div>
        </div>
        '''

    page = read_template("dashboard.html")
    page = page.replace("{{ pwd }}", esc(pwd))
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
    if not check_pwd(pwd):
        raise HTTPException(status_code=403, detail="Неверный пароль")

    final_image_url = image_url.strip()
    data = None
    mime = None

    # Картинка с компьютера хранится прямо в базе (подходит для небольших баннеров)
    if image_file and image_file.filename:
        data = await image_file.read()
        mime = image_file.content_type or "image/jpeg"
        if not mime.startswith("image/"):
            raise HTTPException(status_code=400, detail="Нужен файл-картинка")
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Картинка больше 1 МБ — сожмите её")

    if not data and not final_image_url:
        return RedirectResponse(url=f"/admin?pwd={pwd}", status_code=303)

    with get_conn() as conn:
        banner_id = conn.execute(
            "INSERT INTO banners (title, image_url, target_url, image_data, image_mime) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (title, final_image_url, target_url, data, mime),
        ).fetchone()[0]
        if data:
            conn.execute(
                "UPDATE banners SET image_url = %s WHERE id = %s",
                (f"/banner-img/{banner_id}", banner_id),
            )

    return RedirectResponse(url=f"/admin?pwd={pwd}", status_code=303)


@app.post("/admin/banner/delete/{banner_id}")
async def delete_banner(banner_id: int, pwd: str = Form(...)):
    if not check_pwd(pwd):
        raise HTTPException(status_code=403, detail="Неверный пароль")

    with get_conn() as conn:
        conn.execute("DELETE FROM banners WHERE id = %s", (banner_id,))
    return RedirectResponse(url=f"/admin?pwd={pwd}", status_code=303)
