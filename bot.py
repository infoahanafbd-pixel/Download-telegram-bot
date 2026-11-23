import os
import re
import time
import uuid
import base64
import asyncio
import uvloop

uvloop.install()
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

import yt_dlp
from aiohttp import web
from pyrogram import Client, filters, idle

# ===== Render Environment Variables থেকে ডাটা নেবে =====
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "8080"))

DOWNLOAD_DIR = "/tmp/downloads"
COOKIES_PATH = "/tmp/cookies.txt"
MAX_CONCURRENT_DOWNLOADS = 5  # একসাথে সর্বোচ্চ ৫টি বড় ভিডিও ডাউনলোড হবে

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# YouTube ক্লাউড আইপি ব্লক এড়াতে অপশনাল কুকিজ সাপোর্ট
cookies_b64 = os.environ.get("COOKIES_B64", "").strip()
if cookies_b64:
    with open(COOKIES_PATH, "wb") as f:
        f.write(base64.b64decode(cookies_b64))

URL_PATTERN = re.compile(
    r"(https?://(?:www\.|m\.|web\.|mobile\.)?(?:youtube\.com|youtu\.be|facebook\.com|fb\.watch|fb\.com)/\S+)",
    re.IGNORECASE,
)

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

app = Client(
    "render_fast_dl_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=32,
    max_concurrent_transmissions=10,  # একসাথে একাধিক ইউজারকে ফাস্ট আপলোড
    in_memory=True,
)


def download_video_sync(url: str, uid: str) -> dict:
    out_template = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        # রি-এনকোডিং ছাড়া সরাসরি MP4 মার্জ (সুপারফাস্ট)
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 16,  # হাই-স্পিড মাল্টি-কানেকশন
        "http_chunk_size": 10485760,  # 10 MB চাঙ্ক (YouTube স্পিড লিমিট বাইপাস)
        "buffersize": 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "max_filesize": 2000 * 1024 * 1024,  # ২ GB লিমিট
    }

    if os.path.exists(COOKIES_PATH):
        ydl_opts["cookiefile"] = COOKIES_PATH

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        file_path = ydl.prepare_filename(info)
        base, _ = os.path.splitext(file_path)
        mp4_path = base + ".mp4"

        if os.path.exists(mp4_path):
            file_path = mp4_path
        elif not os.path.exists(file_path):
            for fname in os.listdir(DOWNLOAD_DIR):
                if fname.startswith(uid) and not fname.endswith(".part"):
                    file_path = os.path.join(DOWNLOAD_DIR, fname)
                    break

        return {
            "path": file_path,
            "title": (info.get("title") or "Video")[:100],
            "duration": int(info.get("duration") or 0),
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
        }


def cleanup_files(uid: str):
    for fname in os.listdir(DOWNLOAD_DIR):
        if fname.startswith(uid):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, fname))
            except OSError:
                pass


@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await message.reply_text(
        "স্বাগতম! যেকোনো **Facebook** বা **YouTube** ভিডিওর লিংক পাঠাও—সুপারফাস্ট ডাউনলোড করে দিচ্ছি।"
    )


@app.on_message(filters.text & ~filters.command(["start"]))
async def link_handler(client, message):
    match = URL_PATTERN.search(message.text or "")
    if not match:
        await message.reply_text("সঠিক একটি **Facebook** অথবা **YouTube** ভিডিওর লিংক দাও।")
        return

    url = match.group(1)
    uid = uuid.uuid4().hex[:10]
    status_msg = await message.reply_text("লিংক পেয়েছি, প্রসেস হচ্ছে...")

    try:
        if download_semaphore.locked():
            await status_msg.edit_text("একসাথে অনেক রিকোয়েস্ট চলছে, তোমারটা কিউতে আছে...")

        async with download_semaphore:
            await status_msg.edit_text("সুপারফাস্ট মোডে ডাউনলোড হচ্ছে...")
            result = await asyncio.to_thread(download_video_sync, url, uid)
            file_path = result["path"]

            if not os.path.exists(file_path):
                await status_msg.edit_text("ভিডিও ফাইলটি পাওয়া যায়নি।")
                return

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if file_size_mb > 2000:
                await status_msg.edit_text(
                    f"সাইজ {file_size_mb:.1f} MB। টেলিগ্রাম সর্বোচ্চ ২ GB সাপোর্ট করে।"
                )
                return

            await status_msg.edit_text(f"ডাউনলোড শেষ ({file_size_mb:.1f} MB)! আপলোড হচ্ছে...")

            last_edit = [0.0]

            async def upload_progress(current, total):
                now = time.time()
                if now - last_edit[0] >= 4:
                    last_edit[0] = now
                    percent = (current / total) * 100
                    try:
                        await status_msg.edit_text(
                            f"আপলোড হচ্ছে: {percent:.1f}% ({current / (1024*1024):.1f} / {total / (1024*1024):.1f} MB)"
                        )
                    except Exception:
                        pass

            await message.reply_video(
                video=file_path,
                caption=f"**{result['title']}**",
                duration=result["duration"],
                width=result["width"],
                height=result["height"],
                supports_streaming=True,
                progress=upload_progress,
            )
            await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"ডাউনলোড ব্যর্থ হয়েছে:\n`{str(e)[:300]}`")
    finally:
        cleanup_files(uid)


async def health_check(request):
    return web.Response(text="Bot is running fast!")


async def main():
    # Render Web Service ও Background Worker দুই জায়গাতেই চলার জন্য হেলথ সার্ভার
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await app.start()
    print("Render Superfast Bot চালু হয়েছে...")
    await idle()

    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    loop.run_until_complete(main())
