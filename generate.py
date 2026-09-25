#!/usr/bin/env python3
"""Збирає playlist.m3u з публічних джерел за конфігом channels.json.

Для кожного каналу: знаходить усі потоки в джерелах (за tvg-id та назвами),
перевіряє їх ffprobe, сортує (живі за роздільністю, потім решта) і пише
SLOTS записів. Записи мають однакову назву й різний tvg-name, тож m3u-editor
тримає їх як окремі стабільні канали: слот 1 — основний, 2..N — резервні.
"""
import concurrent.futures as cf
import json
import re
import subprocess
import threading
import urllib.parse
import urllib.request

SLOTS = 3
DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
TR = str.maketrans({'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e', 'є': 'e', 'ж': 'zh',
                    'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
                    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
                    'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ь': '', 'ю': 'yu', 'я': 'ya', 'ы': 'y', 'э': 'e',
                    'ё': 'e', 'ъ': ''})
NOISE = (r'\b(hd|fhd|uhd|sd|4k|tv|tb|тв|тб|telekanal|телеканал|ukraina|ukraine|україна|украина|ua|live|'
         r'online|orig|backup|резерв)\b')


def norm(s):
    s = (s or '').lower()
    s = re.sub(r'\(\d+[pi]\)|\[.*?\]|\(backup\)|\(резерв\)', '', s)
    s = re.sub(NOISE, '', s)
    s = s.replace('+', 'plus').translate(TR)
    return re.sub(r'[^a-z0-9]', '', s)


def parse(text, src):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF'):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            name = line.rsplit(',', 1)[-1].strip() if ',' in line else re.sub(r'^#EXTINF:-?\d+\s*', '', line)
            cur = {'name': name, 'tvg_id': attrs.get('tvg-id', ''), 'ua': attrs.get('http-user-agent'),
                   'ref': attrs.get('http-referrer'), 'src': src}
        elif line.startswith('#EXTVLCOPT:http-user-agent=') and cur:
            cur['ua'] = line.split('=', 1)[1]
        elif line.startswith('#EXTVLCOPT:http-referrer=') and cur:
            cur['ref'] = line.split('=', 1)[1]
        elif line and not line.startswith('#') and cur:
            cur['url'] = line
            out.append(cur)
            cur = None
    return out


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8', errors='ignore')


HOST_LIMIT = {}
HOST_LOCK = threading.Lock()


def _host_sem(url):
    host = urllib.parse.urlsplit(url).hostname or ''
    with HOST_LOCK:
        return HOST_LIMIT.setdefault(host, threading.Semaphore(3))


def probe(e):
    """Висота відео або None. Не більше 3 одночасних запитів на хост, 2 спроби."""
    with _host_sem(e['url']):
        for _ in range(2):
            url, h = _probe_once(e)
            if h is not None:
                break
    return url, h


def _probe_once(e):
    cmd = ['ffprobe', '-v', 'error', '-rw_timeout', '15000000', '-user_agent', e.get('ua') or DEFAULT_UA]
    if e.get('ref'):
        cmd += ['-referer', e['ref']]
    cmd += ['-select_streams', 'v:0', '-show_entries', 'stream=height', '-of', 'csv=p=0', e['url']]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout.split()
        return e['url'], int(out[0]) if out and out[0].isdigit() else None
    except Exception:
        return e['url'], None


def main():
    channels = json.load(open('channels.json'))
    sources = [l.strip() for l in open('sources.txt') if l.strip() and not l.startswith('#')]
    extra = json.load(open('extra_streams.json'))  # ручні потоки: {"Назва каналу": ["url", ...]}

    entries = []
    for s in sources:
        try:
            entries += parse(fetch(s), s)
        except Exception as ex:
            print('WARN source failed:', s, ex)

    by_tvg, by_name = {}, {}
    for ch in channels:
        for t in ch['match_tvg_ids']:
            by_tvg.setdefault(t.lower(), ch['name'])
        for a in ch['aliases']:
            if norm(a):
                by_name.setdefault(norm(a), ch['name'])

    cands = {ch['name']: [] for ch in channels}
    for e in entries:
        name = by_tvg.get(e['tvg_id'].split('@')[0].lower()) or by_name.get(norm(e['name']))
        if name and e['url'] not in (c['url'] for c in cands[name]):
            cands[name].append(e)
    for name, urls in extra.items():
        for u in urls:
            if name in cands and u not in (c['url'] for c in cands[name]):
                cands[name].insert(0, {'url': u, 'name': name, 'src': 'extra'})

    with cf.ThreadPoolExecutor(32) as ex:
        live = dict(ex.map(probe, [e for v in cands.values() for e in v]))

    lines = ['#EXTM3U']
    report = {}
    for num, ch in enumerate(channels, 1):
        streams = cands[ch['name']]
        # живі — за роздільністю, далі неперевірені/мертві в порядку джерел
        streams = sorted(streams, key=lambda e: (live.get(e['url']) is None, -(live.get(e['url']) or 0)))
        alive = sum(live.get(e['url']) is not None for e in streams)
        report[ch['name']] = {'alive': alive, 'total': len(streams)}
        if not streams:
            continue
        # завжди SLOTS записів, щоб канали в m3u-editor не зникали/не зʼявлялись
        slots = (streams * SLOTS)[:SLOTS]
        for i, e in enumerate(slots, 1):
            tvg_name = ch['name'] if i == 1 else f"{ch['name']} #{i}"
            attrs = f'tvg-id="{ch["tvg_id"]}" tvg-name="{tvg_name}" tvg-chno="{num}"'
            if ch.get('logo'):
                attrs += f' tvg-logo="{ch["logo"]}"'
            attrs += f' group-title="{ch["group"]}"'
            if e.get('ua'):
                attrs += f' http-user-agent="{e["ua"]}"'
            lines.append(f'#EXTINF:-1 {attrs},{ch["name"]}')
            if e.get('ua'):
                lines.append(f'#EXTVLCOPT:http-user-agent={e["ua"]}')
            if e.get('ref'):
                lines.append(f'#EXTVLCOPT:http-referrer={e["ref"]}')
            lines.append(e['url'])

    open('playlist.m3u', 'w').write('\n'.join(lines) + '\n')
    json.dump(report, open('status.json', 'w'), ensure_ascii=False, indent=1, sort_keys=True)
    ok = sum(1 for r in report.values() if r['alive'])
    print(f'channels: {len(channels)}, with live stream: {ok}, streams probed: {len(live)}, '
          f'alive: {sum(v is not None for v in live.values())}')


if __name__ == '__main__':
    main()
