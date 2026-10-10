import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9223")
        pages = [page for context in browser.contexts for page in context.pages]
        print("PAGINAS:", len(pages))
        for page in pages:
            print("\nTITULO:", await page.title())
            print("URL:", page.url)
            try:
                text = await page.locator("body").inner_text(timeout=10000)
                print("TEXTO:", text[:1000])
            except Exception as exc:
                print("ERRO AO LER:", str(exc))
        # Não fecha o Edge dedicado.
asyncio.run(main())
