import datetime
import os
import queue
import re
import smtplib
import threading
import time
from email.message import EmailMessage
import pytz
import requests
from faster_whisper import WhisperModel

# --- CONFIGURATION ---
STREAM_URL = "https://netradio.live24.gr/menta88ath?1793984763"
SAVE_DIR = os.path.abspath("./recordings")
MAGIC_WORDS_FILE = os.path.abspath("./magic_words.txt")
CHUNK_DURATION = 60

ATHENS_TZ = pytz.timezone("Europe/Athens")
TARGET_HOURS = [8, 10, 12, 14, 18, 20]

# Tolerant regex that catches Whisper variations such as "ταξιβεύει" or "υαιόρκη"
MAGIC_PATTERN = re.compile(
    r"λέξη.*?(?:ταξ\S+|όρκη|ορκη).*?είναι\s+(?:η\s+|το\s+|τα\s+)?([α-ωάέήίόύώϊϋΐΰa-zA-Z]+)",
    re.IGNORECASE | re.DOTALL,
)


def log(msg: str) -> None:
    now_str = datetime.datetime.now(ATHENS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] {msg}", flush=True)


def send_email_notification(word: str, snippet: str, window_label: str) -> None:
    """Sends an email alert via Gmail SMTP if GMAIL_APP_PASSWORD is set in GitHub Secrets."""
    email_user = os.environ.get("NOTIFY_EMAIL", "x.johny27@gmail.com").strip()
    email_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if not email_pass:
        log("[*] GMAIL_APP_PASSWORD secret not set; skipping email notification.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"🎉 Menta 88 Magic Word ({window_label}): {word}"
    msg["From"] = email_user
    msg["To"] = email_user
    msg.set_content(
        f"Βρέθηκε η μαγική λέξη του Menta 88!\n\n"
        f"Ώρα παραθύρου: {window_label}\n"
        f"Μαγική Λέξη: {word}\n"
        f"Ακριβές απόσπασμα: \"{snippet}\"\n\n"
        f"Καταχώρησέ τη στο: https://menta88.gr/\n"
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(email_user, email_pass)
            smtp.send_message(msg)
        log(f"[+] Email notification sent to {email_user}!")
    except Exception as exc:
        log(f"[!] Failed to send email notification: {exc}")


def append_to_magic_words_file(word: str, snippet: str, chunk_name: str) -> None:
    now_str = datetime.datetime.now(ATHENS_TZ).strftime("%Y-%m-%d %H:%M")
    entry = (
        f"\n[{now_str}] ΛΕΞΗ: {word}\n"
        f"  - Αρχείο: {chunk_name}\n"
        f"  - Απόσπασμα: \"{snippet.strip()}\"\n"
    )
    with open(MAGIC_WORDS_FILE, "a", encoding="utf-8") as mf:
        mf.write(entry)


def record_chunk(url: str, output_file: str, duration: int = 60) -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://menta88.gr/",
    }
    try:
        with requests.get(url, headers=headers, stream=True, timeout=15) as r:
            r.raise_for_status()
            start_time = time.time()
            with open(output_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                    if time.time() - start_time >= duration:
                        break
        return True
    except Exception as exc:
        log(f"[!] Σφάλμα εγγραφής: {exc}")
        return False


def in_any_window(now: datetime.datetime) -> bool:
    """True if `now` is inside an early-trigger -> window-end span for any target hour."""
    for target_hour in TARGET_HOURS:
        window_start = now.replace(hour=target_hour - 1, minute=55, second=0, microsecond=0)
        window_end = now.replace(hour=target_hour, minute=5, second=15, microsecond=0)
        if window_start - datetime.timedelta(minutes=30) <= now <= window_end:
            return True
    return False


def wait_for_window_or_run_test() -> tuple[datetime.datetime, str]:
    """
    If triggered early by GitHub Actions cron (e.g. at :48), waits until :55 Athens time
    and returns the end timestamp (:05 past the hour).
    If triggered manually outside a window, runs a 2-minute test immediately.
    """
    now = datetime.datetime.now(ATHENS_TZ)
    force_test = os.environ.get("TEST_NOW", "false").lower() == "true"

    if force_test:
        end_time = now + datetime.timedelta(minutes=2)
        log("[*] TEST_NOW enabled: running 2-minute test recording immediately.")
        return end_time, now.strftime("%Y-%m-%d %H:%M (TEST)")

    # Check if we are approaching or inside one of the :55 -> :05 windows
    for target_hour in TARGET_HOURS:
        window_start = now.replace(
            hour=target_hour - 1, minute=55, second=0, microsecond=0
        )
        window_end = now.replace(
            hour=target_hour, minute=5, second=15, microsecond=0
        )
        early_trigger_start = window_start - datetime.timedelta(minutes=30)

        if early_trigger_start <= now <= window_end:
            if now < window_start:
                wait_seconds = (window_start - now).total_seconds()
                log(f"[*] Waiting {int(wait_seconds)}s until {window_start.strftime('%H:%M:%S')} Athens time...")
                time.sleep(max(0, wait_seconds))
            return window_end, f"{now.strftime('%Y-%m-%d')} {target_hour:02d}:00"

    # Fallback if launched manually outside scheduled hours: run a 2-minute test
    end_time = now + datetime.timedelta(minutes=2)
    log("[*] Launched outside scheduled window; running 2-minute test recording.")
    return end_time, now.strftime("%Y-%m-%d %H:%M (MANUAL)")


def main() -> None:
    if (
        os.environ.get("GITHUB_EVENT_NAME") == "schedule"
        and not in_any_window(datetime.datetime.now(ATHENS_TZ))
    ):
        log("[*] Scheduled run outside any recording window (DST duplicate cron); exiting.")
        return

    os.makedirs(SAVE_DIR, exist_ok=True)
    if not os.path.exists(MAGIC_WORDS_FILE):
        with open(MAGIC_WORDS_FILE, "w", encoding="utf-8") as mf:
            mf.write("# Menta 88 - Magic Words Log\n")

    log("Loading faster-whisper 'small' model (CPU int8)...")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    log("Model ready.")

    end_time, window_label = wait_for_window_or_run_test()
    log(f"[+] Έναρξη εγγραφής για το παράθυρο {window_label} (λήξη: {end_time.strftime('%H:%M:%S')})...")

    audio_queue: queue.Queue[str | None] = queue.Queue()
    transcripts: list[str] = []
    found_words: list[str] = []

    def worker() -> None:
        while True:
            audio_path = audio_queue.get()
            if audio_path is None:
                audio_queue.task_done()
                break
            try:
                chunk_name = os.path.basename(audio_path)
                segments, _ = model.transcribe(audio_path, language="el", beam_size=5)
                text = " ".join(seg.text for seg in segments).strip().lower()
                log(f"  -> Whisper ({chunk_name}): {text}")
                transcripts.append(text)

                match = MAGIC_PATTERN.search(text)
                if match:
                    word = match.group(1).upper()
                    log("=" * 60)
                    log(f"🎉 ΒΡΕΘΗΚΕ! Η λέξη είναι: {word} (στο {chunk_name}) 🎉")
                    log("=" * 60)
                    if word not in found_words:
                        found_words.append(word)
                        append_to_magic_words_file(word, match.group(0), chunk_name)
                        send_email_notification(word, match.group(0), window_label)
            except Exception as exc:
                log(f"[!] Transcription error ({audio_path}): {exc}")
            finally:
                audio_queue.task_done()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    while datetime.datetime.now(ATHENS_TZ) < end_time:
        now_str = datetime.datetime.now(ATHENS_TZ).strftime("%Y%m%d_%H%M%S")
        chunk_path = os.path.join(SAVE_DIR, f"chunk_{now_str}.mp3")
        if record_chunk(STREAM_URL, chunk_path, CHUNK_DURATION):
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1024:
                audio_queue.put(chunk_path)
        else:
            time.sleep(3)

    log("[*] Ολοκλήρωση παραθύρου. Αναμονή για τα τελευταία chunks...")
    audio_queue.put(None)
    t.join()

    # Fallback check across combined session text in case the phrase crossed a chunk boundary
    if not found_words:
        full_text = " ".join(transcripts)
        match = MAGIC_PATTERN.search(full_text)
        if match:
            word = match.group(1).upper()
            log(f"🎉 ΒΡΕΘΗΚΕ (στο συνολικό κείμενο)! Η λέξη είναι: {word} 🎉")
            append_to_magic_words_file(word, match.group(0), "combined_session")
            send_email_notification(word, match.group(0), window_label)
        else:
            log("[-] Δεν εντοπίστηκε η φράση σε αυτό το παράθυρο.")


if __name__ == "__main__":
    main()
