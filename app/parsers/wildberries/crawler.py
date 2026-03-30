from selenium.webdriver.common.by import By


from app.parsers.wildberries.utils.webdriver import WebDriverManager


from app.parsers.wildberries.scraper.page_loader import PageLoader


from app.parsers.wildberries.config import BASE_URL, WAIT_TIMEOUT


def get_product_ids(limit: int = 20):
    driver = WebDriverManager.create_webdriver()
    page_loader = PageLoader(driver)
    page_loader.load_page(f"{BASE_URL}")
    page_loader.accept_cookies()
    
    
    cards = driver.find_elements(
        By.CSS_SELECTOR,
        "article[data-nm-id]"
    )
    
    
    ids = []
    
    for card in cards:
        pid = card.get_attribute("data-nm-id")

        if pid:
            ids.append(pid)

        if len(ids) >= limit:
            break
    
    driver.quit()
    
    
    return ids



