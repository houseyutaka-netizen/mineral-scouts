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
