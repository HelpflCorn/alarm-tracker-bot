from datetime import datetime, time, timedelta
import os
import threading
import time as time_module
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import pytz
import requests
import schedule
import telebot
from telebot import types

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHANNEL_URL = "https://t.me/s/sirena_dp"
TIMEZONE = pytz.timezone("Europe/Kyiv")

WORK_START = time(10, 0)  # 10:00
WORK_END = time(19, 0)  # 19:00

bot = telebot.TeleBot(BOT_TOKEN)


def fetch_channel_events_for_range(start_date, end_date):
  """Парсить канал та класифікує події на red (червона), yellow (жовта) та end (відбій)."""
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  target_start_dt = TIMEZONE.localize(
      datetime.combine(start_date, time.min)
  ) - timedelta(days=1)
  events_dict = {}
  before_msg_id = None

  for _ in range(15):  # Глибина пагінації (до ~300 повідомлень)
    fetch_url = (
        f"{CHANNEL_URL}?before={before_msg_id}"
        if before_msg_id
        else CHANNEL_URL
    )

    try:
      response = requests.get(fetch_url, headers=headers, timeout=10)
      response.raise_for_status()
    except Exception as e:
      print(f"Parsing error: {e}")
      break

    soup = BeautifulSoup(response.text, "html.parser")
    messages = soup.find_all("div", class_="tgme_widget_message")

    if not messages:
      break

    min_dt_in_page = None

    for msg in messages:
      data_post = msg.get("data-post")
      if not data_post:
        continue

      time_tag = msg.find("time", class_="time")
      text_div = msg.find("div", class_="tgme_widget_message_text")

      if not time_tag or not text_div:
        continue

      dt_str = time_tag.get("datetime")
      if not dt_str:
        continue

      dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
      dt_kyiv = dt_utc.astimezone(TIMEZONE)

      if min_dt_in_page is None or dt_kyiv < min_dt_in_page:
        min_dt_in_page = dt_kyiv

      text = text_div.text.lower()
      is_end = "відбій" in text or "✅" in text

      if is_end:
        events_dict[data_post] = (dt_kyiv, "end")
      else:
        is_yellow = "жовтий" in text or "🟡" in text
        is_red = (
            "червоний" in text
            or "🔴" in text
            or ("оголошено" in text and not is_yellow)
        )

        if is_red:
          events_dict[data_post] = (dt_kyiv, "red")
        elif is_yellow:
          events_dict[data_post] = (dt_kyiv, "yellow")

    first_post = messages[0].get("data-post")
    if first_post and "/" in first_post:
      before_msg_id = first_post.split("/")[-1]
    else:
      break

    if min_dt_in_page and min_dt_in_page < target_start_dt:
      break

    time_module.sleep(0.3)

  sorted_events = sorted(events_dict.values(), key=lambda x: x[0])
  return sorted_events


def calculate_alarm_stats(start_date, end_date):
  events = fetch_channel_events_for_range(start_date, end_date)
  now = datetime.now(TIMEZONE)

  daily_seconds = {}
  daily_red_sec = {}
  daily_yellow_sec = {}
  daily_details = {}

  current_level = None
  alert_start = None

  def process_interval(a_start, a_end, level):
    curr_d = a_start.date()
    while curr_d <= a_end.date():
      if start_date <= curr_d <= end_date:
        w_start = TIMEZONE.localize(datetime.combine(curr_d, WORK_START))
        w_end = TIMEZONE.localize(datetime.combine(curr_d, WORK_END))

        eff_start = max(a_start, w_start)
        eff_end = min(a_end, w_end)
        if curr_d == now.date():
          eff_end = min(eff_end, now)

        if eff_start < eff_end:
          dur = (eff_end - eff_start).total_seconds()
          daily_seconds[curr_d] = daily_seconds.get(curr_d, 0) + dur

          if level == "red":
            daily_red_sec[curr_d] = daily_red_sec.get(curr_d, 0) + dur
          elif level == "yellow":
            daily_yellow_sec[curr_d] = daily_yellow_sec.get(curr_d, 0) + dur

          if curr_d not in daily_details:
            daily_details[curr_d] = []
          daily_details[curr_d].append(
              (eff_start, eff_end, eff_end - eff_start, level)
          )

      curr_d += timedelta(days=1)

  for dt, ev_type in events:
    if ev_type in ["red", "yellow"]:
      if current_level is not None:
        process_interval(alert_start, dt, current_level)
      current_level = ev_type
      alert_start = dt
    elif ev_type == "end":
      if current_level is not None:
        process_interval(alert_start, dt, current_level)
        current_level = None
        alert_start = None

  if current_level is not None:
    process_interval(alert_start, now, current_level)

  return (
      daily_seconds,
      daily_red_sec,
      daily_yellow_sec,
      daily_details,
      current_level,
      alert_start,
  )


def calculate_work_and_open_time(target_date, closed_seconds):
  now = datetime.now(TIMEZONE)
  work_start_dt = TIMEZONE.localize(datetime.combine(target_date, WORK_START))
  work_end_dt = TIMEZONE.localize(datetime.combine(target_date, WORK_END))

  if now < work_start_dt:
    elapsed_work_sec = 0
  else:
    eval_end = min(now, work_end_dt)
    elapsed_work_sec = max(0, (eval_end - work_start_dt).total_seconds())

  open_sec = max(0, elapsed_work_sec - closed_seconds)

  open_h, open_m = int(open_sec // 3600), int((open_sec % 3600) // 60)
  closed_h, closed_m = int(closed_seconds // 3600), int(
      (closed_seconds % 3600) // 60
  )

  return open_h, open_m, closed_h, closed_m


# ==================== ТЕЛЕГРАМ ІНТЕРФЕЙС ====================
def get_main_keyboard():
  markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
  btn_today = types.KeyboardButton("📊 За сьогодні")
  btn_week = types.KeyboardButton("📅 За 7 днів")
  btn_status = types.KeyboardButton("🔴/🟡 Статус зараз")
  markup.add(btn_today, btn_week)
  markup.add(btn_status)
  return markup


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
  msg = (
      "👋 <b>Привіт! Я бот-помічник для обліку тривог.</b>\n\n"
      "Я враховую 🔴 Червоний та 🟡 Жовтий рівні тривог і розраховую час простою"
      " в робочі години (10:00 — 19:00)."
  )
  bot.send_message(
      message.chat.id, msg, parse_mode="HTML", reply_markup=get_main_keyboard()
  )


@bot.message_handler(
    func=lambda msg: msg.text in ["📊 За сьогодні", "📊 За сегодня", "/today"]
)
def handle_today(message):
  today = datetime.now(TIMEZONE).date()
  (
      daily_sec,
      daily_red_sec,
      daily_yellow_sec,
      daily_det,
      _,
      _,
  ) = calculate_alarm_stats(today, today)

  total_closed_sec = daily_sec.get(today, 0)
  open_h, open_m, closed_h, closed_m = calculate_work_and_open_time(
      today, total_closed_sec
  )

  msg = (
      f"📊 <b>Звіт за сьогодні ({today.strftime('%d.%m.%Y')})</b>\n⏰ Робочі"
      f" години: 10:00 — 19:00\n\n"
  )
  msg += f"✅ <b>Магазин працював:</b> {open_h} год {open_m} хв\n"

  if total_closed_sec > 0:
    red_sec = daily_red_sec.get(today, 0)
    yellow_sec = daily_yellow_sec.get(today, 0)

    r_h, r_m = int(red_sec // 3600), int((red_sec % 3600) // 60)
    y_h, y_m = int(yellow_sec // 3600), int((yellow_sec % 3600) // 60)

    msg += f"🚨 <b>Загальний час простою:</b> {closed_h} год {closed_m} хв\n"
    msg += f"├ 🔴 <b>Червоний рівень:</b> {r_h} год {r_m} хв\n"
    msg += f"└ 🟡 <b>Жовтий рівень:</b> {y_h} год {y_m} хв\n\n"

    msg += "<b>Інтервали тривог:</b>\n"
    for start, end, dur, lvl in daily_det.get(today, []):
      h = int(dur.total_seconds() // 3600)
      m = int((dur.total_seconds() % 3600) // 60)
      dur_str = f"{h}год {m}хв" if h > 0 else f"{m} хв"
      icon = "🔴" if lvl == "red" else "🟡"
      msg += f"• {icon} {start.strftime('%H:%M')} — {end.strftime('%H:%M')} ({dur_str})\n"
  else:
    msg += "🎉 <b>У робочий час тривог поки не було!</b>"

  bot.send_message(message.chat.id, msg, parse_mode="HTML")


@bot.message_handler(
    func=lambda msg: msg.text in ["📅 За 7 днів", "📅 За 7 дней", "/week"]
)
def handle_week(message):
  today = datetime.now(TIMEZONE).date()
  start_week = today - timedelta(days=6)

  (
      daily_sec,
      daily_red_sec,
      daily_yellow_sec,
      _,
      _,
      _,
  ) = calculate_alarm_stats(start_week, today)

  total_week_sec = sum(daily_sec.values())
  w_hours, w_minutes = int(total_week_sec // 3600), int(
      (total_week_sec % 3600) // 60
  )

  msg = (
      f"📅 <b>Статистика за останні 7 днів</b>\n({start_week.strftime('%d.%m')} —"
      f" {today.strftime('%d.%m.%Y')})\n\n"
  )

  curr_d = start_week
  while curr_d <= today:
    sec = daily_sec.get(curr_d, 0)
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    day_str = curr_d.strftime("%d.%m (%a)")

    if sec > 0:
      r_s = daily_red_sec.get(curr_d, 0)
      y_s = daily_yellow_sec.get(curr_d, 0)
      r_str = f"{int(r_s//3600)}г {int((r_s%3600)//60)}хв"
      y_str = f"{int(y_s//3600)}г {int((y_s%3600)//60)}хв"

      msg += (
          f"• <b>{day_str}:</b> {h}г {m}хв (🔴 {r_str} | 🟡"
          f" {y_str})\n"
      )
    else:
      msg += f"• <b>{day_str}:</b> тривог не було ✨\n"
    curr_d += timedelta(days=1)

  msg += f"\n🚨 <b>УСЬОГО простою за 7 днів:</b> {w_hours} год {w_minutes} хв"
  bot.send_message(message.chat.id, msg, parse_mode="HTML")


@bot.message_handler(
    func=lambda msg: msg.text
    in [
        "🔴/🟡 Статус зараз",
        "🔴 Статус зараз",
        "🔴 Статус сейчас",
        "/status",
    ]
)
def handle_status(message):
  today = datetime.now(TIMEZONE).date()
  _, _, _, _, current_level, alert_start = calculate_alarm_stats(today, today)

  now = datetime.now(TIMEZONE)

  if current_level:
    dur = now - alert_start
    h, m = int(dur.total_seconds() // 3600), int((dur.total_seconds() % 3600) // 60)
    lvl_name = (
        "🔴 ЧЕРВОНИЙ РІВЕНЬ" if current_level == "red" else "🟡 ЖОВТИЙ РІВЕНЬ"
    )

    msg = (
        f"⚠️ <b>Зараз лунає тривога!</b> ({lvl_name})\n"
        f"Розпочалася о: {alert_start.strftime('%H:%M')}\n"
        f"Триває вже: {h}год {m}хв\n\n"
        f"🚨 Магазин зачинено."
    )
  else:
    msg = (
        "🟢 <b>Зараз тривоги немає!</b>\nМагазин працює в звичайному режимі."
    )

  bot.send_message(message.chat.id, msg, parse_mode="HTML")


def send_daily_report():
  today = datetime.now(TIMEZONE).date()
  (
      daily_sec,
      daily_red_sec,
      daily_yellow_sec,
      daily_det,
      _,
      _,
  ) = calculate_alarm_stats(today, today)

  total_closed_sec = daily_sec.get(today, 0)
  open_h, open_m, closed_h, closed_m = calculate_work_and_open_time(
      today, total_closed_sec
  )

  msg = (
      f"📊 <b>Щоденний звіт по тривогах</b>\n📅 Дата:"
      f" {today.strftime('%d.%m.%Y')}\n⏰ Робочий час: 10:00 — 19:00\n\n"
  )
  msg += f"✅ <b>Магазин працював:</b> {open_h} год {open_m} хв\n"

  if total_closed_sec > 0:
    red_sec = daily_red_sec.get(today, 0)
    yellow_sec = daily_yellow_sec.get(today, 0)
    r_h, r_m = int(red_sec // 3600), int((red_sec % 3600) // 60)
    y_h, y_m = int(yellow_sec // 3600), int((yellow_sec % 3600) // 60)

    msg += f"🚨 <b>Загальний час простою:</b> {closed_h} год {closed_m} хв\n"
    msg += f"├ 🔴 <b>Червоний рівень:</b> {r_h} год {r_m} хв\n"
    msg += f"└ 🟡 <b>Жовтий рівень:</b> {y_h} год {y_m} хв\n\n"

    msg += "<b>Деталізація:</b>\n"
    for start, end, dur, lvl in daily_det.get(today, []):
      h = int(dur.total_seconds() // 3600)
      m = int((dur.total_seconds() % 3600) // 60)
      dur_str = f"{h}год {m}хв" if h > 0 else f"{m} хв"
      icon = "🔴" if lvl == "red" else "🟡"
      msg += f"• {icon} {start.strftime('%H:%M')} — {end.strftime('%H:%M')} ({dur_str})\n"
  else:
    msg += (
        "🎉 <b>У робочий час тривог не було!</b> Магазин працював увесь день."
    )

  if CHAT_ID:
    try:
      bot.send_message(CHAT_ID, msg, parse_mode="HTML")
    except Exception as e:
      print(f"Помилка автоматичної відправки: {e}")


def run_scheduler():
  schedule.every().day.at("19:05").do(send_daily_report)
  while True:
    schedule.run_pending()
    time_module.sleep(30)


if __name__ == "__main__":
  threading.Thread(target=run_scheduler, daemon=True).start()

  print("Інтерактивний бот запущений...")
  bot.infinity_polling()