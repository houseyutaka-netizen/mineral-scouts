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
    if host in {
        'crystalclassics.com', 'www.crystalclassics.com',
        'peanutsminerals.com', 'www.peanutsminerals.com',
        'arkfeldminerals.com', 'www.arkfeldminerals.com',
    }:
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


def auction_item(card, link, base, title=None, price_node=None, price_kind='listed', ends_at=''):
    text = price_node.get_text(' ', strip=True) if price_node is not None else ''
    amounts = list(dict.fromkeys(m.group().strip() for m in MONEY.finditer(text)))
    return {
        'title': title or link.get_text(' ', strip=True),
        'url': absolute_url(link.get('href'), base), 'date': '',
        'image': find_image(link, card, base), 'listing_type': 'auction',
        'availability': 'unknown', 'auction_status': 'listed',
        'ends_at': ends_at, 'price_kind': price_kind,
        'checked_at': now_iso(),
        **price_result(' / '.join(amounts), 'known' if amounts else 'unknown'),
    }


def parse_auction_page(kind, soup, base):
    items = []
    if kind == 'mineralauctions':
        for card in soup.select('.items .item[data-item]'):
            link = card.select_one('.name a[href]')
            if not link:
                continue
            end = card.select_one('[data-timestamp]')
            value = end.get('data-timestamp', '') if end else ''
            # タイムゾーン付きの時刻だけ採用。
            try:
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                value = stamp.isoformat() if stamp.tzinfo else ''
            except ValueError:
                value = ''
            price = card.select_one('.price')
            text = price.get_text(' ', strip=True).lower() if price else ''
            kind_price = 'current_bid' if 'current bid' in text else ('starting' if 'starting' in text else 'listed')
            items.append(auction_item(card, link, base, price_node=price, price_kind=kind_price, ends_at=value))
    elif kind == 'fam_auctions':
        for card in soup.select('.product.product-type-auction'):
            link = card.select_one('.wd-entities-title a[href]')
            if not link:
                continue
            date = card.select_one('.date_auction[data-yith-auction-time]')
            end = ''
            label = card.select_one('.ywcact_auction_end_start_label')
            if date and label and 'ends' in label.get_text().lower():
                try:
                    end = datetime.fromtimestamp(int(date['data-yith-auction-time']), timezone.utc).isoformat()
                except (ValueError, OverflowError, OSError):
                    pass
            price = card.select_one('.price')
            text = price.get_text(' ', strip=True).lower() if price else ''
            item = auction_item(card, link, base, price_node=price,
                                price_kind='current_bid' if 'current bid' in text else ('starting' if 'starting' in text else 'listed'), ends_at=end)
            if label and 'starts' in label.get_text().lower():
                item['auction_status'] = 'upcoming'
            items.append(item)
    elif kind == 'spirifer_auctions':
        for cell in soup.select('td.productList0, td.productList1'):
            row = cell.find_parent('tr')
            link = row.select_one('a[href^="/product,"]') if row else None
            title = cell.select_one('.productListName')
            cols = cell.select('td.p14')
            if not link or not title or len(cols) < 2:
                continue
            # この表の列順は Bids / Price / Time left。取り消し価格を除く。
            price = BeautifulSoup(str(cols[1]), 'html.parser')
            discount = price.select_one('.storeProductRed')
            if discount:
                price = discount
            for old in price.select('s, del, strike, .storeProductPrice2'):
                old.decompose()
            item = auction_item(row, link, base, title=title.get_text(' ', strip=True), price_node=price)
            bids = cols[0].get_text(strip=True)
            if bids.isdigit():
                item['bid_count'] = int(bids)
                item['price_kind'] = 'current_bid' if int(bids) else 'starting'
            items.append(item)
    elif kind == 'erocks_hot':
        # Hot Auction items の3分類だけ。Hot Fixed Priceや関連商品は対象外。
        hot = soup.select_one('#block-panels-mini-homepage-accordian-hot-items')
        if hot is None:
            raise ValueError('e-Rocks: Hot Auction items枠が見つかりません')
        for index, label in enumerate(('Watches', 'Views', 'Bids')):
            pane = hot.select_one(f'#quicktabs-tabpage-homepage_accordian_hot_items-{index}')
            if pane is None:
                raise ValueError(f'e-Rocks: {label}枠が見つかりません')
            for card in pane.select('li.list-item'):
                link = card.select_one('.item-title a[href^="/item/"]')
                if not link:
                    continue
                price = card.select_one('.field-name-field-itm-starting-price')
                # この枠の金額は現在入札額とは断定せず「掲載価格」とする。
                item = auction_item(card, link, base, price_node=price)
                item['hot_categories'] = [label]
                bids = card.select_one('.stat-bids')
                if bids:
                    match = re.search(r'(\d+)\s+bids?', bids.get_text(), re.I)
                    if match:
                        item['bid_count'] = int(match.group(1))
                items.append(item)
    else:
        raise ValueError(f'未知のオークション形式: {kind}')
    return items


def fetch_auctions(site, session):
    kind, url, visited, items = site['type'], site['url'], set(), []
    while url:
        if url in visited or len(visited) >= 20:
            raise ValueError('ページ送りを完了できませんでした')
        if visited:
            time.sleep(1.5)
        visited.add(url)
        response = session.get(url, timeout=25)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        page_items = parse_auction_page(kind, soup, response.url)
        if not page_items:
            # 制限ページや構造変更を0件成功と扱わず、前回分を未確認で保持。
            raise ValueError('オークション取得0件（開催なし、またはページ構造の変更）')
        items.extend(page_items)
        next_link = None
        if kind == 'mineralauctions':
            next_link = soup.select_one('.pagination a[rel="next"]')
        elif kind == 'fam_auctions':
            next_link = soup.select_one('a.next.page-numbers')
        url = absolute_url(next_link.get('href'), response.url) if next_link else ''
        if url and urlparse(url).hostname != urlparse(site['url']).hostname:
            raise ValueError('ページ送りのドメインが異なります')
    unique = {}
    for item in items:
        if not item['url'] or not item['title']:
            continue
        if item['url'] not in unique:
            unique[item['url']] = item
        else:
            old = unique[item['url']]
            old['hot_categories'] = list(dict.fromkeys(old.get('hot_categories', []) + item.get('hot_categories', [])))
            if 'bid_count' in item:
                old['bid_count'] = item['bid_count']
    return list(unique.values())


def retail_pages(site, session, response):
    visited = set()
    limit = int(site.get('max_pages', 200))
    while True:
        url = response.url
        if url in visited:
            raise ValueError('ページ送りが循環しています。前回データを維持します')
        visited.add(url)
        soup = BeautifulSoup(response.content, 'html.parser')
        if not soup.select(site.get('selector', 'a')):
            raise ValueError(f'商品が取得できないページ: {url}')
        print(f"ページ取得: {site['name']} {len(visited)}ページ目", flush=True)
        yield soup, url
        if site.get('paginate', True) is False:
            return
        selector = site.get('next_selector') or (
            'a[rel~="next"][href], .pagination a[aria-label="Next page"][href], '
            'a.next.page-numbers[href]'
        )
        link = soup.select_one(selector)
        if not link:
            return
        next_url = absolute_url(link.get('href'), url)
        if not next_url or urlparse(next_url).netloc != urlparse(url).netloc:
            raise ValueError('ページ送りのリンク先が不正です')
        if len(visited) >= limit:
            raise ValueError(f'ページ上限{limit}に達しました。max_pagesを確認してください')
        time.sleep(1.5)
        response = session.get(next_url, timeout=25)
        response.raise_for_status()


def fetch_items(site, session):
    if site.get('listing_type') == 'auction':
        return fetch_auctions(site, session)
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
        for soup, page_url in retail_pages(site, session, response):
            for tag in soup.select(site.get('selector', 'a')):
                url = absolute_url(tag.get('href'), page_url)
                if not url:
                    continue
                card = product_card(tag)
                title = tag.get_text(' ', strip=True) or card.get_text(' ', strip=True)
                if not title and tag.find('img'):
                    title = tag.find('img').get('alt', '').strip()
                if title:
                    items.append({
                        'title': title, 'url': url, 'date': '',
                        'image': find_image(tag, card, page_url),
                        'availability': detect_status(card, page_url),
                        **extract_price(card, page_url),
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
    state = load_json(STATE, {'urls': []})
    known_urls = set(state.get('urls', []))
    scan_started = now_iso()
    previous = load_json(OUTPUT, {'items': []})
    previous_by_url = {i['url']: i for i in previous.get('items', []) if i.get('url')}
    # 同じ巡回で発見した商品は同時刻。販売店の巡回順で新旧を作らない。
    # 旧データには日時がないため、前回更新日時を移行時の基準にする。
    baseline = previous.get('updated') or '1970-01-01T00:00:00+00:00'
    first_seen = dict(state.get('first_seen', {}))
    for url in known_urls | set(previous_by_url):
        first_seen.setdefault(url, previous_by_url.get(url, {}).get('first_seen_at') or baseline)
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
                item['first_seen_at'] = first_seen.setdefault(url, scan_started)
                item.update(listing_type=site.get('listing_type', 'retail'), site=site['name'], id=hashlib.sha256(url.encode()).hexdigest()[:16], stale=False)
                results.append(item)
                if url not in known_urls and url not in current_urls:
                    new_items.append(item)
                current_urls.add(url)
            counts = {s: sum(i['availability'] == s for i in items) for s in ('available', 'sold_out', 'unknown')}
            images = sum(bool(i['image']) for i in items)
            prices = sum(i.get('price_status') == 'known' for i in items)
            if site.get('listing_type') == 'auction':
                print(f"OK: {site['name']} オークション{len(items)}件 / 画像{images}件 / 価格{prices}件")
            else:
                print(f"OK: {site['name']} {len(items)}件 / 画像{images}件 / 価格{prices}件 / 販売中{counts['available']}件 / SOLD OUT {counts['sold_out']}件 / 未確認{counts['unknown']}件")
        except Exception as exc:
            print(f"ERROR: {site['name']}: {exc}")
            errors.append(site['name'])
            for old in previous.get('items', []):
                if old.get('site') == site['name']:
                    results.append({**old, 'first_seen_at': first_seen.get(old.get('url'), baseline), 'listing_type': site.get('listing_type', 'retail'), 'availability': 'unknown', 'auction_status': 'unknown', 'stale': True, 'price_cached': True})
    if not successful_sites:
        raise RuntimeError('全サイトで取得失敗。保存済みデータを維持します。')
    if not known_urls:
        new_items = []
    save_json(STATE, {'urls': sorted(known_urls | current_urls), 'first_seen': first_seen})
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
