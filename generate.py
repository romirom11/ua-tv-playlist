#!/usr/bin/env python3
"""Збирає playlist.m3u з публічних джерел за конфігом channels.json.

Нічого не перевіряє на живість — це робить m3u-editor зі своєї мережі (Channel Scrubber
+ auto-merge + проксі-failover). Тут лише форматування:
- кожен потік каналу — окремий запис з однаковим tvg-id (m3u-editor обʼєднує їх в один канал
  з резервними потоками), назвою, категорією, логотипом і номером;
- tvg-name унікальний і стабільний для кожного URL, щоб m3u-editor не склеював записи;
- українські канали (tvg-id *.ua) з джерел, яких немає в channels.json, автоматично
  потрапляють у категорію «Нові» (крім перелічених в ignore.txt).
"""
import hashlib
import json
import os
import re
import urllib.request
from datetime import date

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
TR = str.maketrans({'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e', 'є': 'e', 'ж': 'zh',
                    'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
                    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
                    'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ь': '', 'ю': 'yu', 'я': 'ya', 'ы': 'y', 'э': 'e',
                    'ё': 'e', 'ъ': ''})
NEW_GROUP = 'Нові'
CACHE_DIR = 'cache'  # останні успішно завантажені (вже відфільтровані) записи кожного джерела
CACHE_DAYS = 3  # скільки днів джерело може бути недоступним, поки беремо його кеш
NOISE = r'\b(hd|fhd|uhd|sd|4k|tv|tb|тв|тб|telekanal|телеканал|orig|backup|резерв)\b'


def norm(s):
    s = (s or '').lower()
    s = re.sub(r'\(\d+[pi]\)|\[.*?\]|\((backup|резерв|ukraine|україна|украина|ua)\)', '', s)
    s = re.sub(NOISE, '', s)
    s = s.replace('+', 'plus').translate(TR)
    return re.sub(r'[^a-z0-9]', '', s)


def parse(text):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF'):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            name = line.rsplit(',', 1)[-1].strip() if ',' in line else re.sub(r'^#EXTINF:-?\d+\s*', '', line)
            cur = {'name': name, 'tvg_id': attrs.get('tvg-id', ''), 'ua': attrs.get('http-user-agent'),
                   'ref': attrs.get('http-referrer'), 'group': attrs.get('group-title', '')}
        elif line.startswith('#EXTGRP:') and cur:
            cur['group'] = line[8:].strip()
        elif line.startswith('#EXTVLCOPT:http-user-agent=') and cur:
            cur['ua'] = line.split('=', 1)[1]
        elif line.startswith('#EXTVLCOPT:http-referrer=') and cur:
            cur['ref'] = line.split('=', 1)[1]
        elif line and not line.startswith('#') and cur:
            cur['url'] = line
            out.append(cur)
            cur = None
    return out


def clean_title(s):
    s = re.sub(r'\s*\((\d+[pi])\)|\s*\[[^\]]*\]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8', errors='ignore')


def write_if_changed(path, text):
    if os.path.exists(path) and open(path).read() == text:
        return
    open(path, 'w').write(text)


def main():
    channels = json.load(open('channels.json'))
    sources = [l.split() for l in open('sources.txt') if l.strip() and not l.startswith('#')]
    extra = json.load(open('extra_streams.json'))  # ручні потоки: {"tvg_id": ["url", ...]}
    ignore = {l.strip().lower() for l in open('ignore.txt') if l.strip() and not l.startswith('#')}

    os.makedirs(CACHE_DIR, exist_ok=True)
    entries, used_cache = [], set()
    for url, *opts in sources:
        opts = dict(o.split('=', 1) for o in opts)
        cache = os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest()[:12] + '.json')
        try:
            parsed = parse(fetch(url))
            if 'match' in opts:  # регулярний вираз по рядку "group-title|назва"
                parsed = [e for e in parsed if re.search(opts['match'], f"{e['group']}|{e['name']}")]
            if not parsed:
                raise ValueError('no entries')
            write_if_changed(cache, json.dumps({'url': url, 'entries': parsed}, ensure_ascii=False, indent=0))
        except Exception as ex:
            # тимчасовий збій: беремо останню успішну копію, інакше m3u-editor видалить канали
            # джерела, а потім додасть знову; після CACHE_DAYS днів збоїв джерело вважаємо мертвим
            if not os.path.exists(cache):
                raise SystemExit(f'source failed, no cache: {url}: {ex}')
            data = json.load(open(cache))
            since = data.setdefault('failing_since', date.today().isoformat())
            write_if_changed(cache, json.dumps(data, ensure_ascii=False, indent=0))
            days = (date.today() - date.fromisoformat(since)).days
            if days > CACHE_DAYS:
                print(f'::warning::source dead since {since}, skipped — remove it from sources.txt: {url}: {ex}')
                parsed = []
            else:
                print(f'::warning::source failed since {since}, using cached copy: {url}: {ex}')
                parsed = data['entries']
        used_cache.add(cache)
        entries += parsed

    for f in os.listdir(CACHE_DIR):  # кеш джерел, яких більше немає в sources.txt
        if os.path.join(CACHE_DIR, f) not in used_cache:
            os.remove(os.path.join(CACHE_DIR, f))

    by_tvg, by_name = {}, {}
    for ch in channels:
        for t in ch['match_tvg_ids']:
            by_tvg.setdefault(t.lower(), ch['tvg_id'])
        for a in ch['aliases']:
            if norm(a) and not norm(a).isdigit():
                by_name.setdefault(norm(a), ch['tvg_id'])

    streams = {ch['tvg_id']: [] for ch in channels}
    for tvg_id, urls in extra.items():
        streams[tvg_id] += [{'url': u} for u in urls]
    for e in entries:  # порядок джерел = пріоритет потоків
        n = norm(e['name'])
        tvg_id = by_tvg.get(e['tvg_id'].split('@')[0].lower()) or (by_name.get(n) if not n.isdigit() else None)
        if not tvg_id:
            # новий український канал, якого ще немає в channels.json
            base = e['tvg_id'].split('@')[0]
            if not base.lower().endswith('.ua') or base.lower() in ignore or norm(e['name']) in ignore:
                continue
            tvg_id = by_tvg[base.lower()] = base
            if tvg_id not in streams:
                streams[tvg_id] = []
                channels.append({'tvg_id': tvg_id, 'name_ua': clean_title(e['name']), 'group': NEW_GROUP})
        if e['url'] not in (s['url'] for s in streams[tvg_id]):
            streams[tvg_id].append(e)

    lines = ['#EXTM3U']
    for num, ch in enumerate(channels, 1):
        for e in streams[ch['tvg_id']]:
            key = hashlib.md5(e['url'].encode()).hexdigest()[:8]
            attrs = f'tvg-id="{ch["tvg_id"]}" tvg-name="{ch["tvg_id"]}-{key}" tvg-chno="{num}"'
            if ch.get('logo'):
                attrs += f' tvg-logo="{ch["logo"]}"'
            attrs += f' group-title="{ch["group"]}"'
            if e.get('ua'):
                attrs += f' http-user-agent="{e["ua"]}"'
            lines.append(f'#EXTINF:-1 {attrs},{ch["name_ua"]}')
            if e.get('ua'):
                lines.append(f'#EXTVLCOPT:http-user-agent={e["ua"]}')
            if e.get('ref'):
                lines.append(f'#EXTVLCOPT:http-referrer={e["ref"]}')
            lines.append(e['url'])

    write_if_changed('playlist.m3u', '\n'.join(lines) + '\n')
    with_streams = sum(1 for v in streams.values() if v)
    new = [c['name_ua'] for c in channels if c['group'] == NEW_GROUP]
    print(f'new: {new}')
    print(f'channels: {len(channels)}, with streams: {with_streams}, streams: {sum(map(len, streams.values()))}')


if __name__ == '__main__':
    main()
