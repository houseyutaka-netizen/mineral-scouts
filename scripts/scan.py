import json
import os
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sites.yml"
STATE = ROOT / "data" / "seen.json"
OUTPUT = ROOT / "docs" / "data" / "latest.json"

SOLD = re.compile(
    r"sold[\s_-]*out|out\s+of\s+stock|売り切れ|売切れ|完売|在庫なし",
    re.I,
)
AVAILABLE = re.compile(
    r"\bin\s+stock\b|在庫あり|カートに入れる|add\s+to\s+(?:cart|basket)",
    re.I,
)
PRICE = re.compile(r"[¥￥$£€]\s*[\d,]+")
CARD_CLASSES = {
    "item", "card", "card-wrapper", "product-card-wrapper",
    "itemBox", "item-box", "item-box-wrapper", "product",
}


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def absolute_url(value, base):
    if not isinstance(value, str) or not value.strip():
        return ""
    url = urljoin(base, value.strip())
    return url if url.startswith(("http://", "https://")) else ""


def image_url(img, base):
    for key in ("data-src", "data-original", "src"):
        value = img.get(key, "").strip()
        if value and not value.startswith("data:"):
            url = absolute_url(value, base)
            if url:
                return url

    for key in ("data-srcset", "srcset"):
        value = img.get(key, "").strip()
        if value:
            parts = value.split(",")[0].strip().split()
            if parts:
                url = absolute_url(parts[0], base)
                if url:
                    return url
    return ""


def product_card(tag):
    for parent in tag.parents:
        if set(parent.get("class", [])).intersection(CARD_CLASSES):
            return parent
    # 商品画像・商品名・価格が同じリンク内にある場合
    return tag


def find_image(tag, card, base):
    for scope in (tag, card):
        for img in scope.find_all("img"):
            url = image_url(img, base)
            if url:
                return url
    return ""


def detect_status(card, site_url):
    text = card.get_text(" ", strip=True)

    # 売り切れ画像のalt・商品枠内のクラス名も確認
    image_labels = " ".join(
        img.get("alt", "") for img in card.find_all("img")
    )
    class_labels = " ".join(
        " ".join(node.get("class", []))
        for node in [card, *card.find_all(True)]
    )
    evidence = f"{text} {image_labels} {class_labels}"

    if SOLD.search(evidence):
        return "sold_out"

    # 予約・取り置き等を販売中に混ぜない
    if re.search(
        r"\breserved\b|on\s+hold|取り置き|商談中|予約販売|pre[\s-]?order",
        text,
        re.I,
    ):
        return "unknown"

    if AVAILABLE.search(text):
        return "available"

    host = urlparse(site_url).hostname or ""
    
        if host in {"fineartminerals.com", "www.fineartminerals.com"}:
        classes = set(card.get("class", []))
        if "outofstock" in classes:
            return "sold_out"
        if "onhold" in classes or "on-hold" in classes:
            return "unknown"
        if re.search(r"\bsold\b", text, re.I):
            return "sold_out"
        if "instock" in classes:
            return "available"
        return "unknown"

    # AMFは商品枠の見出しが鉱物名、売切れ時はSOLD OUTになる形式。
    if host in {"amf7.com", "www.amf7.com"}:
        heading = card.find("h4")
        if (
            heading
            and heading.get_text(strip=True)
            and card.find("a", href=True)
            and card.find("img")
        ):
            return "available"

    # この2店は、売切れ表示がなく価格を掲載した商品枠を販売中と推定。
    if host in {
        "crystalclassics.com", "www.crystalclassics.com",
        "peanutsminerals.com", "www.peanutsminerals.com",
    }:
        if PRICE.search(text):
            return "available"

    return "unknown"


def fetch_items(site):
    response = requests.get(
        site["url"],
        headers={"User-Agent": "MineralScout/1.0"},
        timeout=25,
    )
    response.raise_for_status()
    items = []

    if site.get("type") == "rss":
        feed = feedparser.parse(response.content)
        for entry in feed.entries:
            media = (
                entry.get("media_thumbnail", [])
                or entry.get("media_content", [])
            )
            image = ""
            if media:
                image = absolute_url(
                    media[0].get("url", ""), response.url
                )
            items.append({
                "title": entry.get("title", "Untitled"),
                "url": absolute_url(entry.get("link", ""), response.url),
                "date": entry.get("published", ""),
                "image": image,
                "availability": "unknown",
            })
    else:
        soup = BeautifulSoup(response.content, "html.parser")
        for tag in soup.select(site.get("selector", "a")):
            url = absolute_url(tag.get("href"), response.url)
            if not url:
                continue

            card = product_card(tag)
            title = tag.get_text(" ", strip=True)
            if not title:
                title = card.get_text(" ", strip=True)
            if not title:
                img = tag.find("img")
                if img:
                    title = img.get("alt", "").strip()
            if not title:
                continue

            items.append({
                "title": title,
                "url": url,
                "date": "",
                "image": find_image(tag, card, response.url),
                "availability": detect_status(card, response.url),
            })

    unique = {}
    priority = {"unknown": 0, "available": 1, "sold_out": 2}
    for item in items:
        url = item["url"]
        if not url:
            continue
        if url not in unique:
            unique[url] = item
            continue

        existing = unique[url]
        if not existing["image"] and item["image"]:
            existing["image"] = item["image"]
        if priority[item["availability"]] > priority[existing["availability"]]:
            existing["availability"] = item["availability"]

    return list(unique.values())


def main():
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    seen = load_json(STATE, {"urls": []})
    known_urls = set(seen.get("urls", []))
    previous = load_json(OUTPUT, {"items": []})

    current_urls = set()
    results = []
    new_items = []
    errors = []
    successful_sites = 0

    for site in config.get("sites", []):
        if not site.get("enabled", False):
            continue

        try:
            items = fetch_items(site)
            if not items:
                raise ValueError("取得0件。アクセス制限やselectorを確認")

            successful_sites += 1
            for item in items:
                url = item["url"]
                item["site"] = site["name"]
                item["id"] = hashlib.sha256(
                    url.encode("utf-8")
                ).hexdigest()[:16]
                item["stale"] = False
                results.append(item)

                if url not in known_urls and url not in current_urls:
                    new_items.append(item)
                current_urls.add(url)

            counts = {
                status: sum(
                    item["availability"] == status for item in items
                )
                for status in ("available", "sold_out", "unknown")
            }
            images = sum(bool(item["image"]) for item in items)
            print(
                f"OK: {site['name']} {len(items)}件 / "
                f"画像{images}件 / "
                f"販売中{counts['available']}件 / "
                f"SOLD OUT {counts['sold_out']}件 / "
                f"未確認{counts['unknown']}件"
            )
        except Exception as exc:
            print(f"ERROR: {site['name']}: {exc}")
            errors.append(site["name"])

            # 一部サイトが失敗しても以前の商品を消さない。
            # 古い在庫情報は「状況未確認」に戻す。
            for old in previous.get("items", []):
                if old.get("site") == site["name"]:
                    item = dict(old)
                    item["availability"] = "unknown"
                    item["stale"] = True
                    results.append(item)

    if not successful_sites:
        raise RuntimeError(
            "全サイトで取得失敗。保存済みデータを維持します。"
        )

    first_run = not known_urls
    if first_run:
        new_items = []

    save_json(STATE, {"urls": sorted(known_urls | current_urls)})
    save_json(OUTPUT, {
        "updated": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
        "items": results,
    })

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic and new_items and not first_run:
        for item in new_items[:20]:
            message = f"{item['site']}: {item['title']}\n{item['url']}"
            try:
                requests.post(
                    f"https://ntfy.sh/{topic}",
                    data=message.encode("utf-8"),
                    headers={"Title": "Mineral Scout"},
                    timeout=15,
                ).raise_for_status()
            except Exception as exc:
                print(f"通知エラー: {exc}")

    print(
        f"完了: {len(results)}件保存、"
        f"新規候補 {len(new_items)}件"
    )


if __name__ == "__main__":
    main()
