"""
Tastytrade Daily Report Bot para Telegram
Envía un informe completo cada día al cierre del mercado (4:15 PM ET)
"""

import os
import asyncio
import logging
from datetime import datetime, date
import pytz
import schedule
import time
import threading

import requests
import telebot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Configuración (viene de variables de entorno en Railway) ───────────────
TASTYTRADE_USER     = os.environ["TASTYTRADE_USER"]
TASTYTRADE_PASS     = os.environ["TASTYTRADE_PASS"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]
ACCOUNT_NUMBER      = os.environ.get("TASTYTRADE_ACCOUNT", "")   # opcional si tienes >1 cuenta
REPORT_HOUR_ET      = int(os.environ.get("REPORT_HOUR_ET", "16"))
REPORT_MINUTE_ET    = int(os.environ.get("REPORT_MINUTE_ET", "15"))
SANDBOX             = os.environ.get("TASTYTRADE_SANDBOX", "false").lower() == "true"

BASE_URL = "https://api.cert.tastyworks.com" if SANDBOX else "https://api.tastyworks.com"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ─── Autenticación Tastytrade ───────────────────────────────────────────────

def get_session_token() -> str:
    """Obtiene un token de sesión usando usuario/contraseña."""
    resp = requests.post(
        f"{BASE_URL}/sessions",
        json={"login": TASTYTRADE_USER, "password": TASTYTRADE_PASS},
        headers={"Content-Type": "application/json"},
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    log.info("Sesión Tastytrade iniciada correctamente.")
    return data["session-token"]


def get_accounts(token: str) -> list[dict]:
    """Retorna la lista de cuentas del usuario."""
    resp = requests.get(
        f"{BASE_URL}/customers/me/accounts",
        headers={"Authorization": token},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()["data"]["items"]


def get_transactions(token: str, account: str, start_date: str) -> list[dict]:
    """Retorna todas las transacciones desde start_date (YYYY-MM-DD)."""
    resp = requests.get(
        f"{BASE_URL}/accounts/{account}/transactions",
        headers={"Authorization": token},
        params={"start-date": start_date, "per-page": 250},
        timeout=20
    )
    resp.raise_for_status()
    return resp.json()["data"]["items"]


def get_positions(token: str, account: str) -> list[dict]:
    """Retorna posiciones abiertas actuales."""
    resp = requests.get(
        f"{BASE_URL}/accounts/{account}/positions",
        headers={"Authorization": token},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()["data"]["items"]

# ─── Análisis de datos ──────────────────────────────────────────────────────

STRATEGY_PATTERNS = {
    "Iron Condor":      lambda legs: len(legs) == 4,
    "Strangle":         lambda legs: len(legs) == 2 and all(l["option-type"] in ("C","P") for l in legs),
    "Iron Butterfly":   lambda legs: len(legs) == 4,
    "Cash Secured Put": lambda legs: len(legs) == 1 and legs[0].get("option-type") == "P",
    "Covered Call":     lambda legs: len(legs) == 1 and legs[0].get("option-type") == "C",
    "Put Spread":       lambda legs: len(legs) == 2 and all(l.get("option-type") == "P" for l in legs),
    "Call Spread":      lambda legs: len(legs) == 2 and all(l.get("option-type") == "C" for l in legs),
    "Naked Put":        lambda legs: len(legs) == 1 and legs[0].get("option-type") == "P",
    "PMCC":             lambda legs: len(legs) == 2,
}


def classify_dte(days: int) -> str:
    if days == 0:   return "0DTE"
    if days == 1:   return "1DTE"
    if days <= 7:   return "2-7 DTE"
    if days <= 21:  return "8-21 DTE"
    return "21+ DTE"


def parse_transactions(txns: list[dict], target_date: date) -> dict:
    """Extrae métricas del día desde la lista de transacciones."""
    today_txns = [
        t for t in txns
        if t.get("executed-at", "")[:10] == target_date.isoformat()
    ]

    trades_closed  = []
    commissions    = 0.0
    fees           = 0.0

    for t in today_txns:
        t_type = t.get("transaction-type", "")
        sub    = t.get("transaction-sub-type", "")
        value  = float(t.get("net-value", 0))

        if t_type == "Trade":
            trades_closed.append({
                "symbol":      t.get("underlying-symbol", "?"),
                "description": t.get("description", ""),
                "value":       value,
                "action":      sub,
                "price":       float(t.get("price", 0)),
            })
        elif t_type in ("Commission", "Fee"):
            commissions += abs(value)
        elif "fee" in t_type.lower():
            fees += abs(value)

    pnl_gross    = sum(t["value"] for t in trades_closed)
    total_costs  = commissions + fees
    pnl_net      = pnl_gross - total_costs

    wins  = [t for t in trades_closed if t["value"] > 0]
    loses = [t for t in trades_closed if t["value"] <= 0]

    return {
        "date":          target_date,
        "trades":        trades_closed,
        "pnl_gross":     pnl_gross,
        "pnl_net":       pnl_net,
        "commissions":   commissions,
        "fees":          fees,
        "total_costs":   total_costs,
        "wins":          wins,
        "losses":        loses,
        "win_rate":      len(wins) / len(trades_closed) * 100 if trades_closed else 0,
        "avg_winner":    sum(t["value"] for t in wins)  / len(wins)  if wins  else 0,
        "avg_loser":     sum(t["value"] for t in loses) / len(loses) if loses else 0,
    }


def format_pnl(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}${value:,.2f}"


def emoji_result(value: float) -> str:
    if value > 0:   return "✅"
    if value < 0:   return "❌"
    return "➖"

# ─── Construcción del mensaje ───────────────────────────────────────────────

def build_report(metrics: dict, positions: list[dict], account: str) -> str:
    d      = metrics["date"]
    trades = metrics["trades"]
    dow    = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"][d.weekday()]

    lines = []
    lines.append(f"📒 *Bitácora Tastytrade · {dow} {d.strftime('%d %b %Y')}*")
    lines.append(f"🏦 Cuenta: `{account}`")
    lines.append("─" * 32)

    # P&L resumen
    emoji = emoji_result(metrics["pnl_net"])
    lines.append(f"{emoji} *P&L neto del día:* `{format_pnl(metrics['pnl_net'])}`")
    lines.append(f"   P&L bruto:      `{format_pnl(metrics['pnl_gross'])}`")
    lines.append(f"   Comisiones:     `-${metrics['total_costs']:.2f}`")
    lines.append("")

    # Trades del día
    if trades:
        lines.append(f"🎯 *Trades cerrados hoy: {len(trades)}*")
        for t in trades[:8]:   # máximo 8 para no saturar
            e = emoji_result(t["value"])
            sym = t["symbol"].ljust(6)
            lines.append(f"   {e} `{sym}` {format_pnl(t['value'])}")
        if len(trades) > 8:
            lines.append(f"   ... y {len(trades)-8} más")
    else:
        lines.append("🎯 *Sin trades cerrados hoy*")
    lines.append("")

    # Win rate del día
    if trades:
        lines.append(f"📈 *Estadísticas del día*")
        lines.append(f"   Win rate:   `{metrics['win_rate']:.0f}%` ({len(metrics['wins'])}W / {len(metrics['losses'])}L)")
        if metrics["avg_winner"]:
            lines.append(f"   Avg ganador: `{format_pnl(metrics['avg_winner'])}`")
        if metrics["avg_loser"]:
            lines.append(f"   Avg perdedor: `{format_pnl(metrics['avg_loser'])}`")
        lines.append("")

    # Posiciones abiertas
    if positions:
        lines.append(f"📋 *Posiciones abiertas: {len(positions)}*")
        for p in positions[:6]:
            sym  = p.get("underlying-symbol", "?")
            qty  = p.get("quantity", 0)
            desc = p.get("instrument-type", "")
            lines.append(f"   • `{sym}` {qty} · {desc}")
        if len(positions) > 6:
            lines.append(f"   ... y {len(positions)-6} más")
    else:
        lines.append("📋 *Sin posiciones abiertas*")

    lines.append("")
    lines.append("─" * 32)
    lines.append(f"🕓 Generado a las {datetime.now(pytz.timezone('US/Eastern')).strftime('%I:%M %p ET')}")

    return "\n".join(lines)

# ─── Envío a Telegram ───────────────────────────────────────────────────────

def send_report():
    """Obtiene datos de Tastytrade y envía el informe a Telegram."""
    log.info("Generando informe diario...")
    try:
        token    = get_session_token()
        accounts = get_accounts(token)

        # Selecciona cuenta (primera o la configurada)
        account_num = ACCOUNT_NUMBER
        if not account_num:
            account_num = accounts[0]["account"]["account-number"]

        today     = date.today()
        txns      = get_transactions(token, account_num, today.isoformat())
        positions = get_positions(token, account_num)
        metrics   = parse_transactions(txns, today)
        message   = build_report(metrics, positions, account_num)

        bot.send_message(
            TELEGRAM_CHAT_ID,
            message,
            parse_mode="Markdown"
        )
        log.info("Informe enviado a Telegram correctamente.")

    except Exception as e:
        log.error(f"Error generando informe: {e}")
        try:
            bot.send_message(
                TELEGRAM_CHAT_ID,
                f"⚠️ Error generando el informe diario:\n`{str(e)}`",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ─── Comandos manuales del bot ──────────────────────────────────────────────

@bot.message_handler(commands=["start", "hola"])
def cmd_start(msg):
    bot.reply_to(msg, (
        "📒 *Bitácora Tastytrade activa*\n\n"
        "Comandos disponibles:\n"
        "/informe — Generar informe ahora\n"
        "/posiciones — Ver posiciones abiertas\n"
        "/status — Estado del bot\n\n"
        "El informe automático llega cada día a las 4:15 PM ET 📊"
    ), parse_mode="Markdown")


@bot.message_handler(commands=["informe", "report"])
def cmd_informe(msg):
    bot.reply_to(msg, "⏳ Generando informe, un momento...")
    send_report()


@bot.message_handler(commands=["posiciones", "positions"])
def cmd_posiciones(msg):
    try:
        token     = get_session_token()
        accounts  = get_accounts(token)
        acct      = ACCOUNT_NUMBER or accounts[0]["account"]["account-number"]
        positions = get_positions(token, acct)

        if not positions:
            bot.reply_to(msg, "📋 No tienes posiciones abiertas.")
            return

        lines = [f"📋 *Posiciones abiertas · {len(positions)} total*\n"]
        for p in positions:
            sym  = p.get("underlying-symbol", "?")
            qty  = p.get("quantity", 0)
            typ  = p.get("instrument-type", "")
            lines.append(f"• `{sym}` ×{qty} — {typ}")
        bot.reply_to(msg, "\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {e}")


@bot.message_handler(commands=["status"])
def cmd_status(msg):
    et = pytz.timezone("US/Eastern")
    now = datetime.now(et)
    bot.reply_to(msg, (
        f"✅ *Bitácora Tastytrade activa*\n"
        f"🕓 Hora ET: `{now.strftime('%I:%M %p')}`\n"
        f"📅 Fecha: `{now.strftime('%d %b %Y')}`\n"
        f"⏰ Próximo informe: `{REPORT_HOUR_ET:02d}:{REPORT_MINUTE_ET:02d} ET`\n"
        f"🔧 Sandbox: `{SANDBOX}`"
    ), parse_mode="Markdown")

# ─── Scheduler ─────────────────────────────────────────────────────────────

def run_scheduler():
    et = pytz.timezone("US/Eastern")
    report_time = f"{REPORT_HOUR_ET:02d}:{REPORT_MINUTE_ET:02d}"

    def job():
        # Solo enviar en días de mercado (lun–vie)
        now = datetime.now(et)
        if now.weekday() < 5:
            send_report()
        else:
            log.info("Fin de semana — informe omitido.")

    schedule.every().day.at(report_time).do(job)
    log.info(f"Scheduler configurado: informe diario a las {report_time} ET (lun–vie).")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ─── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Bitácora Tastytrade iniciando...")

    # Mensaje de arranque
    try:
        bot.send_message(
            TELEGRAM_CHAT_ID,
            "🚀 *Bitácora Tastytrade iniciada*\nEl informe automático llegará cada día hábil al cierre del mercado.",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.warning(f"No se pudo enviar mensaje de arranque: {e}")

    # Scheduler en hilo separado
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    # Polling de comandos
    log.info("Escuchando comandos de Telegram...")
    bot.infinity_polling()
