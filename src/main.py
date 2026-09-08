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

# Gmail operator prefix; text uses bare terms (body/anywhere search).
FIELD_PREFIX = {
    "from": "from",
    "to": "to",
    "subject": "subject",
    "text": None,
}
CRITERIA_FIELDS = ("from", "to", "subject", "text")


def load_yaml(filepath):
    with open(filepath, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


# Keep old name used elsewhere.
load_filters = load_yaml


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_app_settings(raw):
    """Normalize settings.yaml into a flat runtime dict with defaults."""
    raw = raw or {}
    filtering = raw.get("filtering") or {}
    imap = raw.get("imap") or {}
    runtime = raw.get("runtime") or {}
    logging_cfg = raw.get("logging") or {}
    creds = raw.get("credentials") or {}

    level = str(logging_cfg.get("level") or "INFO").upper()
    if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        level = "INFO"

    return {
        "email": creds.get("email"),
        "app_password": creds.get("app_password"),
        "filtering_enabled": _as_bool(filtering.get("enabled"), False),
        "imap_host": (imap.get("host") or "imap.gmail.com").strip(),
        "imap_folder": (imap.get("folder") or "INBOX").strip() or "INBOX",
        "idle_timeout_seconds": max(5, _as_int(runtime.get("idle_timeout_seconds"), 30)),
        "new_mail_settle_seconds": max(0, _as_int(runtime.get("new_mail_settle_seconds"), 10)),
        "reconnect_delay_seconds": max(1, _as_int(runtime.get("reconnect_delay_seconds"), 10)),
        "missing_config_retry_seconds": max(
            5, _as_int(runtime.get("missing_config_retry_seconds"), 30)
        ),
        "invalid_credentials_retry_seconds": max(
            5, _as_int(runtime.get("invalid_credentials_retry_seconds"), 60)
        ),
        "auth_backoff_initial_seconds": max(
            5, _as_int(runtime.get("auth_backoff_initial_seconds"), 30)
        ),
        "auth_backoff_max_seconds": max(
            30, _as_int(runtime.get("auth_backoff_max_seconds"), 300)
        ),
        "disabled_poll_seconds": max(5, _as_int(runtime.get("disabled_poll_seconds"), 30)),
        "log_level": level,
        "body_peek_bytes": max(0, _as_int(logging_cfg.get("body_peek_bytes"), 2000)),
    }


def apply_log_level(level_name):
    level = getattr(logging, level_name, logging.INFO)
    log.setLevel(level)
    logging.getLogger().setLevel(level)


def credentials_ready(email_user, app_password, settings_file=None):
    where = str(settings_file) if settings_file else "settings.yaml"
    if not email_user or not app_password:
        log.error("Missing credentials.email or credentials.app_password in %s", where)
        return False
    if email_user.strip().lower() in PLACEHOLDER_EMAILS:
        log.error(
            "Config still has placeholder email (%s). "
            "Edit %s on the host (hot-reloads on save).",
            email_user,
            where,
        )
        return False
    if app_password.strip().lower() in PLACEHOLDER_PASSWORDS:
        log.error(
            "Config still has placeholder app_password. "
            "Set a Gmail App Password in %s (hot-reloads on save).",
            where,
        )
        return False
    return True


def as_list(value):
    if value is None or value is False:
        return []
    if isinstance(value, list):
        return value
    return [value]


def split_unescaped(text, sep):
    """Split on sep, ignoring separators escaped with a backslash."""
    parts = []
    buf = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            buf.append(text[i])
            buf.append(text[i + 1])
            i += 2
            continue
        if text[i] == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(text[i])
        i += 1
    parts.append("".join(buf))
    return parts


def unescape_token(text):
    """Resolve \\+ \\! \\\\ escapes; other backslashes stay literal."""
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in "+!\\":
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def parse_list_item(item):
    """
    Parse one list entry into AND-parts: (negated, term).

    - 'a + b + c'  → AND of a, b, c
    - 'a \\+ b'    → literal 'a + b'
    - '!spam'      → negated spam
    - '\\!spam'    → literal '!spam'
    """
    if item is None:
        return []
    if not isinstance(item, str):
        item = str(item)

    result = []
    for chunk in split_unescaped(item, "+"):
        raw = chunk.strip()
        if not raw:
            continue
        negated = False
        if raw.startswith("!") and not raw.startswith("\\!"):
            negated = True
            raw = raw[1:].strip()
        term = unescape_token(raw)
        if term:
            result.append((negated, term))
    return result


def quote_term(term):
    if any(ch in term for ch in ' "(){}'):
        return '"' + term.replace('"', '\\"') + '"'
    return term


def _prefixed(prefix, term, negate=False):
    q = quote_term(term)
    if prefix:
        core = f"{prefix}:{q}"
    else:
        core = q
    return f"-{core}" if negate else core


def build_field_clause(field, values):
    """
    Build a Gmail clause for one field.

    YAML list items are OR'd. Within one item, '+' means AND.
    Items that are only '!term' apply as AND NOT on the whole clause.
    """
    prefix = FIELD_PREFIX[field]
    or_branches = []
    extra_negatives = []

    for item in as_list(values):
        parts = parse_list_item(item)
        if not parts:
            continue

        positives = [term for negated, term in parts if not negated]
        negatives = [term for negated, term in parts if negated]

        if not positives:
            extra_negatives.extend(negatives)
            continue

        bits = []
        if len(positives) == 1:
            bits.append(_prefixed(prefix, positives[0]))
        elif prefix:
            bits.append(f"{prefix}:({' '.join(quote_term(t) for t in positives)})")
        else:
            bits.append("(" + " ".join(quote_term(t) for t in positives) + ")")

        for term in negatives:
            bits.append(_prefixed(prefix, term, negate=True))

        or_branches.append(" ".join(bits))

    if not or_branches and not extra_negatives:
        return None

    pieces = []
    if or_branches:
        joined = " OR ".join(or_branches)
        pieces.append(f"({joined})" if len(or_branches) > 1 else joined)
    for term in extra_negatives:
        pieces.append(_prefixed(prefix, term, negate=True))
    return " ".join(pieces)


def build_rule_query(rule):
    """One rule: fields AND'd together; optional raw Gmail query."""
    if not isinstance(rule, dict):
        return None

    parts = []
    raw = rule.get("query")
    if raw is not None and str(raw).strip():
        parts.append(f"({str(raw).strip()})")

    for field in CRITERIA_FIELDS:
        if field not in rule or rule[field] is None or rule[field] == "":
            continue
        clause = build_field_clause(field, rule[field])
        if clause:
            parts.append(clause)

    if not parts:
        return None
    return " ".join(parts)


def build_label_query(criteria):
    """Build Gmail search for a label: rules OR'd, fields inside a rule AND'd."""
    if not isinstance(criteria, dict):
        return None

    rules = criteria.get("rules")
    if rules is None:
        return None
    if not isinstance(rules, list):
        log.warning("Label rules must be a list")
        return None

    rule_queries = []
    for idx, rule in enumerate(rules):
        q = build_rule_query(rule)
        if q:
            rule_queries.append(f"({q})")
        else:
            log.warning("Skipping empty rule at index %d", idx)
    if not rule_queries:
        return None
    return " OR ".join(rule_queries)


def _as_str(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def format_address_list(addresses):
    if not addresses:
        return ""
    parts = []
    for addr in addresses:
        mailbox = _as_str(addr.mailbox)
        host = _as_str(addr.host)
        email = f"{mailbox}@{host}" if mailbox and host else mailbox or host
        name = _as_str(addr.name).strip()
        if name and email:
            parts.append(f"{name} <{email}>")
        elif email:
            parts.append(email)
        elif name:
            parts.append(name)
    return ", ".join(parts)


def positive_terms(values):
    """Flatten field values to positive (non-negated) searchable terms."""
    terms = []
    for item in as_list(values):
        for negated, term in parse_list_item(item):
            if not negated and term:
                terms.append(term)
    return terms


def term_hits(haystack, terms):
    hay = haystack.lower()
    return [term for term in terms if term.lower() in hay]


def explain_match(rules, from_text, to_text, subject_text, body_text):
    """
    Best-effort reason why a message matched.
    Returns a short English phrase, e.g. "from='@acme.com', subject='invoice'".
    """
    if not isinstance(rules, list):
        return "search query"

    field_haystacks = {
        "from": from_text,
        "to": to_text,
        "subject": subject_text,
        "text": f"{subject_text} {from_text} {to_text} {body_text}",
    }

    for rule in rules:
        if not isinstance(rule, dict):
            continue

        hits = []
        fields_ok = True
        has_structured = False

        for field in CRITERIA_FIELDS:
            if field not in rule or rule[field] is None or rule[field] == "":
                continue
            has_structured = True
            terms = positive_terms(rule[field])
            if not terms:
                continue
            matched = term_hits(field_haystacks[field], terms)
            if matched:
                # Prefer the first hit for readability; AND groups already enforced by search.
                hits.append(f"{field}={matched[0]!r}")
            else:
                fields_ok = False
                break

        raw = rule.get("query")
        has_query = raw is not None and str(raw).strip()

        if has_structured and fields_ok and hits:
            if has_query:
                hits.append("query")
            return ", ".join(hits)

        if has_query and not has_structured:
            q = str(raw).strip()
            if len(q) > 80:
                q = q[:77] + "..."
            return f"query={q!r}"

    return "search query"


def fetch_message_details(server, uids, body_peek_bytes=2000):
    """Fetch envelope (+ short body peek) for logging."""
    if not uids:
        return {}
    fetch_items = ["ENVELOPE"]
    if body_peek_bytes > 0:
        # Whole-message peek catches text in multipart bodies better than BODY[TEXT].
        fetch_items.append(f"BODY.PEEK[]<0.{body_peek_bytes}>".encode("ascii"))
    fetched = server.fetch(uids, fetch_items)
    details = {}
    for uid in uids:
        data = fetched.get(uid) or {}
        env = data.get(b"ENVELOPE")
        body = b""
        for key, val in data.items():
            key_s = key.decode("ascii", errors="replace") if isinstance(key, bytes) else str(key)
            if key_s.startswith("BODY") and not key_s.startswith("BODYSTRUCTURE"):
                if isinstance(val, (bytes, bytearray)):
                    body = bytes(val)
                elif isinstance(val, str):
                    body = val.encode("utf-8", errors="replace")
                break
        details[uid] = {
            "from": format_address_list(env.from_) if env else "",
            "to": format_address_list(env.to) if env else "",
            "subject": _as_str(env.subject) if env else "",
            "body": _as_str(body),
        }
    return details


def describe_actions(label, archive, mark_read, star):
    actions = [f"label={label}"]
    if mark_read:
        actions.append("mark_read")
    if star:
        actions.append("star")
    if archive:
        actions.append("archive")
    return actions


def process_emails(server, filters_config, body_peek_bytes=2000):
    defaults = filters_config.get("defaults", {})
    default_archive = defaults.get("archive", True)
    default_mark_read = defaults.get("mark_read", False)
    default_star = defaults.get("star", False)

    labels_config = filters_config.get("labels", {})
    total_matched = 0

    for label, criteria in labels_config.items():
        if not isinstance(criteria, dict):
            log.warning("Label %r has invalid config — skipping", label)
            continue

        if not server.folder_exists(label):
            log.info("Creating label/folder: %s", label)
            server.create_folder(label)

        if criteria.get("rules") is None:
            log.warning("Label %r has no rules: — skipping", label)
            continue

        search_query = build_label_query(criteria)
        if not search_query:
            log.warning("Label %r has no valid search criteria — skipping", label)
            continue

        search_query = search_query + " "
        log.debug("Label %r search: %s", label, search_query.strip())
        messages = server.gmail_search(search_query, charset="UTF-8")

        if not messages:
            log.debug("Label %r: no matches for %r", label, search_query.strip())
            continue

        count = len(messages)
        total_matched += count

        archive_flag = criteria.get("archive", default_archive)
        mark_read_flag = criteria.get("mark_read", default_mark_read)
        star_flag = criteria.get("star", default_star)
        actions = describe_actions(label, archive_flag, mark_read_flag, star_flag)
        actions_text = ", ".join(actions)

        try:
            details = fetch_message_details(server, messages, body_peek_bytes=body_peek_bytes)
        except Exception:
            log.exception("Failed to fetch message details for label %r", label)
            details = {}

        log.info("Label %r: %d message(s) matched", label, count)
        for uid in messages:
            info = details.get(uid, {})
            reason = explain_match(
                criteria.get("rules") or [],
                info.get("from", ""),
                info.get("to", ""),
                info.get("subject", ""),
                info.get("body", ""),
            )
            log.info(
                "  uid=%s from=%r subject=%r — matched %s; applying %s",
                uid,
                info.get("from", "") or "(unknown)",
                info.get("subject", "") or "(no subject)",
                reason,
                actions_text,
            )

        server.copy(messages, label)

        flags_to_add = []
        if mark_read_flag:
            flags_to_add.append(b"\\Seen")
        if star_flag:
            flags_to_add.append(b"\\Flagged")
        if archive_flag:
            flags_to_add.append(b"\\Deleted")

        if flags_to_add:
            server.add_flags(messages, flags_to_add)
        if archive_flag:
            server.expunge()

        log.info("Label %r: finished applying [%s] to %d message(s)", label, actions_text, count)

    if total_matched == 0:
        log.info("Pass complete — no messages matched any label")
    else:
        log.info("Pass complete — processed %d message(s) total", total_matched)


def config_mtime(filepath):
    try:
        return filepath.stat().st_mtime
    except OSError:
        return None


def resolve_config_paths():
    src_dir = Path(__file__).parent.resolve()
    filters_file = Path(os.environ.get("FILTERS_PATH", src_dir / "filters.yaml"))
    settings_file = Path(os.environ.get("SETTINGS_PATH", src_dir / "settings.yaml"))
    return filters_file, settings_file


def run_idle():
    filters_file, settings_file = resolve_config_paths()
    log.info(
        "Starting gmail-filters; settings=%s filters=%s",
        settings_file,
        filters_file,
    )

    auth_backoff = 30

    while True:
        server = None
        try:
            if not settings_file.is_file():
                log.error("Settings file not found: %s", settings_file)
                time.sleep(30)
                continue

            app = parse_app_settings(load_yaml(settings_file))
            apply_log_level(app["log_level"])
            auth_backoff = app["auth_backoff_initial_seconds"]

            if not filters_file.is_file():
                log.error("Filters file not found: %s", filters_file)
                time.sleep(app["missing_config_retry_seconds"])
                continue

            if not app["filtering_enabled"]:
                log.warning(
                    "Filtering is disabled (filtering.enabled=false). "
                    "Set it to true in %s to start. Checking again in %ds...",
                    settings_file,
                    app["disabled_poll_seconds"],
                )
                time.sleep(app["disabled_poll_seconds"])
                continue

            filters_config = load_yaml(filters_file)
            last_settings_mtime = config_mtime(settings_file)
            last_filters_mtime = config_mtime(filters_file)

            email_user = app["email"]
            app_password = app["app_password"]

            if not credentials_ready(email_user, app_password, settings_file):
                time.sleep(app["invalid_credentials_retry_seconds"])
                continue

            labels = list((filters_config.get("labels") or {}).keys())
            log.info(
                "Loaded config: account=%s host=%s folder=%s labels=%d (%s)",
                email_user,
                app["imap_host"],
                app["imap_folder"],
                len(labels),
                ", ".join(labels) or "none",
            )

            log.info("Connecting to %s as %s ...", app["imap_host"], email_user)
            server = IMAPClient(app["imap_host"], use_uid=True)
            server.login(email_user, app_password)
            server.select_folder(app["imap_folder"])
            log.info("Connected — %s selected", app["imap_folder"])
            auth_backoff = app["auth_backoff_initial_seconds"]
            session_host = app["imap_host"]
            session_folder = app["imap_folder"]

            process_emails(server, filters_config, body_peek_bytes=app["body_peek_bytes"])

            log.info(
                "Entering IDLE (timeout=%ds, waiting for new mail)...",
                app["idle_timeout_seconds"],
            )
            server.idle()

            while True:
                try:
                    responses = server.idle_check(timeout=app["idle_timeout_seconds"])
                    settings_mtime = config_mtime(settings_file)
                    filters_mtime = config_mtime(filters_file)
                    settings_changed = (
                        settings_mtime is not None and settings_mtime != last_settings_mtime
                    )
                    filters_changed = (
                        filters_mtime is not None and filters_mtime != last_filters_mtime
                    )

                    if not responses and not settings_changed and not filters_changed:
                        log.debug("IDLE heartbeat (no new mail)")
                        continue

                    if responses:
                        log.info("IDLE wake-up: %s", responses)
                    if settings_changed:
                        log.info("Settings changed — reloading %s", settings_file)
                    if filters_changed:
                        log.info("Filters changed — reloading %s", filters_file)

                    server.idle_done()
                    if responses and app["new_mail_settle_seconds"] > 0:
                        time.sleep(app["new_mail_settle_seconds"])

                    app = parse_app_settings(load_yaml(settings_file))
                    apply_log_level(app["log_level"])
                    filters_config = load_yaml(filters_file)
                    last_settings_mtime = config_mtime(settings_file)
                    last_filters_mtime = config_mtime(filters_file)

                    if not app["filtering_enabled"]:
                        log.warning(
                            "Filtering disabled via settings — disconnecting"
                        )
                        break

                    new_email = app["email"]
                    new_password = app["app_password"]

                    if not credentials_ready(new_email, new_password, settings_file):
                        raise RuntimeError("Invalid credentials after settings reload")

                    if (new_email, new_password) != (email_user, app_password):
                        log.info("Credentials changed — reconnecting")
                        break
                    if (app["imap_host"], app["imap_folder"]) != (
                        session_host,
                        session_folder,
                    ):
                        log.info("IMAP host/folder changed — reconnecting")
                        break

                    email_user = new_email
                    app_password = new_password
                    process_emails(
                        server, filters_config, body_peek_bytes=app["body_peek_bytes"]
                    )
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
            try:
                retry_app = parse_app_settings(load_yaml(settings_file))
                reconnect_delay = retry_app["reconnect_delay_seconds"]
                auth_max = retry_app["auth_backoff_max_seconds"]
            except Exception:
                reconnect_delay = 10
                auth_max = 300

            if "AUTHENTICATIONFAILED" in err or "Invalid credentials" in err:
                log.error(
                    "Gmail login failed (invalid credentials). "
                    "Check email + App Password in %s. Retrying in %ds...",
                    settings_file,
                    auth_backoff,
                )
                time.sleep(auth_backoff)
                auth_backoff = min(auth_backoff * 2, auth_max)
            else:
                log.exception(
                    "Error: %s — reconnecting in %ds...", e, reconnect_delay
                )
                time.sleep(reconnect_delay)
        finally:
            if server is not None:
                try:
                    server.logout()
                except Exception:
                    pass


if __name__ == "__main__":
    run_idle()
