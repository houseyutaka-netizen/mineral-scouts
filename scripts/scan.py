import json
import os
import re
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config' / 'sites.yml'
STATE = ROOT / 'data' / 'seen.json'
OUTPUT = ROOT / 'docs' / 'data' / 'latest.json'
# AMFの商品ページへの追加アクセスは1巡回20件まで。取得済みは7日再利用。
DETAIL_LIMIT = 20
PRICE_CACHE_DAYS = 7
SOLD = re.compile(r'sold[\s_-]*out|out\s+of\s+stock|売り切れ|売切れ|完売|在庫なし', re.I)
AVAILABLE = re.compile(r'\bin\s+stock\b|在庫あり|カートに入れる|add\s+to\s+(?:cart|basket)', re.I)
PRICE = re.compile(r'[¥￥$£€]\s*[\d,]+')
MONEY = re.compile(
    r'(?:US\$|HK\$|CA\$|AU\$|[¥￥$£€]|USD|JPY|GBP|EUR)\s*'
    r'\d[\d,]*(?:\.\d{1,2})?(?:\s*(?:USD|JPY|GBP|EUR))?'
    r'|\d[\d,]*(?:\.\d{1,2})?\s*(?:円|USD|JPY|GBP|EUR)', re.I
)
INQUIRY = re.compile(r'\bPOR\b|price\s+(?:on|upon)\s+request|価格.*(?:問合せ|問い合わせ)|価格応相談', re.I)
CARD_CLASSES = {'item', 'card', 'card-wrapper', 'product-card-wrapper', 'itemBox', 'item-box', 'item-box-wrapper', 'product'}
PRICE_KEYS = ('price_text', 'price_status', 'price_checked_at', 'price_source')


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def absolute_url(value, base):
    if not isinstance(value, str) or not value.strip():
        return ''
    url = urljoin(base, value.strip())
    return url if url.startswith(('http://', 'https://')) else ''


def image_url(img, base):
    for key in ('data-src', 'data-original', 'src'):
        value = img.get(key, '').strip()
        if value and not value.startswith('data:'):
            url = absolute_url(value, base)
            if url:
                return url
    for key in ('data-srcset', 'srcset'):
        parts = img.get(key, '').split(',')[0].strip().split()
        if parts:
            url = absolute_url(parts[0], base)
            if url:
                return url
    return ''


def product_card(tag):
    for parent in tag.parents:
        if set(parent.get('class', [])).intersection(CARD_CLASSES):
            return parent
    return tag


def find_image(tag, card, base):
    for scope in (tag, card):
        for img in scope.find_all('img'):
            url = image_url(img, base)
            if url:
                return url
    return ''


def detect_status(card, site_url):
    text = card.get_text(' ', strip=True)
    image_labels = ' '.join(img.get('alt', '') for img in card.find_all('img'))
    class_labels = ' '.join(' '.join(n.get('class', [])) for n in [card, *card.find_all(True)])
    if SOLD.search(f'{text} {image_labels} {class_labels}'):
        return 'sold_out'
    if re.search(r'\breserved\b|on\s+hold|取り置き|商談中|予約販売|pre[\s-]?order', text, re.I):
        return 'unknown'
    host = urlparse(site_url).hostname or ''
    if host in {'fineartminerals.com', 'www.fineartminerals.com'}:
        classes = set(card.get('class', []))
        if 'outofstock' in classes or re.search(r'\bsold\b', text, re.I):
            return 'sold_out'
        if classes.intersection({'onhold', 'on-hold'}):
            return 'unknown'
        return 'available' if 'instock' in classes else 'unknown'
    if AVAILABLE.search(text):
        return 'available'
    if host in {'amf7.com', 'www.amf7.com'}:
        heading = card.find('h4')
        if heading and heading.get_text(strip=True) and card.find('a', href=True) and card.find('img'):
            return 'available'
    if host in {'crystalclassics.com', 'www.crystalclassics.com', 'peanutsminerals.com', 'www.peanutsminerals.com'}:
        if PRICE.search(text):
            return 'available'
    return 'unknown'


def price_result(text='', status='unknown', source='listing'):
    return {'price_text': text, 'price_status': status,
            'price_checked_at': now_iso(), 'price_source': source,
            'price_cached': False}


def extract_price(card, page_url, detail=False):
    host = urlparse(page_url).hostname or ''
    source = 'detail' if detail else 'listing'
    # 必ず商品枠内で検索。AMFの詳細ページでは .price だけを対象にする。
    if not detail and INQUIRY.search(card.get_text(' ', strip=True)):
        return price_result(status='inquiry', source=source)
    nodes = card.select('.price, .itemPrice, .item-price, .item_price')
    if not nodes and not detail and host in {'peanutsminerals.com', 'www.peanutsminerals.com'}:
        nodes = [card]
    amounts = []
    for node in nodes:
        # Shopifyの通常価格・セール価格は両方HTMLに存在する。
        if node.select_one('.price__container'):
            selector = '.price__sale .price-item--sale' if 'price--on-sale' in node.get('class', []) else '.price__regular .price-item--regular'
            node = node.select_one(selector)
            if node is None:
                continue
        clone = BeautifulSoup(str(node), 'html.parser')
        for obsolete in clone.select('del, s, .visually-hidden, .screen-reader-text, script, style'):
            obsolete.decompose()
        text = clone.get_text(' ', strip=True)
        if INQUIRY.search(text):
            return price_result(status='inquiry', source=source)
        for match in MONEY.finditer(text):
            amount = re.sub(r'\s+', ' ', match.group()).strip()
            if amount not in amounts:
                amounts.append(amount)
    if amounts:
        # 表示通貨をそのまま保持。$を勝手にUSDと解釈しない。
        if detail and len(amounts) > 1:
            return price_result('複数価格（販売ページ参照）', 'multiple', source)
        return price_result(' / '.join(amounts), 'known', source)
    return price_result(source=source)


def fetch_items(site, session):
    response = session.get(site['url'], timeout=25)
    response.raise_for_status()
    items = []
    if site.get('type') == 'rss':
        for entry in feedparser.parse(response.content).entries:
            media = entry.get('media_thumbnail', []) or entry.get('media_content', [])
            items.append({
                'title': entry.get('title', 'Untitled'),
                'url': absolute_url(entry.get('link', ''), response.url),
                'date': entry.get('published', ''),
                'image': absolute_url(media[0].get('url', ''), response.url) if media else '',
                'availability': 'unknown', **price_result(),
            })
    else:
        soup = BeautifulSoup(response.content, 'html.parser')
        for tag in soup.select(site.get('selector', 'a')):
            url = absolute_url(tag.get('href'), response.url)
            if not url:
                continue
            card = product_card(tag)
            title = tag.get_text(' ', strip=True) or card.get_text(' ', strip=True)
            if not title and tag.find('img'):
                title = tag.find('img').get('alt', '').strip()
            if title:
                items.append({
                    'title': title, 'url': url, 'date': '',
                    'image': find_image(tag, card, response.url),
                    'availability': detect_status(card, response.url),
                    **extract_price(card, response.url),
                })
    unique = {}
    priority = {'unknown': 0, 'available': 1, 'sold_out': 2}
    for item in items:
        url = item['url']
        if not url:
            continue
        if url not in unique:
            unique[url] = item
            continue
        old = unique[url]
        if not old['image']:
            old['image'] = item['image']
        if priority[item['availability']] > priority[old['availability']]:
            old['availability'] = item['availability']
        if old['price_status'] == 'unknown' and item['price_status'] != 'unknown':
            old.update({k: item[k] for k in PRICE_KEYS})
    return list(unique.values())


def price_age(item):
    try:
        checked = datetime.fromisoformat(item['price_checked_at'])
        return (datetime.now(timezone.utc) - checked).total_seconds() / 86400
    except (KeyError, ValueError, TypeError):
        return float('inf')


def enrich_amf(items, previous_by_url, session):
    pending = []
    for item in items:
        old = previous_by_url.get(item['url'], {})
        if old.get('price_source') == 'detail':
            item.update({k: old[k] for k in PRICE_KEYS if k in old})
            item['price_cached'] = True
        if price_age(old) >= PRICE_CACHE_DAYS or old.get('price_source') != 'detail':
            pending.append(item)
    # 販売中で未取得の商品を優先。その後、古い価格から順に更新。
    pending.sort(key=lambda i: (i['availability'] != 'available', i.get('price_checked_at', '') if i.get('price_source') == 'detail' else ''))
    checked = 0
    for item in pending[:DETAIL_LIMIT]:
        time.sleep(1.5)
        try:
            response = session.get(item['url'], timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            price = extract_price(soup, response.url, detail=True)
            # 予期せぬ制限ページ等で既存価格を消さない。
            if price['price_status'] != 'unknown' or not item.get('price_text'):
                item.update(price)
            checked += 1
        except requests.RequestException as exc:
            print(f"価格取得エラー: {item['url']}: {exc}")
            if isinstance(exc, requests.HTTPError) and exc.response.status_code in (403, 429):
                break
    print(f'AMF詳細価格: 今回{checked}ページ確認 / 今回対象外{max(0, len(pending)-checked)}件')


def main():
    config = yaml.safe_load(CONFIG.read_text(encoding='utf-8')) or {}
    known_urls = set(load_json(STATE, {'urls': []}).get('urls', []))
    previous = load_json(OUTPUT, {'items': []})
    previous_by_url = {i['url']: i for i in previous.get('items', []) if i.get('url')}
    current_urls, results, new_items, errors = set(), [], [], []
    successful_sites = 0
    session = requests.Session()
    session.headers.update({'User-Agent': 'MineralScout/1.0'})
    for site in config.get('sites', []):
        if not site.get('enabled', False):
            continue
        try:
            items = fetch_items(site, session)
            if not items:
                raise ValueError('取得0件。アクセス制限やselectorを確認')
            if urlparse(site['url']).hostname in {'amf7.com', 'www.amf7.com'}:
                enrich_amf(items, previous_by_url, session)
            successful_sites += 1
            for item in items:
                url = item['url']
                item.update(site=site['name'], id=hashlib.sha256(url.encode()).hexdigest()[:16], stale=False)
                results.append(item)
                if url not in known_urls and url not in current_urls:
                    new_items.append(item)
                current_urls.add(url)
            counts = {s: sum(i['availability'] == s for i in items) for s in ('available', 'sold_out', 'unknown')}
            images = sum(bool(i['image']) for i in items)
            prices = sum(i.get('price_status') == 'known' for i in items)
            print(f"OK: {site['name']} {len(items)}件 / 画像{images}件 / 価格{prices}件 / 販売中{counts['available']}件 / SOLD OUT {counts['sold_out']}件 / 未確認{counts['unknown']}件")
        except Exception as exc:
            print(f"ERROR: {site['name']}: {exc}")
            errors.append(site['name'])
            for old in previous.get('items', []):
                if old.get('site') == site['name']:
                    results.append({**old, 'availability': 'unknown', 'stale': True, 'price_cached': True})
    if not successful_sites:
        raise RuntimeError('全サイトで取得失敗。保存済みデータを維持します。')
    if not known_urls:
        new_items = []
    save_json(STATE, {'urls': sorted(known_urls | current_urls)})
    save_json(OUTPUT, {'updated': now_iso(), 'errors': errors, 'items': results})
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    if topic and known_urls:
        for item in new_items[:20]:
            try:
                session.post(f'https://ntfy.sh/{topic}', data=f"{item['site']}: {item['title']}\n{item['url']}".encode(), headers={'Title': 'Mineral Scout'}, timeout=15).raise_for_status()
            except requests.RequestException as exc:
                print(f'通知エラー: {exc}')
    print(f'完了: {len(results)}件保存、新規候補 {len(new_items)}件')


if __name__ == '__main__':
    main()
