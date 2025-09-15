import scrapy
import json
import time
import random

class DoubanSpider(scrapy.Spider):
    name = "douban"

    async def start(self):
        GRAPHQL_QUERY = """
        query getFilterWorksList($works_ids: [ID!]) {
        worksList(worksIds: $works_ids) {
            id
            isOrigin
            isEssay
            title
            cover(useSmall: false)
            url
            isBundle
            coverLabel(preferVip: true)
            url
            title
            author { name url }
            origAuthor { name url }
            translator { name url }
            abstract
            authorHighlight
            editorHighlight
            isOrigin
            kinds { name @skip(if: true) shortName @include(if: true) id }
            ... on WorksBase @include(if: true) { wordCount wordCountUnit }
            ... on WorksBase @include(if: false) { inLibraryCount }
            ... on WorksBase @include(if: false) {
            isEssay
            ... on EssayWorks { favorCount }
            averageRating ratingCount url isColumn isFinished
            }
            ... on EbookWorks @include(if: true) {
            ... on EbookWorks { book { url averageRating ratingCount } }
            }
            ... on WorksBase @include(if: false) {
            isColumn isEssay onSaleTime
            ... on ColumnWorks { updateTime }
            }
            ... on WorksBase @include(if: true) {
            isColumn
            ... on ColumnWorks { isFinished finalizedCn }
            }
            ... on EssayWorks {
            essayActivityData { title uri tag { name color background icon2x icon3x iconSize { height } iconPosition { x y } } }
            }
            highlightTags { name }
            ... on WorksBase @include(if: false) { fanfiction { tags { id name url } } }
            ... on WorksBase { copyrightInfo { newlyAdapted newlyPublished adaptedName publishedName } }
            isInLibrary
            ... on WorksBase @include(if: false) {
            fixedPrice salesPrice realPrice { price priceType }
            isOrigin
            ... on ColumnWorks { isAutoPricing }
            }
            ... on EbookWorks { fixedPrice salesPrice realPrice { price priceType } }
            ... on WorksBase @include(if: true) {
            ... on EbookWorks { id isPurchased isInWishlist }
            }
            ... on WorksBase @include(if: false) { fanfiction { fandoms { title url } } }
            ... on WorksBase @include(if: false) { fanfiction { kudoCount } }
        }
        }
        """

        max_page = 10
        url = 'https://read.douban.com/j/kind/'
        
        for page in range(max_page):
            headers = {
                "accept": "application/json",
                "accept-language": "zh-CN,zh;q=0.9,ru;q=0.8,ja;q=0.7",
                "content-type": "application/json",
                "dnt": "1",
                "origin": "https://read.douban.com",
                "priority": "u=1, i",
                "referer": f"https://read.douban.com/category/1?sort=book_rating&page={page}",
                "sec-ch-ua": "\"Chromium\";v=\"142\", \"Google Chrome\";v=\"142\", \"Not_A Brand\";v=\"99\"",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": "\"Linux\"",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                # "x-csrf-token": ".eJwNx8EBwCAIA8BdnKCCEDKOkrr_CO39bhSxEBUJ2wW3CfTOOEG2ANO6LDbLTf5c6i_eqcQk1Wd8HjQQ-A.aMfpfw.TuQyIyYGEwOPoufBLF8pLjjGNs8",
                "x-requested-with": "XMLHttpRequest"
            }

            request_body = {
                "sort": "book_rating",
                "page": page,       # 页码
                "kind": 1,          # 分类ID,链接是1所以它就是1,没有为什么
                "query": GRAPHQL_QUERY,  # GraphQL
                "variables": {}
            }

            # 2. 转换为JSON字符串
            body_json = json.dumps(request_body)
            

            yield scrapy.Request(
                url=url,
                method='POST',
                body=body_json,
                dont_filter=True,
                headers=headers,
            )

    def parse(self, response):
        # 解析api的json
        data = json.loads(response.body)["list"]
        for book in data:
            book_link = "https://read.douban.com" + book["url"]
            time.sleep(random.randint(3,5))
            # call爬取书籍详情页
            yield scrapy.Request(
                url=book_link,
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                    "Cookie": """ll="118318"; bid=xufvZDXS2yA; _vwo_uuid_v2=DA9EC0BDE71CD9B5289B1DDE344E25A35|00faae151b4ff0d5af454dd36c689be9; uaid="85bfcd12c0750cb2f259c2561ec4361c3f2949e3"; _pk_id.100001.a7dd=72083afd1753d158.1757927437.; _ga=GA1.3.1156627623.1757927382; _gid=GA1.3.1495182336.1757927437; _ga=GA1.1.1156627623.1757927382; __utmc=30149280; __utma=30149280.1156627623.1757927382.1757932143.1757935850.3; __utmz=30149280.1757935850.3.3.utmcsr=read.douban.com|utmccn=(referral)|utmcmd=referral|utmcct=/; _pk_ref.100001.a7dd=%5B%22%22%2C%22%22%2C1757942293%2C%22https%3A%2F%2Fbook.douban.com%2F%22%5D; _pk_ses.100001.a7dd=1; _ga_RXNMP372GL=GS2.1.s1757935836$o3$g1$t1757942556$j60$l0$h0""",
                    "Referer": "https://read.douban.com/category/1?sort=book_rating&page=0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "zh-CN,zh;q=0.9"
                },
                callback=self.parse_book
                )

    def parse_book(self, response):
        result = {}
        article_meta = response.xpath('//div[has-class("article-profile-bd")]')
        # 标题
        result["title"] = response.xpath('//h1[@class="article-title"]/text()').get()
        # 作者
        result["author"] = article_meta.xpath('.//p[@class="author"]//a/text()').get()

        # 类别
        result["category"] = article_meta.xpath('.//p[@class="category"]//span[@itemprop="genre"]/text()').get()

        # 出版社 & 出版时间
        publisher_text = article_meta.xpath('.//p[span[@class="label" and text()="出版社"]]/span[@class="labeled-text"]/text()').get()
        # 分割 "作家出版社 / 2015-12"
        parts = [part.strip() for part in publisher_text.split("/")]
        result["publisher"] = parts[0] if len(parts) > 0 else ""
        result["publish_time"] = parts[1] if len(parts) > 1 else ""

        # 提供方
        result["provider"] = article_meta.xpath('.//p[span[@class="label" and text()="提供方"]]//a/text()').get()

        # 字数
        result["word_count"] = article_meta.xpath('.//p[span[@class="label" and text()="字数"]]/span[@class="labeled-text"]/text()').get()

        # ISBN
        result["isbn"] = article_meta.xpath('.//p[span[@class="label" and text()="ISBN"]]//a/@title').get()

        # 简介
        info = ""
        info_div = response.xpath('//div[@class="info"]')
        for div in info_div:
            for p in div.xpath('./p/text()'):
                info += p.get()
        result["info"] = info

        # 评论
        comment = []
        comments_div = response.xpath('//div[@class="comment-content"]')
        for div in comments_div:
            comment.append(div.xpath('./a/text()').get())

        yield result
