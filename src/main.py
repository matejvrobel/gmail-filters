import logging
import os
import time
from pathlib import Path

import yaml
from imapclient import IMAPClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail-filters")

PLACEHOLDER_EMAILS = {"your_email@gmail.com"}
PLACEHOLDER_PASSWORDS = {"your_app_password_here", "xxxx xxxx xxxx xxxx"}


def load_filters(filepath):
    with open(filepath, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def credentials_ready(email_user, app_password):
    if not email_user or not app_password:
        log.error("Missing credentials.email or credentials.app_password in config")
        return False
    if email_user.strip().lower() in PLACEHOLDER_EMAILS:
        log.error(
            "Config still has placeholder email (%s). "
            "Edit filters.yaml on the host and restart.",
            email_user,
        )
        return False
    if app_password.strip().lower() in PLACEHOLDER_PASSWORDS:
        log.error(
            "Config still has placeholder app_password. "
            "Set a Gmail App Password in filters.yaml and restart."
        )
        return False
    return True


def process_emails(server, filters_config):
    defaults = filters_config.get("defaults", {})
    default_archive = defaults.get("archive", True)
    default_mark_read = defaults.get("mark_read", False)
    default_star = defaults.get("star", False)

    labels_config = filters_config.get("labels", {})
    total_matched = 0

    for label, criteria in labels_config.items():
        if not server.folder_exists(label):
            log.info("Creating label/folder: %s", label)
            server.create_folder(label)

        query_parts = []

        def format_kw(words):
            return [f'"{w}"' if " " in w else w for w in words]

        if "subject" in criteria and criteria["subject"]:
            kw = " OR ".join(format_kw(criteria["subject"]))
            query_parts.append(f"subject:({kw})")

        if "from" in criteria and criteria["from"]:
            kw = " OR ".join(format_kw(criteria["from"]))
            query_parts.append(f"from:({kw})")

        if "text" in criteria and criteria["text"]:
            kw = " OR ".join(format_kw(criteria["text"]))
            query_parts.append(kw)

        if "to" in criteria and criteria["to"]:
            kw = " OR ".join(format_kw(criteria["to"]))
            query_parts.append(f"to:({kw})")

        if not query_parts:
            log.warning("Label %r has no search criteria — skipping", label)
            continue

        search_query = " OR ".join(query_parts) + " "
        messages = server.gmail_search(search_query, charset="UTF-8")

        if not messages:
            log.debug("Label %r: no matches for %r", label, search_query.strip())
            continue

        count = len(messages)
        total_matched += count
        log.info("Label %r: matched %d message(s)", label, count)

        server.copy(messages, label)

        flags_to_add = []
        archive_flag = criteria.get("archive", default_archive)
        mark_read_flag = criteria.get("mark_read", default_mark_read)
        star_flag = criteria.get("star", default_star)

        actions = [f"label={label}"]
        if mark_read_flag:
            flags_to_add.append(b"\\Seen")
            actions.append("mark_read")
        if star_flag:
            flags_to_add.append(b"\\Flagged")
            actions.append("star")
        if archive_flag:
            flags_to_add.append(b"\\Deleted")
            actions.append("archive")

        if flags_to_add:
            server.add_flags(messages, flags_to_add)
        if archive_flag:
            server.expunge()

        log.info("Applied [%s] to %d message(s)", ", ".join(actions), count)

    if total_matched == 0:
        log.info("Pass complete — no messages matched any label")
    else:
        log.info("Pass complete — processed %d message(s) total", total_matched)


def config_mtime(filepath):
    try:
        return filepath.stat().st_mtime
    except OSError:
        return None


def run_idle():
    filters_file = Path(
        os.environ.get("FILTERS_PATH", Path(__file__).parent.resolve() / "filters.yaml")
    )
    log.info("Starting gmail-filters; config=%s", filters_file)

    auth_backoff = 30

    while True:
        server = None
        try:
            if not filters_file.is_file():
                log.error("Config file not found: %s", filters_file)
                time.sleep(30)
                continue

            filters_config = load_filters(filters_file)
            last_mtime = config_mtime(filters_file)
            creds = filters_config.get("credentials", {})
            email_user = creds.get("email")
            app_password = creds.get("app_password")

            if not credentials_ready(email_user, app_password):
                time.sleep(60)
                continue

            labels = list((filters_config.get("labels") or {}).keys())
            log.info("Loaded config: account=%s labels=%d (%s)", email_user, len(labels), ", ".join(labels) or "none")

            log.info("Connecting to imap.gmail.com as %s ...", email_user)
            server = IMAPClient("imap.gmail.com", use_uid=True)
            server.login(email_user, app_password)
            server.select_folder("INBOX")
            log.info("Connected — INBOX selected")
            auth_backoff = 30

            process_emails(server, filters_config)

            log.info("Entering IDLE (waiting for new mail)...")
            server.idle()

            while True:
                try:
                    responses = server.idle_check(timeout=30)
                    mtime = config_mtime(filters_file)
                    config_changed = mtime is not None and mtime != last_mtime

                    if not responses and not config_changed:
                        log.debug("IDLE heartbeat (no new mail)")
                        continue

                    if responses:
                        log.info("IDLE wake-up: %s", responses)
                    if config_changed:
                        log.info("Config changed — reloading %s", filters_file)

                    server.idle_done()
                    if responses:
                        time.sleep(10)

                    new_config = load_filters(filters_file)
                    last_mtime = config_mtime(filters_file)
                    new_creds = new_config.get("credentials", {})
                    new_email = new_creds.get("email")
                    new_password = new_creds.get("app_password")

                    if not credentials_ready(new_email, new_password):
                        raise RuntimeError("Invalid credentials after config reload")

                    if (new_email, new_password) != (email_user, app_password):
                        log.info("Credentials changed — reconnecting")
                        break

                    filters_config = new_config
                    email_user = new_email
                    app_password = new_password
                    process_emails(server, filters_config)
                    log.info("Re-entering IDLE...")
                    server.idle()
                except Exception as idle_err:
                    try:
                        server.idle_done()
                    except Exception:
                        pass
                    raise idle_err

        except Exception as e:
            err = str(e)
            if "AUTHENTICATIONFAILED" in err or "Invalid credentials" in err:
                log.error(
                    "Gmail login failed (invalid credentials). "
                    "Check email + App Password in %s. Retrying in %ds...",
                    filters_file,
                    auth_backoff,
                )
                time.sleep(auth_backoff)
                auth_backoff = min(auth_backoff * 2, 300)
            else:
                log.exception("Error: %s — reconnecting in 10s...", e)
                time.sleep(10)
        finally:
            if server is not None:
                try:
                    server.logout()
                except Exception:
                    pass


if __name__ == "__main__":
    run_idle()
