import json
import re
import os
import requests
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from playwright.sync_api import sync_playwright

# --- AYARLAR ---
FULL_SCRAPE_MODE = True  # True: Tüm oyunları sıfırdan ve güncel olarak tarar
HEADLESS_MODE = os.getenv("GITHUB_ACTIONS", "false").lower() == "true"


def load_existing_source():
    """Var olan source.json dosyasını okur."""
    if os.path.exists("source.json") and not FULL_SCRAPE_MODE:
        try:
            with open("source.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                downloads = data.get("downloads", [])
                
                url_dict = {}
                for item in downloads:
                    url_dict[item.get("title")] = item
                return url_dict
        except Exception as e:
            print(f"Mevcut source.json okunamadı: {e}")
    return {}


def get_all_steamrip_games(page):
    """SteamRIP sitemap veya listesinden oyun linklerini toplar."""
    print("SteamRIP oyun listesi çekiliyor...")
    game_links = set()

    sitemap_urls = [
        "https://steamrip.com/post-sitemap.xml",
        "https://steamrip.com/post-sitemap1.xml",
        "https://steamrip.com/post-sitemap2.xml"
    ]
    
    for s_url in sitemap_urls:
        try:
            response = requests.get(s_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                for elem in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc'):
                    url = elem.text.strip()
                    if url and not any(x in url for x in ["/category/", "/tag/", "/games-list", "/faq", "/discord"]):
                        game_links.add(url)
        except Exception:
            pass

    if not game_links:
        try:
            page.goto("https://steamrip.com/games-list/", wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(2000)
            extracted = page.evaluate("""
                () => {
                    const anchors = Array.from(document.querySelectorAll("a[href]"));
                    return anchors
                        .map(a => a.href)
                        .filter(href => 
                            href.includes("steamrip.com/") && 
                            !href.includes("/category/") && 
                            !href.includes("/tag/") && 
                            !href.includes("/games-list") && 
                            !href.includes("/page/") && 
                            !href.includes("/faq") && 
                            !href.includes("/discord") &&
                            !href.endsWith("steamrip.com/")
                        );
                }
            """)
            game_links.update(extracted)
        except Exception as e:
            print(f"Listeden tarama hatası: {e}")

    unique_links = list(game_links)
    print(f"Toplam {len(unique_links)} adet SteamRIP oyunu bulundu.")
    return unique_links


def scrape_game_details(context, page, game_url, existing_dict):
    """Oyun detaylarını resmi SteamRIP JSON formatına %100 uyumlu şekilde çeker."""
    try:
        page.goto(game_url, wait_until="domcontentloaded", timeout=25000)

        # 1. Ham Başlık (Resmi kaynakla birebir aynı format)
        title_el = page.query_selector("h1.entry-title") or page.query_selector("h1")
        raw_title = title_el.inner_text().strip() if title_el else ""

        if not raw_title:
            return None

        # 2. Güncelleme Tarihi Tespiti
        date_str = ""
        date_el = page.query_selector("time.updated") or page.query_selector("time.entry-date") or page.query_selector("meta[property='article:modified_time']")
        if date_el:
            date_str = date_el.get_attribute("datetime") or date_el.get_attribute("content") or date_el.inner_text().strip()

        # ISO format düzenleme (+00:00)
        formatted_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        if date_str:
            try:
                clean_date = date_str.split(".")[0].replace("Z", "")
                if "T" in clean_date:
                    formatted_date = f"{clean_date}+00:00" if "+" not in clean_date else clean_date
            except Exception:
                pass

        # 3. Dosya Boyutu Tespiti
        file_size = page.evaluate("""
            () => {
                const elements = Array.from(document.querySelectorAll("p, div, li, span, td"));
                for (const el of elements) {
                    const txt = el.innerText || '';
                    if (/game\\s*size|file\\s*size/i.test(txt)) {
                        const match = txt.match(/(?:Game Size|File Size|Size)\\s*[:\\-–]?\\s*([0-9.,]+\\s*(?:GB|MB))/i);
                        if (match) return match[1].trim().toUpperCase();
                    }
                }
                const bodyText = document.body.innerText;
                const fallbackMatch = bodyText.match(/Game\\s*Size\\s*[:\\-–]?\\s*([0-9.,]+\\s*(?:GB|MB))/i) || bodyText.match(/File\\s*Size\\s*[:\\-–]?\\s*([0-9.,]+\\s*(?:GB|MB))/i);
                return fallbackMatch ? fallbackMatch[1].trim().toUpperCase() : "N/A";
            }
        """) or "N/A"

        # 4. İndirme Bağlantılarını Çek
        all_uris = []
        links = page.query_selector_all("a[href]")
        for link in links:
            href = link.get_attribute("href") or ""
            
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://steamrip.com" + href

            if any(host in href for host in ["bzzhr.to", "buzzheavier.com", "gofile.io", "pixeldrain.com", "qiwi.gg", "megadb.net", "megadb.xyz"]):
                # Gofile URL temizliği
                if "gofile.io" in href:
                    href = href.split('?')[0]
                all_uris.append(href)

        # Filtreleme
        clean_resolved_uris = []
        junk_domains = ["ankergames.net", "steamrip.com", "doubleclick", "google.com", "yandex", "facebook.com"]

        for r_uri in set(all_uris):
            r_uri_clean = r_uri.strip()
            r_uri_lower = r_uri_clean.lower()

            if not (r_uri_lower.startswith("http://") or r_uri_lower.startswith("https://")):
                continue

            if any(junk in r_uri_lower for junk in junk_domains):
                continue

            clean_resolved_uris.append(r_uri_clean)

        clean_resolved_uris = list(dict.fromkeys(clean_resolved_uris))

        if not clean_resolved_uris:
            return None

        # Resmi şema ile birebir uyumlu çıktı
        return {
            "title": raw_title,
            "uploadDate": formatted_date,
            "fileSize": file_size,
            "uris": clean_resolved_uris
        }

    except Exception as err:
        print(f"   -> Hata ({game_url}): {err}")
        return None


def run_scraper():
    existing_dict = load_existing_source()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS_MODE,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        target_urls = get_all_steamrip_games(page)

        if not target_urls:
            print("Taranacak oyun bulunamadı.")
            browser.close()
            return

        downloads = []
        total_count = len(target_urls)
        print(f"\nTÜM OYUNLAR İÇİN DÜZELTİLMİŞ TARAMA BAŞLATILIYOR (Toplam: {total_count} oyun)...\n")

        for idx, url in enumerate(target_urls, 1):
            print(f"[{idx}/{total_count}] {url}")
            game_data = scrape_game_details(context, page, url, existing_dict)
            if game_data:
                downloads.append(game_data)
                print(f"   -> BAŞARILI: {game_data['title']} | Boyut: {game_data['fileSize']} | Link Sayısı: {len(game_data['uris'])}\n")
            else:
                print("   -> İndirme linki bulunamadı.\n")

            if idx >= 20:  # 20 oyundan sonra durdur ve test et
                break

        browser.close()

    if not downloads:
        print("\n[!] UYARI: Veri çekilemedi.")
        return

    source_data = {
        "name": "SteamRIP",
        "downloads": downloads
    }

    with open("source.json", "w", encoding="utf-8") as f:
        json.dump(source_data, f, ensure_ascii=False, indent=2)

    print(f"\nTEST TARAMASI BİTTİ! Toplam {len(downloads)} oyun 'source.json' dosyasına yazıldı.")


if __name__ == "__main__":
    run_scraper()