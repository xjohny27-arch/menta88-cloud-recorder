import base64
import datetime
import glob
import json
import os
import queue
import re
import smtplib
import subprocess
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

SHIFT_DURATION_MINUTES = 330  # 5.5 hours per shift job (GitHub max is 360m)
HANDOFF_BUFFER_MINUTES = 15   # hand off to next shift at least 15m before a window starts

# Common words and ad phrases that should never be identified as the magic word
STOPWORDS = {
    "ΚΑΘΕ", "ΜΕΡΑ", "ΣΤΙΣ", "ΣΤΟ", "ΚΑΙ", "ΓΙΑ", "ΠΟΥ", "ΣΕ", "ΝΑ", "ΜΕ", "ΑΠΟ",
    "ΤΗΣ", "ΤΟΥ", "ΤΩΝ", "ΤΟΥΣ", "ΤΙΣ", "ΜΙΑ", "ΕΝΑ", "ΕΝΑΝ", "ΕΙΝΑΙ", "ΗΤΑΝ",
    "ΟΧΙ", "ΝΑΙ", "ΕΔΩ", "ΕΚΕΙ", "ΤΩΡΑ", "ΜΕΤΑ", "ΠΡΙΝ", "ΜΕΣΑ", "ΕΞΩ", "ΖΩΝΤΑΝΟΣ",
    "ΖΩΝΤΑΝΟ", "ΖΩΝΤΑΝΗ", "ΟΛΑ", "ΟΛΟΙ", "ΟΛΕΣ", "ΠΩΣ", "ΠΟΙΟΣ", "ΠΟΙΑ", "ΠΟΙΟ",
    "MENTA", "MENTA88", "META", "META88", "GR", "WWW", "HTTP", "HTTPS", "RADIO",
    "ΧΡΙΣΤΟΥΓΕΝΝΑ", "ΔΙΑΓΩΝΙΣΜΟΣ", "ΔΙΑΓΩΝΙΣΜΟ", "ΔΙΑΓΩΝΙΣΜΟΥ", "ΤΑΞΙΔΙ"
}

# Matches "λέξη" followed within 0-10 words by "είναι" or ":" followed by the candidate word.
# Prevents matching across ad breaks or distant sentences.
MAGIC_PATTERN = re.compile(
    r"λέξη\s+(?:(?:\S+\s+){0,10}?)(?:είναι|:)\s+(?:η\s+|το\s+|τα\s+|ο\s+)?([α-ωάέήίόύώϊϋΐΰa-zA-Z]+)",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    now_str = datetime.datetime.now(ATHENS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] {msg}", flush=True)


def fmt(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def send_email_notification(word: str, snippet: str, window_label: str, spoken_at: datetime.datetime,
                            attachments: list[str] | None = None) -> None:
    """Sends an email alert via Resend or Gmail SMTP."""
    email_user = os.environ.get("NOTIFY_EMAIL", "").strip() or "x.johny27@gmail.com"
    email_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    log(f"[*] Email setup: RESEND_API_KEY {'found' if resend_key else 'NOT found'}, "
        f"GMAIL_APP_PASSWORD {'found' if email_pass else 'NOT found'}, script version 5.1.")
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


def commit_and_push_magic_words() -> None:
    """Commits and pushes magic_words.txt immediately after a window finds a word."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "magic_words.txt"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if not status:
            return
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "magic_words.txt"], check=False)
        subprocess.run(["git", "commit", "-m", "Update magic words"], check=False)
        subprocess.run(["git", "pull", "--rebase"], check=False)
        subprocess.run(["git", "push"], check=False)
        log("[+] Pushed updated magic_words.txt to GitHub.")
    except Exception as exc:
        log(f"[!] Could not push magic_words.txt: {exc}")


def find_magic(pieces: list[tuple[datetime.datetime, str]]):
    """
    pieces: (absolute start time of a Whisper segment, lowercased stripped text), in order.
    Returns (word, snippet, spoken_at) or (None, None, None).
    """
    full = " ".join(text for _, text in pieces)
    for match in MAGIC_PATTERN.finditer(full):
        word = match.group(1).upper()
        if len(word) >= 3 and word not in STOPWORDS:
            pos = match.start()
            cursor = 0
            spoken_at = pieces[-1][0]
            for start_time, text in pieces:
                end = cursor + len(text)
                if pos <= end:
                    spoken_at = start_time
                    break
                cursor = end + 1
            return word, match.group(0), spoken_at
    return None, None, None


def nearby_recordings(audio_path: str | None) -> list[str]:
    """
    The recording where the word was heard plus the minute before and after.
    With no specific chunk (combined fallback), returns every recording of this window.
    """
    files = sorted(glob.glob(os.path.join(SAVE_DIR, "chunk_*.mp3")))
    if audio_path is None or audio_path not in files:
        return files
    i = files.index(audio_path)
    return files[max(0, i - 1): i + 2]


def clear_old_recordings() -> None:
    for f in glob.glob(os.path.join(SAVE_DIR, "chunk_*.mp3")):
        try:
            os.remove(f)
        except OSError:
            pass


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


def get_next_window(now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime, str]:
    """
    Returns (window_start, window_end, window_label) for the current or next upcoming
    recording window in Athens time.
    """
    for day_offset in (0, 1, 2):
        day = (now + datetime.timedelta(days=day_offset)).date()
        for target_hour in TARGET_HOURS:
            w_start = ATHENS_TZ.localize(
                datetime.datetime(day.year, day.month, day.day, target_hour - 1, 55, 0)
            )
            w_end = ATHENS_TZ.localize(
                datetime.datetime(day.year, day.month, day.day, target_hour, 5, 15)
            )
            if now <= w_end:
                label = f"{day.strftime('%Y-%m-%d')} {target_hour:02d}:00"
                return w_start, w_end, label
    raise RuntimeError("No upcoming window found")


def sleep_until(target: datetime.datetime, description: str) -> None:
    """Sleeps until `target` Athens time, logging a heartbeat every 10 minutes."""
    while True:
        now = datetime.datetime.now(ATHENS_TZ)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        if remaining > 600:
            mins = int(remaining // 60)
            log(f"[*] Αναμονή για {description} ({mins} λεπτά ακόμα, μέχρι {target.strftime('%H:%M:%S')})...")
            time.sleep(min(600.0, remaining))
        else:
            log(f"[*] Αναμονή {int(remaining)}s μέχρι {target.strftime('%H:%M:%S')} ώρα Ελλάδας ({description})...")
            time.sleep(remaining)
            break


def record_single_window(model: WhisperModel, drive, end_time: datetime.datetime, window_label: str) -> None:
    clear_old_recordings()
    log(f"[+] Έναρξη εγγραφής για το παράθυρο {window_label} (λήξη: {end_time.strftime('%H:%M:%S')})...")

    audio_queue: queue.Queue = queue.Queue()
    all_pieces: list[tuple[datetime.datetime, str]] = []
    found_words: list[str] = []

    def handle_match(word: str, snippet: str, spoken_at: datetime.datetime, chunk_name: str, audio_path: str | None) -> None:
        log("=" * 60)
        log(f"🎉 ΒΡΕΘΗΚΕ! Η λέξη είναι: {word} (στο {chunk_name}, ~{fmt(spoken_at)}) 🎉")
        log("=" * 60)
        if word not in found_words:
            found_words.append(word)
            append_to_magic_words_file(word, snippet, chunk_name, spoken_at)
            send_email_notification(word, snippet, window_label, spoken_at,
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

                pieces = []
                for seg in segments:
                    text = seg.text.strip().lower()
                    if text:
                        pieces.append((chunk_start + datetime.timedelta(seconds=seg.start), text))

                log(f"  -> Whisper ({chunk_name}): {' '.join(t for _, t in pieces)}")
                all_pieces.extend(pieces)

                if drive is not None:
                    upload_to_drive(drive, audio_path)

                word, snippet, spoken_at = find_magic(pieces)
                if word:
                    handle_match(word, snippet, spoken_at, chunk_name, audio_path)
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

    log(f"[*] Ολοκλήρωση παραθύρου {window_label}. Αναμονή για τα τελευταία chunks...")
    audio_queue.put(None)
    t.join()

    # Fallback: the phrase may have been split across two chunks
    if not found_words:
        word, snippet, spoken_at = find_magic(all_pieces)
        if word:
            log(f"🎉 ΒΡΕΘΗΚΕ (στο συνολικό κείμενο)! Η λέξη είναι: {word} (~{fmt(spoken_at)}) 🎉")
            append_to_magic_words_file(word, snippet, "combined_session", spoken_at)
            send_email_notification(word, snippet, window_label, spoken_at,
                                    attachments=nearby_recordings(None))
        else:
            log(f"[-] Δεν εντοπίστηκε η φράση στο παράθυρο {window_label}.")


def main() -> None:
    shift_num = os.environ.get("SHIFT_NUM", "1").strip()
    force_test = os.environ.get("TEST_NOW", "false").lower() == "true"
    send_test = os.environ.get("SEND_TEST_EMAIL", "false").lower() == "true"

    # If the user explicitly asked for a quick 2-minute test or test email, only run it in Shift 1
    if (force_test or send_test) and shift_num != "1":
        log(f"[*] Shift #{shift_num} skipped because TEST_NOW / SEND_TEST_EMAIL was only for Shift #1.")
        return

    if send_test:
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

    if force_test:
        now = datetime.datetime.now(ATHENS_TZ)
        end_time = now + datetime.timedelta(minutes=2)
        log("[*] TEST_NOW enabled: running 2-minute test recording immediately.")
        record_single_window(model, drive, end_time, now.strftime("%Y-%m-%d %H:%M (TEST)"))
        commit_and_push_magic_words()
        return

    shift_start = datetime.datetime.now(ATHENS_TZ)
    shift_deadline = shift_start + datetime.timedelta(minutes=SHIFT_DURATION_MINUTES)
    log(f"[*] Βάρδια #{shift_num} ξεκίνησε στις {fmt(shift_start)} (διάρκεια έως {fmt(shift_deadline)})")

    while True:
        now = datetime.datetime.now(ATHENS_TZ)
        w_start, w_end, window_label = get_next_window(now)

        # If the next window finishes after our 5.5h shift deadline and hasn't started yet:
        if w_end > shift_deadline and now < w_start:
            handoff_time = min(shift_deadline, w_start - datetime.timedelta(minutes=HANDOFF_BUFFER_MINUTES))
            if handoff_time > now:
                log(f"[*] Το επόμενο παράθυρο ({window_label}) ολοκληρώνεται μετά το τέλος της Βάρδιας #{shift_num}.")
                sleep_until(handoff_time, f"παράδοση σκυτάλης στη Βάρδια #{int(shift_num) + 1}")
            log(f"[*] Ολοκλήρωση Βάρδιας #{shift_num}. Παράδοση στην επόμενη βάρδια...")
            break

        if now < w_start:
            log(f"[*] Επόμενο προγραμματισμένο παράθυρο: {window_label} (έναρξη {w_start.strftime('%H:%M:%S')})")
            sleep_until(w_start, f"έναρξη παραθύρου {window_label}")

        record_single_window(model, drive, w_end, window_label)
        commit_and_push_magic_words()


if __name__ == "__main__":
    main()
