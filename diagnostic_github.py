from playwright.sync_api import sync_playwright

URL = "https://betwinner.cm/fr/line/football?platform_type=mobile"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    try:
        page.goto(URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(15000)
    except Exception as e:
        print(f"Erreur de chargement : {e}")

    page.screenshot(path="diagnostic_github.png", full_page=True)

    texte = page.inner_text("body")
    with open("diagnostic_github.txt", "w", encoding="utf-8") as f:
        f.write(texte[:5000])

    print("URL finale :", page.url)
    print("Titre de la page :", page.title())
    print("--- Premiers 500 caractères du texte ---")
    print(texte[:500])

    browser.close()
