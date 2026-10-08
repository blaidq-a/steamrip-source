import json
import re
import os
import requests
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from playwright.sync_api import sync_playwright

# --- AYARLAR ---
FULL_SCRAPE_MODE = True  # True: Tüm oyunları tarar ve güncellemeleri kontrol eder

# GitHub Actions ortamındaysa otomatik headless (arkaplanda) çalışır, yerelde ekran açılır
HEADLESS_MODE = os.getenv("GITHUB_ACTIONS", "false").lower() == "true"


def load_existing_source():
    """Var olan source.json dosyasını okuyarak URL ve build tabanlı hafızaya alır."""
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


def resolve_pixeldrain_direct_link(raw_url):
    """Pixeldrain linklerini doğrudan API indirme adresine çevirir."""
    match = re.search(r'pixeldrain\.com/u/([a-zA-Z0-9]+)', raw_url)
    if match:
        file_id = match.group(1)
        return f"https://pixeldrain.com/api/file/{file_id}?download"
    return raw_url


def resolve_bzzhr_direct_link(context, raw_url, referer_url):
    """BZZHR indirme sayfasındaki doğrudan ts.bzzhr.to CDN adresini yakalar."""
    sub_page = None
    direct_link = None
    try:
        sub_page = context.new_page()

        def handle_response(response):
            nonlocal direct_link
            hx_redirect = response.headers.get("hx-redirect")
            if hx_redirect and ("ts.bzzhr.to" in hx_redirect or "/d/" in hx_redirect or "buzzheavier" in hx_redirect):
                direct_link = hx_redirect
            elif "ts.bzzhr.to/d/" in response.url or "buzzheavier.com/d/" in response.url:
                direct_link = response.url

        sub_page.on("response", handle_response)
        sub_page.goto(raw_url, referer=referer_url, wait_until="domcontentloaded", timeout=15000)
        sub_page.wait_for_timeout(1000)

        script_res = sub_page.evaluate("""
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
            sub_page.close()

    return raw_url


def scrape_game_details(context, page, game_url, existing_dict):
    """Oyun detaylarını, doğru oyun boyutunu ve build bilgilerini çeker."""
    try:
        clean_url = game_url.rstrip('/')
        page.goto(game_url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(500)

        page_content = page.content()

        # 1. Başlık Tespiti ve Temizlik
        title_el = page.query_selector("h1.entry-title") or page.query_selector("h1")
        raw_title = title_el.inner_text().strip() if title_el else ""
        clean_title = re.sub(r'\s*Free Download.*$', '', raw_title, flags=re.IGNORECASE).strip()

        # 2. Build Numarası Tespiti
        build_number = page.evaluate("""
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

        # 3. Metin İçi Versiyon Tespiti
        version_match = re.search(r'Version\s*:\s*([^\n<]+)', page_content, re.IGNORECASE)
        has_v = False
        if version_match:
            v_str = version_match.group(1).strip()
            if v_str and v_str.lower() not in clean_title.lower():
                clean_title = f"{clean_title} – {v_str}"
                has_v = True

        if build_number and build_number not in clean_title:
            clean_title = f"{clean_title} (Build {build_number})"

        # 4. Güncelleme Tarihi Tespiti
        date_str = ""
        date_el = page.query_selector("time.updated") or page.query_selector("time.entry-date") or page.query_selector("meta[property='article:modified_time']")
        if date_el:
            date_str = date_el.get_attribute("datetime") or date_el.get_attribute("content") or date_el.inner_text().strip()

        if not has_v and not build_number and date_str:
            short_date = date_str.split("T")[0] if "T" in date_str else date_str
            clean_title = f"{clean_title} – [{short_date}]"

        # --- GÜNCELLEME KONTROLÜ ---
        if clean_url in existing_dict:
            old_item = existing_dict[clean_url]
            old_upload_date = old_item.get("uploadDate", "")
            
            if old_upload_date == date_str or old_item.get("title") == clean_title:
                print(f"   -> [ATLANDI] Oyun güncel: {clean_title}")
                return old_item
            else:
                print(f"   -> [YENİ BUILD / PATCH] Eski: '{old_item.get('title')}' | Yeni: '{clean_title}'")

        # 5. Kesin ve Doğru Dosya Boyutu Tespiti
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

        # 6. İndirme Bağlantılarını Çek
        all_uris = []
        links = page.query_selector_all("a[href]")
        for link in links:
            href = link.get_attribute("href") or ""
            
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://steamrip.com" + href

            if any(host in href for host in ["bzzhr.to", "buzzheavier.com", "gofile.io", "qiwi.gg", "pixeldrain.com", "megadb.net", "megadb.xyz"]):
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
                resolved = resolve_bzzhr_direct_link(context, uri, game_url)
                resolved_uris.append(resolved)
            elif "gofile.io" in uri:
                clean_gofile = uri.split('?')[0]
                resolved_uris.append(clean_gofile)
            elif "pixeldrain.com" in uri:
                resolved = resolve_pixeldrain_direct_link(uri)
                resolved_uris.append(resolved)
            else:
                # MegaDB dahil diğer tüm linkler direkt ham haliyle çekilir (Sayfaya girmez, 12sn beklemez)
                resolved_uris.append(uri)

        # --- REKLAM VE ÇÖP LİNK FİLTRESİ ---
        clean_resolved_uris = []
        junk_domains = ["ankergames.net", "steamrip.com", "doubleclick", "google.com", "yandex", "facebook.com"]

        for r_uri in resolved_uris:
            if not r_uri or not isinstance(r_uri, str):
                continue
            
            r_uri_clean = r_uri.strip()
            r_uri_lower = r_uri_clean.lower()

            # HTTP/HTTPS formatı dışındaki geçersiz bağlantıları atla
            if not (r_uri_lower.startswith("http://") or r_uri_lower.startswith("https://")):
                continue

            # Çöp / Reklam domain içeren yönlendirmeleri eler
            if any(junk in r_uri_lower for junk in junk_domains):
                continue

            clean_resolved_uris.append(r_uri_clean)

        # Çift kayıtları temizle
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
        if game_url.rstrip('/') in existing_dict:
            return existing_dict[game_url.rstrip('/')]
        return None


def run_scraper():
    existing_dict = load_existing_source()
    print(f"Mevcut kayıtlı oyun sayısı: {len(existing_dict)}")

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
        print(f"\nTÜM OYUNLAR İÇİN TARAMA BAŞLATILIYOR (Toplam: {total_count} oyun)...\n")

        for idx, url in enumerate(target_urls, 1):
            clean_url = url.rstrip('/')
            print(f"[{idx}/{total_count}] {url}")
            game_data = scrape_game_details(context, page, url, existing_dict)
            if game_data:
                downloads.append(game_data)
                print(f"   -> BAŞARILI: {game_data['title']} | Boyut: {game_data['fileSize']} | Link Sayısı: {len(game_data['uris'])}\n")
            elif clean_url in existing_dict:
                # Ağ hatası veya erişim engeli olursa mevcut veriyi korur (Link sıfırlanmasını önler)
                downloads.append(existing_dict[clean_url])
                print(f"   -> TARAMA BAŞARISIZ OLDU, MEVCUT VERİ KORUNDU: {existing_dict[clean_url]['title']}\n")
            else:
                print("   -> İndirme linki bulunamadı.\n")

            # Her 20 oyunda bir canlı kaydet
            if idx % 20 == 0 and len(downloads) > 0:
                with open("source.json", "w", encoding="utf-8") as f:
                    json.dump({"name": "SteamRIP", "downloads": downloads}, f, ensure_ascii=False, indent=2)

        browser.close()

    if downloads:
        source_data = {
            "name": "SteamRIP",
            "downloads": downloads
        }

        with open("source.json", "w", encoding="utf-8") as f:
            json.dump(source_data, f, ensure_ascii=False, indent=2)

        print(f"\nTÜM TARAMA BİTTİ! Toplam {len(downloads)} oyun 'source.json' dosyasına yazıldı.")
    else:
        print("\n[!] UYARI: Hiçbir oyun verisi işlenemedi, source.json güncellenmedi.")


if __name__ == "__main__":
    run_scraper()