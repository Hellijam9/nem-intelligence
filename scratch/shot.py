import asyncio, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for scheme in ["light","dark"]:
            pg = await b.new_page(viewport={"width":390,"height":844}, device_scale_factor=2, color_scheme=scheme)
            msgs=[]; pg.on("console", lambda m: msgs.append(m.text)); pg.on("pageerror", lambda e: msgs.append("ERR "+str(e)))
            await pg.goto("file://" + __import__("os").path.abspath("docs/bidstack/index.html")); await pg.wait_for_timeout(2500)
            await pg.screenshot(path=f"scratch/{scheme}.png", full_page=True)
            print(scheme, msgs[:5])
        await b.close()
asyncio.run(main())
