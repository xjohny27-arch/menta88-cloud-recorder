import base64
import datetime
import glob
import json
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
TARGET_HOURS = [8, 10, 12, 14, 16, 18, 20]
# Tolerant regex that catches Whisper variations such as "ταξιβεύει" or "υαιόρκη"
MAGIC_PATTERN = re.compile(
    r"λέξη.*?(?:ταξ\S+|όρκη|ορκη).*?είναι\s+(?:η\s+|το\s+|τα\s+)?([α-ωάέήίόύώϊϋΐΰa-zA-Z]+)",
    re.IGNORECASE | re.DOTALL,
)


def log(msg: str) -> None:
    now_str = datetime.datetime.now(ATHENS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] {msg}", flush=True)


def fmt(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def send_email_notification(word: str, snippet: str, window_label: str, spoken_at: datetime.datetime,
                            attachments: list[str] | None = None) -> None:
    """Sends an email alert via Gmail SMTP if GMAIL_APP_PASSWORD is set in GitHub Secrets."""
    email_user = os.environ.get("NOTIFY_EMAIL", "").strip() or "x.johny27@gmail.com"
    email_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    log(f"[*] Email setup: RESEND_API_KEY {'found' if resend_key else 'NOT found'}, "
        f"GMAIL_APP_PASSWORD {'found' if email_pass else 'NOT found'}, script version 4.")
    if not email_pass and not resend_key:
        log("[*] Neither RESEND_API_KEY nor GMAIL_APP_PASSWORD is set; skipping email notification.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"🎉 Menta 88 Magic Word ({window_label}): {word}"
    msg["From"] = email_user
    msg["To"] = email_user
    body_text = (
        f"Βρέθηκε η μαγική λέξη του Menta 88!\n\n"
        f"Ώρα παραθύρου: {window_label}\n"
        f"Ώρα ομιλίας (εκτίμηση): {fmt(spoken_at)}\n"
        f"Μαγική Λέξη: {word}\n"
        f"Ακριβές απόσπασμα: \"{snippet}\"\n\n"
        f"Καταχώρησέ τη στο: https://menta88.gr/\n"
    )
    msg.set_content(body_text)

    for path in attachments or []:
        try:
            with open(path, "rb") as fh:
                msg.add_attachment(fh.read(), maintype="audio", subtype="mpeg", filename=os.path.basename(path))
        except Exception as exc:
            log(f"[!] Could not attach {path}: {exc}")

    if resend_key:
        try:
            payload = {
                "from": "Menta 88 <onboarding@resend.dev>",
                "to": [email_user],
                "subject": msg["Subject"],
                "text": body_text,
                "attachments": [
                    {
                        "filename": os.path.basename(path),
                        "content": base64.b64encode(open(path, "rb").read()).decode(),
                    }
                    for path in (attachments or [])
                    if os.path.exists(path)
                ],
            }
            resp = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}"},
                json=payload,
                timeout=30,
            )
            if resp.status_code < 300:
                log(f"[+] Email notification sent to {email_user} via Resend!")
                return
            log(f"[!] Resend failed ({resp.status_code}): {resp.text[:300]}")
        except Exception as exc:
            log(f"[!] Resend request failed: {exc}")

    if not email_pass:
        log("[!] No Gmail password set to fall back on; email not sent.")
        return

    # Try SSL on port 465, then STARTTLS on port 587, two attempts each.
    for mode, port in [("ssl", 465), ("starttls", 587)]:
        for attempt in (1, 2):
            try:
                if mode == "ssl":
                    smtp_conn = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=20)
                else:
                    smtp_conn = smtplib.SMTP("smtp.gmail.com", port, timeout=20)
                with smtp_conn as smtp:
                    if mode == "starttls":
                        smtp.starttls()
                    smtp.login(email_user, email_pass)
                    smtp.send_message(msg)
                log(f"[+] Email notification sent to {email_user} (port {port}, {mode})!")
                return
            except Exception as exc:
                log(f"[!] Email attempt {attempt} via port {port} ({mode}) failed: {exc}")
    log("[!] All email attempts failed. Check GMAIL_APP_PASSWORD and the log lines above.")


def append_to_magic_words_file(word: str, snippet: str, chunk_name: str, spoken_at: datetime.datetime) -> None:
    logged_at = datetime.datetime.now(ATHENS_TZ)
    entry = (
        f"\n[{fmt(spoken_at)}] ΛΕΞΗ: {word}\n"
        f"  - Ώρα ομιλίας (εκτίμηση): {fmt(spoken_at)}\n"
        f"  - Καταγράφηκε: {fmt(logged_at)}\n"
        f"  - Αρχείο: {chunk_name}\n"
        f"  - Απόσπασμα: \"{snippet.strip()}\"\n"
    )
    with open(MAGIC_WORDS_FILE, "a", encoding="utf-8") as mf:
        mf.write(entry)


def find_magic(pieces: list[tuple[datetime.datetime, str]]):
    """
    pieces: (absolute start time of a Whisper segment, lowercased stripped text), in order.
    Returns (regex match, absolute time of the segment where the phrase starts), or (None, None).
    """
    full = " ".join(text for _, text in pieces)
    match = MAGIC_PATTERN.search(full)
    if not match:
        return None, None

    pos = match.start()
    cursor = 0
    for start_time, text in pieces:
        end = cursor + len(text)
        if pos <= end:
            return match, start_time
        cursor = end + 1  # account for the joining space
    return match, pieces[-1][0]


def nearby_recordings(audio_path: str | None) -> list[str]:
    """
    The recording where the word was heard plus the minute before and after.
    With no specific chunk (combined fallback), returns every recording of this run.
    """
    files = sorted(glob.glob(os.path.join(SAVE_DIR, "chunk_*.mp3")))
    if audio_path is None or audio_path not in files:
        return files
    i = files.index(audio_path)
    return files[max(0, i - 1): i + 2]


def init_drive():
    """Returns a Google Drive client if GDRIVE_SA_JSON is set, otherwise None."""
    raw = os.environ.get("GDRIVE_SA_JSON", "").strip()
    if not raw or not os.environ.get("GDRIVE_FOLDER_ID", "").strip():
        log("[*] GDRIVE_SA_JSON / GDRIVE_FOLDER_ID not set; skipping Drive backup.")
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"]
        )
        log("[+] Google Drive backup enabled.")
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        log(f"[!] Could not start Google Drive client: {exc}")
        return None


def upload_to_drive(service, path: str) -> None:
    """Uploads one recording to the Drive folder. Failures are logged, never fatal."""
    try:
        from googleapiclient.http import MediaFileUpload

        body = {
            "name": os.path.basename(path),
            "parents": [os.environ["GDRIVE_FOLDER_ID"].strip()],
        }
        media = MediaFileUpload(path, mimetype="audio/mpeg", resumable=False)
        result = service.files().create(
            body=body, media_body=media, fields="id,name", supportsAllDrives=True
        ).execute()
        log(f"[+] Uploaded to Drive: {result.get('name')}")
    except Exception as exc:
        log(f"[!] Drive upload failed for {os.path.basename(path)}: {exc}")


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


EARLY_TRIGGER_MINUTES = 109


def in_any_window(now: datetime.datetime) -> bool:
    """True if `now` is inside an early-trigger -> window-end span for any target hour."""
    for target_hour in TARGET_HOURS:
        window_start = now.replace(hour=target_hour - 1, minute=55, second=0, microsecond=0)
        window_end = now.replace(hour=target_hour, minute=5, second=15, microsecond=0)
        if window_start - datetime.timedelta(minutes=EARLY_TRIGGER_MINUTES) <= now <= window_end:
            return True
    return False


def wait_for_window_or_run_test() -> tuple[datetime.datetime, str]:
    """
    If triggered early by GitHub Actions cron (e.g. at :20 or :45), waits until :55 Athens time
    and returns the end timestamp (:05 past the hour).
    If triggered manually outside a window, runs a 2-minute test immediately.
    """
    now = datetime.datetime.now(ATHENS_TZ)
    force_test = os.environ.get("TEST_NOW", "false").lower() == "true"

    if force_test:
        end_time = now + datetime.timedelta(minutes=2)
        log("[*] TEST_NOW enabled: running 2-minute test recording immediately.")
        return end_time, now.strftime("%Y-%m-%d %H:%M (TEST)")

    for target_hour in TARGET_HOURS:
        window_start = now.replace(hour=target_hour - 1, minute=55, second=0, microsecond=0)
        window_end = now.replace(hour=target_hour, minute=5, second=15, microsecond=0)
        early_trigger_start = window_start - datetime.timedelta(minutes=EARLY_TRIGGER_MINUTES)

        if early_trigger_start <= now <= window_end:
            if now < window_start:
                wait_seconds = (window_start - now).total_seconds()
                log(f"[*] Waiting {int(wait_seconds)}s until {window_start.strftime('%H:%M:%S')} Athens time...")
                time.sleep(max(0, wait_seconds))
            return window_end, f"{now.strftime('%Y-%m-%d')} {target_hour:02d}:00"

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

    if os.environ.get("SEND_TEST_EMAIL", "false").lower() == "true":
        log("[*] SEND_TEST_EMAIL enabled: sending a test email now.")
        send_email_notification(
            "TEST", "Δοκιμαστικό μήνυμα από το Menta 88 recorder", "TEST",
            datetime.datetime.now(ATHENS_TZ),
        )

    os.makedirs(SAVE_DIR, exist_ok=True)
    if not os.path.exists(MAGIC_WORDS_FILE):
        with open(MAGIC_WORDS_FILE, "w", encoding="utf-8") as mf:
            mf.write("# Menta 88 - Magic Words Log\n")

    log("Loading faster-whisper 'small' model (CPU int8)...")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    log("Model ready.")
    drive = init_drive()

    end_time, window_label = wait_for_window_or_run_test()
    log(f"[+] Έναρξη εγγραφής για το παράθυρο {window_label} (λήξη: {end_time.strftime('%H:%M:%S')})...")

    audio_queue: queue.Queue = queue.Queue()
    all_pieces: list[tuple[datetime.datetime, str]] = []  # every Whisper segment, in order
    found_words: list[str] = []

    def handle_match(match, spoken_at: datetime.datetime, chunk_name: str, audio_path: str | None) -> None:
        word = match.group(1).upper()
        log("=" * 60)
        log(f"🎉 ΒΡΕΘΗΚΕ! Η λέξη είναι: {word} (στο {chunk_name}, ~{fmt(spoken_at)}) 🎉")
        log("=" * 60)
        if word not in found_words:
            found_words.append(word)
            append_to_magic_words_file(word, match.group(0), chunk_name, spoken_at)
            send_email_notification(word, match.group(0), window_label, spoken_at,
                                    attachments=nearby_recordings(audio_path))

    def worker() -> None:
        while True:
            item = audio_queue.get()
            if item is None:
                audio_queue.task_done()
                break
            audio_path, chunk_start = item
            try:
                chunk_name = os.path.basename(audio_path)
                segments, _ = model.transcribe(audio_path, language="el", beam_size=5)

                # Convert each segment's offset (seconds into the chunk) to absolute Athens time
                pieces = []
                for seg in segments:
                    text = seg.text.strip().lower()
                    if text:
                        pieces.append((chunk_start + datetime.timedelta(seconds=seg.start), text))

                log(f"  -> Whisper ({chunk_name}): {' '.join(t for _, t in pieces)}")
                all_pieces.extend(pieces)

                if drive is not None:
                    upload_to_drive(drive, audio_path)

                match, spoken_at = find_magic(pieces)
                if match:
                    handle_match(match, spoken_at, chunk_name, audio_path)
            except Exception as exc:
                log(f"[!] Transcription error ({audio_path}): {exc}")
            finally:
                audio_queue.task_done()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    while datetime.datetime.now(ATHENS_TZ) < end_time:
        chunk_start = datetime.datetime.now(ATHENS_TZ)
        now_str = chunk_start.strftime("%Y%m%d_%H%M%S")
        chunk_path = os.path.join(SAVE_DIR, f"chunk_{now_str}.mp3")
        if record_chunk(STREAM_URL, chunk_path, CHUNK_DURATION):
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1024:
                audio_queue.put((chunk_path, chunk_start))
        else:
            time.sleep(3)

    log("[*] Ολοκλήρωση παραθύρου. Αναμονή για τα τελευταία chunks...")
    audio_queue.put(None)
    t.join()

    # Fallback: the phrase may have been split across two chunks
    if not found_words:
        match, spoken_at = find_magic(all_pieces)
        if match:
            word = match.group(1).upper()
            log(f"🎉 ΒΡΕΘΗΚΕ (στο συνολικό κείμενο)! Η λέξη είναι: {word} (~{fmt(spoken_at)}) 🎉")
            append_to_magic_words_file(word, match.group(0), "combined_session", spoken_at)
            send_email_notification(word, match.group(0), window_label, spoken_at,
                                    attachments=nearby_recordings(None))
        else:
            log("[-] Δεν εντοπίστηκε η φράση σε αυτό το παράθυρο.")


if __name__ == "__main__":
    main()
