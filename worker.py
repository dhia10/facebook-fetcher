import os, re, json, requests
from datetime import datetime, timezone

GRAPH_VER      = os.getenv('FB_GRAPH_VER', 'v23.0')
RECEIVER_URL   = os.getenv('RECEIVER_URL')         # Apps Script web app URL
RECEIVER_SECRET= os.getenv('RECEIVER_SECRET')       # same as in Apps Script
PAGES_JSON     = os.getenv('PAGES_JSON', '[]')      # JSON: [{ "id": "...", "token": "..." }, ...]

POST_LIMIT     = int(os.getenv('POST_LIMIT', '4'))          # last N posts
COMMENT_PAGES  = int(os.getenv('COMMENT_PAGES', '2'))       # extra comment pages beyond first 100
CONV_PAGES     = int(os.getenv('CONV_PAGES', '1'))          # conversation pages/run
LIVE_LIMIT     = int(os.getenv('LIVE_LIMIT', '3'))          # live videos to inspect
LIVE_COMMENT_PAGES = int(os.getenv('LIVE_COMMENT_PAGES', '2'))
DEDUP_WINDOW_S = int(os.getenv('DEDUP_WINDOW_S', str(60*60)))  # 1h/phone

IGNORE = set(os.getenv('IGNORE_LIST', '36011012').split(',')) if os.getenv('IGNORE_LIST') else {'36011012'}
PHONE_RE = re.compile(r'(^|[^\d])((?:\d[ .-]?){8})(?!\d)')

def normalize_digits(s):
    if not s: return ''
    return ''.join(chr(ord(c)-0x0660+ord('0')) if '\u0660' <= c <= '\u0669' else c for c in s)

def find_phones(text):
    text = normalize_digits(text or '')
    found = set()
    for m in PHONE_RE.finditer(text):
        cleaned = re.sub(r'\D','', m.group(2))
        if len(cleaned) == 8:
            found.add(cleaned)
    for run in re.split(r'\D+', text):
        if len(run) == 8: found.add(run)
        elif len(run) > 8:
            for i in range(0, len(run)-7, 8):
                found.add(run[i:i+8])
    return [n for n in found if n not in IGNORE and re.match(r'^[234579]', n)]

def ts_fmt(iso_str):
    dt = datetime.fromisoformat(iso_str.replace('Z','+00:00'))
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def g(token, path, params):
    url = f'https://graph.facebook.com/{GRAPH_VER}/{path}'
    params = {**params, 'access_token': token}
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    j = r.json()
    if 'error' in j:
        raise RuntimeError(j['error'])
    return j

def receiver_call(payload):
    r = requests.post(RECEIVER_URL, json=payload, timeout=60)
    r.raise_for_status()
    return r

def receiver_append(rows):
    if not rows: return 'no rows'
    return receiver_call({'secret': RECEIVER_SECRET, 'op': 'append', 'rows': rows}).text

def receiver_get_state(page_id):
    r = receiver_call({'secret': RECEIVER_SECRET, 'op': 'get_state', 'page_id': page_id})
    try: return r.json()
    except: return {}

def receiver_set_state(page_id, state):
    return receiver_call({'secret': RECEIVER_SECRET, 'op': 'set_state', 'page_id': page_id, 'state': state}).text

def can_use_phone(phone, last_phone):
    prev = last_phone.get(phone)
    if not prev: return True
    prev_ts = datetime.fromisoformat(prev.replace('Z','+00:00'))
    return (datetime.now(timezone.utc) - prev_ts).total_seconds() >= DEDUP_WINDOW_S

def mark_phone(phone, last_phone):
    last_phone[phone] = datetime.now(timezone.utc).isoformat()

def handle_comment_to_rows(page_id, c, fallback_iso, state, rows):
    ct = c.get('created_time') or fallback_iso or datetime.now(timezone.utc).isoformat()
    name = (c.get('from') or {}).get('name') or 'Commenter'
    for n in find_phones(c.get('message') or ''):
        if can_use_phone(n, state['lastPhone']):
            key = f'{page_id}|{c["id"]}|comment|{n}'
            if key not in state['seen']:
                rows.append([ts_fmt(ct), 'Comment', name, n, c.get('message') or ''])
                state['seen'].add(key); mark_phone(n, state['lastPhone'])

def fetch_all_comments(token, object_id, pages_max):
    out = []
    params = {'fields': 'id,from{id,name},message,created_time', 'limit': '100'}
    for _ in range(pages_max):
        j = g(token, f'{object_id}/comments', params)
        out.extend(j.get('data', []))
        nextc = j.get('paging', {}).get('cursors', {}).get('after')
        if not nextc: break
        params['after'] = nextc
    return out

def fetch_recent_published_posts(page, state):
    rows = []
    j = g(page['token'], f'{page["id"]}/published_posts', {
        'fields': 'id,from{id,name},message,permalink_url,created_time,updated_time,'
                  'comments.limit(100).summary(true){id,from{id,name},message,created_time}',
        'limit': str(POST_LIMIT)
    })
    for post in j.get('data', []):
        pt = post.get('created_time') or datetime.now(timezone.utc).isoformat()
        author = (post.get('from') or {}).get('name') or '(Page)'
        msg = post.get('message')

        if msg:
            for n in find_phones(msg):
                if can_use_phone(n, state['lastPhone']):
                    key = f'{page["id"]}|{post["id"]}|post|{n}'
                    if key not in state['seen']:
                        rows.append([ts_fmt(pt), 'Post', author, n, msg])
                        state['seen'].add(key); mark_phone(n, state['lastPhone'])

        for c in (post.get('comments', {}).get('data') or []):
            handle_comment_to_rows(page['id'], c, pt, state, rows)

        total = post.get('comments', {}).get('summary', {}).get('total_count', 0)
        inline = len(post.get('comments', {}).get('data') or [])
        if total > inline and COMMENT_PAGES > 0:
            for c in fetch_all_comments(page['token'], post['id'], COMMENT_PAGES):
                handle_comment_to_rows(page['id'], c, pt, state, rows)
    return rows

def fetch_messages_since(page, since_iso, state):
    rows = []
    if CONV_PAGES <= 0: return rows
    params = {'fields': 'id,updated_time,participants.limit(2){id,name},'
                        'messages.limit(100){id,created_time,message,from{id,name}}',
              'limit': '25'}
    if since_iso:
        # conversations supports 'since' (epoch)
        epoch = int(datetime.fromisoformat(since_iso.replace('Z','+00:00')).timestamp()) - 90
        params['since'] = epoch
    for _ in range(CONV_PAGES):
        j = g(page['token'], f'{page["id"]}/conversations', params)
        for conv in j.get('data', []):
            for m in (conv.get('messages', {}).get('data') or []):
                mt = m.get('created_time') or conv.get('updated_time') or datetime.now(timezone.utc).isoformat()
                if since_iso and datetime.fromisoformat(mt.replace('Z','+00:00')) <= datetime.fromisoformat(since_iso.replace('Z','+00:00')):
                    continue
                if (m.get('from') or {}).get('id') == page['id']:
                    continue
                name = (m.get('from') or {}).get('name') or 'Messenger User'
                text = m.get('message') or ''
                for n in find_phones(text):
                    if can_use_phone(n, state['lastPhone']):
                        key = f'{page["id"]}|{m["id"]}|msg|{n}'
                        if key not in state['seen']:
                            rows.append([ts_fmt(mt), 'Message', name, n, text])
                            state['seen'].add(key); mark_phone(n, state['lastPhone'])
        break
    return rows

def recent_live_video_ids(page):
    j = g(page['token'], f'{page["id"]}/live_videos', {
        'fields': 'id,creation_time,video{id}', 'limit': str(LIVE_LIMIT)
    })
    vids = []
    for lv in j.get('data', []):
        vid = (lv.get('video') or {}).get('id')
        if vid: vids.append(vid)
    return vids

def fetch_video_comments(page, video_id, pages_max):
    out = []
    params = {'fields': 'id,created_time,message,from{id,name}',
              'order': 'reverse_chronological', 'limit': '100'}
    for _ in range(pages_max):
        j = g(page['token'], f'{video_id}/comments', params)
        out.extend(j.get('data', []))
        nextc = j.get('paging', {}).get('cursors', {}).get('after')
        if not nextc: break
        params['after'] = nextc
    return out

def fetch_live_since(page, since_iso, state):
    rows = []
    if LIVE_LIMIT <= 0: return rows
    for vid in recent_live_video_ids(page):
        for c in fetch_video_comments(page, vid, LIVE_COMMENT_PAGES):
            if not c.get('message'): continue
            ct = c.get('created_time') or datetime.now(timezone.utc).isoformat()
            if since_iso and datetime.fromisoformat(ct.replace('Z','+00:00')) <= datetime.fromisoformat(since_iso.replace('Z','+00:00')):
                continue
            if (c.get('from') or {}).get('id') == page['id']: continue
            name = (c.get('from') or {}).get('name') or 'Live'
            for n in find_phones(c.get('message')):
                if can_use_phone(n, state['lastPhone']):
                    key = f'{page["id"]}|{c["id"]}|live|{n}'
                    if key not in state['seen']:
                        rows.append([ts_fmt(ct), 'Live Comment', name, n, c.get('message') or ''])
                        state['seen'].add(key); mark_phone(n, state['lastPhone'])
    return rows

def run_for_page(page):
    # load persisted state from Apps Script
    raw = receiver_get_state(page['id']) or {}
    state = {
        'seen': set(raw.get('seen', [])),
        'lastPhone': raw.get('lastPhone', {}),
        'lastByPage': raw.get('lastByPage', {})
    }
    since_iso = state['lastByPage'].get(page['id'])

    rows = []
    rows += fetch_recent_published_posts(page, state)
    rows += fetch_messages_since(page, since_iso, state)
    rows += fetch_live_since(page, since_iso, state)

    if rows:
        receiver_append(rows)

    # advance watermark + persist state (trim seen)
    state['lastByPage'][page['id']] = datetime.now(timezone.utc).isoformat()
    if len(state['seen']) > 10000:
        state['seen'] = set(list(state['seen'])[-10000:])
    receiver_set_state(page['id'], {
        'seen': list(state['seen']),
        'lastPhone': state['lastPhone'],
        'lastByPage': state['lastByPage']
    })

def main():
    assert RECEIVER_URL and RECEIVER_SECRET, "Missing RECEIVER_URL or RECEIVER_SECRET"
    pages = json.loads(PAGES_JSON)
    for page in pages:
        run_for_page(page)

if __name__ == '__main__':
    main()
