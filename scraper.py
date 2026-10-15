import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import requests
from playwright.async_api import async_playwright

# Logların GitHub Actions ve terminale anlık akmasını sağlar
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

# --- AYARLAR ---
MAX_CONCURRENT_TASKS = 5  # Aynı anda paralel çalışacak sekme (worker) sayısı
FULL_SCRAPE_MODE = True

HEADLESS_MODE = os.getenv("GITHUB_ACTIONS", "false").lower() == "true" and os.getenv("DISPLAY") is None
FORCE_REFRESH = os.getenv("FORCE_REFRESH", "false").lower() == "true"


def load_existing_source():
    """Var olan source.json dosyasını okuyarak URL hafızasına alır."""
    if FORCE_REFRESH:
        print("\n[!] FORCE_REFRESH AKTİF: Eski source.json verileri yok sayılıyor, tüm oyunlar sıfırdan taranacak...\n")
        return {}

    if os.path.exists("source.json"):
        try:
            with open("source.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                downloads = data.get("downloads", [])
                
                url_dict = {}
                for item in downloads:
                    match = re.search(r'href=["\'](https?://[^"\']+)["\']', item.get("descriptionHtml", ""))
                    if match:
                        game_url = match.group(1).rstrip('/')
                        url_dict[game_url] = item
                return url_dict
        except Exception as e:
            print(f"Mevcut source.json okunamadı: {e}")
    return {}


async def get_all_steamrip_games(page):
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
            response = requests.get(s_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=10)
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
            await page.goto("https://steamrip.com/games-list/", wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(2000)
            extracted = await page.evaluate("""
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


def resolve_pixeldrain_direct_link(raw_url):
    """Pixeldrain linklerini doğrudan API indirme adresine çevirir."""
    match = re.search(r'pixeldrain\.com/u/([a-zA-Z0-9]+)', raw_url)
    if match:
        file_id = match.group(1)
        return f"https://pixeldrain.com/api/file/{file_id}?download"
    return raw_url


async def resolve_bzzhr_direct_link(context, raw_url, referer_url):
    """BZZHR indirme sayfasındaki doğrudan ts.bzzhr.to CDN adresini yakalar."""
    sub_page = None
    direct_link = None
    try:
        sub_page = await context.new_page()

        def handle_response(response):
            nonlocal direct_link
            hx_redirect = response.headers.get("hx-redirect")
            if hx_redirect and ("ts.bzzhr.to" in hx_redirect or "/d/" in hx_redirect or "buzzheavier" in hx_redirect):
                direct_link = hx_redirect
            elif "ts.bzzhr.to/d/" in response.url or "buzzheavier.com/d/" in response.url:
                direct_link = response.url

        sub_page.on("response", handle_response)
        await sub_page.goto(raw_url, referer=referer_url, wait_until="domcontentloaded", timeout=15000)
        await sub_page.wait_for_timeout(1000)

        script_res = await sub_page.evaluate("""
            async () => {
                window.__captured_link = null;
                if (navigator.clipboard) {
                    navigator.clipboard.writeText = async (text) => {
                        window.__captured_link = text;
                        return true;
                    };
                }

                const allLinks = Array.from(document.querySelectorAll('a, button'));
                const copyBtn = allLinks.find(el => {
                    const txt = (el.innerText || '').toLowerCase();
                    return txt.includes('kopyala') || txt.includes('copy') || txt.includes('indirme bağlantısını');
                });

                if (copyBtn) {
                    copyBtn.click();
                    await new Promise(r => setTimeout(r, 600));
                    if (window.__captured_link) return window.__captured_link;
                }
                return null;
            }
        """)

        if script_res:
            direct_link = script_res

        if direct_link:
            direct_link = direct_link.strip()
            if direct_link.startswith("//"):
                direct_link = "https:" + direct_link
            return direct_link

    except Exception as e:
        print(f"      [!] BZZHR çözme hatası ({raw_url}): {e}")
    finally:
        if sub_page and not sub_page.is_closed():
            await sub_page.close()

    return raw_url


async def scrape_game_details(context, page, game_url, existing_dict):
    """Oyun detaylarını ve Hydra uyumlu başlığını (varsa Build numarasıyla) çeker."""
    clean_url = game_url.rstrip('/')
    try:
        await page.goto(game_url, wait_until="domcontentloaded", timeout=30000)

        # Cloudflare Kontrolü
        page_title = await page.title()
        if "Just a moment" in page_title or "Cloudflare" in page_title or "Attention Required" in page_title:
            print(f"   -> [!] Cloudflare algılandı ({clean_url}), bekleniyor...")
            await page.wait_for_timeout(6000)

        try:
            await page.wait_for_selector("h1.entry-title, h1", timeout=10000)
        except Exception:
            pass

        await page.wait_for_timeout(500)

        # 1. Başlık Tespiti ve Temizlik
        title_el = await page.query_selector("h1.entry-title") or await page.query_selector("h1")
        raw_title = (await title_el.inner_text()).strip() if title_el else ""
        
        if not raw_title:
            if clean_url in existing_dict:
                return existing_dict[clean_url]
            return None

        # Temizlik: Download ibareleri, alternatif isimler (/...), "- v1.06", "- Updated Version", tarihler
        clean_title = re.sub(r'\s*(Free|Direct)?\s*Download.*$', '', raw_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'\s*/.*$', '', clean_title).strip()
        clean_title = re.sub(r'\s*-\s*v?[0-9]+(\.[0-9]+)*.*$', '', clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'\s*-\s*Updated\s*Version.*$', '', clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'\s*\(Build\s*\d+\).*$', '', clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'\s*–\s*\[.*\]$', '', clean_title).strip()

        # 2. Build Numarası Tespiti
        build_number = await page.evaluate("""
            () => {
                const textNodes = Array.from(document.querySelectorAll("div, span, td, p, button"));
                for (const el of textNodes) {
                    const txt = (el.innerText || '').trim();
                    if (txt.includes("Posted Build")) {
                        const match = txt.match(/Posted\\s*Build\\s*(\\d+)/i) || el.parentElement?.innerText.match(/Posted\\s*Build\\s*(\\d+)/i);
                        if (match) return match[1];
                    }
                }
                const match = document.body.innerText.match(/Posted\\s*Build\\s*[:\\s]*(\\d+)/i);
                return match ? match[1] : null;
            }
        """)

        # Build numarası varsa başlığın sonuna Hydra uyumlu (Build XXX) ekle
        if build_number:
            clean_title = f"{clean_title} (Build {build_number})"

        # 3. Güncelleme Tarihi Tespiti
        date_str = ""
        date_el = await page.query_selector("time.updated") or await page.query_selector("time.entry-date") or await page.query_selector("meta[property='article:modified_time']")
        if date_el:
            date_str = (await date_el.get_attribute("datetime")) or (await date_el.get_attribute("content")) or (await date_el.inner_text()).strip()

        # --- NORMAL MODDA GÜNCELLEME KONTROLÜ ---
        if not FORCE_REFRESH and clean_url in existing_dict:
            old_item = existing_dict[clean_url]
            old_upload_date = old_item.get("uploadDate", "")
            
            if old_upload_date == date_str:
                return old_item

        # 4. Dosya Boyutu Tespiti
        file_size = (await page.evaluate("""
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
        """)) or "N/A"

        # 5. İndirme Bağlantılarını Çek
        all_uris = []
        valid_hosts = ["bzzhr.to", "buzzheavier.com", "gofile.io", "qiwi.gg", "pixeldrain.com", "megadb.net", "megadb.xyz"]
        
        links = await page.query_selector_all("a[href]")
        for link in links:
            raw_href = (await link.get_attribute("href")) or ""
            
            match = re.search(r'(https?://[^\s"\'<>]+)', raw_href)
            if match:
                href = match.group(1)
            elif raw_href.startswith("//"):
                href = "https:" + raw_href
            elif raw_href.startswith("/"):
                href = "https://steamrip.com" + raw_href
            else:
                href = raw_href

            href_lower = href.lower()
            if "-free-download" in href_lower:
                continue

            is_valid_host = any(host in href_lower for host in valid_hosts)
            is_redirect = any(p_path in href_lower for p_path in ["/redirect/", "/goto/", "/dl/"])

            if is_valid_host or is_redirect:
                all_uris.append(href)

        all_uris = list(set(all_uris))

        if not all_uris:
            if clean_url in existing_dict:
                return existing_dict[clean_url]
            return None

        # Sunucu çözme adımları
        resolved_uris = []
        for uri in all_uris:
            if "bzzhr.to" in uri or "buzzheavier.com" in uri:
                resolved = await resolve_bzzhr_direct_link(context, uri, game_url)
                resolved_uris.append(resolved)
            elif "gofile.io" in uri:
                clean_gofile = uri.split('?')[0]
                resolved_uris.append(clean_gofile)
            elif "pixeldrain.com" in uri:
                resolved = resolve_pixeldrain_direct_link(uri)
                resolved_uris.append(resolved)
            else:
                resolved_uris.append(uri)

        # Reklam ve çöp link filtresi
        clean_resolved_uris = []
        junk_domains = ["ankergames.net", "doubleclick", "google.com", "yandex", "facebook.com"]

        for r_uri in resolved_uris:
            if not r_uri or not isinstance(r_uri, str):
                continue
            
            r_uri_clean = r_uri.strip()
            r_uri_lower = r_uri_clean.lower()

            if not (r_uri_lower.startswith("http://") or r_uri_lower.startswith("https://")):
                continue

            if any(junk in r_uri_lower for junk in junk_domains):
                continue

            if "steamrip.com" in r_uri_lower:
                if not any(p_path in r_uri_lower for p_path in ["/redirect/", "/goto/", "/dl/"]):
                    continue

            if r_uri_clean.rstrip('/') == clean_url:
                continue

            clean_resolved_uris.append(r_uri_clean)

        clean_resolved_uris = list(dict.fromkeys(clean_resolved_uris))

        if not clean_resolved_uris:
            if clean_url in existing_dict:
                return existing_dict[clean_url]
            return None

        formatted_date = date_str if date_str else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "title": clean_title,
            "uris": clean_resolved_uris,
            "uploadDate": formatted_date,
            "fileSize": file_size,
            "descriptionHtml": f'<a href="{game_url}">Website with instructions for launching the game</a>'
        }

    except Exception as err:
        print(f"   -> Hata ({game_url}): {err}")
        if clean_url in existing_dict:
            return existing_dict[clean_url]
        return None


async def worker(worker_id, queue, context, existing_dict, downloads, total_count, counter_lock):
    """Paralel çalışan işçi havuzu."""
    page = await context.new_page()
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """)

    while not queue.empty():
        item = await queue.get()
        idx, url = item
        clean_url = url.rstrip('/')

        game_data = await scrape_game_details(context, page, url, existing_dict)
        
        async with counter_lock:
            if game_data:
                downloads.append(game_data)
                print(f"[{idx}/{total_count}] BAŞARILI: {game_data['title']} | Boyut: {game_data['fileSize']} | Link: {len(game_data['uris'])}")
            elif clean_url in existing_dict:
                downloads.append(existing_dict[clean_url])
                print(f"[{idx}/{total_count}] KORUNDU: {existing_dict[clean_url]['title']}")
            else:
                print(f"[{idx}/{total_count}] LİNK BULUNAMADI: {url}")

            if len(downloads) % 20 == 0 and len(downloads) > 0:
                with open("source.json", "w", encoding="utf-8") as f:
                    json.dump({"name": "SteamRIP", "downloads": downloads}, f, ensure_ascii=False, indent=2)

        queue.task_done()

    await page.close()


async def main():
    existing_dict = load_existing_source()
    print(f"Mevcut kayıtlı oyun sayısı: {len(existing_dict)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS_MODE,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-size=1920,1080"
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="Europe/Istanbul"
        )

        init_page = await context.new_page()
        target_urls = await get_all_steamrip_games(init_page)
        await init_page.close()

        if not target_urls:
            print("Taranacak oyun bulunamadı.")
            await browser.close()
            return

        total_count = len(target_urls)
        print(f"\nPARALEL TARAMA BAŞLATILIYOR ({MAX_CONCURRENT_TASKS} Sekme | Toplam: {total_count} Oyun)...\n")

        queue = asyncio.Queue()
        for idx, url in enumerate(target_urls, 1):
            await queue.put((idx, url))

        downloads = []
        counter_lock = asyncio.Lock()

        tasks = [
            asyncio.create_task(worker(i, queue, context, existing_dict, downloads, total_count, counter_lock))
            for i in range(MAX_CONCURRENT_TASKS)
        ]

        await queue.join()
        for t in tasks:
            t.cancel()

        await browser.close()

    if downloads:
        source_data = {
            "name": "SteamRIP",
            "downloads": downloads
        }
        with open("source.json", "w", encoding="utf-8") as f:
            json.dump(source_data, f, ensure_ascii=False, indent=2)

        print(f"\nTÜM TARAMA BİTTİ! Toplam {len(downloads)} oyun 'source.json' dosyasına yazıldı.")
    else:
        print("\n[!] UYARI: Hiçbir oyun verisi işlenemedi.")


def run_scraper():
    asyncio.run(main())


if __name__ == "__main__":
    run_scraper()