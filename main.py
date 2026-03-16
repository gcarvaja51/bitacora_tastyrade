"""
Bitacora Tastytrade - Bot de Telegram
Informe diario automatico al cierre del mercado (4:15 PM ET)
Autenticación via OAuth2
"""

import os
import logging
from datetime import datetime, date, timedelta
from collections import defaultdict
import pytz
import schedule
import time
import threading

import requests
import telebot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Configuración ──────────────────────────────────────────────────────────
TASTYTRADE_CLIENT_SECRET = os.environ["TASTYTRADE_CLIENT_SECRET"]
TASTYTRADE_REFRESH_TOKEN = os.environ["TASTYTRADE_REFRESH_TOKEN"]
TELEGRAM_BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
ACCOUNT_NUMBER           = os.environ.get("TASTYTRADE_ACCOUNT", "")
REPORT_HOUR_ET           = int(os.environ.get("REPORT_HOUR_ET", "16"))
REPORT_MINUTE_ET         = int(os.environ.get("REPORT_MINUTE_ET", "15"))
SANDBOX                  = os.environ.get("TASTYTRADE_SANDBOX", "false").lower() == "true"

BASE_URL = "https://api.cert.tastytrade.com" if SANDBOX else "https://api.tastytrade.com"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ─── Autenticación OAuth ────────────────────────────────────────────────────

def get_access_token() -> str:
    resp = requests.post(
        f"{BASE_URL}/oauth/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": TASTYTRADE_REFRESH_TOKEN,
            "client_secret": TASTYTRADE_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15
    )
    if not resp.ok:
        log.error(f"OAuth error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    log.info(f"OAuth response keys: {list(data.keys())}")
    token = data.get("access_token") or data.get("session-token") or str(data)
    log.info("Token OAuth obtenido correctamente.")
    return token

def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ─── API Tastytrade ─────────────────────────────────────────────────────────

def get_accounts(token: str) -> list:
    resp = requests.get(f"{BASE_URL}/customers/me/accounts", headers=auth_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]["items"]

def get_transactions(token: str, account: str, start_date: str) -> list:
    resp = requests.get(
        f"{BASE_URL}/accounts/{account}/transactions",
        headers=auth_headers(token),
        params={"start-date": start_date, "per-page": 250},
        timeout=20
    )
    resp.raise_for_status()
    return resp.json()["data"]["items"]

def get_positions(token: str, account: str) -> list:
    resp = requests.get(f"{BASE_URL}/accounts/{account}/positions", headers=auth_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]["items"]

# ─── Análisis ───────────────────────────────────────────────────────────────

def parse_transactions(txns: list, target_date: date) -> dict:
    today_txns = [t for t in txns if t.get("executed-at", "")[:10] == target_date.isoformat()]

    trades, commissions, fees = [], 0.0, 0.0
    for t in today_txns:
        t_type = t.get("transaction-type", "")
        value  = float(t.get("net-value", 0))
        if t_type == "Trade":
            trades.append({"symbol": t.get("underlying-symbol", "?"), "value": value, "action": t.get("transaction-sub-type", "")})
        elif t_type == "Commission":
            commissions += abs(value)
        elif "fee" in t_type.lower():
            fees += abs(value)

    pnl_gross   = sum(t["value"] for t in trades)
    total_costs = commissions + fees
    wins  = [t for t in trades if t["value"] > 0]
    loses = [t for t in trades if t["value"] <= 0]

    return {
        "date": target_date, "trades": trades,
        "pnl_gross": pnl_gross, "pnl_net": pnl_gross - total_costs,
        "commissions": commissions, "total_costs": total_costs,
        "wins": wins, "losses": loses,
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "avg_winner": sum(t["value"] for t in wins) / len(wins) if wins else 0,
        "avg_loser":  sum(t["value"] for t in loses) / len(loses) if loses else 0,
    }

def fmt(v: float) -> str:
    return f"{'+'if v>=0 else ''}${v:,.2f}"

def emoji(v: float) -> str:
    return "✅" if v > 0 else ("❌" if v < 0 else "➖")

# ─── Mensaje ────────────────────────────────────────────────────────────────

def build_report(metrics: dict, positions: list, account: str) -> str:
    d   = metrics["date"]
    dow = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"][d.weekday()]
    lines = [
        f"📒 *Bitacora Tastytrade · {dow} {d.strftime('%d %b %Y')}*",
        f"🏦 Cuenta: `{account}`",
        "─" * 30,
        f"{emoji(metrics['pnl_net'])} *P&L neto del dia:* `{fmt(metrics['pnl_net'])}`",
        f"   P&L bruto:    `{fmt(metrics['pnl_gross'])}`",
        f"   Comisiones:   `-${metrics['total_costs']:.2f}`",
        "",
    ]

    trades = metrics["trades"]
    if trades:
        lines.append(f"🎯 *Trades cerrados hoy: {len(trades)}*")
        for t in trades[:8]:
            lines.append(f"   {emoji(t['value'])} `{t['symbol'].ljust(6)}` {fmt(t['value'])}")
        if len(trades) > 8:
            lines.append(f"   ... y {len(trades)-8} más")
        lines += [
            "",
            f"📈 *Estadisticas*",
            f"   Win rate:    `{metrics['win_rate']:.0f}%` ({len(metrics['wins'])}W / {len(metrics['losses'])}L)",
        ]
        if metrics["avg_winner"]: lines.append(f"   Avg ganador: `{fmt(metrics['avg_winner'])}`")
        if metrics["avg_loser"]:  lines.append(f"   Avg perdedor:`{fmt(metrics['avg_loser'])}`")
    else:
        lines.append("🎯 *Sin trades cerrados hoy*")

    lines.append("")
    if positions:
        lines.append(f"📋 *Posiciones abiertas: {len(positions)}*")
        for p in positions[:6]:
            lines.append(f"   • `{p.get('underlying-symbol','?')}` ×{p.get('quantity',0)} {p.get('instrument-type','')}")
        if len(positions) > 6:
            lines.append(f"   ... y {len(positions)-6} más")
    else:
        lines.append("📋 *Sin posiciones abiertas*")

    et_now = datetime.now(pytz.timezone("US/Eastern"))
    lines += ["", "─" * 30, f"🕓 `{et_now.strftime('%I:%M %p ET')}`"]
    return "\n".join(lines)

# ─── Envío ──────────────────────────────────────────────────────────────────

def send_report():
    log.info("Generando informe diario...")
    try:
        token    = get_access_token()
        accounts = get_accounts(token)
        acct     = ACCOUNT_NUMBER or accounts[0]["account"]["account-number"]
        today    = date.today()
        txns     = get_transactions(token, acct, today.isoformat())
        positions= get_positions(token, acct)
        metrics  = parse_transactions(txns, today)
        message  = build_report(metrics, positions, acct)
        bot.send_message(TELEGRAM_CHAT_ID, message, parse_mode="Markdown")
        log.info("Informe enviado.")
    except Exception as e:
        log.error(f"Error: {e}")
        try:
            bot.send_message(TELEGRAM_CHAT_ID, f"⚠️ Error generando informe:\n`{str(e)}`", parse_mode="Markdown")
        except Exception:
            pass

# ─── Comandos ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "hola"])
def cmd_start(msg):
    bot.reply_to(msg, (
        "📒 *Bitacora Tastytrade activa*\n\n"
        "/informe — Informe del dia\n"
        "/dia — Resumen de hoy\n"
        "/semana — Ultimos 7 dias\n"
        "/mes — Ultimos 30 dias\n"
        "/historico — Ultimos 90 dias\n"
        "/posiciones — Posiciones abiertas\n"
        "/status — Estado del bot"
    ), parse_mode="Markdown")

@bot.message_handler(commands=["informe", "report"])
def cmd_informe(msg):
    bot.reply_to(msg, "⏳ Generando informe, un momento...")
    send_report()

@bot.message_handler(commands=["posiciones", "positions"])
def cmd_posiciones(msg):
    try:
        token     = get_access_token()
        accounts  = get_accounts(token)
        acct      = ACCOUNT_NUMBER or accounts[0]["account"]["account-number"]
        positions = get_positions(token, acct)
        if not positions:
            bot.reply_to(msg, "📋 No tienes posiciones abiertas.")
            return
        lines = [f"📋 *Posiciones abiertas · {len(positions)} total*\n"]
        for p in positions:
            lines.append(f"• `{p.get('underlying-symbol','?')}` ×{p.get('quantity',0)} — {p.get('instrument-type','')}")
        bot.reply_to(msg, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {e}")

@bot.message_handler(commands=["status"])
def cmd_status(msg):
    et = pytz.timezone("US/Eastern")
    now = datetime.now(et)
    bot.reply_to(msg, (
        f"✅ *Bitacora Tastytrade activa*\n"
        f"🕓 Hora ET: `{now.strftime('%I:%M %p')}`\n"
        f"📅 Fecha: `{now.strftime('%d %b %Y')}`\n"
        f"⏰ Proximo informe: `{REPORT_HOUR_ET:02d}:{REPORT_MINUTE_ET:02d} ET`"
    ), parse_mode="Markdown")

# ─── Scheduler ──────────────────────────────────────────────────────────────

def run_scheduler():
    et = pytz.timezone("US/Eastern")
    report_time = f"{REPORT_HOUR_ET:02d}:{REPORT_MINUTE_ET:02d}"

    def job():
        if datetime.now(et).weekday() < 5:
            send_report()

    schedule.every().day.at(report_time).do(job)
    log.info(f"Scheduler: informe diario a las {report_time} ET (lun-vie).")
    while True:
        schedule.run_pending()
        time.sleep(30)

# ─── Main ───────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    log.info("Bitacora Tastytrade iniciando...")
    try:
        bot.send_message(TELEGRAM_CHAT_ID,
            "Bitacora Tastytrade iniciada. El informe automatico llegara cada dia habil al cierre del mercado.",
            parse_mode="Markdown")
    except Exception as e:
        log.warning(f"No se pudo enviar mensaje de arranque: {e}")

    threading.Thread(target=run_scheduler, daemon=True).start()
    log.info("Escuchando comandos de Telegram...")
    bot.infinity_polling()

from datetime import timedelta
from collections import defaultdict

def get_period_metrics(days_back, label):
    try:
        token = get_access_token()
        accounts = get_accounts(token)
        acct = ACCOUNT_NUMBER or accounts[0]["account"]["account-number"]
        et = pytz.timezone("US/Eastern")
        today = datetime.now(et).date()
        start = today - timedelta(days=days_back)
        txns = get_transactions(token, acct, start.isoformat())
        positions = get_positions(token, acct)
        daily = defaultdict(list)
        commissions_total = 0.0
        fees_total = 0.0
        for t in txns:
            d = t.get("executed-at", "")[:10]
            t_type = t.get("transaction-type", "")
            value = float(t.get("net-value", 0))
            if t_type == "Trade":
                daily[d].append({"symbol": t.get("underlying-symbol","?"), "value": value})
            elif t_type == "Commission":
                commissions_total += abs(value)
            elif "fee" in t_type.lower():
                fees_total += abs(value)
        all_trades = [t for trades in daily.values() for t in trades]
        pnl_gross = sum(t["value"] for t in all_trades)
        total_costs = commissions_total + fees_total
        pnl_net = pnl_gross - total_costs
        wins = [t for t in all_trades if t["value"] > 0]
        losses = [t for t in all_trades if t["value"] <= 0]
        win_rate = len(wins)/len(all_trades)*100 if all_trades else 0
        sym_pnl = defaultdict(float)
        for t in all_trades:
            sym_pnl[t["symbol"]] += t["value"]
        top = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = [
            f"Bitacora Tastytrade - {label}",
            f"Cuenta: {acct}",
            f"PnL neto: {fmt(pnl_net)}",
            f"PnL bruto: {fmt(pnl_gross)}",
            f"Comisiones: -${total_costs:.2f}",
            f"Trades: {len(all_trades)} en {len(daily)} dias",
            f"Win rate: {win_rate:.0f}% ({len(wins)}W/{len(losses)}L)",
        ]
        if top:
            for sym, pnl in top:
                lines.append(f"{sym}: {fmt(pnl)}")
        lines.append(f"Posiciones abiertas: {len(positions)}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"

@bot.message_handler(commands=["dia","hoy"])
def cmd_dia(msg):
    bot.reply_to(msg, "test dia funcionando")
@bot.message_handler(commands=["semana"])
def cmd_semana(msg):
    bot.reply_to(msg, get_period_metrics(7, "7 dias"))

@bot.message_handler(commands=["mes"])
def cmd_mes(msg):
    bot.reply_to(msg, get_period_metrics(30, "30 dias"))

@bot.message_handler(commands=["historico"])
def cmd_historico(msg):
    bot.reply_to(msg, get_period_metrics(90, "90 dias"))

if __name__ == "__main__":
    log.info("Bitacora Tastytrade iniciando...")
    try:
        bot.send_message(TELEGRAM_CHAT_ID,
            "Bitacora Tastytrade iniciada. El informe automatico llegara cada dia habil al cierre del mercado.",
            parse_mode="Markdown")
    except Exception as e:
        log.warning(f"No se pudo enviar mensaje de arranque: {e}")

    threading.Thread(target=run_scheduler, daemon=True).start()
    log.info("Escuchando comandos de Telegram...")
    bot.infinity_polling(skip_pending=True, allowed_updates=["message"])
