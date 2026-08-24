import os
import re
import signal
import asyncio
import logging
import sqlite3
import hashlib
import httpx
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from telethon import TelegramClient, events, errors
from dotenv import load_dotenv

load_dotenv()

logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(level=logging.WARNING)

API_ID       = int(os.environ["TG_API_ID"])
API_HASH     = os.environ["TG_API_HASH"]
PHONE        = os.environ.get("TG_PHONE", "")
BOT_TOKEN    = os.environ.get("TG_BOT_TOKEN", "")
SESSION_NAME = os.environ["TG_SESSION_NAME"]
WEBHOOK_URL  = os.environ["DISCORD_WEBHOOK_URL"]
WEBHOOK_NAME = os.environ["DISCORD_WEBHOOK_NAME"]
SOURCE_ID    = os.environ["TG_SOURCE_ID"]
MENTION_USER = os.environ.get("DISCORD_USER_ID", "")
BANNER_URL   = os.environ.get("DISCORD_BANNER_URL", "").strip()
AVATAR_URL   = os.environ.get("DISCORD_AVATAR_URL", "").strip()
DB_PATH      = os.environ["DB_PATH"]
VERSION      = os.environ["APP_VERSION"]

try:
    SOURCE_ID = int(SOURCE_ID) if str(SOURCE_ID).lstrip("-").isdigit() \
                else SOURCE_ID.replace("@", "")
except (ValueError, AttributeError):
    SOURCE_ID = None

bot = TelegramClient(SESSION_NAME, API_ID, API_HASH)

WATCHLIST: dict[str, dict] = {
    "TURF": {
        "keywords":   ["angegriffen", "organisation hat"],
        "label":      "Turf War",
        "icon":       "⚔️",
        "hex_color":  0x9B59B6,
        "mention":    True,
    },
    "PENALTY": {
        "keywords":   ["strafe erhalten", "strafe", "bann", "ban", "warn", "sperrung"],
        "label":      "Admin Action",
        "icon":       "🛡️",
        "hex_color":  0xE74C3C,
        "mention":    True,
    },
    "SALE": {
        "keywords":   ["gegenstand verkauft", "gegenstand-log", "verkauft"],
        "label":      "Item Sold",
        "icon":       "💰",
        "hex_color":  0x2ECC71,
        "mention":    False,
    },
    "STOCK": {
        "keywords":   ["auf lager"],
        "label":      "Stock Update",
        "icon":       "📦",
        "hex_color":  0x3498DB,
        "mention":    False,
    },
}

WATCHLIST_DEFAULT = {
    "label":      "System",
    "icon":       "🔧",
    "hex_color":  0x95A5A6,
    "mention":    False,
}

for _cat in WATCHLIST.values():
    _cat["keywords"] = sorted(_cat["keywords"], key=len, reverse=True)


def _norm(text: str) -> str:
    """Collapse whitespace for stable hashing."""
    return " ".join(text.split()) if text else ""


def classify(text: str) -> tuple[str, dict]:
    low = text.lower()
    for cat, cfg in WATCHLIST.items():
        for kw in cfg["keywords"]:
            if kw in low:
                return cat, cfg
    return "SYSTEM", WATCHLIST_DEFAULT


_NON_DIGIT_RE = re.compile(r"\D")

class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        self._hash_cache: dict[str, bool] = {}
        self._MAX_CACHE = 2000

    def _migrate(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_hash  TEXT    UNIQUE NOT NULL,
                category  TEXT    NOT NULL,
                ts        DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stats (
                key          TEXT PRIMARY KEY,
                total_earned INTEGER DEFAULT 0,
                total_items  INTEGER DEFAULT 0
            );
            INSERT OR IGNORE INTO stats(key) VALUES('global');
            CREATE TABLE IF NOT EXISTS sales_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name  TEXT    NOT NULL,
                amount     INTEGER DEFAULT 1,
                price      INTEGER DEFAULT 0,
                ts         DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._conn.commit()

    def get_hash(self, text: str) -> str:
        return hashlib.md5(_norm(text).encode()).hexdigest()

    def is_duplicate(self, h: str) -> bool:
        if h in self._hash_cache:
            return True
        row = self._conn.execute(
            "SELECT 1 FROM processed_logs WHERE msg_hash=?", (h,)
        ).fetchone()
        if row:
            self._hash_cache[h] = True
            if len(self._hash_cache) > self._MAX_CACHE:
                self._hash_cache.pop(next(iter(self._hash_cache)))
            return True
        return False

    def mark(self, h: str, category: str):
        self._hash_cache[h] = True
        if len(self._hash_cache) > self._MAX_CACHE:
            self._hash_cache.pop(next(iter(self._hash_cache)))
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_logs(msg_hash, category) VALUES(?,?)",
            (h, category),
        )
        self._conn.commit()

    def add_revenue(self, price_str, amount_str) -> int:
        try:
            p_raw = str(price_str).replace(".", "").replace(",", "")
            price = int(_NON_DIGIT_RE.sub("", p_raw) or 0)
            amount = int(_NON_DIGIT_RE.sub("", str(amount_str)) or 1)
        except Exception:
            price, amount = 0, 1
        self._conn.execute(
            "UPDATE stats SET total_earned=total_earned+?, total_items=total_items+? WHERE key='global'",
            (price, amount),
        )
        self._conn.commit()
        return self.revenue()

    def record_sale(self, item, amount_str, price_str):
        try:
            p_raw = str(price_str).replace(".", "").replace(",", "")
            price = int(_NON_DIGIT_RE.sub("", p_raw) or 0)
            amount = int(_NON_DIGIT_RE.sub("", str(amount_str)) or 1)
        except Exception:
            price, amount = 0, 1
        self._conn.execute(
            "INSERT INTO sales_history(item_name, amount, price) VALUES(?,?,?)",
            (str(item or "Unknown"), amount, price)
        )
        self._conn.commit()

    def revenue(self) -> int:
        row = self._conn.execute(
            "SELECT total_earned FROM stats WHERE key='global'"
        ).fetchone()
        return row[0] if row else 0

    def total_processed(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM processed_logs").fetchone()
        return row[0] if row else 0

    def counts_by_category(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT category, COUNT(*) FROM processed_logs GROUP BY category"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def close(self):
        self._conn.close()


db = Database(DB_PATH)

class UIState:
    def __init__(self):
        self.status = "starting up"
        self.queue_depth = 0
        self.session_sent = 0
        self.session_synced = 0
        self.discord_ok = 0
        self.discord_fail = 0
        self.cat_counts: dict[str, int] = defaultdict(int)
        self.total_processed = db.total_processed()
        self.total_revenue = db.revenue()

    def push(self, category: str, cfg: dict, data: dict, is_sync: bool):
        parts = []
        if data.get("user"):   parts.append(data["user"])
        if data.get("admin"):  parts.append(f"↳ {data['admin']}")
        if data.get("item"):   parts.append(data["item"])
        if data.get("price"):  parts.append(data["price"])
        if data.get("server"): parts.append(f"@ {data['server']}")
        summary = "  ·  ".join(parts) if parts else "—"
        mode = " SYNC" if is_sync else ""
        print(f"[{datetime.now():%H:%M:%S}] {cfg['icon']} {cfg['label']}{mode}: {summary}", flush=True)
        self.total_processed += 1
        self.session_sent    += 1
        self.cat_counts[category] += 1
        if is_sync:
            self.session_synced += 1


ui = UIState()


class Parser:
    _PATTERNS_RAW = {
        "server": [r"Server:\s*([^\n\r]+)"],
        "id":     [r"Zeichen:\s*([^\n\r]+)", r"Charakter:\s*([^\n\r]+)", r"ID:\s*#?(\d+)"],
        "item":   [r"Titel:\s*([^\n\r]+)", r"Artikel:\s*([^\n\r]+)", r"Gegenstand:\s*([^\n\r]+)", r"Grund:\s*([^\n\r]+)"],
        "price":  [r"Verkaufspreis:\s*([^\n\r]+)", r"Preis:\s*([^\n\r]+)", r"Summe:\s*([^\n\r]+)"],
        "amount": [r"Menge:\s*([^\n\r]+)", r"Anzahl:\s*([^\n\r]+)"],
        "user":   [r"Käufer:\s*([^\n\r]+)", r"Spieler:\s*([^\n\r]+)", r"Name:\s*([^\n\r]+)"],
        "admin":  [r"Administrator:\s*([^\n\r]+)", r"Admin:\s*([^\n\r]+)"],
        "org_v":  [r"Organisation\s+([^\n\r]+?)\s+wurde"],
        "org_a":  [r"von\s+([^\n\r]+?)\s+angegriffen", r"Organisation\s+([^\n\r]+?)\s+hat"],
        "time":   [r"Dauer:\s*([^\n\r]+)"],
    }
    _COMPILED = {k: [re.compile(p, re.I | re.M) for p in v] for k, v in _PATTERNS_RAW.items()}
    @classmethod
    def parse(cls, text):
        out = {}
        for key, pats in cls._COMPILED.items():
            out[key] = None
            for p in pats:
                m = p.search(text)
                if m:
                    out[key] = m.group(1).strip()
                    break
        return out

def _build_embed(category, cfg, data, text, is_sync, rev_total):
    if category == "TURF": involved = f"**{data.get('org_a') or 'Attacker'}**  ➜  **{data.get('org_v') or 'Target'}**"
    elif data.get("admin"): involved = f"**{data['admin']}**  ➜  {data.get('user') or 'Player'}"
    elif data.get("user"): involved = data["user"]
    else: involved = "—"
    if category in ("PENALTY", "TURF"):
        desc_body = text.strip()[:1800]
    else:
        desc_body = (data.get("item") or text.strip())[:1800]
    fields = [{"name": "Involved", "value": involved, "inline": False}]
    if data.get("server") or data.get("id"):
        fields.extend([
            {"name": "Server", "value": f"`{data.get('server') or '—'}`", "inline": True},
            {"name": "Character ID", "value": f"`{data.get('id') or '—'}`", "inline": True},
        ])
    if data.get("price") or data.get("amount"):
        fields.extend([
            {"name": "Price", "value": f"**{data.get('price') or '—'}**", "inline": True},
            {"name": "Amount", "value": f"`{data.get('amount') or '—'}`", "inline": True},
        ])
    embed = {
        "author": {"name": WEBHOOK_NAME},
        "title": f"{cfg['icon']} {cfg['label']}{'  |  SYNC' if is_sync else ''}",
        "description": f"```\n{desc_body or text.strip()[:1800]}\n```",
        "color": cfg["hex_color"],
        "fields": fields,
        "footer": {"text": f"NRLX v{VERSION}  •  Telegram → Discord"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if BANNER_URL:
        embed["image"] = {"url": BANNER_URL}
    return embed

_http_client = None
async def _deliver(payload, webhook) -> bool:
    """Send to Discord webhook. Returns True on success, False after all retries fail."""
    if _http_client is None or not webhook: return False
    for attempt in range(5):
        try:
            res = await _http_client.post(webhook, json=payload)
            if res.status_code in (200, 204): 
                ui.discord_ok += 1
                return True
            if res.status_code == 429: 
                retry_after = float(res.json().get("retry_after", 2.0))
                await asyncio.sleep(retry_after)
                continue
            await asyncio.sleep(min(2 ** attempt, 30))
        except Exception: 
            await asyncio.sleep(min(2 ** attempt, 30))
    ui.discord_fail += 1
    return False


_queue = asyncio.Queue()
_shutdown = asyncio.Event()

async def _worker():
    while True:
        item = await _queue.get()
        try:
            if item is None:
                break
            text, is_sync = item
            await _process(text, is_sync)
        except Exception as e:
            ui.status = f"worker error: {e}"
        finally:
            _queue.task_done()
            ui.queue_depth = _queue.qsize()

async def enqueue(text, is_sync=False):
    if text and not _shutdown.is_set(): await _queue.put((text, is_sync)); ui.queue_depth = _queue.qsize()

async def _process(text, is_sync=False):
    h = db.get_hash(text)
    if db.is_duplicate(h): return
    category, cfg = classify(text)
    data = Parser.parse(text)

    current_rev = db.revenue()
    rev_for_message = current_rev
    if category == "SALE":
        try:
            p_raw = str(data.get("price", "0")).replace(".", "").replace(",", "")
            price = int(_NON_DIGIT_RE.sub("", p_raw) or 0)
            rev_for_message += price
        except: pass

    should_mention = cfg.get("mention", False)
    if category == "PENALTY":
        low_text = text.casefold()
        serious_kws = ("strafe erhalten", "ban", "bann", "sperrung", "warn")
        should_mention = any(keyword in low_text for keyword in serious_kws)

    mention = f"<@{MENTION_USER}>" if (should_mention and MENTION_USER) else ""
    embed = _build_embed(category, cfg, data, text, is_sync, rev_for_message if category == "SALE" else current_rev)
    payload = {
        "content": mention,
        "username": WEBHOOK_NAME,
        "avatar_url": AVATAR_URL,
        "embeds": [embed],
        "allowed_mentions": {"users": [MENTION_USER]} if mention else {"parse": []},
    }
    delivered = await _deliver(payload, cfg.get("webhook") or WEBHOOK_URL)
    
    if not delivered:
        ui.status = f"delivery failed - retrying  · {datetime.now().strftime('%H:%M:%S')}"
        asyncio.create_task(_retry_later(text, is_sync, delay=10))
        return

    if category == "SALE":
        rev_total = db.add_revenue(data["price"], data["amount"])
        db.record_sale(data["item"], data["amount"], data["price"])
        ui.total_revenue = rev_total
    
    db.mark(h, category)

    ui.push(category, cfg, data, is_sync)
    ui.status = f"{cfg['icon']} {cfg['label']} · {datetime.now().strftime('%H:%M:%S')}"

async def _retry_later(text: str, is_sync: bool, delay: float):
    """Wait `delay` seconds then re-queue a failed message."""
    await asyncio.sleep(delay)
    await enqueue(text, is_sync)


async def startup_and_sync():
    ui.status = "connecting..."
    try:
        await bot.get_input_entity(SOURCE_ID)
        ui.status = "connected"
    except Exception as e: 
        ui.status = f"error: {e}"
        print(f"Failed to connect to SOURCE_ID: {e}", flush=True)
        return

    ui.status = "syncing the last 48 hours..."
    new_count = 0
    dupe_count = 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    recent_messages = []
    async for msg in bot.iter_messages(SOURCE_ID):
        if _shutdown.is_set(): break
        message_date = msg.date
        if message_date and message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        if message_date and message_date < cutoff:
            break
        if not msg.text: continue

        recent_messages.append(msg)

    for msg in reversed(recent_messages):
        if _shutdown.is_set(): break
        h = db.get_hash(msg.text)
        if not db.is_duplicate(h):
            new_count += 1
            await enqueue(msg.text, is_sync=True)

            delay = 0.05
            if new_count % 5 == 0: delay = 0.2
            if new_count % 20 == 0: delay = 0.8

            if new_count % 10 == 0:
                ui.status = f"smart sync... ({new_count} new)"

            await asyncio.sleep(delay)
        else:
            dupe_count += 1

    ui.status = "live" if not _shutdown.is_set() else "stopped"
    print(f"48-hour sync finished: {new_count} new, {dupe_count} already saved.", flush=True)

@bot.on(events.NewMessage(chats=SOURCE_ID))
async def telegram_handler(event):
    if event.text: await enqueue(event.text)


async def start_bot():
    global _http_client, _main_loop
    _main_loop = asyncio.get_running_loop()
    _http_client = httpx.AsyncClient(timeout=12.0)
    try:
        if BOT_TOKEN: await bot.start(bot_token=BOT_TOKEN)
        elif PHONE: await bot.start(phone=PHONE)
        else: await bot.start()
    except Exception as e:
        print(f"Login failed: {e}", flush=True)
        return
    await startup_and_sync()
    worker_task = asyncio.create_task(_worker())
    ui.status = "live"
    try:
        while not _shutdown.is_set():
            await asyncio.sleep(0.5)
        ui.status = "draining queue..."
        await _queue.join()
        worker_task.cancel()
    except Exception as e:
        logging.error(f"Bot loop error: {e}")
    await bot.disconnect()
    if _http_client: await _http_client.aclose()
    db.close()

_main_loop = None

def _handle_sigint(*_):
    ui.status = "shutting down..."
    if _main_loop:
        _main_loop.call_soon_threadsafe(_shutdown.set)
        _main_loop.call_soon_threadsafe(_queue.put_nowait, None)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        _handle_sigint()
