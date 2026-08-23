"""下载模块 — 喜马拉雅音频下载"""

import re
import time
import os
import threading
import hashlib
import requests
import urllib3
from pathlib import Path
from core.config import DEFAULT_QUALITY, DEFAULT_FORMAT
from core import config as _config
from core.crypto import decrypt_download_url
from core.login import load_cookie_string
from core import account_manager

# 抑制 SSL 验证关闭的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class XimalayaDownloader:
    # ── 类级别：每账号独立浏览器实例池（LRU 池化，指纹隔离）──
    _drivers = {}                             # account_key -> WebDriver
    _driver_locks = {}                        # account_key -> threading.Lock（同账号串行）
    _driver_last_used = {}                    # account_key -> 最近使用时间戳(monotonic)
    _drivers_global_lock = threading.RLock()  # 保护上面字典（可重入，便于池化内部再入）
    _MAX_DRIVERS = int(os.environ.get("XM_MAX_DRIVERS", "8"))            # 池上限，防内存爆炸
    _DRIVER_IDLE_EVICT = float(os.environ.get("XM_DRIVER_IDLE_EVICT", "600"))  # 闲置秒数阈值

    def __init__(self, cookie: str | None = None, account_id: str | None = None,
                 card_id: int | None = None, download_root: "Path | None" = None):
        """初始化下载器

        Args:
            cookie: 直接传入 cookie 字符串
            account_id: 指定账号 ID，从账号管理器获取 cookie
            card_id: 卡密 ID（per-card 隔离：账号 cookie 必须按此维度取）
            download_root: 下载根目录（按卡密隔离时使用），默认全局 DOWNLOAD_DIR
        """
        self.session = requests.Session()
        self.card_id = card_id
        self.account_id = account_id
        self.download_root = Path(download_root) if download_root else _config.DOWNLOAD_DIR
        if cookie:
            self.cookie = cookie
        elif account_id:
            # per-card 重构：账号 cookie 必须按 (card_id, account_id) 维度取；
            # 若未传入 card_id（理论上不会发生），退化到全局 cookie 文件，避免崩溃
            if card_id is not None:
                self.cookie = account_manager.get_account_cookie(card_id, account_id) or ""
            else:
                self.cookie = load_cookie_string() or ""
        else:
            # 免登录场景（如获取章节列表）：优先取该卡密默认账号 cookie
            # （含后端注入的账号），没有再回退全局 cookie 文件
            self.cookie = ""
            if card_id is not None:
                self.cookie = account_manager.get_default_cookie(card_id) or ""
            if not self.cookie:
                self.cookie = load_cookie_string() or ""
        self.xm_sign = None
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/102.0.5005.167 "
                "Electron/19.1.1 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Origin": "https://mobile.ximalaya.com",
            "Referer": "https://mobile.ximalaya.com",
            "Cookie": self.cookie,
        }

    # xm-sign 生成脚本（与官方 SDK 一致），抽到常量避免重复
    _SIGN_JS = """
        var callback = arguments[arguments.length - 1];
        var cid = "t6pfoml9679z52kqw93uqu75eflqdg1bykhl";
        var KFp = "h5_goyxvzyohd";
        var browserID = "";
        var browserIDPromise = new Promise((resolve) => {
            window.du_web_sdk.getBrowserID(cid, KFp, "", (t) => {
                if (t) browserID = t;
                resolve(t || "");
            });
        });
        var sessionIDPromise = new Promise((resolve) => {
            window.du_web_sdk.getSessionID(cid, KFp, "", (t) => {
                resolve(t || "");
            });
        });
        Promise.all([browserIDPromise, sessionIDPromise]).then(([browserID, sessionID]) => {
            callback(browserID + "&&" + sessionID);
        });
    """

    @classmethod
    def _account_profile_dir(cls, account_key: str) -> Path:
        """每账号独立 user-data-dir（安全路径，隔离浏览器指纹 browserID）"""
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', account_key)
        d = _config.DATA_DIR / "chrome_profiles" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def _init_browser_inner(cls, account_key: str):
        """创建一个独立 user-data-dir 的 Chrome（每账号指纹隔离）"""
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        # 清理上次崩溃/强杀残留的 Chrome 单例锁；否则新 Chrome 启动即退，
        # 报 "session not created: Chrome instance exited"。进本函数即说明无活 Chrome 占用此档案，
        # 这些锁必为死锁。仅删这三个锁文件，保留其余指纹缓存以维持每账号指纹隔离。
        profile_dir = cls._account_profile_dir(account_key)
        for _lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (profile_dir / _lock_name).unlink()
            except FileNotFoundError:
                pass
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")

        # Docker 环境：使用系统 Chromium
        chrome_bin = os.environ.get("CHROME_BIN")
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
        if chrome_bin:
            chrome_options.binary_location = chrome_bin
        if chromedriver_path:
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            driver = webdriver.Chrome(options=chrome_options)
        driver.get("https://s1.xmcdn.com")
        driver.execute_script("""
            var script = document.createElement('script');
            script.src = 'https://s1.xmcdn.com/yx/static-source/last/dist/js/dws1.6.8.js';
            script.onload = function() { window.sdkLoaded = true; };
            document.head.appendChild(script);
        """)
        for _ in range(10):
            if driver.execute_script("return window.sdkLoaded"):
                break
            time.sleep(0.5)
        return driver

    @classmethod
    def _ensure_lock(cls, account_key: str) -> threading.Lock:
        """确保该账号的锁存在（调用方会在 with 中使用返回锁）"""
        with cls._drivers_global_lock:
            if account_key not in cls._driver_locks:
                cls._driver_locks[account_key] = threading.Lock()
            return cls._driver_locks[account_key]

    @classmethod
    def _evict_idle_drivers(cls):
        """驱逐闲置超阈值的 driver（调用方需持有 _drivers_global_lock）"""
        now = time.monotonic()
        idle = [(k, t) for k, t in cls._driver_last_used.items()
                if now - t > cls._DRIVER_IDLE_EVICT and k in cls._drivers]
        if not idle:
            return
        idle.sort(key=lambda x: x[1])
        for k, _ in idle:
            if len(cls._drivers) <= cls._MAX_DRIVERS - 1:
                break
            d = cls._drivers.pop(k, None)
            cls._driver_last_used.pop(k, None)
            # 注意：不弹出 _driver_locks[k]——可能有线程正持有该锁执行签名，
            # 弹出后 _ensure_lock 会新建第二把锁，同账号串行隔离失效。锁对象极小，保留无妨。
            if d is not None:
                try:
                    d.quit()
                except Exception:
                    pass

    @classmethod
    def _get_driver(cls, account_key: str):
        """获取（或懒创建）某账号的浏览器；带 LRU 池化上限"""
        # 快路径也必须持全局锁：_evict_idle_drivers 会遍历/弹出这些 dict，
        # 无锁读写可抛 "dictionary changed size during iteration" 或拿到刚被 quit 的 driver
        with cls._drivers_global_lock:
            driver = cls._drivers.get(account_key)
            if driver is not None:
                cls._driver_last_used[account_key] = time.monotonic()
                return driver
            if account_key not in cls._driver_locks:
                cls._driver_locks[account_key] = threading.Lock()
            if len(cls._drivers) >= cls._MAX_DRIVERS:
                cls._evict_idle_drivers()
            driver = cls._init_browser_inner(account_key)
            cls._drivers[account_key] = driver
            cls._driver_last_used[account_key] = time.monotonic()
            return driver

    @classmethod
    def _reset_driver(cls, account_key: str):
        """崩溃重建：仅重置该账号的 driver（不影响其他账号）"""
        with cls._drivers_global_lock:
            d = cls._drivers.pop(account_key, None)
            cls._driver_last_used.pop(account_key, None)
            if d is not None:
                try:
                    d.quit()
                except Exception:
                    pass

    def get_xm_sign(self) -> str:
        """生成 xm-sign。

        按账号独立浏览器（独立 user-data-dir → 独立 browserID，指纹隔离）；
        同账号串行、不同账号并行；崩溃时仅重建该账号浏览器，不波及其他账号。
        """
        key = self._account_key()
        lock = XimalayaDownloader._ensure_lock(key)
        with lock:
            driver = XimalayaDownloader._get_driver(key)
            try:
                self.xm_sign = driver.execute_async_script(XimalayaDownloader._SIGN_JS)
            except Exception:
                # 该账号浏览器可能崩溃，仅重置它并重建一次（不影响其他账号）
                XimalayaDownloader._reset_driver(key)
                driver = XimalayaDownloader._get_driver(key)
                self.xm_sign = driver.execute_async_script(XimalayaDownloader._SIGN_JS)
        self.headers["xm-sign"] = self.xm_sign
        return self.xm_sign

    def _account_key(self) -> str:
        """账号维度标识：用作独立浏览器池的 key（不同登录账号 → 不同浏览器指纹）"""
        if self.account_id is not None:
            return f"acct_{self.card_id}_{self.account_id}" if self.card_id is not None else f"acct__{self.account_id}"
        if self.cookie:
            return "anon_" + hashlib.md5(self.cookie.encode("utf-8")).hexdigest()[:12]
        return "anon_none"

    def _sanitize_dirname(self, name: str) -> str:
        """清理目录名，移除非法字符"""
        return re.sub(r'[\\/:*?"<>|\r\n]', "", name).strip()

    def _get_album_dir(self, album_title: str | None = None) -> str:
        """获取专辑保存目录（基于专辑名称），返回相对路径"""
        if album_title:
            safe_name = self._sanitize_dirname(album_title)
            if safe_name:
                return safe_name
        return ""

    def check_track_exists(self, title: str, album_title: str | None = None, fmt: str = "m4a") -> str | None:
        """检查音频文件是否已存在本地

        Args:
            title: 文件名（不含扩展名），如 "第1集" 或原始标题
            album_title: 专辑名称
            fmt: 文件格式

        Returns:
            存在则返回文件路径，否则返回 None
        """
        safe_title = re.sub(r'[\\/:*?"<>|\r\n]', "", title).strip()
        if not safe_title:
            return None

        # 确定可能的保存路径（检查两种格式）
        candidates = []
        subdir = self._get_album_dir(album_title)
        for ext in (fmt, "m4a" if fmt == "mp3" else "mp3"):
            if subdir:
                candidates.append(self.download_root / subdir / f"{safe_title}.{ext}")
            candidates.append(self.download_root / f"{safe_title}.{ext}")

        for p in candidates:
            if p.exists() and p.stat().st_size > 0:
                return str(p)
        return None

    def download_by_track_id(
        self,
        track_id: int,
        quality: int = DEFAULT_QUALITY,
        album_title: str | None = None,
        skip_existing: bool = False,
        fmt: str = DEFAULT_FORMAT,
        episode_num: int | None = None,
    ) -> dict:
        """按单集 ID 下载，返回结果字典

        Args:
            track_id: 单集ID
            quality: 音质 0=标准 1=高 2=超高
            album_title: 专辑名称（有则创建专辑子目录）
            skip_existing: 是否跳过已存在的文件
            fmt: 保存格式 m4a 或 mp3
            episode_num: 集数编号，提供后文件名用"第X集"格式
        """
        self.get_xm_sign()

        url = f"https://mobile.ximalaya.com/mobile/download/v2/track/{track_id}/ts-{int(time.time()*1000)}"
        params = {
            "trackId": track_id,
            "device": "win32",
            "trackQualityLevel": quality,
        }

        response = self.session.get(url, headers=self.headers, params=params, verify=False, timeout=30)
        data = response.json()

        if data.get("ret") != 0:
            return {"success": False, "error": data.get("msg", "请求失败"), "detail": data}

        encrypted_url = data["data"].get("downloadAacUrl")
        title = data["data"].get("title", f"track_{track_id}")

        if not encrypted_url:
            return {"success": False, "error": "未找到下载链接"}

        # 确定保存文件名：优先用"第X集"格式，否则用原始标题
        if episode_num is not None:
            file_name = f"第{episode_num}集"
        else:
            file_name = re.sub(r'[\\/:*?"<>|\r\n]', "", title).strip()

        # 跳过已存在文件
        if skip_existing:
            existing = self.check_track_exists(file_name, album_title, fmt)
            if existing:
                return {
                    "success": True,
                    "skipped": True,
                    "title": title,
                    "track_id": track_id,
                    "file_path": existing,
                    "file_size": 0,
                }

        download_url = decrypt_download_url(encrypted_url)

        # 确定保存目录：有专辑名则存到 {download_root}/专辑名/ 下，否则存 {download_root}/
        save_dir = self.download_root
        subdir = self._get_album_dir(album_title)
        if subdir:
            save_dir = self.download_root / subdir
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / f"{file_name}.{fmt}"

        # 写入前最终检查：文件已存在则直接返回（防止并发/重试任务重复下载同一集）
        if save_path.exists() and save_path.stat().st_size > 0:
            return {
                "success": True,
                "skipped": True,
                "title": title,
                "track_id": track_id,
                "file_path": str(save_path),
                "file_size": save_path.stat().st_size,
            }

        # 下载文件：先写 .part 临时文件，校验 Content-Length 后再 rename，
        # 避免网络中断留下"size>0 的残破文件"被误认为已下载完成；
        # 数据全部收完但连接被重置时视为成功，避免误报失败导致重复下载
        resp = self.session.get(download_url, stream=True, verify=False, timeout=(30, 60))
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        tmp_path = save_path.with_name(save_path.name + ".part")
        error = None
        try:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
        except Exception as e:
            error = e
        if error is not None and not (total and downloaded == total and downloaded > 0):
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise error
        if total and downloaded != total:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise IOError(f"下载不完整: 期望 {total} 字节，实际收到 {downloaded} 字节")
        os.replace(tmp_path, save_path)

        result = {
            "success": True,
            "title": title,
            "track_id": track_id,
            "file_path": str(save_path),
            "file_size": downloaded,
            "download_url": download_url,
        }

        # 附加专辑信息
        if album_title:
            result["album_title"] = album_title

        return result

    def scan_local_album(self, album_title: str | None = None) -> set[str]:
        """扫描本地已下载的音频文件名（不含扩展名），用于批量跳过

        Returns:
            已存在的文件名集合（小写）
        """
        existing = set()
        if not self.download_root.exists():
            return existing

        scan_dir = self.download_root
        subdir = self._get_album_dir(album_title)
        if subdir:
            scan_dir = self.download_root / subdir

        if not scan_dir.exists():
            return existing

        for f in scan_dir.iterdir():
            if f.is_file() and f.suffix in (".m4a", ".mp3", ".aac") and f.stat().st_size > 0:
                existing.add(f.stem.lower())
        return existing

    @staticmethod
    def _extract_episode_num(title: str) -> int | None:
        """从标题中提取集数编号，用于排序

        匹配模式：
        - 第1集 / 第12回 / 第100章 / 第3期 / 第5话
        - 空格/下划线/点包围的2~4位数字（如  003  / _010_ / .12.）
        - 开头的1~4位数字后跟分隔符（1. / 001 ）
        - 末尾的2~4位数字
        """
        patterns = [
            r'第\s*(\d+)\s*[集回章节期话]',
            r'[\s_\-\.]\s*(\d{2,4})\s*[\s_\-\.]',
            r'^\s*(\d{1,4})\s*[\s_\-\.]',
            r'[\s_\-\.]\s*(\d{2,4})\s*$',
        ]
        for pat in patterns:
            m = re.search(pat, title)
            if m:
                return int(m.group(1))
        return None

    @classmethod
    def _sort_key(cls, track: dict) -> tuple:
        """排序键：有集数编号的按编号排，否则按 trackId 升序，有编号的排在前面"""
        ep = cls._extract_episode_num(track.get("title", ""))
        if ep is not None:
            return (0, ep, track["trackId"])
        else:
            return (1, track["trackId"], 0)

    def get_track_list(self, album_id: int) -> dict:
        """获取专辑章节列表（官方接口，免登录，自动翻页）"""
        url = "http://mobwsa.ximalaya.com/mobile/playlist/album/page"
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        }

        all_tracks = []
        page = 1
        max_page = 1
        album_title = ""
        total_count = 0

        while True:
            params = {"albumId": album_id, "pageId": page}
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                return {"success": False, "error": f"获取章节列表失败: {e}"}

            if data.get("ret") != 0:
                return {"success": False, "error": data.get("msg", "请求失败"), "detail": data}

            track_list = data.get("list", [])
            if not track_list:
                break

            # 首页提取专辑信息
            if page == 1:
                album_title = track_list[0].get("albumTitle", "")
                total_count = data.get("totalCount", 0)
                max_page = data.get("maxPageId", 1)

            all_tracks.extend(track_list)

            if page >= max_page:
                break
            page += 1

        if not all_tracks:
            return {"success": False, "error": "未获取到章节列表"}

        tracks = sorted(all_tracks, key=self._sort_key)
        return {
            "success": True,
            "album_title": album_title,
            "track_total": total_count,
            "tracks": tracks,
        }

    def download_by_chapter(self, album_id: int, chapter_num: int, quality: int = DEFAULT_QUALITY, fmt: str = DEFAULT_FORMAT) -> dict:
        """按章节号下载"""
        list_result = self.get_track_list(album_id)
        if not list_result["success"]:
            return list_result

        tracks = list_result["tracks"]
        if chapter_num > len(tracks):
            return {"success": False, "error": f"专辑只有 {len(tracks)} 集，找不到第 {chapter_num} 集"}

        track = tracks[chapter_num - 1]
        track_id = track["trackId"]

        result = self.download_by_track_id(
            track_id, quality,
            album_title=list_result["album_title"],
            fmt=fmt,
            episode_num=chapter_num,
        )
        result["chapter_num"] = chapter_num
        return result

    def close(self):
        """关闭下载器实例持有的引用。Chrome driver 由每账号独立池统一管理，不在此关闭。"""
        try:
            self.session.close()
        except Exception:
            pass
