from typing import Dict, List

from app.chain import _ChainBase
from app.chain.common import CommonChain
from app.chain.search import SearchChain
from app.core import MetaInfo, TorrentInfo, Context, MediaInfo
from app.db.subscribes import Subscribes
from app.helper.sites import SitesHelper
from app.log import logger
from app.utils.string import StringUtils
from app.utils.types import MediaType


class SubscribeChain(_ChainBase):
    """
    订阅处理链
    """

    # 站点最新种子缓存 {站点域名: 种子上下文}
    _torrents_cache: Dict[str, List[Context]] = {}

    def __init__(self):
        super().__init__()
        self.common = CommonChain()
        self.searchchain = SearchChain()
        self.subscribes = Subscribes()
        self.siteshelper = SitesHelper()

    def process(self, title: str,
                mtype: MediaType = None,
                tmdbid: str = None,
                season: int = None,
                username: str = None,
                **kwargs) -> bool:
        """
        识别媒体信息并添加订阅
        """
        logger.info(f'开始添加订阅，标题：{title} ...')
        # 识别前预处理
        result = self.run_module('prepare_recognize', title=title)
        if result:
            title, _ = result
        # 识别元数据
        metainfo = MetaInfo(title)
        if mtype:
            metainfo.type = mtype
        if season:
            metainfo.type = MediaType.TV
            metainfo.begin_season = season
        # 识别媒体信息
        mediainfo = self.run_module('recognize_media', meta=metainfo, tmdbid=tmdbid)
        if not mediainfo:
            logger.warn(f'未识别到媒体信息，标题：{title}，tmdbid：{tmdbid}')
            return False
        # 更新媒体图片
        self.run_module('obtain_image', mediainfo=mediainfo)
        # 添加订阅
        state, err_msg = self.subscribes.add(mediainfo, season=season, **kwargs)
        if state:
            logger.info(f'{mediainfo.get_title_string()} {err_msg}')
        else:
            logger.error(f'{mediainfo.get_title_string()} 添加订阅成功')
            self.common.post_message(title=f"{mediainfo.get_title_string()} 已添加订阅",
                                     text="用户：{username}",
                                     image=mediainfo.get_message_image())
        # 返回结果
        return state

    def search(self, sid: int = None, state: str = 'N'):
        """
        订阅搜索
        :param sid: 订阅ID，有值时只处理该订阅
        :param state: 订阅状态 N:未搜索 R:已搜索
        :return: 更新订阅状态为R或删除订阅
        """
        if sid:
            subscribes = [self.subscribes.get(sid)]
        else:
            subscribes = self.subscribes.list(state)
        # 遍历订阅
        for subscribe in subscribes:
            # 如果状态为N则更新为R
            if subscribe.state == 'N':
                self.subscribes.update(subscribe.id, {'state': 'R'})
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season
            meta.type = MediaType.MOVIE if subscribe.type == MediaType.MOVIE.value else MediaType.TV
            # 识别媒体信息
            mediainfo = self.run_module('recognize_media', meta=meta, tmdbid=subscribe.tmdbid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}')
                continue
            # 查询缺失的媒体信息
            exist_flag, no_exists = self.common.get_no_exists_info(mediainfo=mediainfo)
            if exist_flag:
                logger.info(f'{mediainfo.get_title_string()} 媒体库中已存在，完成订阅')
                self.subscribes.delete(subscribe.id)
                continue
            # 搜索
            contexts = self.searchchain.process(meta=meta, mediainfo=mediainfo, keyword=subscribe.keyword)
            if not contexts:
                logger.warn(f'{subscribe.keyword or subscribe.name} 未搜索到资源')
                continue
            # 自动下载
            _, lefts = self.common.batch_download(contexts=contexts, need_tvs=no_exists)
            if not lefts:
                # 全部下载完成
                logger.info(f'{mediainfo.get_title_string()} 下载完成，完成订阅')
                self.subscribes.delete(subscribe.id)
            else:
                # 未完成下载
                logger.info(f'{mediainfo.get_title_string()} 未下载未完整，继续订阅 ...')

    def refresh(self):
        """
        刷新站点最新资源
        """
        # 所有站点索引
        indexers = self.siteshelper.get_indexers()
        # 遍历站点缓存资源
        for indexer in indexers:
            domain = StringUtils.get_url_domain(indexer.get("domain"))
            torrents: List[TorrentInfo] = self.run_module("refresh_torrents", sites=[indexer])
            if torrents:
                self._torrents_cache[domain] = []
                for torrent in torrents:
                    # 识别
                    meta = MetaInfo(torrent.title, torrent.description)
                    # 识别媒体信息
                    mediainfo = self.run_module('recognize_media', meta=meta)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{torrent.title}')
                        continue
                    # 上下文
                    context = Context(meta=meta, mediainfo=mediainfo, torrentinfo=torrent)
                    self._torrents_cache[domain].append(context)
        # 从缓存中匹配订阅
        self.match()

    def match(self):
        """
        从缓存中匹配订阅，并自动下载
        """
        # 所有订阅
        subscribes = self.subscribes.list('R')
        # 遍历订阅
        for subscribe in subscribes:
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season
            meta.type = MediaType.MOVIE if subscribe.type == MediaType.MOVIE.value else MediaType.TV
            # 识别媒体信息
            mediainfo: MediaInfo = self.run_module('recognize_media', meta=meta, tmdbid=subscribe.tmdbid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}')
                continue
            # 查询缺失的媒体信息
            exist_flag, no_exists = self.common.get_no_exists_info(mediainfo=mediainfo)
            if exist_flag:
                logger.info(f'{mediainfo.get_title_string()} 媒体库中已存在，完成订阅')
                self.subscribes.delete(subscribe.id)
                continue
            # 遍历缓存种子
            _match_context = []
            for domain, contexts in self._torrents_cache.items():
                for context in contexts:
                    # 检查是否匹配
                    torrent_meta = context.meta_info
                    torrent_mediainfo = context.media_info
                    torrent_info = context.torrent_info
                    if torrent_mediainfo.tmdb_id == mediainfo.tmdb_id \
                            and torrent_mediainfo.type == mediainfo.type:
                        if meta.begin_season and meta.begin_season != torrent_meta.begin_season:
                            continue
                        # 匹配成功
                        logger.info(f'{mediainfo.get_title_string()} 匹配成功：{torrent_info.title}')
                        _match_context.append(context)
            logger(f'{mediainfo.get_title_string()} 匹配完成，共匹配到{len(_match_context)}个资源')
            if _match_context:
                # 批量择优下载
                _, lefts = self.common.batch_download(contexts=_match_context, need_tvs=no_exists)
                if not lefts:
                    # 全部下载完成
                    logger.info(f'{mediainfo.get_title_string()} 下载完成，完成订阅')
                    self.subscribes.delete(subscribe.id)
                else:
                    # 未完成下载，计算剩余集数
                    left_episodes = lefts.get(mediainfo.tmdb_id, {}).get("episodes", [])
                    logger.info(f'{mediainfo.get_title_string()} 未下载未完整，更新缺失集数为{len(left_episodes)} ...')
                    self.subscribes.update(subscribe.id, {
                        "lack_episode": len(left_episodes)
                    })