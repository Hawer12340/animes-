from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)  # False = você vê o navegador abrindo
    page = browser.new_page()
    page.goto("https://books.toscrape.com")
    for livro in page.locator("article.product_pod").all():
        titulo = livro.locator("h3 a").get_attribute("title")
        preco = livro.locator(".price_color").inner_text()
        print(titulo, preco)
    browser.close()