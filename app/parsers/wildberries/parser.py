from app.parsers.wildberries.config import BASE_URL


from app.parsers.wildberries.scraper.page_loader import PageLoader
from app.parsers.wildberries.scraper.review_extractor import ReviewExtractor


from app.parsers.wildberries.utils.webdriver import WebDriverManager


def parse_product(pid: str):
    driver = WebDriverManager.create_webdriver()
    
    
    page_loader = PageLoader(driver)
    page_loader.load_page(
        f'{BASE_URL}/catalog/{pid}/detail.aspx'
    )
    page_loader.accept_cookies()
    
    
    product_name = ReviewExtractor.get_product_name(driver)
    
    
    reviews = []
    
    
    if page_loader.open_reviews_section():
        page_loader.scroll_to_load_reviews()
        reviews = ReviewExtractor.extract_reviews(
            driver, pid, product_name
        )
    
    
    driver.quit()
    
    
    return reviews