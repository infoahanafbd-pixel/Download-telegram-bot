import os
import re
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot.types import BotCommand
import yt_dlp

BOT_TOKEN = os.environ.get("BOT_API") or os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_API environment variable is missing.")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
URL_REGEX = re.compile(r"https?://\S+")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
YOUTUBE_CLIENTS = ["tv", "android_vr", "ios", "mweb"]


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def get_cookie_file(tmp_dir):
    cookies_text = os.environ.get("YT_COOKIES")
    if not cookies_text:
        return None
    cookie_path = os.path.join(tmp_dir, "cookies.txt")
    with open(cookie_path, "w", encoding="utf-8") as f:
        f.write(cookies_text)
    return cookie_path


def download_video(url, tmp_dir):
    is_youtube = "youtube.com" in url or "youtu.be" in url
    cookie_file = get_cookie_file(tmp_dir)
    clients_to_try = YOUTUBE_CLIENTS if is_youtube else [None]
    last_error = None

    for client in clients_to_try:
        out_dir = tempfile.mkdtemp(dir=tmp_dir)
        ydl_opts = {
            "outtmpl": os.path.join(out_dir, "video.%(ext)s"),
            "format": (
                "best[ext=mp4][filesize<48M]/"
                "best[filesize<48M]/"
                "bestvideo[height<=480]+bestaudio/"
                "bestvideo[height<=360]+bestaudio/"
                "bestvideo+bestaudio/best"
            ),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
        }

        if cookie_file:
            ydl_opts["cookiefile"] = cookie_file

        if client:
            ydl_opts["extractor_args"] = {
                "youtube": {
                    "player_client": [client],
                }
            }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "Video")
                file_path = ydl.prepare_filename(info)

            if not os.path.exists(file_path):
                files = [
                    os.path.join(out_dir, f)
                    for f in os.listdir(out_dir)
                    if not f.endswith(".part")
                ]
                if not files:
                    raise FileNotFoundError("ডাউনলোড করা ফাইল পাওয়া যায়নি।")
                file_path = max(files, key=os.path.getsize)

            return file_path, title
        except Exception as e:
            print(f"Client {client} failed: {e}", flush=True)
            last_error = e
            shutil.rmtree(out_dir, ignore_errors=True)
            continue

    raise last_error or RuntimeError("ভিডিও ডাউনলোড ব্যর্থ হয়েছে।")


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(
        message,
        "স্বাগতম! YouTube বা Facebook ভিডিওর লিংক পাঠান, আমি ডাউনলোড করে দিচ্ছি।",
    )


@bot.message_handler(func=lambda m: bool(m.text))
def handle_link(message):
    match = URL_REGEX.search(message.text)
    if not match:
        bot.reply_to(message, "একটি সঠিক ভিডিও লিংক পাঠান।")
        return

    url = match.group(0)
    status_msg = bot.reply_to(message, "ভিডিও ডাউনলোড হচ্ছে, একটু অপেক্ষা করুন...")
    tmp_dir = tempfile.mkdtemp(prefix="ytdl_")

    try:
        file_path, title = download_video(url, tmp_dir)

        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            bot.edit_message_text(
                f"ভিডিওটির সাইজ {size_mb:.1f} MB। শুধু Bot Token দিয়ে ৫০ MB-এর বড় ফাইল পাঠানো যায় না।",
                message.chat.id,
                status_msg.message_id,
            )
            return

        bot.edit_message_text(
            "ডাউনলোড শেষ! টেলিগ্রামে আপলোড হচ্ছে...",
            message.chat.id,
            status_msg.message_id,
        )

        with open(file_path, "rb") as video_file:
            bot.send_video(
                message.chat.id,
                video_file,
                caption=title[:1000],
                supports_streaming=True,
                timeout=120,
            )

        bot.delete_message(message.chat.id, status_msg.message_id)

    except Exception as e:
        bot.edit_message_text(
            f"ডাউনলোড করা যায়নি: {str(e)[:200]}",
            message.chat.id,
            status_msg.message_id,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    threading.Thread(target=start_web_server, daemon=True).start()
    time.sleep(5)
    bot.remove_webhook()
    bot.set_my_commands([BotCommand("start", "বট চালু করুন")])
    print("Bot is running...", flush=True)
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=30)
