"""
Bitacora Tastytrade - Bot de Telegram
Usa python-telegram-bot v20 (asyncio)
"""

import os
import logging
from datetime import datetime, date, timedelta
from collections import defaultdict
import pytz
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TASTYTRADE_CLIENT_SECRET = os.environ["TASTYTRADE_CLIENT_SECRET"]
TASTYTRADE_REFRESH_TOKEN = os.environ["TASTYTRADE_REFRESH_TOKEN"]
TELEGRAM_BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
ACCOUNT_NUMBER           = os.environ.get("TASTYTRADE_ACCOUNT", "")
REPORT_HOUR_ET           = int(os.environ.get("REPORT_HOUR_ET", "16"))
REPORT_MINUTE_ET         = int(os.environ.get("REPORT_MINUTE_ET", "15"))
SANDBOX                  = os.environ.get("TASTYTRADE_SANDBOX", "false").lower() == "true"
BASE_URL = "https://api.cert.tastytrade.com" if SANDBOX else "https://api.tastytrade.com"

def get_access_token():
    resp = requests.post(
        f"{BASE_URL}/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": TASTYTRADE_REFRESH_TOKEN, "client_secret": TASTYTRADE_CLIENT_SECRET},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def get_accounts(token):
    resp = requests.get(f"{BASE_URL}/customers/me/accounts", headers=auth_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]["items"]

def get_transactions(token, account, start_date):
    resp = requests.get(f"{BASE_URL}/accounts/{account}/transactions", headers=auth_headers(token), params={"start-date": start_date, "per-page": 250}, timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]["items"]

def get_positions(token, account):
    resp = requests.get(f"{BASE_URL}/accounts/{account}/positions", headers=auth_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]["items"]

def fmt(v):
    return f"{'+'if v>=0 else ''}${v:,.2f}"

def get_account():
    token = get_access_token()
    accounts = get_accounts(token)
    return ACCOUNT_NUMBER or accounts[0]["account"]["account-number"], token

def build_period_report(days_back, label):
    try:
        acct, token = get_account()
        et = pytz.timezone("US/Eastern")
        today = datetime.now(et).date()
        start = today - timedelta(days=days_back)
        txns = get_transactions(token, acct, start.isoformat())
        positions = get_positions(token, acct)
        daily = defaultdict(list)
        commissions_total = 0.0
        for t in txns:
            d = t.get("executed-at", "")[:10]
            t_type = t.get("transaction-type", "")
            value = float(t.get("net-value", 0))
            if t_type == "Trade":
                daily[d].append({"symbol": t.get("underlying-symbol","?"), "value": value})
            elif t_type in ("Commission", "Fee"):
                commissions_total += abs(value)
        all_trades = [t for trades in daily.values() for t in trades]
        pnl_gross = sum(t["value"] for t in all_trades)
        pnl_net = pnl_gross - commissions_total
        wins = [t for t in all_trades if t["value"] > 0]
        losses = [t for t in all_trades if t["value"] <= 0]
        win_rate = len(wins)/len(all_trades)*100 if all_trades else 0
        sym_pnl = defaultdict(float)
        for t in all_trades:
            sym_pnl[t["symbol"]] += t["value"]
        top = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = [
            f"Bitacora Tastytrade - {label}",
            f"Periodo: {start.strftime('%d %b')} al {today.strftime('%d %b %Y')}",
            f"Cuenta: {acct}",
            "---",
            f"PnL neto: {fmt(pnl_net)}",
            f"PnL bruto: {fmt(pnl_gross)}",
            f"Comisiones: -${commissions_total:.2f}",
            "",
            f"Trades: {len(all_trades)} en {len(daily)} dias",
            f"Win rate: {win_rate:.0f}% ({len(wins)}W/{len(losses)}L)",
        ]
        if wins:
            lines.append(f"Avg ganador: {fmt(sum(t['value'] for t in wins)/len(wins))}")
        if losses:
            lines.append(f"Avg perdedor: {fmt(sum(t['value'] for t in losses)/len(losses))}")
        if top:
            lines.append("\nTop simbolos:")
            for sym, pnl in top:
                lines.append(f"  {sym}: {fmt(pnl)}")
        lines.append(f"\nPosiciones abiertas: {len(positions)}")
        lines.append(f"Generado: {datetime.now(et).strftime('%I:%M %p ET')}")
        return "\n".join(lines)
    except Exception as e:
        log.error(f"Error {label}: {e}")
        return f"Error generando informe {label}: {str(e)}"

def build_daily_report():
    try:
        acct, token = get_account()
        et = pytz.timezone("US/Eastern")
        today = date.today()
        txns = get_transactions(token, acct, today.isoformat())
        positions = get_positions(token, acct)
        trades, commissions = [], 0.0
        for t in txns:
            t_type = t.get("transaction-type", "")
            value = float(t.get("net-value", 0))
            if t_type == "Trade":
                trades.append({"symbol": t.get("underlying-symbol","?"), "value": value})
            elif t_type in ("Commission", "Fee"):
                commissions += abs(value)
        pnl_gross = sum(t["value"] for t in trades)
        pnl_net = pnl_gross - commissions
        wins = [t for t in trades if t["value"] > 0]
        losses = [t for t in trades if t["value"] <= 0]
        dow = ["Lunes","Martes","Miercoles","Jueves","Viernes","Sabado","Domingo"][today.weekday()]
        lines = [
            f"Bitacora Tastytrade - {dow} {today.strftime('%d %b %Y')}",
            f"Cuenta: {acct}",
            "---",
            f"PnL neto: {fmt(pnl_net)}",
            f"PnL bruto: {fmt(pnl_gross)}",
            f"Comisiones: -${commissions:.2f}",
            "",
        ]
        if trades:
            lines.append(f"Trades cerrados: {len(trades)}")
            for t in trades[:8]:
                e = "+" if t["value"] > 0 else "-"
                lines.append(f"  {e} {t['symbol']}: {fmt(t['value'])}")
            lines.append(f"\nWin rate: {len(wins)/len(trades)*100:.0f}% ({len(wins)}W/{len(losses)}L)")
        else:
            lines.append("Sin trades cerrados hoy")
        if positions:
            lines.append(f"\nPosiciones abiertas: {len(positions)}")
            for p in positions[:6]:
                lines.append(f"  {p.get('underlying-symbol','?')} x{p.get('quantity',0)} {p.get('instrument-type','')}")
        lines.append(f"\nGenerado: {datetime.now(et).strftime('%I:%M %p ET')}")
        return "\n".join(lines)
    except Exception as e:
        log.error(f"Error informe diario: {e}")
        return f"Error: {str(e)}"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bitacora Tastytrade activa\n\n"
        "/informe - Informe del dia\n"
        "/dia - Resumen de hoy\n"
        "/semana - Ultimos 7 dias\n"
        "/mes - Ultimos 30 dias\n"
        "/historico - Ultimos 90 dias\n"
        "/posiciones - Posiciones abiertas\n"
        "/status - Estado del bot"
    )

async def cmd_informe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generando informe...")
    await update.message.reply_text(build_daily_report())

async def cmd_dia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generando informe del dia...")
    await update.message.reply_text(build_period_report(1, "Hoy"))

async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generando informe semanal...")
    await update.message.reply_text(build_period_report(7, "7 dias"))

async def cmd_mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generando informe mensual...")
    await update.message.reply_text(build_period_report(30, "30 dias"))

async def cmd_historico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generando historico 90 dias...")
    await update.message.reply_text(build_period_report(90, "90 dias"))

async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        acct, token = get_account()
        positions = get_positions(token, acct)
        if not positions:
            await update.message.reply_text("Sin posiciones abiertas.")
            return
        lines = [f"Posiciones abiertas: {len(positions)}\n"]
        for p in positions:
            lines.append(f"  {p.get('underlying-symbol','?')} x{p.get('quantity',0)} {p.get('instrument-type','')}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    et = pytz.timezone("US/Eastern")
    now = datetime.now(et)
    await update.message.reply_text(
        f"Bitacora Tastytrade activa\n"
        f"Hora ET: {now.strftime('%I:%M %p')}\n"
        f"Fecha: {now.strftime('%d %b %Y')}\n"
        f"Proximo informe: {REPORT_HOUR_ET:02d}:{REPORT_MINUTE_ET:02d} ET"
    )

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    et = pytz.timezone("US/Eastern")
    if datetime.now(et).weekday() < 5:
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=build_daily_report())

def main():
    log.info("Bitacora Tastytrade iniciando...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("hola", cmd_start))
    app.add_handler(CommandHandler("informe", cmd_informe))
    app.add_handler(CommandHandler("dia", cmd_dia))
    app.add_handler(CommandHandler("hoy", cmd_dia))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("mes", cmd_mes))
    app.add_handler(CommandHandler("historico", cmd_historico))
    app.add_handler(CommandHandler("posiciones", cmd_posiciones))
    app.add_handler(CommandHandler("status", cmd_status))

    et = pytz.timezone("US/Eastern")
    app.job_queue.run_daily(
        send_daily_report,
        time=datetime.now(et).replace(hour=REPORT_HOUR_ET, minute=REPORT_MINUTE_ET, second=0).timetz()
    )

    log.info("Bot escuchando comandos...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
