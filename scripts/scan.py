scripts/scan.py
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


def fetch_items(site):
    headers = {"User-Agent": "MineralScout/1.0"}
    response = requests.get(
        site["url"], headers=headers, timeout=25
    )
    response.raise_for_status()

    items = []
    if site.get("type") == "rss":
        feed = feedparser.parse(response.content)
        for entry in feed.entries:
            link = entry.get("link", "")
            items.append({
                "title": entry.get("title", "Untitled"),
                "url": link,
                "date": entry.get("published", ""),
            })
    else:
        soup = BeautifulSoup(response.content, "html.parser")
        selector = site.get("selector", "a")
        for tag in soup.select(selector):
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
                    "url": urljoin(site["url"], link),
                    "date": "",
                })

    return items


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
            for item in fetch_items(site):
                url = item.get("url", "").strip()
                if not url.startswith(("http://", "https://")):
                    continue

                item["site"] = site["name"]
                item["id"] = hashlib.sha256(
                    url.encode("utf-8")
                ).hexdigest()[:16]
                current_urls.add(url)
                results.append(item)

                if url not in known_urls:
                    new_items.append(item)

            print(f"OK: {site['name']}")
        except Exception as exc:
            print(f"ERROR: {site['name']}: {exc}")

    # 初回は既存商品を通知せず、基準データとして登録する
    first_run = not STATE.exists()
    if first_run:
        new_items = []

    save_json(STATE, {
        "urls": sorted(known_urls | current_urls)
    })
    save_json(OUTPUT, {
        "updated": datetime.now(timezone.utc).isoformat(),
        "items": results[:500],
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
                    headers={"Title": "新しい鉱物標本"},
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
