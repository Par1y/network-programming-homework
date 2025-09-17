from pathlib import Path

import scrapy

class ArtistSpider(scrapy.Spider):
    name = "netease_male"

    async def start(self):
        urls = [
            "https://music.163.com/discover/artist/cat?id=1001"
        ]
        for url in urls:
            yield scrapy.Request(url=url, callback=self.parse)

    def parse(self, response):
        artist = response.xpath('//*[@id="m-artist-box"]')
        # 从里面提取 <a href="/artist?id=12631485" class="nm nm-icn f-thide s-fc0" title="h3R3的音乐">h3R3</a>
        atags = artist.xpath('//a[has-class("nm nm-icn f-thide s-fc0")]')
        for a in atags:
            # 链接，方便call专辑爬取
            artist_homepage_link = response.urljoin("/artist/album?"+a.xpath("./@href").get().split("?")[-1])
            # call歌手主页爬取
            yield scrapy.Request(
                url=artist_homepage_link,
                callback=self.parse_artist
                )

    def parse_artist(self, response):
        # 从里面提取 <a href="/album?id=267552418" class="tit s-fc0">野路</a>
        atags = response.xpath('//a[has-class("tit s-fc0")]')
        for a in atags:
            # 只拿链接
            album_link = response.urljoin(a.xpath("./@href").get())
            # 一页全显，不需要翻页了
            album_link = album_link+"&limit=120&offset=0"
            # call专辑爬取
            yield scrapy.Request(
                url=album_link,
                callback=self.parse_album
            )

    def parse_album(self, response):
        album = ""
        artist = ""
        release_date = ""
        company = ""
        songs = ""

        # 专辑名
        album = response.xpath('//h2[has-class("f-ff2")]/text()').get()
        # 专辑信息
        info = response.xpath('//p[has-class("intr")]')
        for i in info:
            b = i.xpath('./b/text()').get()
            if b == "歌手：":
                artist = i.xpath("./span/@title").get()
            elif b == "发行时间：":
                release_date = i.xpath("./text()").get()
            elif b == "发行公司：":
                company = i.xpath("./text()").get().strip()
            else:
                self.log(f"专辑信息出现未定义值： {str(i)}")
        # 歌曲名
        songs = response.xpath('//ul[has-class("f-hide")]/li/a/text()').getall()
        # 输出
        yield {
            "专辑": album,
            "歌手": artist,
            "发行时间": release_date,
            "发行公司": company,
            "歌曲名": songs
        }