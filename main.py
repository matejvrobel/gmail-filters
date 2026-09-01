import time
from pathlib import Path
import yaml
from imapclient import IMAPClient

def load_filters(filepath):
    with open(filepath, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)

def process_emails(server, filters_config):
    defaults = filters_config.get('defaults', {})
    default_archive = defaults.get('archive', True)
    default_mark_read = defaults.get('mark_read', False)
    default_star = defaults.get('star', False)

    labels_config = filters_config.get('labels', {})

    for label, criteria in labels_config.items():
        if not server.folder_exists(label):
            server.create_folder(label)

        query_parts = []
        
        def format_kw(words):
            return [f'"{w}"' if ' ' in w else w for w in words]
        
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
            continue
            
        search_query = " OR ".join(query_parts) + " "
        
        messages = server.gmail_search(search_query, charset='UTF-8')
        
        if messages:
            server.copy(messages, label)
            
            flags_to_add = []
            
            archive_flag = criteria.get('archive', default_archive)
            mark_read_flag = criteria.get('mark_read', default_mark_read)
            star_flag = criteria.get('star', default_star)
            
            if mark_read_flag:
                flags_to_add.append(b'\\Seen')
            if star_flag:
                flags_to_add.append(b'\\Flagged')
            if archive_flag:
                flags_to_add.append(b'\\Deleted')
                
            if flags_to_add:
                server.add_flags(messages, flags_to_add)
            
            if archive_flag:
                server.expunge()

def run_idle():
    script_dir = Path(__file__).parent.resolve()
    filters_file = script_dir / "filters.yaml"
    
    while True:
        try:
            filters_config = load_filters(filters_file)
            creds = filters_config.get('credentials', {})
            email_user = creds.get('email')
            app_password = creds.get('app_password')
            
            if not email_user or not app_password:
                print("Error: Missing credentials in YAML file.")
                time.sleep(10)
                continue

            server = IMAPClient("imap.gmail.com", use_uid=True)
            server.login(email_user, app_password)
            server.select_folder('INBOX')
            
            process_emails(server, filters_config)
            
            server.idle()
            
            while True:
                responses = server.idle_check(timeout=30)
                if responses:
                    server.idle_done()
                    
                    time.sleep(1)
                    
                    filters_config = load_filters(filters_file)
                    process_emails(server, filters_config)
                    
                    server.idle()
                    
        except Exception as e:
            print(f"Error: {e}. Reconnecting...")
            time.sleep(10)

if __name__ == "__main__":
    run_idle()