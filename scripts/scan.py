import json
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sites.yml"
STATE = ROOT / "data" / "seen.json"
OUTPUT = ROOT / "docs" / "data" / "latest.json"


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


def image_url(img, base_url):
    if img is None:
        return ""

    for key in ("data-src", "data-original", "src"):
        value = img.get(key, "").strip()
        if value and not value.startswith("data:"):
            url = urljoin(base_url, value)
            if url.startswith(("http://", "https://")):
                return url

    for key in ("data-srcset", "srcset"):
        value = img.get(key, "").strip()
        if value:
            source = value.split(",")[0].strip().split()[0]
            url = urljoin(base_url, source)
            if url.startswith(("http://", "https://")):
                return url

    return ""


def find_image(tag, base_url):
    # AMF・PEANUTSなど、リンクの中にある画像
    for img in tag.find_all("img"):
        url = image_url(img, base_url)
        if url:
            return url

    # 商品名リンクと画像が別になっている商品カード
    for parent in tag.parents:
        classes = set(parent.get("class", []))
        if classes.intersection({
            "item",
            "card",
            "card-wrapper",
            "product-card-wrapper",
            "itemBox",
            "item-box",
            "product",
        }):
            for img in parent.find_all("img"):
                url = image_url(img, base_url)
                if url:
                    return url
            return ""

    return ""


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
            image = ""
            media = (
                entry.get("media_thumbnail", [])
                or entry.get("media_content", [])
            )
            if media:
                image = urljoin(
                    response.url, media[0].get("url", "")
                )
            items.append({
                "title": entry.get("title", "Untitled"),
                "url": entry.get("link", ""),
                "date": entry.get("published", ""),
                "image": image,
            })
    else:
        soup = BeautifulSoup(response.content, "html.parser")

        for tag in soup.select(site.get("selector", "a")):
            link = tag.get("href")
            title = tag.get_text(" ", strip=True)

            if not title:
                card = tag.find_parent(class_="item")
                if card:
                    title = card.get_text(" ", strip=True)
                else:
                    img = tag.find("img")
                    if img:
                        title = img.get("alt", "").strip()

            if link and title:
                items.append({
                    "title": title,
                    "url": urljoin(response.url, link),
                    "date": "",
                    "image": find_image(tag, response.url),
                })

    # 同じ商品へのリンクが複数ある場合はまとめる
    unique = {}
    for item in items:
        url = item["url"].strip()
        if not url.startswith(("http://", "https://")):
            continue
        item["url"] = url
        if url not in unique:
            unique[url] = item
        elif not unique[url].get("image") and item.get("image"):
            unique[url]["image"] = item["image"]

    return list(unique.values())


def main():
    config = yaml.safe_load(
        CONFIG.read_text(encoding="utf-8")
    ) or {}
    seen = load_json(STATE, {"urls": []})
    known_urls = set(seen.get("urls", []))
    current_urls = set()
    results = []
    new_items = []

    for site in config.get("sites", []):
        if not site.get("enabled", False):
            continue

        try:
            items = fetch_items(site)

            if not items:
                raise ValueError(
                    "取得0件。アクセス制限やselectorを確認してください"
                )

            for item in items:
                url = item["url"]
                item["site"] = site["name"]
                item["id"] = hashlib.sha256(
                    url.encode("utf-8")
                ).hexdigest()[:16]
                results.append(item)

                if url not in known_urls and url not in current_urls:
                    new_items.append(item)
                current_urls.add(url)

            image_count = sum(
                bool(item.get("image")) for item in items
            )
            print(
                f"OK: {site['name']} "
                f"{len(items)}件 / 画像付き{image_count}件"
            )
        except Exception as exc:
            print(f"ERROR: {site['name']}: {exc}")

    if not results:
        raise RuntimeError(
            "全サイトで取得できませんでした。保存済みデータを維持します。"
        )

    first_run = not known_urls
    if first_run:
        new_items = []

    save_json(STATE, {
        "urls": sorted(known_urls | current_urls)
    })
    save_json(OUTPUT, {
        "updated": datetime.now(timezone.utc).isoformat(),
        "items": results,
    })

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic and new_items and not first_run:
        for item in new_items[:20]:
            message = (
                f"{item['site']}: {item['title']}\n{item['url']}"
            )
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
        f"完了: {len(results)}件確認、"
        f"新規候補 {len(new_items)}件"
    )


if __name__ == "__main__":
    main()
